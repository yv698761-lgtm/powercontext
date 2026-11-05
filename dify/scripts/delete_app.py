"""Delete the current 离谱侦探社 app from Dify (used before re-importing a new DSL)."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parents[1]
access = json.loads(io.open(ROOT / "runtime" / "dify-access.json", encoding="utf-8").read())
app_id = access.get("app_id")
if not app_id:
    raise SystemExit("runtime/dify-access.json 里没有 app_id")

client = httpx.Client(timeout=120, trust_env=False)
base = "http://127.0.0.1:18080/console/api"
client.post(
    base + "/login",
    json={
        "email": "detective@example.com",
        "password": "Detective_Local_2026",
        "language": "zh-Hans",
        "remember_me": True,
    },
)
headers = {"X-CSRF-Token": str(client.cookies.get("csrf_token") or "")}
response = client.delete(base + f"/apps/{app_id}", headers=headers)
print("delete app", app_id, response.status_code, response.text[:120])
