"""Fetch one player's memory via the bridge and store it as evidence."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
BRIDGE = "http://127.0.0.1:8012"
KEY = "local_detective_api_key_2026"


def main() -> int:
    verify = ROOT / "evidence" / "verify-flow.json"
    player = sys.argv[1] if len(sys.argv) > 1 else ""
    if not player and verify.exists():
        player = json.loads(io.open(verify, encoding="utf-8").read())["players"]["primary"]
    if not player:
        raise SystemExit("需要玩家 ID：python scripts/recap_player.py <player>")

    access = json.loads(io.open(ROOT / "runtime" / "dify-access.json", encoding="utf-8").read())
    app_id = access.get("app_id", "")
    # Dify 里的 player = "dify:<app_id>:<user>"，这里允许只传 user
    full_player = player if player.startswith("dify:") else f"dify:{app_id}:{player}"

    # trust_env=False：避免继承系统代理，否则本机直连会被代理拦成 502
    with httpx.Client(timeout=60, trust_env=False) as client:
        response = client.get(
            BRIDGE + "/session/recap",
            params={"player": full_player},
            headers={"Authorization": f"Bearer {KEY}"},
        )
    print("status", response.status_code)
    if response.status_code != 200:
        print(response.text[:400])
        return 1
    data = response.json()
    out = ROOT / "evidence" / "powercontext-recap.json"
    out.write_text(json.dumps({"player": full_player, "recap": data}, ensure_ascii=False, indent=2), encoding="utf-8")
    print("seq", data["seq"], "| rank", data["rank"]["title"], "| artifact", data["artifact_id"])
    print("rounds", [(item["case"], item["pass"]) for item in data["rounds"]])
    print("written", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
