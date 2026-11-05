"""PowerContext 记忆客户端（HTTP API）。

本作品用 PowerContext 保存"跨会话身份"：玩家等级、对三位侦探的信任度、
档案员老砚记下的黑历史、以及最近几场案卷。DSL 每次开场都会先把这些读回来，
再决定用哪份案卷、哪位侦探先发言；每轮结束写回新状态。

不同的 PowerContext 版本把同类能力暴露在不同路径上，因此这里先探测
`/openapi.json`，按实际存在的路由选择调用方式，而不是硬编码单一路径。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

MEMORY_KIND = "detective-casefile-v1"


class MemoryUnavailable(RuntimeError):
    """PowerContext 不可用；调用方必须把它当成错误，而不是"没有记忆"。"""


def player_digest(player: str) -> str:
    return hashlib.sha256(player.encode("utf-8")).hexdigest()


@dataclass
class Snapshot:
    """一名玩家在侦探社里的全部状态。"""

    player: str
    seq: int = 0
    active_case: str = ""
    rounds: list[dict] = field(default_factory=list)
    trust: dict[str, int] = field(default_factory=dict)
    seen_cases: list[str] = field(default_factory=list)
    verdicts: list[str] = field(default_factory=list)
    grudge: list[str] = field(default_factory=list)
    artifact_id: str | None = None

    def to_dict(self) -> dict:
        return {
            "player": self.player,
            "seq": self.seq,
            "active_case": self.active_case,
            "rounds": self.rounds[-6:],
            "trust": self.trust,
            "seen_cases": self.seen_cases[-12:],
            "verdicts": self.verdicts[-6:],
            "grudge": self.grudge[-6:],
        }

    @classmethod
    def from_dict(cls, data: dict, artifact_id: str | None = None) -> "Snapshot":
        return cls(
            player=data.get("player", ""),
            seq=int(data.get("seq", 0)),
            active_case=data.get("active_case", ""),
            rounds=list(data.get("rounds", [])),
            trust=dict(data.get("trust", {})),
            seen_cases=list(data.get("seen_cases", [])),
            verdicts=list(data.get("verdicts", [])),
            grudge=list(data.get("grudge", [])),
            artifact_id=artifact_id,
        )

    def rank(self) -> dict:
        passes = sum(1 for item in self.rounds if item.get("pass"))
        levels = [
            (0, "实习记录员", "还在给档案员端茶倒水。"),
            (1, "见习侦探", "第一次让案卷自己开口说话。"),
            (3, "正式侦探", "镇上的巡捕开始主动递烟。"),
            (5, "首席侦探", "档案员开始偷偷抄你的批注。"),
            (8, "神探", "案卷在你手上会自己招供。"),
        ]
        title, note = levels[0][1], levels[0][2]
        for need, name, remark in levels:
            if passes >= need:
                title, note = name, remark
        return {"title": title, "note": note, "passes": passes, "rounds": self.seq}


class PowerContextMemory:
    """与 PowerContext Server 通信的最小封装。"""

    def __init__(self, base_url: str, token: str = "", scope_id: str = "", timeout: float = 30.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.fixed_scope = scope_id.strip()
        self._client = httpx.AsyncClient(timeout=timeout, trust_env=False)
        self._routes: dict | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            response = await self._client.request(method, self.base_url + path, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise MemoryUnavailable(f"PowerContext 连接失败：{exc}") from exc
        if response.status_code >= 400:
            raise MemoryUnavailable(f"PowerContext {method} {path} 返回 {response.status_code}: {response.text[:300]}")
        return response.json() if response.content else {}

    async def describe(self) -> dict:
        return await self._request("GET", "/v1/server-info")

    async def routes(self) -> set[str]:
        """读取 OpenAPI 路由，用来适配不同的 PowerContext 版本。"""
        if self._routes is None:
            headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
            try:
                response = await self._client.get(self.base_url + "/openapi.json", headers=headers)
                response.raise_for_status()
                self._routes = response.json().get("paths", {})
            except httpx.HTTPError as exc:
                raise MemoryUnavailable(f"读取 PowerContext 接口描述失败：{exc}") from exc
        return set(self._routes)

    async def ensure_scope(self, player: str) -> str:
        if self.fixed_scope:
            return self.fixed_scope
        digest = player_digest(player)[:12]
        created = await self._request(
            "POST",
            "/v1/scopes",
            {
                "title": f"离谱侦探社 {digest}",
                "summary": "Dify 终端用户在侦探社里的身份、信任度与办案记录。",
                "idempotency_key": f"detective-scope-{digest}",
            },
        )
        scope_id = created.get("scope_id") or created.get("id")
        if not scope_id:
            raise MemoryUnavailable(f"PowerContext 未返回 scope_id：{created}")
        return scope_id

    async def load(self, scope_id: str, player: str) -> Snapshot:
        """读取该玩家最新的状态快照。"""
        paths = await self.routes()
        entries: list[dict] = []
        cursor = None
        for _ in range(50):
            if "/v1/memory/entries/list" in paths:
                # 1.5.0 的 entries/list 不接受 kind 过滤（extra_forbidden），在本地按 kind 过滤
                body = {"scope_id": scope_id, "limit": 100, "include_inactive": False}
                if cursor:
                    body["cursor"] = cursor
                result = await self._request("POST", "/v1/memory/entries/list", body)
            else:
                body = {"scope_id": scope_id, "limit": 100}
                if cursor:
                    body["cursor"] = cursor
                result = await self._request("POST", "/v1/atomic-memory/list", body)
            entries.extend(result.get("entries") or result.get("records") or [])
            cursor = result.get("next_cursor")
            if not cursor:
                break
        best = Snapshot(player=player)
        for entry in entries:
            kind = entry.get("kind")
            if kind not in (None, MEMORY_KIND):
                continue
            text = entry.get("text") or entry.get("content") or ""
            try:
                parsed = json.loads(text)
            except (TypeError, ValueError):
                logger.warning("跳过无法解析的记忆条目：%s", entry.get("id") or entry.get("artifact"))
                continue
            if parsed.get("player") != player:
                continue
            if int(parsed.get("seq", 0)) < best.seq:
                continue
            artifact = entry.get("artifact") or {}
            best = Snapshot.from_dict(parsed, artifact.get("artifact_id") or entry.get("artifact_id"))
        return best

    async def save(self, scope_id: str, snapshot: Snapshot) -> str:
        """写回状态快照，返回新的记忆记录 ID。"""
        payload = snapshot.to_dict()
        text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        paths = await self.routes()
        if "/v1/memory/remember" in paths:
            saved = await self._request(
                "POST",
                "/v1/memory/remember",
                {
                    "scope_id": scope_id,
                    "kind": MEMORY_KIND,
                    "text": text,
                    "reason": "Dify 完成一轮判定，写回侦探社档案",
                },
            )
        else:
            saved = await self._request(
                "POST",
                "/v1/scopes/%s/artifacts" % scope_id,
                {"family": "atomic_memory", "kind": MEMORY_KIND, "text": text, "title": f"侦探社档案 · {snapshot.player}"},
            )
        records = saved.get("records") or saved.get("artifacts") or []
        if records:
            record = records[0]
            return record.get("artifact", {}).get("artifact_id") or record.get("artifact_id") or record.get("id", "")
        return saved.get("artifact_id") or saved.get("id", "")
