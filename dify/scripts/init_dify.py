"""把一份干净的 Dify 变成"离谱侦探社"：装插件、配模型、建知识库、导入并发布 DSL。

前置：Dify 已按 README 启动（默认 http://127.0.0.1:18080），后端的 Embedding 服务
与侦探社 bridge 已经能访问。脚本是幂等的：重复执行会复用已有的知识库、应用和密钥。

    python scripts/init_dify.py --env-file .env

执行完会把应用密钥写进 runtime/dify-access.json（该文件被 .gitignore 忽略），
控制台管理员账号与数据集 ID 也记录在那里，便于评审核对。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import httpx
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app import caseflow  # noqa: E402

RUNTIME = ROOT / "runtime"
PLUGINS = {
    "langgenius/deepseek": "langgenius/deepseek:0.0.26@bdf47cb41e9062bba00d26625d2a795c1874011d8ce64bff738c44b5a31787ea",
    "langgenius/openai": "langgenius/openai:1.1.0@1ded02acee56efd5788c86fd447d792e4923e959554a4942f440c7859d164340",
}
DATASET_NAME = "离谱侦探社·案卷库"


def log(message: str) -> None:
    print(f"[init] {message}", flush=True)


def load_env(path: Path) -> dict:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


class Dify:
    def __init__(self, base_url: str, console_prefix: str = "/console/api") -> None:
        self.base = base_url.rstrip("/")
        self.console = self.base + console_prefix
        self.client = httpx.Client(timeout=300, trust_env=False)
        self.token = ""
        self.csrf = ""

    def _headers(self) -> dict:
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if self.csrf:
            headers["X-CSRF-Token"] = self.csrf
        return headers

    def get(self, path: str, **kwargs) -> httpx.Response:
        return self.client.get(self.console + path, headers=self._headers(), **kwargs)

    def post(self, path: str, payload: dict | None = None, **kwargs) -> httpx.Response:
        return self.client.post(self.console + path, headers=self._headers(), json=payload, **kwargs)

    def patch(self, path: str, payload: dict | None = None) -> httpx.Response:
        return self.client.patch(self.console + path, headers=self._headers(), json=payload)

    def delete(self, path: str) -> httpx.Response:
        return self.client.delete(self.console + path, headers=self._headers())


def call(response: httpx.Response, what: str) -> dict:
    if response.status_code >= 400:
        raise RuntimeError(f"{what} 失败：HTTP {response.status_code} {response.text[:500]}")
    try:
        return response.json()
    except ValueError:
        return {"raw": response.text}


def wait_ready(base_url: str, timeout: int = 900) -> None:
    deadline = time.time() + timeout
    url = base_url.rstrip("/") + "/console/api/setup"
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=10, trust_env=False)
            if response.status_code in (200, 401):
                log("Dify 控制台已就绪")
                return
        except httpx.HTTPError:
            pass
        time.sleep(5)
    raise RuntimeError(f"Dify 在 {timeout}s 内没有就绪：{url}")


def ensure_admin(dify: Dify, email: str, password: str, init_password: str = "") -> None:
    """自托管 Dify 1.6+ 先用 INIT_PASSWORD 做一次初始化校验（依赖会话 Cookie），再创建管理员。"""
    state = call(dify.get("/setup"), "读取 setup 状态")
    if str(state.get("step", "")).lower() == "finished":
        log("管理员已初始化，跳过 setup")
        return
    if init_password:
        validated = dify.post("/init", {"password": init_password})
        if validated.status_code >= 400:
            raise RuntimeError(
                f"初始化校验失败：{validated.status_code} {validated.text[:300]}"
                "（INIT_PASSWORD 必须与 Dify 容器里的 INIT_PASSWORD 一致）"
            )
        log("初始化密码已校验")
    payload = {
        "email": email,
        "name": "档案室管理员",
        "password": password,
        "language": "zh-Hans",
    }
    call(dify.post("/setup", payload), "初始化管理员")
    log(f"管理员已创建：{email}")


def login(dify: Dify, email: str, password: str) -> None:
    """登录控制台。

    Dify 1.10 把 access_token / csrf_token 放在 Cookie 里，之后所有控制台请求
    都要带 X-CSRF-Token（状态变更和查询都需要），所以这里顺便取出 CSRF。
    """
    payload = {"email": email, "password": password, "language": "zh-Hans", "remember_me": True}
    data = call(dify.post("/login", payload), "登录")
    token = (data.get("data") or {}).get("access_token") or data.get("access_token")
    if token:
        dify.token = token
    else:
        dify.token = ""
    csrf = dify.client.cookies.get("csrf_token") or ""
    if not csrf and not token:
        raise RuntimeError(f"登录既没有拿到 Cookie，也没有拿到 access_token：{data}")
    dify.csrf = csrf
    log("已登录控制台" + ("" if csrf else "（未取到 CSRF，可能不是 1.10+ 版本）"))


def installed_plugins(dify: Dify) -> set[str]:
    response = dify.get("/workspaces/current/plugin/list", params={"page": 1, "page_size": 100})
    if response.status_code >= 400:
        return set()
    plugins = response.json().get("plugins") or []
    names = set()
    for item in plugins:
        plugin_id = item.get("plugin_id") or ""
        names.add(plugin_id)
        names.add(plugin_id.split(":")[0])
    return names


def ensure_plugins(dify: Dify) -> None:
    wanted = PLUGINS
    present = installed_plugins(dify)
    missing = {name: uid for name, uid in wanted.items() if name not in present and uid not in present}
    if not missing:
        log("模型插件已安装")
        return
    log(f"安装插件：{', '.join(missing)}")
    call(
        dify.post(
            "/workspaces/current/plugin/install/marketplace",
            {"plugin_unique_identifiers": list(missing.values())},
        ),
        "安装插件",
    )
    for _ in range(60):
        time.sleep(5)
        present = installed_plugins(dify)
        if all(name in present for name in missing):
            log("插件安装完成")
            return
    raise RuntimeError("插件安装超时，请检查 Dify 容器到 marketplace.dify.ai 的网络")


def set_provider_credentials(dify: Dify, provider: str, credentials: dict, label: str) -> None:
    response = dify.post(f"/workspaces/current/model-providers/{provider}/credentials", {"credentials": credentials, "name": None})
    if response.status_code in (200, 201):
        log(f"{label} 凭据已保存")
        return
    # 已存在凭据时 Dify 返回 400，这里换成更新
    current = dify.get(f"/workspaces/current/model-providers/{provider}/credentials")
    if current.status_code == 200:
        data = current.json().get("credentials") or {}
        credential_id = data.get("credential_id") if isinstance(data, dict) else None
        if credential_id:
            updated = dify.put(
                f"/workspaces/current/model-providers/{provider}/credentials",
                {"credentials": credentials, "credential_id": credential_id, "name": None},
            )
            if updated.status_code < 400:
                log(f"{label} 凭据已更新")
                return
    raise RuntimeError(f"配置 {label} 凭据失败：{response.status_code} {response.text[:400]}")


def find_dataset(dify: Dify) -> dict | None:
    response = dify.get("/datasets", params={"page": 1, "limit": 50})
    if response.status_code >= 400:
        return None
    for item in response.json().get("data") or []:
        if item.get("name") == DATASET_NAME:
            return item
    return None


def ensure_dataset(dify: Dify, embedding_provider: str, embedding_model: str) -> dict:
    dataset = find_dataset(dify)
    if dataset is None:
        payload = {
            "name": DATASET_NAME,
            "description": "离谱侦探社的 12 份案卷：现场、线索、矛盾、真相、关键证据与坊间流言。",
            "indexing_technique": "high_quality",
            "permission": "all_team_members",
            "provider": "vendor",
        }
        dataset = call(dify.post("/datasets", payload), "创建知识库")
        log(f"知识库已创建：{dataset.get('id')}")
    else:
        log(f"复用知识库：{dataset.get('id')}")

    dataset_id = dataset["id"]
    patch = {
        "name": DATASET_NAME,
        "description": dataset.get("description") or "离谱侦探社案卷库",
        "indexing_technique": "high_quality",
        "embedding_model": embedding_model,
        "embedding_model_provider": embedding_provider,
        "retrieval_model": {
            "search_method": "hybrid_search",
            "reranking_enable": True,
            "reranking_mode": "weighted_score",
            "weights": {"weight_type": "customized", "vector_setting": {"vector_weight": 0.7, "embedding_weight": 0.7}, "keyword_setting": {"keyword_weight": 0.3}},
            "top_k": 6,
            "score_threshold_enabled": False,
            "score_threshold": 0.0,
        },
    }
    updated = dify.patch(f"/datasets/{dataset_id}", patch)
    if updated.status_code >= 400:
        log(f"提示：更新知识库检索设置返回 {updated.status_code}（稍后由 DSL 节点内联设置兜底）")
    else:
        log("知识库检索设置：hybrid_search，向量 0.7 / 关键词 0.3")
    return call(dify.get(f"/datasets/{dataset_id}"), "读取知识库")


def ensure_dataset_key(dify: Dify, dataset_id: str) -> str:
    keys = dify.get(f"/datasets/{dataset_id}/api-keys")
    if keys.status_code == 200:
        data = keys.json().get("data") or []
        if data:
            return data[0]["token"]
    created = call(dify.post(f"/datasets/{dataset_id}/api-keys"), "创建知识库 API Key")
    return created["token"]


def wait_indexing(dify: Dify, dataset_id: str, timeout: int = 600) -> dict:
    deadline = time.time() + timeout
    status = {}
    while time.time() < deadline:
        status = call(dify.get(f"/datasets/{dataset_id}/indexing-status"), "读取索引状态")
        items = status.get("data") or []
        if items and all(item.get("indexing_status") in ("completed", "error") for item in items):
            break
        time.sleep(3)
    return status


def upload_documents(dify: Dify, dataset_id: str, embedding_model: str, force: bool) -> int:
    """通过控制台的「上传文件 → 建文档」两步接口灌入 12 份案卷。

    不用数据集 API Key：Dify 1.10.1 的 /datasets/<id>/api-keys 在部分构建里会抛
    AttributeError（ApiToken.dataset_id），控制台接口本身是稳定的。
    """
    existing = call(dify.get(f"/datasets/{dataset_id}/documents", params={"page": 1, "limit": 100}), "读取已有文档")
    names = {item.get("name") for item in existing.get("data") or []}
    uploaded = 0
    for case in caseflow.CASES:
        name = f"{case['case_id']} {case['title']}.txt"
        if not force and name in names:
            continue
        content = case["document"].encode("utf-8")
        upload = dify.client.post(
            dify.console + "/files/upload",
            headers={"X-CSRF-Token": dify.csrf},
            files={"file": (name, content, "text/plain")},
            data={"source": "datasets"},
        )
        file_id = call(upload, f"上传 {name}").get("id")
        payload = {
            "indexing_technique": "high_quality",
            "data_source": {
                "type": "upload_file",
                "info_list": {
                    "data_source_type": "upload_file",
                    "file_info_list": {"file_ids": [file_id]},
                },
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
            "embedding_model": embedding_model,
            "embedding_model_provider": "langgenius/openai/openai",
        }
        call(dify.post(f"/datasets/{dataset_id}/documents", payload), f"建立文档 {name}")
        uploaded += 1
        log(f"已上传案卷：{case['case_id']} {case['title']}")
    return uploaded

def import_app(dify: Dify, dsl_path: Path) -> dict:
    content = dsl_path.read_text(encoding="utf-8")
    payload = {"mode": "yaml-content", "yaml_content": content}
    result = call(dify.post("/apps/imports", payload), "导入 DSL")
    if result.get("status") == "pending":
        import_id = result.get("import_id") or result.get("id")
        result = call(dify.post(f"/apps/imports/{import_id}/confirm"), "确认导入 DSL")
    app_id = result.get("app_id")
    if not app_id:
        raise RuntimeError(f"导入 DSL 没有返回 app_id：{result}")
    log(f"应用已导入：{app_id}（{result.get('status')}）")
    return result


def publish_app(dify: Dify, app_id: str) -> None:
    response = dify.post(f"/apps/{app_id}/workflows/publish")
    if response.status_code >= 400:
        raise RuntimeError(f"发布失败：{response.status_code} {response.text[:600]}")
    log("工作流已发布")


def ensure_app_key(dify: Dify, app_id: str) -> str:
    keys = dify.get(f"/apps/{app_id}/api-keys")
    if keys.status_code == 200:
        data = keys.json().get("data") or []
        if data:
            return data[0]["token"]
    created = call(dify.post(f"/apps/{app_id}/api-keys"), "创建应用 API Key")
    return created["token"]


def find_app(dify: Dify, name: str = "离谱侦探社") -> str | None:
    response = dify.get("/apps", params={"page": 1, "limit": 100})
    if response.status_code >= 400:
        return None
    for item in response.json().get("data") or []:
        if item.get("name") == name:
            return item.get("id")
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    parser.add_argument("--skip-dsl", action="store_true")
    parser.add_argument("--force-corpus", action="store_true")
    args = parser.parse_args()

    env = {**load_env(Path(args.env_file)), **os.environ}
    base_url = env.get("DIFY_URL", "http://127.0.0.1:18080")
    email = env.get("DIFY_ADMIN_EMAIL", "detective@example.com")
    password = env.get("DIFY_ADMIN_PASSWORD", "")
    deepseek_key = env.get("DEEPSEEK_API_KEY", "")
    embed_base = env.get("EMBED_BASE_URL", "http://host.docker.internal:8013/v1")
    embed_model = env.get("EMBED_MODEL", "bge-text-embedding")
    bridge_url = env.get("DETECTIVE_BASE_URL", "http://host.docker.internal:8012")
    detective_key = env.get("DETECTIVE_API_KEY", "")
    if not password:
        raise SystemExit("请在 .env 里设置 DIFY_ADMIN_PASSWORD")
    if not detective_key:
        raise SystemExit("请在 .env 里设置 DETECTIVE_API_KEY")

    RUNTIME.mkdir(parents=True, exist_ok=True)
    wait_ready(base_url)
    dify = Dify(base_url)
    ensure_admin(dify, email, password, env.get("INIT_PASSWORD", ""))
    login(dify, email, password)
    ensure_plugins(dify)

    if deepseek_key:
        set_provider_credentials(
            dify,
            "langgenius/deepseek/deepseek",
            {"api_key": deepseek_key, "endpoint_url": env.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")},
            "DeepSeek",
        )
    else:
        log("未设置 DEEPSEEK_API_KEY，跳过大模型凭据（评审需自行配置）")

    set_provider_credentials(
        dify,
        "langgenius/openai/openai",
        {
            "openai_api_key": env.get("EMBED_API_KEY", "local-embed"),
            "openai_api_base": embed_base,
            "api_protocol": "chat",
        },
        "本地 Embedding",
    )

    dataset = ensure_dataset(dify, "langgenius/openai/openai", env.get("EMBED_DIFY_MODEL", "text-embedding-3-small"))
    dataset_id = dataset["id"]
    uploaded = upload_documents(dify, dataset_id, env.get("EMBED_DIFY_MODEL", "text-embedding-3-small"), args.force_corpus)
    if uploaded:
        status = wait_indexing(dify, dataset_id)
        log(f"索引状态：{json.dumps(status.get('data'), ensure_ascii=False)[:400]}")
    else:
        log("案卷已全部在库，跳过上传（--force-corpus 可强制重传）")

    access = {
        "dify_url": base_url,
        "admin_email": email,
        "dataset_id": dataset_id,
        "dataset_name": DATASET_NAME,
        "embedding_provider": "langgenius/openai/openai",
        "embedding_model": embed_model,
        "bridge_url": bridge_url,
    }

    if not args.skip_dsl:
        dsl_path = RUNTIME / "casefile-detective.rendered.yml"
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts" / "build_dsl.py"),
                "--dataset-id",
                dataset_id,
                "--llm-provider",
                env.get("LLM_PROVIDER", "langgenius/deepseek/deepseek"),
                "--llm-model",
                env.get("LLM_MODEL", "deepseek-flash"),
                "--bridge-url",
                bridge_url,
                "--embedding-provider",
                "langgenius/openai/openai",
                "--embedding-model",
                embed_model,
                "--api-key-placeholder",
                detective_key,
                "--output",
                str(dsl_path),
            ],
            check=True,
        )
        existing = find_app(dify)
        if existing:
            log(f"删除旧的应用实例 {existing}（保持幂等）")
            dify.delete(f"/apps/{existing}")
        result = import_app(dify, dsl_path)
        app_id = result["app_id"]
        publish_app(dify, app_id)
        app_key = ensure_app_key(dify, app_id)
        access.update({"app_id": app_id, "app_api_key": app_key, "app_mode": "advanced-chat"})
        log(f"控制台：{base_url}/app/{app_id}/workflow")
        log(f"体验地址：{base_url}/chat/{app_key}")

    (RUNTIME / "dify-access.json").write_text(json.dumps(access, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"接入信息已写入 {RUNTIME / 'dify-access.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
