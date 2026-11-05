"""离谱侦探社 · Dify 桥接服务（FastAPI）。

Dify 负责编排：解析玩家输入 → 读取记忆 → 知识库检索 → 三位侦探发言 → 判定 → 分支回答。
本服务只承担"不能交给模型"的部分：

1. `/session/open`  选案卷、给出检索兜底片段、生成回合票据（带签名，含 base_seq）。
2. `/session/judge` 依据案卷 canon 与谣言表做确定性判定，并把本轮写回 PowerContext。
3. `/session/recap` 单独读取某位玩家的记忆，供样例与评审核对（跨会话是否真的记住）。
4. `/health`        服务与 PowerContext 的依赖健康状态。

记忆读写全部落 PowerContext；本服务不落任何本地状态（无数据库、无文件缓存），
所以进程重启后玩家的等级、信任度、黑历史都只能从 PowerContext 读回来。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from pydantic import BaseModel, Field

from . import caseflow, rules
from .data.cases import CASES
from .powercontext import MemoryUnavailable, PowerContextMemory, Snapshot, player_digest

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("detective.bridge")

API_KEY = os.getenv("DETECTIVE_API_KEY", "")
SIGNING_KEY = os.getenv("DETECTIVE_SIGNING_KEY", "")
PC_URL = os.getenv("POWERCONTEXT_URL", "http://127.0.0.1:8011")
PC_TOKEN = os.getenv("POWERCONTEXT_TOKEN", "")
PC_SCOPE = os.getenv("POWERCONTEXT_SCOPE_ID", "")
TICKET_TTL = int(os.getenv("DETECTIVE_TICKET_TTL", "900"))
DATA_DIR = Path(os.getenv("DETECTIVE_DATA_DIR", "runtime"))

CASES_BY_ID = {case["case_id"]: case for case in CASES}
CHUNKS = caseflow.build_chunks()
CASE_ORDER = [case["case_id"] for case in CASES]


@dataclass
class Services:
    memory: PowerContextMemory


def require_key(authorization: str = Header(default="")) -> None:
    if not API_KEY or not secrets.compare_digest(authorization, "Bearer " + API_KEY):
        raise HTTPException(401, "invalid detective API key")


@asynccontextmanager
async def lifespan(app: FastAPI):
    if len(API_KEY) < 16 or len(SIGNING_KEY) < 16:
        raise RuntimeError("DETECTIVE_API_KEY / DETECTIVE_SIGNING_KEY 至少 16 个字符")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    memory = PowerContextMemory(PC_URL, PC_TOKEN, PC_SCOPE)
    app.state.services = Services(memory=memory)
    logger.info("bridge ready: cases=%d chunks=%d powercontext=%s", len(CASES), len(CHUNKS), PC_URL)
    try:
        yield
    finally:
        await memory.aclose()


app = FastAPI(title="离谱侦探社 · Dify bridge", version="1.0.0", lifespan=lifespan)


def services(request: Request) -> Services:
    return request.app.state.services


# --------------------------------------------------------------------- tickets


def _sign(raw: str) -> str:
    return hmac.new(SIGNING_KEY.encode(), raw.encode(), hashlib.sha256).hexdigest()


def pack_ticket(payload: dict) -> str:
    raw = base64.urlsafe_b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    ).decode("ascii")
    return raw + "." + _sign(raw)


def unpack_ticket(ticket: str) -> dict:
    try:
        raw, signature = ticket.rsplit(".", 1)
        if not secrets.compare_digest(signature, _sign(raw)):
            raise ValueError("bad signature")
        payload = json.loads(base64.urlsafe_b64decode(raw.encode("ascii")))
    except (ValueError, KeyError, TypeError) as exc:
        raise HTTPException(422, f"回合票据无效：{exc}") from None
    if payload.get("expires", 0) < int(time.time()):
        raise HTTPException(409, "回合票据已过期，请重新开场（发送「开场」）")
    return payload


# --------------------------------------------------------------------- models


class OpenRequest(BaseModel):
    player: str = Field(min_length=1, max_length=160)
    app_id: str = Field(default="", max_length=64)
    user_input: str = Field(default="", max_length=2000)


class JudgeRequest(BaseModel):
    ticket: str = Field(min_length=1, max_length=48000)
    accusation: str = Field(default="", max_length=4000)
    speeches: dict[str, str] = Field(default_factory=dict)


def choose_case(snapshot: Snapshot, user_input: str) -> str:
    """优先按玩家点名，其次挑一份没办过的案卷。"""
    named = caseflow.case_by_title(user_input) if user_input else None
    if named:
        return named["case_id"]
    seen = set(snapshot.seen_cases)
    remaining = [case_id for case_id in CASE_ORDER if case_id not in seen]
    if not remaining:
        return CASE_ORDER[snapshot.seq % len(CASE_ORDER)]
    index = int(player_digest(snapshot.player), 16) % len(remaining)
    return remaining[index]


def archivist_line(snapshot: Snapshot, case: dict) -> str:
    """档案员老砚的台词：跨会话记忆的直接证据。"""
    if snapshot.seq == 0:
        return f"新面孔。规矩只有一条：{case['title']}的案卷可以翻，别在上面乱画。"
    last = snapshot.rounds[-1] if snapshot.rounds else None
    if last and not last.get("pass"):
        return (
            f"又是你。上回你想拿「{last.get('accused', '某个说法')}」结案，被驳回了——"
            f"{last.get('case', '那份案卷')}的批注还在呢。这次先看清楚再开口。"
        )
    if last:
        return f"上回{last.get('case', '那份案卷')}你办得还行，档案我留着。这次是「{case['title']}」，别急着抄上回的路子。"
    return f"档案还空着。这一份是「{case['title']}」。"


def refresh_trust(snapshot: Snapshot) -> dict[str, int]:
    """按历史记录统计三位侦探的命中率，形成"信任度"记忆。"""
    trust = {item["name"]: 0 for item in rules.DETECTIVES}
    for round_item in snapshot.rounds:
        for name, hit in (round_item.get("detective_hits") or {}).items():
            trust[name] = trust.get(name, 0) + int(hit)
    return trust


def opening_briefing(case: dict) -> str:
    """开场简报：从案卷里抽出与线索相关的三句话。"""
    fragments = [chunk for chunk in CHUNKS if chunk.case_id == case["case_id"] and chunk.kind in ("现场", "线索")]
    lines = [f"· {chunk.heading}：{chunk.text}" for chunk in fragments[:4]]
    return "\n".join(lines)


# --------------------------------------------------------------------- routes


@app.get("/health")
async def health(request: Request):
    info = {
        "service": "detective-bridge",
        "cases": len(CASES),
        "chunks": len(CHUNKS),
        "detectives": [item["name"] for item in rules.DETECTIVES],
    }
    try:
        described = await services(request).memory.describe()
        info["powercontext"] = {"ok": True, "info": described}
    except (MemoryUnavailable, HTTPException) as exc:
        info["powercontext"] = {"ok": False, "error": str(exc)}
    return info


@app.get("/catalog")
async def catalog():
    return {
        "cases": [
            {"case_id": case["case_id"], "title": case["title"], "red_herring": case["red_herring"]}
            for case in CASES
        ],
        "detectives": rules.DETECTIVES,
        "archivist": rules.ARCHIVIST,
    }


@app.post("/session/open", dependencies=[Depends(require_key)])
async def open_session(req: OpenRequest, request: Request):
    memory = services(request).memory
    player = req.player.strip()
    try:
        scope_id = await memory.ensure_scope(player)
        snapshot = await memory.load(scope_id, player)
    except MemoryUnavailable as exc:
        raise HTTPException(503, f"记忆服务不可用：{exc}") from exc

    wants_new = any(word in (req.user_input or "") for word in ("换", "下一", "新案", "重开"))
    active = getattr(snapshot, "active_case", "") or ""
    if active in CASES_BY_ID and not wants_new:
        # 玩家已经开场但还没结案：保持同一份案卷，直到他提交指控。
        case_id = active
    else:
        case_id = choose_case(snapshot, req.user_input)
        if case_id != active:
            snapshot.active_case = case_id
            try:
                await memory.save(scope_id, snapshot)
            except MemoryUnavailable as exc:
                raise HTTPException(503, f"记忆写入失败：{exc}") from exc
    case = CASES_BY_ID[case_id]
    ticket = pack_ticket(
        {
            "player": player,
            "app": req.app_id,
            "scope": scope_id,
            "case": case_id,
            "base_seq": snapshot.seq,
            "recall": snapshot.artifact_id,
            "expires": int(time.time()) + TICKET_TTL,
            "nonce": secrets.token_hex(8),
        }
    )
    fragments = [chunk.to_dict() for chunk in caseflow.fallback_retrieval(case_id, top_k=4)]
    return {
        "ticket": ticket,
        "case": {
            "case_id": case_id,
            "title": case["title"],
            "briefing": opening_briefing(case),
            "red_herring": case["red_herring"],
            "document": case["document"],
        },
        "hint": next(
            (
                chunk.text
                for chunk in CHUNKS
                if chunk.case_id == case_id
                and chunk.kind in caseflow.DECISIVE_KINDS
                and any(keyword in chunk.text for keyword in case["decisive_keywords"])
            ),
            case["answer_line"],
        ),
        "detectives": rules.DETECTIVES,
        "archivist": {**rules.ARCHIVIST, "line": archivist_line(snapshot, case)},
        "memory": {
            "scope_id": scope_id,
            "artifact_id": snapshot.artifact_id,
            "seq": snapshot.seq,
            "rank": snapshot.rank(),
            "trust": refresh_trust(snapshot),
            "seen_cases": snapshot.seen_cases,
            "last_verdict": (snapshot.verdicts or [""])[-1],
            "grudge": snapshot.grudge,
            "recap": archivist_line(snapshot, case),
        },
        "fragments": fragments,
        "corpus": {
            "cases": len(CASES),
            "chunks": len(CHUNKS),
            "store": "seekdb（Dify 知识库：向量 + 全文索引）",
        },
    }


@app.post("/session/judge", dependencies=[Depends(require_key)])
async def judge_session(req: JudgeRequest, request: Request):
    payload = unpack_ticket(req.ticket)
    case_id = payload["case"]
    case = CASES_BY_ID.get(case_id)
    if case is None:
        raise HTTPException(422, f"票据里的案卷不存在：{case_id}")
    player, scope_id = payload["player"], payload["scope"]
    memory = services(request).memory

    try:
        snapshot = await memory.load(scope_id, player)
    except MemoryUnavailable as exc:
        raise HTTPException(503, f"记忆服务不可用：{exc}") from exc

    result = caseflow.judge(case, req.accusation)
    # 记录每位侦探是否在本轮给出过决定性依据，用于后续信任度
    detective_hits = {}
    for detective in rules.DETECTIVES:
        speech = (req.speeches or {}).get(detective["key"], "")
        detective_hits[detective["name"]] = int(
            any(keyword in speech for keyword in case["decisive_keywords"])
        )

    seq = snapshot.seq + 1
    rounds = list(snapshot.rounds)
    rounds.append(
        {
            "seq": seq,
            "case": case["title"],
            "case_id": case_id,
            "accused": req.accusation[:120] or "（未写出指控）",
            "pass": result["pass"],
            "score": result["score"],
            "detective_hits": detective_hits,
        }
    )
    seen = list(snapshot.seen_cases)
    if case_id not in seen:
        seen.append(case_id)
    verdict_line = f"{case['title']}：{'勘结成立' if result['pass'] else '勘结被驳回'}"
    grudge = list(snapshot.grudge)
    if not result["pass"]:
        grudge.append(f"第 {seq} 轮「{case['title']}」被驳回：{case['red_herring']}")
    updated = Snapshot(
        player=player,
        seq=seq,
        active_case="",
        rounds=rounds,
        trust=refresh_trust(snapshot),
        seen_cases=seen,
        verdicts=list(snapshot.verdicts) + [verdict_line],
        grudge=grudge,
    )
    try:
        artifact_id = await memory.save(scope_id, updated)
    except MemoryUnavailable as exc:
        raise HTTPException(503, f"记忆写入失败，本轮不作为已归档处理：{exc}") from exc

    evidence = next(
        (
            chunk
            for chunk in CHUNKS
            if chunk.case_id == case_id
            and chunk.kind in caseflow.DECISIVE_KINDS
            and any(keyword in chunk.text for keyword in case["decisive_keywords"])
        ),
        None,
    )
    return {
        "verdict": "勘结成立" if result["pass"] else "勘结被驳回",
        "pass": result["pass"],
        "score": result["score"],
        "canon_hits": result["canon_hits"],
        "rumour_hits": result["rumour_hits"],
        "evidence_keywords": result["evidence_keywords"],
        "expected_answer": case["answer_line"],
        "evidence_fragment": evidence.to_dict() if evidence else None,
        "detective_hits": detective_hits,
        "trust": updated.trust,
        "rank": updated.rank(),
        "seq": seq,
        "memory_id": artifact_id,
        "verdict_line": verdict_line,
        "next_hint": "输入「开场」领下一份案卷；「战绩」看档案；「提示」让档案员给一条线索。",
    }


@app.get("/session/recap", dependencies=[Depends(require_key)])
async def recap(player: str, request: Request):
    """读某位玩家的记忆。

    - `player` 以 `dify:` 开头时按 Dify 的完整身份读取；
    - 否则视为 Dify 用户 ID，自动补上 app_id 前缀（app_id 未知时取环境变量）。
    """
    if not player.startswith("dify:"):
        player = "dify:" + (os.getenv("DIFY_APP_ID", "local")) + ":" + player
    memory = services(request).memory
    try:
        scope_id = await memory.ensure_scope(player)
        snapshot = await memory.load(scope_id, player)
    except MemoryUnavailable as exc:
        raise HTTPException(503, f"记忆服务不可用：{exc}") from exc
    return {
        "player": player,
        "scope_id": scope_id,
        "artifact_id": snapshot.artifact_id,
        "seq": snapshot.seq,
        "rank": snapshot.rank(),
        "trust": refresh_trust(snapshot),
        "seen_cases": snapshot.seen_cases,
        "verdicts": snapshot.verdicts,
        "grudge": snapshot.grudge,
        "rounds": snapshot.rounds,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=os.getenv("DETECTIVE_HOST", "0.0.0.0"), port=int(os.getenv("DETECTIVE_PORT", "8012")))
