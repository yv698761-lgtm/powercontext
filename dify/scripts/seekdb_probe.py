"""证明 seekdb 真的承担了 Dify 知识库的存储与检索。

它直接连 seekdb（默认 127.0.0.1:2886，库 test），做四件事：

1. 列出 Dify 建的向量集合表，数出向量行数；
2. 打印建表语句里的 VECTOR KEY（HNSW）与 FULLTEXT KEY（分词器），证明两类索引真的存在；
3. 直接跑一次向量近似检索与一次全文检索，返回真实命中的片段摘要；
4. 把结果写入 evidence/seekdb-probe.json，供评审核对。

用法：
    python scripts/seekdb_probe.py --host 127.0.0.1 --port 2886 \
        --password <seekdb_root_pw> --database test
"""

from __future__ import annotations

import argparse
import json
import os
import re
import time
from pathlib import Path

import pymysql

ROOT = Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"


def connect(host: str, port: int, user: str, password: str, database: str):
    return pymysql.connect(
        host=host,
        port=port,
        user=user,
        password=password,
        database=database,
        charset="utf8mb4",
        autocommit=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default=os.getenv("SEEKDB_HOST", "127.0.0.1"))
    parser.add_argument("--port", type=int, default=int(os.getenv("SEEKDB_PORT", "2886")))
    parser.add_argument("--user", default=os.getenv("SEEKDB_USER", "root"))
    parser.add_argument("--password", default=os.getenv("SEEKDB_PASSWORD", ""))
    parser.add_argument("--database", default=os.getenv("SEEKDB_DATABASE", "test"))
    parser.add_argument("--query", default="铜铃 撬痕 朝院内")
    args = parser.parse_args()

    if not args.password:
        raise SystemExit("请通过 --password 或 SEEKDB_PASSWORD 提供 seekdb 口令")

    conn = connect(args.host, args.port, args.user, args.password, args.database)
    cur = conn.cursor()

    cur.execute("SELECT VERSION()")
    version = cur.fetchone()[0]

    cur.execute("SHOW TABLES")
    tables = [row[0] for row in cur.fetchall()]

    report = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "endpoint": f"{args.host}:{args.port}/{args.database}",
        "server_version": version,
        "tables": [],
        "vector_search": [],
        "fulltext_search": [],
        "query": args.query,
    }

    for table in tables:
        if table in ("t_probe", "t_probe2", "t_probe3"):
            continue
        cur.execute(f"SELECT COUNT(*) FROM `{table}`")
        rows = cur.fetchone()[0]
        cur.execute(f"SHOW CREATE TABLE `{table}`")
        ddl = cur.fetchone()[1] or ""
        vector_idx = re.findall(r"VECTOR KEY `([^`]+)`[^,\n]*", ddl)
        fulltext_idx = re.findall(r"FULLTEXT KEY `([^`]+)`", ddl)
        parser = re.search(r"WITH PARSER (\w+)", ddl)
        report["tables"].append(
            {
                "table": table,
                "rows": rows,
                "vector_indexes": vector_idx,
                "fulltext_indexes": fulltext_idx,
                "fulltext_parser": parser.group(1) if parser else None,
            }
        )

        if rows and vector_idx:
            cur.execute(f"SHOW COLUMNS FROM `{table}` LIKE 'vector'")
            if cur.fetchone():
                # 取第一条向量当查询向量；seekdb 的向量近似检索不接受子查询作为向量常量，
                # 所以这里先取出来再拼成字面量（与 Dify 的 ann_search 行为一致）。
                cur.execute(f"SELECT vector FROM `{table}` LIMIT 1")
                probe_vector = cur.fetchone()[0]
                cur.execute(
                    f"SELECT id, LEFT(text, 80), ROUND(l2_distance(vector, %s), 4) AS dist FROM `{table}` "
                    f"ORDER BY l2_distance(vector, %s) APPROXIMATE LIMIT 3",
                    (probe_vector, probe_vector),
                )
                report["vector_search"] = [
                    {"id": row[0], "text": row[1], "distance": float(row[2])} for row in cur.fetchall()
                ]

            if fulltext_idx:
                try:
                    cur.execute(
                        f"SELECT id, LEFT(text, 90), MATCH(text) AGAINST (%s) AS score FROM `{table}` "
                        f"WHERE MATCH(text) AGAINST (%s) > 0 ORDER BY score DESC LIMIT 3",
                        (args.query, args.query),
                    )
                    report["fulltext_search"] = [
                        {"id": row[0], "text": row[1], "score": float(row[2])} for row in cur.fetchall()
                    ]
                except Exception as exc:  # noqa: BLE001
                    report["fulltext_search_error"] = str(exc)[:300]

    conn.close()

    EVIDENCE.mkdir(parents=True, exist_ok=True)
    target = EVIDENCE / "seekdb-probe.json"
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2)[:2500])
    print(f"\n写入 {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
