# 端侧知识库 · seekdb 混合检索

浏览器本地完成向量化，seekdb 负责存储与混合检索的迷你知识库应用。

- 文档向量化与查询向量化都在浏览器里跑（Transformers.js + ONNX Runtime Web），**不调用任何云端推理接口**
- 向量、原文与元数据写入 seekdb 集合；向量检索与全文检索的融合排序由**数据库内一条 SQL**完成
- 默认优先 WebGPU（FP16 权重），不支持时自动降级到 WASM（INT8 权重），界面顶部显示当前后端

## 1. 前置条件

| 项 | 要求 |
| --- | --- |
| Node.js | ≥ 20（实测 24.21.0） |
| 浏览器 | Chrome / Edge 最新版（WebGPU）；其他浏览器自动走 WASM |
| seekdb | 服务端模式，实测官方 `oceanbase/seekdb:1.4.0` 镜像（`seekdb-v1.4.0.0`） |
| 网络 | 首次启动需能下载模型文件（约 24–95 MB，取决于后端） |

> **关于 seekdb 版本**：seekdb-js 把集合存放在内部表 `c$v2$<collection id>`，混合检索语句读取该表。
> 本仓库在官方 1.4.0 发行包上验证通过，**不需要**自行编译 `2026-competition` 分支。
> 服务启动时会做一次能力探测（建集合 → 写入 → 刷新 → 混合检索 → 删集合），
> 不支持时在日志与 `/api/health` 中明确报错。

## 2. 启动

```bash
# 1) 启动 seekdb（已有实例可跳过）。官方镜像：
docker run -d --name seekdb-kb -p 2881:2881 -e MODE=mini \
  oceanbase/seekdb:1.4.0-100000172026082615

# 2) 启动应用
cd stepfun
npm install
SEEKDB_PORT=2881 npm start
# 打开 http://127.0.0.1:8787
```

环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `SEEKDB_HOST` / `SEEKDB_PORT` | `127.0.0.1` / `2881` | seekdb 服务地址 |
| `SEEKDB_USER` / `SEEKDB_PASSWORD` | `root` / 空 | 账号密码 |
| `SEEKDB_DATABASE` | `test` | 数据库名 |
| `PORT` | `8787` | 应用端口 |
| `MODEL_HOSTS` | `https://hf-mirror.com/,https://huggingface.co/` | 模型文件下载源 |

首次启动时模型文件由服务端代理下载并缓存到 `stepfun/models/`，之后可离线运行；
浏览器只与本机服务通信，推理全过程不出本机。

## 3. 使用

1. 点「载入内置语料（50 条）」或粘贴/选择自己的文本（每行一段，或 JSONL）。
2. 点「向量化并写入」：浏览器本地批量编码 → 写入 seekdb → 刷新索引。
3. 输入查询，点「检索」：查询向量在本地生成 → seekdb 混合检索 → 展示排序结果与原文。
4. 展开「seekdb 实际执行的语句」可看到本次检索真正发送的那一条 SQL；
   点「查看索引与执行计划」可读取集合的索引定义。

「清空知识库」会删除集合；重复导入同一份语料是幂等的（写入用 upsert）。

## 4. 复现实验数据

```bash
# 三种配置的完整对照（模型加载、向量化、写入、刷新、检索、端到端）
node scripts/evaluate.mjs --label webgpu                              # FP16 + WebGPU
node scripts/evaluate.mjs --wasm true --label wasm-int8               # INT8 + WASM
node scripts/evaluate.mjs --wasm true --dtype fp32 --label wasm-fp32  # FP32 基线

# 量化精度漂移（同一批文本在 FP32 / INT8 下的向量余弦与纯向量召回）
node scripts/compare-precision.mjs

# 执行计划：确认一条混合检索语句同时用上全文索引与向量索引
MYSQL_CLIENT=mysql python scripts/explain-plan.py
```

结果写入 `stepfun/results/*.json`，汇总见 [TECHNICAL.md](./TECHNICAL.md)。
脚本默认在常见路径下查找 Chrome，可用 `--chrome <path>` 或 `CHROME_PATH` 覆盖。

## 5. 目录

```
stepfun/
├── server/index.mjs               # 静态服务 + seekdb-js 写入与混合检索 + 模型文件代理缓存
├── web/                           # 前端：Transformers.js、WebGPU/WASM 选择、批量向量化、检索界面
├── data/corpus.jsonl              # 内置语料 50 条
├── data/queries.jsonl             # 评测查询 20 条（含标准答案）
├── scripts/evaluate.mjs           # 端到端评测（真实浏览器）
├── scripts/compare-precision.mjs  # 量化精度对比
├── scripts/explain-plan.py        # 取回混合检索语句的执行计划
├── results/                       # 评测输出
├── README.md
└── TECHNICAL.md                   # 一页技术说明
```

## 6. 已知限制

- 元数据过滤未启用：seekdb-js 1.4.0 把 `where` 渲染成裸列名（`WHERE category = ...`），
  而集合的元数据存在 JSON 列里，过滤条件匹配不到任何行，两路分支中未过滤的一路仍会返回结果。
  要支持按类别过滤，需要绕过 SDK 自己拼 `metadata->>'$.field'` 条件。
- `EXPLAIN` 通过 seekdb-js 执行时返回空，因此执行计划改由 `scripts/explain-plan.py` 用 MySQL 客户端取回。
- 评测集为 50 条语料 / 20 条查询，规模较小，指标只用于同一实现内的配置对比。
