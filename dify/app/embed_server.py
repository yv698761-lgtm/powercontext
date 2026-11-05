"""本地 Embedding 服务（OpenAI 兼容），供 Dify 知识库使用。

评审环境不一定有商用 Embedding API，因此这里用 onnxruntime + tokenizers 直接跑
`BAAI/bge-small-zh-v1.5`（512 维中文模型）的 ONNX 导出，不依赖 torch、不联网推理。

模型文件由 `scripts/download_embedding_model.py` 一次性下载到 `models/bge-small-zh-v1.5`，
也可以用 EMBED_MODEL_DIR 指向已有目录。

启动：
    python -m app.embed_server --port 8013
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

import numpy as np
import onnxruntime as ort
from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Any
from tokenizers import Tokenizer

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "models" / "bge-small-zh-v1.5"
MODEL_DIR = Path(os.getenv("EMBED_MODEL_DIR", DEFAULT_DIR))
MODEL_NAME = os.getenv("EMBED_MODEL_NAME", "bge-small-zh-v1.5")
MAX_LEN = int(os.getenv("EMBED_MAX_LEN", "512"))

_session: ort.InferenceSession | None = None
_tokenizer: Tokenizer | None = None


def load() -> tuple[ort.InferenceSession, Tokenizer]:
    global _session, _tokenizer
    if _session is None or _tokenizer is None:
        onnx_path = next(iter(sorted(MODEL_DIR.glob("onnx/*.onnx"))), None)
        tokenizer_path = MODEL_DIR / "tokenizer.json"
        if onnx_path is None or not tokenizer_path.exists():
            raise RuntimeError(f"未找到 Embedding 模型：{MODEL_DIR}（先运行 scripts/download_embedding_model.py）")
        _session = ort.InferenceSession(str(onnx_path), providers=["CPUExecutionProvider"])
        _tokenizer = Tokenizer.from_file(str(tokenizer_path))
        _tokenizer.enable_truncation(max_length=MAX_LEN)
        _tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
    return _session, _tokenizer


def encode(texts: list[str]) -> np.ndarray:
    session, tokenizer = load()
    batch = tokenizer.encode_batch(texts)
    ids = np.array([item.ids for item in batch], dtype=np.int64)
    mask = np.array([item.attention_mask for item in batch], dtype=np.int64)
    types = np.array([item.type_ids for item in batch], dtype=np.int64)
    feed = {"input_ids": ids, "attention_mask": mask}
    if "token_type_ids" in {item.name for item in session.get_inputs()}:
        feed["token_type_ids"] = types
    hidden = session.run(None, feed)[0]
    expanded = mask[..., None].astype(np.float32)
    pooled = (hidden * expanded).sum(axis=1) / np.clip(expanded.sum(axis=1), 1e-9, None)
    return pooled / np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-12, None)


app = FastAPI(title="local bge-small-zh embedding server", version="1.0.0")


class EmbeddingRequest(BaseModel):
    """OpenAI Embeddings 请求体的最小子集。

    不同客户端会附带 dimensions、user、encoding_format 等字段，这里一律忽略多余字段，
    只保证 input 可用；返回端口固定输出 float 数组。
    """

    model_config = {"extra": "allow"}

    input: Any
    model: str | None = None
    encoding_format: str | None = "float"
    dimensions: int | None = None
    user: str | None = None


@app.get("/health")
def health() -> dict:
    session, _ = load()
    return {
        "status": "ok",
        "model": MODEL_NAME,
        "dimension": int(session.get_outputs()[0].shape[-1]),
        "backend": "onnxruntime-cpu",
        "dir": str(MODEL_DIR),
    }


@app.get("/v1/models")
def models() -> dict:
    return {"object": "list", "data": [{"id": MODEL_NAME, "object": "model", "owned_by": "local"}]}


@app.post("/v1/embeddings")
def embeddings(req: EmbeddingRequest):
    raw = req.input
    if isinstance(raw, str):
        texts = [raw]
    elif isinstance(raw, list):
        texts = [item if isinstance(item, str) else str(item) for item in raw]
    else:
        texts = [str(raw)]
    print(json.dumps({"input_type": type(raw).__name__, "count": len(texts)}, ensure_ascii=False), flush=True)
    if not texts:
        raise HTTPException(422, "input 不能为空")
    started = time.time()
    vectors = encode(texts)
    return JSONResponse(
        {
            "object": "list",
            "model": req.model or MODEL_NAME,
            "data": [
                {"object": "embedding", "index": index, "embedding": vector.tolist()}
                for index, vector in enumerate(vectors)
            ],
            "usage": {"prompt_tokens": 0, "total_tokens": 0},
            "elapsed_ms": round((time.time() - started) * 1000, 1),
        }
    )


@app.post("/v1/chat/completions")
def chat_completions(req: dict):
    """凭据校验用的最小对话回应。

    Dify 的 OpenAI 插件在保存供应商凭据时会发一次极小的 chat 请求做连通性校验。
    本服务只负责 Embedding，这里返回一句固定文本，明确它不是对话模型；
    工作流里的对话/推理全部走 DeepSeek（见 README 的模型分工）。
    """
    return JSONResponse(
        {
            "id": "chatcmpl-local-embed",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": req.get("model") or MODEL_NAME,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": "本服务仅提供本地 Embedding，不提供对话补全。"},
                    "finish_reason": "stop",
                }
            ],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8013)
    args = parser.parse_args()
    import uvicorn

    print(json.dumps({"model": MODEL_NAME, "dir": str(MODEL_DIR), "dimension": int(encode(["预热"]).shape[1])}, ensure_ascii=False))
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
