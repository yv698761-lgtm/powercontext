"""一次性拉取本地 Embedding 模型（BAAI/bge-small-zh-v1.5 的 ONNX 导出）。

评审环境不一定能访问 HuggingFace，本脚本按顺序尝试多个镜像，只下载运行需要的
四个文件（约 100 MB），落到 models/bge-small-zh-v1.5/。

    python scripts/download_embedding_model.py

也可以自己准备：把 config.json、tokenizer.json、tokenizer_config.json、onnx/model.onnx
放到 models/bge-small-zh-v1.5/ 即可，或用 EMBED_MODEL_DIR 指向已有目录。
"""

from __future__ import annotations

import sys
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

TARGET = Path(__file__).resolve().parents[1] / "models" / "bge-small-zh-v1.5"
FILES = [
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "onnx/model.onnx",
]
MIRRORS = [
    "https://hf-mirror.com/Xenova/bge-small-zh-v1.5/resolve/main/{file}",
    "https://huggingface.co/Xenova/bge-small-zh-v1.5/resolve/main/{file}",
    "https://modelscope.cn/models/Xenova/bge-small-zh-v1.5/resolve/master/{file}",
]


def download(url: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    print(f"  {url}", flush=True)
    with urllib.request.urlopen(url, timeout=120) as response, dest.open("wb") as handle:
        while True:
            chunk = response.read(1024 * 512)
            if not chunk:
                break
            handle.write(chunk)


def main() -> int:
    print(f"目标目录：{TARGET}")
    for name in FILES:
        dest = TARGET / name
        if dest.exists() and dest.stat().st_size > 1024:
            print(f"已存在：{name} ({dest.stat().st_size // 1024} KiB)")
            continue
        for template in MIRRORS:
            url = template.format(file=name)
            try:
                download(url, dest)
                print(f"完成：{name} ({dest.stat().st_size // 1024} KiB)")
                break
            except Exception as exc:  # noqa: BLE001
                print(f"  失败：{exc}")
        else:
            print(f"❌ 所有镜像都下载失败：{name}")
            return 1
    print("模型就绪。启动 Embedding 服务：python -m app.embed_server --port 8013")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
