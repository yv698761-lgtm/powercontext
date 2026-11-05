"""Reindex the whole knowledge base: delete all documents, then re-upload the 12 case files.

Used after switching the embedding service or the seekdb instance, because Dify
indexes documents asynchronously and a failed run leaves the document in `error`.
"""

from __future__ import annotations

import io
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import caseflow  # noqa: E402


def main() -> int:
    access = json.loads(io.open(ROOT / "runtime" / "dify-access.json", encoding="utf-8").read())
    dataset_id = access["dataset_id"]
    base = "http://127.0.0.1:18080/console/api"
    client = httpx.Client(timeout=300, trust_env=False)
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

    documents = client.get(
        base + f"/datasets/{dataset_id}/documents", headers=headers, params={"page": 1, "limit": 100}
    ).json().get("data", [])
    ids = [item["id"] for item in documents]
    if ids:
        params = [("document_id", item) for item in ids]
        removed = client.delete(
            base + f"/datasets/{dataset_id}/documents", headers=headers, params=params
        )
        print("delete documents:", removed.status_code, removed.text[:160], flush=True)
        time.sleep(5)

    for case in caseflow.CASES:
        name = f"{case['case_id']} {case['title']}.txt"
        upload = client.post(
            base + "/files/upload",
            headers={"X-CSRF-Token": headers["X-CSRF-Token"]},
            files={"file": (name, case["document"].encode("utf-8"), "text/plain")},
            data={"source": "datasets"},
        )
        upload.raise_for_status()
        file_id = upload.json()["id"]
        payload = {
            "indexing_technique": "high_quality",
            "data_source": {
                "type": "upload_file",
                "info_list": {"data_source_type": "upload_file", "file_info_list": {"file_ids": [file_id]}},
            },
            "process_rule": {
                "mode": "custom",
                "rules": {
                    "pre_processing_rules": [
                        {"id": "remove_extra_spaces", "enabled": True},
                        {"id": "remove_urls_emails", "enabled": False},
                    ],
                    "segmentation": {"separator": "\n", "max_tokens": 500, "chunk_overlap": 50},
                },
            },
            "doc_form": "text_model",
            "doc_language": "Chinese",
            "embedding_model": access.get("embedding_model", "text-embedding-3-small"),
            "embedding_model_provider": "langgenius/openai/openai",
        }
        created = client.post(base + f"/datasets/{dataset_id}/documents", headers=headers, json=payload)
        print("create", name, created.status_code, flush=True)
        if created.status_code >= 400:
            print(created.text[:500], flush=True)

    for attempt in range(40):
        time.sleep(10)
        docs = client.get(
            base + f"/datasets/{dataset_id}/documents", headers=headers, params={"page": 1, "limit": 100}
        ).json().get("data", [])
        states = [item["indexing_status"] for item in docs]
        done = sum(1 for state in states if state == "completed")
        print(f"poll {attempt}: completed={done}/{len(docs)} states={sorted(set(states))}", flush=True)
        if states and all(state in ("completed", "error") for state in states):
            for item in docs:
                if item["indexing_status"] != "completed":
                    print("FAILED", item["name"][:28], (item.get("error") or "")[:300], flush=True)
            return 0 if all(state == "completed" for state in states) else 1
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
