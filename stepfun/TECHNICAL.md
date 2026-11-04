# 端侧 AI 推理 + seekdb 混合检索：技术说明

> 全部数字来自本仓库 `scripts/evaluate.mjs`、`scripts/compare-precision.mjs` 与
> `scripts/explain-plan.py` 的实测输出，原始数据见 `results/`。
>
> **测试环境**：Windows 11 / Chrome 142（无头，真实浏览器内核）/ NVIDIA RTX 4060 Laptop（WebGPU 可用）/
> Node.js 24.21.0 / seekdb `5.7.25-OceanBase seekdb-v1.4.0.0`（官方 `oceanbase/seekdb:1.4.0` 镜像，
> Docker Desktop，4 vCPU）。全文与向量索引都与应用同机，所有延迟都是本机回环。

## 1. 模型与量化

| 项 | 值 |
| --- | --- |
| 模型 | `Xenova/bge-small-zh-v1.5`（BAAI/bge-small-zh-v1.5 的 ONNX 版本，Apache-2.0） |
| 来源 | https://huggingface.co/Xenova/bge-small-zh-v1.5 （公开可获取） |
| 维度 | 512 |
| 池化 | CLS + L2 归一化 |
| 运行时 | Transformers.js 3.7.5（ONNX Runtime Web，`executionProviders` 由 `device` 决定） |

**体积（实测：浏览器实际请求的文件，含权重 + 分词器 + 配置）**

| 精度 | 权重文件 | 总体积 | 相对 FP32 |
| --- | --- | ---: | ---: |
| FP32 基线 | `onnx/model.onnx` | 95,292,085 B（90.88 MiB） | 1.00× |
| FP16 | `onnx/model_fp16.onnx` | 47,937,635 B（45.72 MiB） | **1.99×** |
| INT8 | `onnx/model_quantized.onnx` | 24,451,050 B（23.32 MiB） | **3.90×** |

分词器与配置合计 440,208 B，三种配置相同（`:716 + :367 + tokenizer.json:439,125`）。
体积按“实际加载的文件”统计：用 Chrome 请求日志确认每种配置**只**拉取自己那一份权重
（`model_fp16.onnx` / `model_quantized.onnx` / `model.onnx`），不会重复下载。

**量化对检索质量的影响（实测，`scripts/compare-precision.mjs`）**

| 指标 | FP32 | INT8 | 变化 |
| --- | ---: | ---: | ---: |
| 文档向量与 FP32 的余弦（均值 / 最小） | 1.0000 | 0.9891 / 0.9851 | −1.1% |
| 查询向量与 FP32 的余弦（均值 / 最小） | 1.0000 | 0.9910 / 0.9863 | −0.9% |
| 纯向量 Recall@3 / @5 | 0.7667 / 0.7667 | 0.7667 / 0.7667 | 0 |

结论：INT8 把体积压到 1/3.9，向量方向偏移约 1%，在本评测集上排序结果没有变化。
三种精度下的混合检索指标完全相同（见第 4 节），与这一观察一致。

## 2. 端侧运行与后端降级

- 后端选择：`navigator.gpu.requestAdapter()` 拿到适配器才启用 WebGPU，否则用 WASM。
  只检查 `navigator.gpu` 是否存在会误判，因此以适配器为准。
- 权重精度跟随后端：WebGPU → FP16，WASM → INT8；也可用 `?device=wasm&dtype=fp32` 强制任一组合。
- 主后端构建失败时自动回退到 WASM + INT8，界面徽章显示最终生效的后端与精度。
- 模型文件由本服务的 `/hf/...` 路由代理下载（规避模型源 CORS 限制），并落盘缓存到 `stepfun/models/`；
  二次启动不再走网络。浏览器仍从 CDN 加载 Transformers.js 本身。
- 页面响应头带 `Cross-Origin-Opener-Policy: same-origin` 与 `Cross-Origin-Embedder-Policy: require-corp`，
  使 WASM 后端可用多线程。

## 3. seekdb 使用

| 项 | 值 |
| --- | --- |
| 接入方式 | `seekdb-js`（npm `seekdb@1.4.0`，Server 模式，MySQL 协议） |
| 集合 | `kb_stepfun_demo`，维度 512，距离 `cosine` |
| 全文索引 | `ik` 分词器（`ik_mode=smart`），字段 `document` |
| 向量索引 | HNSW（`M=16, EF_CONSTRUCTION=200, EF_SEARCH=64`），字段 `embedding` |
| 元数据 | `metadata` JSON 列（`title`、`category`） |
| 写入 | `collection.upsert()` 分批写入（默认 16 条/批），重复导入幂等 |
| 刷新 | 写入批次结束后调用 `collection.refresh_index()` |

**混合检索在数据库内完成**：`seekdb-js` 的 `hybridSearch()` 经 `DBMS_HYBRID_SEARCH.GET_SQL`
把检索参数编译成**一条 SQL**，全文分支（`MATCH ... AGAINST` + `RANK()`）与向量分支
（`COSINE_DISTANCE ... ORDER BY ... APPROXIMATE`）`FULL JOIN`，按 RRF 打分。
客户端只发送一条查询向量，**不会**分别拿到两路结果再在应用层合并。

实际执行计划（`results/query-plan.txt`，由 `scripts/explain-plan.py` 在 seekdb 上取回）：

```
|6 |  │       └─VECTOR INDEX SCAN|c$v2$<id>(idx_vec)|1 |
|7 |  └─SUBPLAN SCAN             |_fts             |46|
|10|        └─TEXT RETRIEVAL SCAN|c$v2$<id>(idx_fts)|46|
|1 |└─HASH FULL OUTER JOIN       |                 |46|
```

即两路各自走原生索引（`idx_vec` / `idx_fts`），在库内做 JOIN 与排序，没有把整表拉到应用层。

写入可见性：实测写入完成后**不调用** `refresh_index()` 也能立刻检索到新文档，
刷新主要影响索引结构（同样的查询在刷新前后 R@5 都是 0.85）；
`refresh_index()` 本身约 200–800 ms，因此它的耗时在接口里单独上报，不混进写入延迟。

## 4. 性能与检索质量（实测）

评测集：50 条中文语料、20 条带标准答案的查询；每条查询跑 3 次取**最后一次**，各阶段取 20 条查询的中位数。

| 配置 | 模型加载 | 文档向量化（50 条） | 写入 | 索引刷新 | 查询向量化 | 混合检索 | 端到端 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| FP16 + WebGPU | 7.46 s | 664 ms（75 条/秒） | 164 ms | 505 ms | 6.1 ms | **5.2 ms** | **14.0 ms** |
| INT8 + WASM | 14.40 s | 582 ms（86 条/秒） | 195 ms | 502 ms | 4.8 ms | 5.6 ms | **13.1 ms** |
| FP32 + WASM（基线） | 43.31 s | 778 ms（64 条/秒） | 198 ms | 802 ms | 5.7 ms | 5.6 ms | **13.5 ms** |

检索质量（同一语料、同一查询向量化模型、同一混合检索语句）：

| 配置 | Recall@5 | nDCG@5 | MRR@5 |
| --- | ---: | ---: | ---: |
| FP16 + WebGPU | 0.850 | 0.8625 | 0.950 |
| INT8 + WASM | 0.850 | 0.8625 | 0.950 |
| FP32 + WASM | 0.850 | 0.8625 | 0.950 |

**怎么读这些数字（重要）**

- 50 条文档的语料太小，向量化和检索都在毫秒级，**端到端差异主要来自“查询向量化”这一段**，
  而这一段在三种配置间只差一两毫秒（WebGPU 6.1 ms / INT8-WASM 4.8 ms / FP32-WASM 5.7 ms）。
  因此本表**不能**证明 WebGPU 比 WASM 更快；要看出后端的吞吐差异，需要几百条以上的文档并放大 batch。
- 模型加载与“首次查询”明显高于后续查询（FP32 要下载 90 MB 权重，43 s），
  首次与预热后的数据必须分开报告。
- 混合检索稳定在 5 ms 左右，且与模型精度无关——这一段的开销在 seekdb 侧。

**检索质量的短板**（逐查询记录见 `results/evaluation-*.json`）：
20 条查询里 15 条 R@5 = 1.0；4 条为 0.5（第一个标准答案排第 1，第二个排在 5 名之外）；
1 条（“检索结果太少应该调什么”）两个答案都没进前 5，是唯一的 0 分项。

## 5. 复现

```bash
npm install
SEEKDB_PORT=2881 npm start
node scripts/evaluate.mjs --label webgpu
node scripts/evaluate.mjs --wasm true --label wasm-int8
node scripts/evaluate.mjs --wasm true --dtype fp32 --label wasm-fp32
node scripts/compare-precision.mjs
python scripts/explain-plan.py      # 需 mysql 客户端
```
