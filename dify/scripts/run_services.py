"""按 README 约定启动本地三个服务（PowerContext / Embedding / 侦探社后端）。

    python scripts/run_services.py --env-file .env              # 全部前台启动
    python scripts/run_services.py --env-file .env --only bridge
    python scripts/run_services.py --env-file .env --only embed
    python scripts/run_services.py --env-file .env --only powercontext

PowerContext 需要单独的 `powercontext` 命令（uv tool install），这里通过
POWERCONTEXT_BIN 指定，缺省则从 PATH 里找。
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def load_env(path: Path) -> dict:
    values: dict[str, str] = {}
    if path.exists():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    parser.add_argument("--only", choices=["powercontext", "embed", "bridge", "all"], default="all")
    args = parser.parse_args()

    env = {**load_env(Path(args.env_file)), **os.environ}
    env.setdefault("POWERCONTEXT_URL", "http://127.0.0.1:8011")
    env.setdefault("DETECTIVE_PORT", "8012")
    env.setdefault("EMBED_PORT", "8013")

    processes: list[subprocess.Popen] = []
    try:
        if args.only in ("all", "powercontext"):
            binary = env.get("POWERCONTEXT_BIN") or shutil.which("powercontext")
            if not binary:
                raise SystemExit("未找到 powercontext 命令，请先 uv tool install \"powercontext[cli,server]==1.5.0\"")
            env_file = env.get("POWERCONTEXT_ENV_FILE", str(ROOT / "deploy" / "powercontext.env"))
            processes.append(
                subprocess.Popen([binary, "server", "run", "--env-file", env_file], env=env)
            )
            print(f"[powercontext] 启动中 → {env['POWERCONTEXT_URL']}", flush=True)

        if args.only in ("all", "embed"):
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-m", "app.embed_server", "--port", str(env.get("EMBED_PORT", "8013"))],
                    cwd=str(ROOT),
                    env=env,
                )
            )
            print(f"[embed] 启动中 → http://127.0.0.1:{env.get('EMBED_PORT', '8013')}", flush=True)

        if args.only in ("all", "bridge"):
            processes.append(
                subprocess.Popen(
                    [sys.executable, "-m", "app.main"],
                    cwd=str(ROOT),
                    env=env,
                )
            )
            print(f"[bridge] 启动中 → http://127.0.0.1:{env.get('DETECTIVE_PORT', '8012')}", flush=True)

        print("Ctrl+C 结束。健康检查：/health（bridge）、/health（embed）、/v1/server-info（PowerContext）")
        for process in processes:
            process.wait()
    except KeyboardInterrupt:
        print("\n正在停止…")
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
