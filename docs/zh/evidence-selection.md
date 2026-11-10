# 按查询和预算选择原文证据

## 使用

```python
from powercontext.builtin.runtime import PrepareContextRequest

request = PrepareContextRequest(
    query="Which treatment reduced mortality?",
    max_bytes=1400,
    evidence_selection=True,
)
context = await runtime.context.for_scope(scope_id).prepare(request)
```

`evidence_selection` 是严格布尔值，默认 `False`。关闭时沿用普通 prepare 的选择、前缀截断、引用和输出格式。HTTP `/v1/context/prepare` 同样接受该字段。

开启时只选择 Memory 原文，忽略 `assembly` 的展示/家族选择及 `include_code`，始终使用带引用的 JSON envelope。检索阶段仍负责提供候选；本功能不扩大候选集合，不读取外部语料、标注或模型资源，不调用网络服务。

## 原文和身份

`PreparedContextBuilder.build_scopes_result(...)` 的返回值包含 `fragments` 元组。每个 `PreparedMemoryFragment` 包含：

- `address: MemoryEntryAddress`：scope、Memory artifact/revision、entry ID、entry version ID；
- `start: int`、`end: int`：原 entry 的 Python 字符半开区间；
- `text: str`：原文的 `entry.text[start:end]`。

`origins` 按片段顺序返回相应的带 scope 地址。JSON 每项的 `citation` 包含 `scope_id`、`memory_ref`、`entry_id`、`entry_version_id`，并附 `content`、`start`、`end`。评分阶段的大小写转换、词形处理不影响输出原文。同一 entry 的相邻句合并为原文连续区间；非相邻证据可以返回多个片段。重复正文只保留一个真实来源，不拼接不同 scope 的身份。

## 离线选择与预算

1. 扫描全文，按句末和换行生成原字符区间，保护常见缩写及小数。
2. 查询与句子使用 Unicode 词项匹配、轻量词形归一化、候选文档词项逆频率和文档级覆盖评分。
3. 对重复句子去重，用有界 beam 搜索在预算内组合完整句，降低重复词项的边际收益。
4. 每个搜索状态均以最终 trust policy、envelope、JSON 转义、完整 citation 和正文序列化后的 UTF-8 字节数计费。没有完整句可装入时返回合法空 Context，不通过删减引用或生成摘要挤入预算。

搜索最多保留 96 个相关句、128 个状态，每状态至多 12 个句子；状态按字节桶剪枝。这是确定性的启发式预算搜索，不承诺全局最优。`probe_budget` 使用同一选择路径，因此已交付数量和剩余字节与实际构建一致。

## 验证范围

公开验收验证真实 Memory capture、flush、FTS、citation 回查及最终字节数。产品回归测试另外覆盖多片段、跨 scope、Unicode/转义、预算 N/N−1、严格布尔值和默认兼容。

验证结果：

- 官方 Docker 镜像，Linux x86_64、Python 3.12、固定依赖、禁用网络、1 CPU / 2 GiB、源码只读：公开验收 **3 passed**，产品证据选择专项 **20 passed**。
- Windows Python 3.12：证据选择、普通 prepare、runtime 端到端、文本组装、recall gate、API/JS 契约合计 **190 passed**。
- API 生成结果检查、JS 操作生成结果检查、修改代码 Ruff 检查和 `git diff --check` 通过。

公开验收复现命令：

```bash
docker compose -f competition/compose.yaml build selftest
docker compose -f competition/compose.yaml run --rm selftest \
  python -m pytest -q -c /selftest/pytest.ini --confcutdir=/selftest \
  /selftest/challenges/context-01-evidence-selection/acceptance
```

Windows checkout 若把 shell 脚本转换成 CRLF，需要在容器临时目录将入口脚本转换为 LF 再运行。测试时仅转换临时副本，未改动公开测试、输入、依赖或评分逻辑。

公开 SciFact 样例没有 evidence 标注；公开测试通过不能证明正式 48 次请求的平均 evidence token recall ≥ 0.65 或完整证据覆盖比例 ≥ 40%。实现不读取或依赖隐藏标注，正式质量结论以平台验收为准。
