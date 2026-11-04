#!/usr/bin/env python3
"""Print the real query plan of the hybrid search statement.

The seekdb-js client returns no rows for `EXPLAIN`, so the plan is read with a
MySQL client against the same server. The statement is rebuilt here from the
same template the server uses (`renderHybridSql` in `server/index.mjs`) with a
unit vector, which is enough to show which access paths the optimiser picks.

    python scripts/explain-plan.py [--collection NAME] [--text QUERY]

Requires the `mysql` client on PATH or in PATH via MYSQL_CLIENT, and seekdb
reachable at SEEKDB_HOST / SEEKDB_PORT (default 127.0.0.1:2881).
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys


def find_client() -> str:
    return os.environ.get("MYSQL_CLIENT", "mysql")


def fetch_table(client: str, host: str, port: str, database: str, collection: str) -> str:
    sql = (
        "SELECT table_name FROM information_schema.tables "
        f"WHERE table_schema = '{database}' AND table_name NOT IN ('sdk_collections') "
        "ORDER BY create_time DESC LIMIT 1"
    )
    out = run_sql(client, host, port, database, sql)
    table = out.strip().splitlines()[-1] if out.strip() else ""
    if not table:
        sys.exit(f"no collection table found in {database}; import the corpus first")
    return table


def run_sql(client: str, host: str, port: str, database: str, sql: str) -> str:
    proc = subprocess.run(
        [client, f"-h{host}", f"-P{port}", "-uroot", "-N", "-B",
         "--default-character-set=utf8mb4", database, "-e", sql],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0:
        sys.exit(proc.stderr.strip() or f"mysql exited with {proc.returncode}")
    return proc.stdout


def build_sql(table: str, text: str, dim: int = 512, k: int = 5, window: int = 60, branch: int = 40) -> str:
    """Mirror of renderHybridSql() in server/index.mjs."""
    def lit(value: str) -> str:
        return "'" + value.replace("'", "''") + "'"

    vector = json.dumps([1.0] + [0.0] * (dim - 1))
    fts = (
        "(SELECT *, RANK() OVER (ORDER BY _keyword_score DESC) AS _keyword_rank FROM ("
        "SELECT _id, document, embedding, metadata, "
        f"MATCH(document) AGAINST ({lit(text)} IN NATURAL LANGUAGE MODE) AS _keyword_score "
        f"FROM {table} WHERE MATCH(document) AGAINST ({lit(text)} IN NATURAL LANGUAGE MODE) "
        "ORDER BY _keyword_score DESC LIMIT 800))"
    )
    vec = (
        "(SELECT *, RANK() OVER (ORDER BY _semantic_score DESC) AS _semantic_rank FROM ("
        "SELECT _id, document, embedding, metadata, "
        f"COSINE_DISTANCE(embedding, '{vector}') AS _distance, "
        f"ROUND(1 - COSINE_DISTANCE(embedding, '{vector}') / 2, 8) AS _semantic_score "
        f"FROM {table} ORDER BY _distance APPROXIMATE LIMIT {branch}))"
    )
    fusion = (
        f"IFNULL(1 / (_fts._keyword_rank + {window}), 0) + "
        f"IFNULL(1 / (_vs._semantic_rank + {window}), 0)"
    )
    return (
        f"SELECT IFNULL(_fts._id, _vs._id) AS _id, _fts.document, _fts.metadata, {fusion} AS _score "
        f"FROM {fts} _fts FULL JOIN {vec} _vs ON _fts._id = _vs._id "
        f"ORDER BY _score DESC LIMIT {k}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--collection", default="kb_stepfun_demo")
    parser.add_argument("--text", default="新写入的数据查不到怎么办")
    parser.add_argument("--dim", type=int, default=512)
    parser.add_argument("--host", default=os.environ.get("SEEKDB_HOST", "127.0.0.1"))
    parser.add_argument("--port", default=os.environ.get("SEEKDB_PORT", "2881"))
    parser.add_argument("--database", default=os.environ.get("SEEKDB_DATABASE", "test"))
    args = parser.parse_args()

    client = find_client()
    table = fetch_table(client, args.host, args.port, args.database, args.collection)
    sql = build_sql(table, args.text, dim=args.dim)
    plan = run_sql(client, args.host, args.port, args.database, f"EXPLAIN {sql}")

    print(f"collection : {args.collection}")
    print(f"table      : {table}")
    print(f"query      : {args.text}")
    print()
    print(plan.rstrip())
    print()

    checks = {
        "TEXT RETRIEVAL SCAN": "native fulltext index (idx_fts)",
        "VECTOR INDEX SCAN": "native vector index (idx_vec)",
    }
    ok = True
    for needle, label in checks.items():
        hit = needle in plan
        ok &= hit
        print(f"[{'PASS' if hit else 'FAIL'}] {needle:22} -> {label}")
    return 0 if ok else 1


if __name__ == "__main__":
    # The plan uses box-drawing characters; keep them intact on legacy codepages.
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:  # pragma: no cover - very old Python
        pass
    raise SystemExit(main())
