"""部署本作品使用的 Dify 1.10.1（Docker Compose）+ seekdb 向量库适配。

做四件事：
1. 检查镜像是否齐全，缺哪个就提示（本机网络无法直连 Docker Hub 时，评审需自行取镜像）；
2. 生成运行时 .env（从 .env.example 派生，保留用户已填的密钥）；
3. 用 deploy/docker-compose.seekdb.yaml 覆盖 api/worker 镜像（含 seekdb 适配补丁）；
4. 起 seekdb 容器、启动 Dify 服务栈、等控制台就绪。

    python scripts/deploy_dify.py --env-file .env
    python scripts/deploy_dify.py --env-file .env --dify-src D:\\code\\dify-1.10.1
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_IMAGES = [
    "langgenius/dify-api:1.10.1",
    "langgenius/dify-api:1.10.1-seekdb",
    "langgenius/dify-web:1.10.1",
    "langgenius/dify-sandbox:0.2.12",
    "langgenius/dify-plugin-daemon:0.4.1-local",
    "postgres:15-alpine",
    "redis:6-alpine",
    "nginx:latest",
    "ubuntu/squid:latest",
    "oceanbase/seekdb:1.4.0-100000172026082615",
]


def sh(cmd: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    print("$", " ".join(cmd), flush=True)
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, text=True, check=check)


def images_present() -> tuple[list[str], list[str]]:
    output = subprocess.run(
        ["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"], capture_output=True, text=True
    ).stdout
    have = set(output.split())
    missing = [image for image in REQUIRED_IMAGES if image not in have]
    return sorted(have), missing


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
    parser.add_argument("--dify-src", default=os.getenv("DIFY_SRC", ""))
    parser.add_argument("--project", default="dify-seekdb-kb")
    parser.add_argument("--seekdb-container", default="dify-seekdb")
    parser.add_argument("--seekdb-port", type=int, default=None)
    parser.add_argument("--skip-start", action="store_true")
    args = parser.parse_args()

    env = {**load_env(Path(args.env_file)), **os.environ}
    dify_src = Path(args.dify_src or env.get("DIFY_SRC", ""))
    compose_file = dify_src / "docker" / "docker-compose.yaml"
    if not compose_file.exists():
        raise SystemExit(
            "找不到 Dify 源码里的 docker/docker-compose.yaml。\n"
            "请先 git clone --depth 1 --branch 1.10.1 https://github.com/langgenius/dify.git <目录>，"
            "然后 --dify-src <目录> 或设置 DIFY_SRC。"
        )

    _, missing = images_present()
    if missing:
        print("⚠️  缺少镜像：")
        for image in missing:
            print("   -", image)
        print("   本作品在离线网络里用 scripts/pull 的镜像打包流程获取过；如无网络请提前导入镜像（docker load）。")

    # 1. 构建 seekdb 适配镜像（幂等，几秒）
    print("构建 seekdb 适配镜像 langgenius/dify-api:1.10.1-seekdb …", flush=True)
    sh(["docker", "build", "-f", str(ROOT / "patches" / "Dockerfile.api-seekdb"),
        "-t", "langgenius/dify-api:1.10.1-seekdb", str(ROOT / "patches")])

    # 2. seekdb 容器（知识库向量 + 全文存储）
    port = args.seekdb_port or int(env.get("OCEANBASE_VECTOR_PORT", "2886"))
    password = env.get("OCEANBASE_VECTOR_PASSWORD", env.get("SEEKDB_PASSWORD", ""))
    if not password:
        raise SystemExit("请在 .env 里设置 OCEANBASE_VECTOR_PASSWORD（seekdb root 口令）")
    data_dir = Path(env.get("SEEKDB_DATA_DIR", ROOT / "runtime" / "seekdb"))
    data_dir.mkdir(parents=True, exist_ok=True)
    existing = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=^{args.seekdb_container}$", "--format", "{{.Names}}"],
        capture_output=True, text=True,
    ).stdout.strip()
    if not existing:
        sh([
            "docker", "run", "-d", "--name", args.seekdb_container,
            "-p", f"{port}:2881",
            "-e", f"ROOT_PASSWORD={password}",
            "-v", f"{data_dir}:/var/lib/oceanbase",
            "--restart", "unless-stopped",
            "oceanbase/seekdb:1.4.0-100000172026082615",
        ])
    else:
        sh(["docker", "start", args.seekdb_container], check=False)
    print(f"seekdb 容器就绪：端口 {port}，数据目录 {data_dir}", flush=True)

    # 3. 启动 Dify
    if args.skip_start:
        return 0
    compose = [
        "docker", "compose", "-p", args.project,
        "--env-file", str(Path(args.env_file)),
        "--project-directory", str(dify_src / "docker"),
        "-f", str(compose_file),
        "-f", str(ROOT / "deploy" / "docker-compose.seekdb.yaml"),
    ]
    sh([*compose, "up", "-d", "--pull", "never"])

    # 4. 等控制台就绪
    url = env.get("DIFY_URL", "http://127.0.0.1:18080").rstrip("/") + "/console/api/setup"
    deadline = time.time() + 900
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=10, trust_env=False)
            if response.status_code in (200, 401):
                print(f"Dify 控制台就绪：{url}", flush=True)
                return 0
        except httpx.HTTPError:
            pass
        time.sleep(10)
    print("Dify 控制台在 900s 内没有就绪，请检查 docker compose ps 与 api 日志", flush=True)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
