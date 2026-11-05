"""收集一次完整运行的证据，写入 evidence/。

包含：环境与版本、Dify 应用与工作流规模、seekdb 侧的表与检索记录、
PowerContext 侧的玩家档案（跨会话记忆）、以及端到端验证结果摘要。
"""

from __future__ import annotations

import io
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"
sys.path.insert(0, str(ROOT))


def sh(cmd: list[str]) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout.strip()
    except Exception as exc:  # noqa: BLE001
        return f"<failed: {exc}>"


def console_client() -> httpx.Client:
    client = httpx.Client(timeout=180, trust_env=False)
    client.post(
        "http://127.0.0.1:18080/console/api/login",
        json={
            "email": "detective@example.com",
            "password": "Detective_Local_2026",
            "language": "zh-Hans",
            "remember_me": True,
        },
    )
    return client


def main() -> int:
    EVIDENCE.mkdir(parents=True, exist_ok=True)
    access = json.loads(io.open(ROOT / "runtime" / "dify-access.json", encoding="utf-8").read())

    environment = {
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "host": platform.platform(),
        "python": sys.version.split()[0],
        "docker_images": sh(["docker", "images", "--format", "{{.Repository}}:{{.Tag}} {{.Size}}"]).splitlines(),
        "containers": sh(["docker", "ps", "--format", "{{.Names}} {{.Status}} {{.Ports}}"]).splitlines(),
        "dify_url": access["dify_url"],
        "dataset_id": access["dataset_id"],
        "embedding": {"provider": access.get("embedding_provider"), "model": access.get("embedding_model")},
    }
    (EVIDENCE / "environment.json").write_text(json.dumps(environment, ensure_ascii=False, indent=2), encoding="utf-8")
    print("environment.json written")

    client = console_client()
    headers = {"X-CSRF-Token": str(client.cookies.get("csrf_token") or "")}
    app = client.get(f"http://127.0.0.1:18080/console/api/apps/{access['app_id']}", headers=headers).json()
    workflow = client.get(
        f"http://127.0.0.1:18080/console/api/apps/{access['app_id']}/workflows/draft", headers=headers
    ).json()
    graph = workflow.get("graph") or {}
    nodes = graph.get("nodes") or []
    app_info = {
        "app_id": access["app_id"],
        "name": app.get("name"),
        "mode": app.get("mode"),
        "node_count": len(nodes),
        "edge_count": len(graph.get("edges") or []),
        "nodes": [
            {"id": node.get("id"), "type": (node.get("data") or {}).get("type"), "title": (node.get("data") or {}).get("title")}
            for node in nodes
        ],
        "dataset_id": access["dataset_id"],
        "environment_variables": [item.get("name") for item in (workflow.get("environment_variables") or [])],
    }
    (EVIDENCE / "dify-app.json").write_text(json.dumps(app_info, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"dify-app.json written ({app_info['node_count']} nodes)")

    player = os.getenv("EVIDENCE_PLAYER", "")
    verify = EVIDENCE / "verify-flow.json"
    if verify.exists():
        data = json.loads(io.open(verify, encoding="utf-8").read())
        player = player or data["players"]["primary"]
    if player:
        recap = httpx.get(
            "http://127.0.0.1:8012/session/recap",
            params={"player": player},
            headers={"Authorization": "Bearer " + os.getenv("DETECTIVE_API_KEY", "local_detective_api_key_2026")},
            timeout=60,
        )
        if recap.status_code == 200:
            (EVIDENCE / "powercontext-recap.json").write_text(
                json.dumps({"player": player, "recap": recap.json()}, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print("powercontext-recap.json written")

    seekdb = sh(
        [
            sys.executable,
            str(ROOT / "scripts" / "seekdb_probe.py"),
            "--password",
            os.getenv("SEEKDB_PASSWORD", "dify_seekdb_pw"),
        ]
    )
    print("seekdb probe done:", "seekdb-probe.json" in os.listdir(EVIDENCE))

    summary = {
        "captured_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "app": {"name": app_info["name"], "nodes": app_info["node_count"], "edges": app_info["edge_count"]},
        "seekdb": {
            "tables": [
                {"table": item["table"], "rows": item["rows"], "vector_indexes": item["vector_indexes"],
                 "fulltext_indexes": item["fulltext_indexes"], "fulltext_parser": item["fulltext_parser"]}
                for item in json.loads(io.open(EVIDENCE / "seekdb-probe.json", encoding="utf-8").read())["tables"]
            ]
        },
        "verify_flow": json.loads(io.open(verify, encoding="utf-8").read())["checks"] if verify.exists() else [],
    }
    (EVIDENCE / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print("summary.json written")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
