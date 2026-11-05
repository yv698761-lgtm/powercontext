"""Print the verification summary from evidence/verify-flow.json with UTF-8 output."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, ValueError):
    pass

path = Path(__file__).resolve().parents[1] / "evidence" / "verify-flow.json"
data = json.loads(io.open(path, encoding="utf-8").read())
print(f"players={data['players']} case={data['case_under_test']} passed={data['passed']}/{data['total']}")
for item in data["checks"]:
    flag = "PASS" if item["pass"] else "FAIL"
    detail = " ".join(item["detail"].split())[:110]
    print(f"[{flag}] {item['check']} :: {detail}")
