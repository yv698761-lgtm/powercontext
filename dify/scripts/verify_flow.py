"""端到端验证：通过已发布的 Dify 应用 API 跑完整玩法。

它验证的不是后端单点，而是 Dify 编排（意图分支、知识库检索、三位侦探、判定、分支回答）
与 PowerContext 记忆在这条链路上的真实表现：

1. 新用户开场 → 抽取案卷、检索片段。
2. 故意用一条"谣言"结案 → 必须被驳回（证明判定不是模型随口说的）。
3. 同一用户在新的 conversation 里开场 → 档案员必须复述上一轮的失败（跨会话记忆）。
4. 用决定性证据结案 → 必须成立。
5. 另一个新用户开场 → 不能看到前一个用户的档案（身份隔离）。

结果写入 evidence/verify-flow.json，并把每轮的回答与节点轨迹保存下来。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# Windows 控制台默认 GBK，中文与 ✅ 会直接抛 UnicodeEncodeError
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

from app import caseflow  # noqa: E402

EVIDENCE = ROOT / "evidence"


def log(message: str) -> None:
    print(f"[verify] {message}", flush=True)


def load_env(path: Path) -> dict:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


class App:
    def __init__(self, base_url: str, api_key: str) -> None:
        self.base = base_url.rstrip("/")
        self.key = api_key
        self.client = httpx.Client(timeout=300, trust_env=False)

    def chat(self, query: str, user: str, conversation_id: str = "") -> dict:
        payload = {
            "inputs": {},
            "query": query,
            "response_mode": "blocking",
            "conversation_id": conversation_id,
            "user": user,
            "auto_generate_name": False,
        }
        response = self.client.post(
            self.base + "/v1/chat-messages",
            headers={"Authorization": f"Bearer {self.key}", "Content-Type": "application/json"},
            json=payload,
        )
        if response.status_code >= 400:
            raise RuntimeError(f"Dify 调用失败：{response.status_code} {response.text[:600]}")
        return response.json()


def extract_case_title(answer: str) -> str:
    """从回答里取出本轮真正的案卷名字（优先「### 案卷：「X」」这一段）。"""
    match = re.search(r"### 案卷：[「\"]([^」\"\n]+)[」\"]", answer)
    if match:
        return match.group(1).strip()
    for case in caseflow.CASES:
        if f"「{case['title']}」" in answer:
            return case["title"]
    return ""


def record(checks: list, name: str, passed: bool, detail: str = "") -> None:
    checks.append({"check": name, "pass": bool(passed), "detail": detail[:800]})
    log(f"{'✅' if passed else '❌'} {name} {detail[:120]}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    parser.add_argument("--access-file", default=str(ROOT / "runtime" / "dify-access.json"))
    parser.add_argument("--player", default="")
    parser.add_argument("--other-player", default="")
    args = parser.parse_args()

    env = {**load_env(Path(args.env_file)), **os.environ}
    access = json.loads(Path(args.access_file).read_text(encoding="utf-8"))
    base_url = access.get("dify_url") or env.get("DIFY_URL", "http://127.0.0.1:18080")
    app_key = access.get("app_api_key")
    if not app_key:
        raise SystemExit("runtime/dify-access.json 里没有 app_api_key，请先运行 scripts/init_dify.py")

    stamp = str(int(time.time()))[-6:]
    player = args.player or f"verify-a-{stamp}"
    other = args.other_player or f"verify-b-{stamp}"
    app = App(base_url, app_key)
    checks: list[dict] = []
    turns: list[dict] = []

    # 1. 开场
    first = app.chat("开场", player)
    turns.append({"step": "open", "query": "开场", "answer": first.get("answer", "")[:2000],
                  "conversation_id": first.get("conversation_id")})
    record(checks, "开场返回案卷", "案卷" in first.get("answer", "") or "离谱侦探社" in first.get("answer", ""),
           first.get("answer", "")[:200])

    case_title = extract_case_title(first.get("answer", ""))
    record(checks, "开场包含案卷标题", bool(case_title), case_title)
    record(checks, "开场带出档案员的记忆台词", "档案员老砚" in first.get("answer", ""), "")

    # 2. 用谣言结案（必须被驳回）
    rumor_case = next((case for case in caseflow.CASES if case["title"] == case_title), None)
    rumor = ""
    if rumor_case:
        rumor = rumor_case["rumors"][0]
    accusation_rumour = "真凶是野猫。" + (rumor[:40] if rumor else "")
    second = app.chat(accusation_rumour, player, first.get("conversation_id", ""))
    turns.append({"step": "accuse-rumour", "query": accusation_rumour, "answer": second.get("answer", "")[:2000],
                  "conversation_id": second.get("conversation_id")})
    record(checks, "谣言指控被驳回", "驳回" in second.get("answer", ""), second.get("answer", "")[:200])

    # 3. 新会话开场：档案员必须记得上一轮
    third = app.chat("开场", player)
    answer3 = third.get("answer", "")
    turns.append({"step": "open-new-conversation", "query": "开场", "answer": answer3[:2000],
                  "conversation_id": third.get("conversation_id")})
    record(checks, "新会话仍然是同一份档案", "上回" in answer3 or "又" in answer3 or "驳回" in answer3,
           answer3[:200])
    record(checks, "新会话 conversation_id 不同",
           third.get("conversation_id") != first.get("conversation_id"),
           f"{first.get('conversation_id')} vs {third.get('conversation_id')}")

    # 4. 用决定性证据结案
    revealed = extract_case_title(answer3)
    decisive_case = next((case for case in caseflow.CASES if case["title"] == revealed), None) or rumor_case
    answer_line = decisive_case["answer_line"] if decisive_case else ""
    fourth = app.chat("真凶是他。" + answer_line, player, third.get("conversation_id", ""))
    turns.append({"step": "accuse-decisive", "query": answer_line, "answer": fourth.get("answer", "")[:2000],
                  "conversation_id": fourth.get("conversation_id")})
    record(checks, "决定性证据使勘结成立", "勘结：✅ 成立" in fourth.get("answer", ""),
           fourth.get("answer", "")[:200])

    # 5. 另一个用户看不到前一个用户的档案
    fifth = app.chat("战绩", other)
    answer5 = fifth.get("answer", "")
    turns.append({"step": "other-player-rank", "query": "战绩", "answer": answer5[:2000],
                  "conversation_id": fifth.get("conversation_id")})
    record(checks, "新用户档案为空", "还没有" in answer5 or "实习记录员" in answer5, answer5[:200])
    record(checks, "新用户看不到他人案卷", case_title not in answer5 if case_title else True, "")

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    result = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "dify_url": base_url,
        "app_id": access.get("app_id"),
        "dataset_id": access.get("dataset_id"),
        "players": {"primary": player, "other": other},
        "case_under_test": case_title,
        "checks": checks,
        "passed": sum(1 for item in checks if item["pass"]),
        "total": len(checks),
        "turns": turns,
    }
    (EVIDENCE / "verify-flow.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"结果：{result['passed']}/{result['total']} 通过，证据写入 evidence/verify-flow.json")
    return 0 if result["passed"] == result["total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
