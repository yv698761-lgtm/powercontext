# 离谱侦探社 🕵️ — Dify × seekdb × PowerContext

**三位 AI 侦探会对同一份案卷各说各话，你只能靠案卷里的证据盖章；换个会话回来，档案员还记得你上回栽在哪。**

Dify 负责整条流程编排（意图分支、记忆读取、知识库检索、三位侦探并发发言、判定、分支回答）；
seekdb 是 Dify 知识库的向量 + 全文存储；PowerContext 保存每位玩家的跨会话记忆。
AI 侦探可以说错话（这是玩法），但"勘结是否成立"由确定性规则 + 案卷 canon 决定，模型说了不算。

- 题目：Just for Fun（Dify × OceanBase）
- 基线：`oceanbase/powercontext` 的 `2026-competition-ecosystem` 分支，资产目录 `dify/`
- DSL：`dsl/casefile-detective.yml`（18 节点 / 19 条连线的 Chatflow）

---

## 1. 玩法

1. 输入 **开场**：档案员老砚抽一份案卷（12 份原创卷宗），给出「现场 / 线索」摘要，并从知识库里回捞片段。
2. 三位侦探各说一句：

   | 侦探 | 人设 | 常见错误 |
   | --- | --- | --- |
   | 林默 | 物证派，只认痕迹 | 忽略动机 |
   | 阿鬼 | 人心派，先问谁得利 | 容易被"谁有好处"带偏 |
   | 妙音 | 风闻派，满嘴流言 | 会把坊间谣传当成线索 |

3. 你写下 **指控**（点名凶手 + 给出决定性那处证据）。
4. 判定：`✅ 勘结成立` 或 `❌ 勘结被驳回`（并给出案卷里的决定性证据、可成立的要点、你踩中的谣言）。
5. **提示** 让老砚给一条线索；**战绩** 看档案（等级、办过的案卷、侦探信任度、PowerContext 记忆凭证）。
6. 换新会话、关掉页面再回来，档案员仍然记得你的上一轮——因为记忆不在 Dify 会话里，而在 PowerContext 里。

案卷语料是**真假混杂**的：每份卷宗既有官府勘结的真相与决定性证据，也有三条坊间流言。
检索很容易，验证很难——这就是这个游戏的"离谱"之处，也是它需要跨会话记忆与确定性判定的原因。

## 2. 技术分工

| 组件 | 版本 | 在本作品里的实际作用 |
| --- | --- | --- |
| **Dify** | 1.10.1（官方 Docker Compose，DSL `0.5.0`） | Chatflow 编排：意图解析 → 读记忆（HTTP）→ 知识库检索（knowledge-retrieval）→ 三位侦探并发 LLM → 判定（HTTP）→ 条件分支 → 两个 answer 分支；对话模型与 Embedding 都在 Dify 里配置 |
| **seekdb** | 1.4.0（`oceanbase/seekdb:1.4.0-100000172026082615`） | Dify 的**向量库**：知识库 12 份案卷 → 108 条分片写入 seekdb，同一张表上建 `VECTOR KEY`（HNSW, L2）与 `FULLTEXT KEY`（ik 分词）；Dify 的混合检索（向量 0.7 / 关键词 0.3）就是在它上面跑的 |
| **PowerContext** | 1.5.0（`powercontext[cli,server]`，HTTP Memory API） | **跨会话记忆**：每个玩家一个 Scope，保存 `seq / rounds / active_case / seen_cases / trust / grudge`；每轮开场先读、判定后写。台账外的进程重启后仍能续案 |
| 侦探社后端 | FastAPI（`app/`） | 只做模型不该做的事：选案卷、给检索兜底、确定性判定、读写记忆、签回合票据；不落任何本地状态 |
| 本地 Embedding | onnxruntime + `bge-small-zh-v1.5`（512 维） | 评审环境没有商用 Embedding 时，用本地 ONNX 模型提供 OpenAI 兼容的 `/v1/embeddings` |
| 对话模型 | DeepSeek（Dify 插件 `langgenius/deepseek` 0.0.26，模型 `deepseek-flash`） | 三位侦探的台词；换成任意 OpenAI 兼容模型都可以，DSL 里改 `provider/model` 即可 |

> seekdb 与 PowerContext 的分工是"存储 vs 记忆"：seekdb 存**案卷语料与索引**（知识库检索），
> PowerContext 存**玩家状态与历史**（跨会话记忆）。两者不重复。

## 3. 目录

```
dify/
├── README.md                      # 本文件：运行说明
├── ACCEPTANCE.md                  # 验收记录与证据索引（实跑结果）
├── SUBMISSION.md                  # 提交信息（分支 / commit / 推送命令）
├── .env.example                   # 配置模板（全部是占位值）
├── requirements.txt               # 后端与 Embedding 服务依赖
├── dsl/casefile-detective.yml     # 唯一 DSL（可直接导入）
├── app/                           # 侦探社后端 + 本地 Embedding 服务 + 案卷数据
├── scripts/                       # 生成 DSL、部署、初始化、校验、取证
├── patches/                       # Dify 1.10.1 的 seekdb 适配补丁与 Dockerfile
├── deploy/                        # compose 覆盖文件、squid 允许清单、PowerContext 配置
├── samples/SCENARIO.md            # 评审按步骤体验的样例
└── evidence/                      # 实跑证据（JSON）
```

## 4. 运行环境

| 项 | 要求 |
| --- | --- |
| 操作系统 | Windows 11 / Linux / macOS（本作品在 Windows 11 + Docker Desktop 上实测） |
| Docker | Docker Desktop 4.x（Linux 容器），建议 ≥ 8 GB 内存 |
| Python | 3.11–3.12 |
| 磁盘 | 约 12 GB（Dify 镜像 ~6 GB + seekdb 数据 + 模型） |
| 端口 | 18080（Dify）、2886（seekdb）、8011（PowerContext）、8012（后端）、8013（Embedding） |
| 网络 | 首次需能拉取 Docker 镜像、PyPI 包、以及模型文件（约 100 MB，可换镜像源） |

已实测版本：Dify 1.10.1、seekdb 1.4.0、PowerContext 1.5.0、Python 3.12.16、onnxruntime 1.31.1、tokenizers 0.23.3。

## 5. 从零跑起来

### 5.1 取代码与配置

```bash
cd dify
cp .env.example .env          # 然后按注释填写
```

`.env` 里必须自己填的四项：

| 变量 | 说明 |
| --- | --- |
| `DIFY_ADMIN_PASSWORD` | Dify 控制台管理员口令（自己定） |
| `DEEPSEEK_API_KEY` | 对话模型 key；用别家就同时改 `LLM_PROVIDER` / `LLM_MODEL` |
| `DETECTIVE_API_KEY` / `DETECTIVE_SIGNING_KEY` | 后端接口密钥与票据签名密钥，各 16 位以上，随机生成即可 |
| `OCEANBASE_VECTOR_PASSWORD` | seekdb 的 root 口令（自己定，容器初始化时使用） |

Dify 容器自己的初始密码：`INIT_PASSWORD`（自托管 Dify 1.6+ 需要它才能创建管理员，写在同一个 `.env` 里）。

### 5.2 准备 Dify 源码（用于 compose 文件）

```bash
git clone --depth 1 --branch 1.10.1 https://github.com/langgenius/dify.git ../dify-1.10.1
```

### 5.3 部署 Dify + seekdb

```bash
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python scripts/deploy_dify.py --env-file .env --dify-src ../dify-1.10.1
```

这条命令会：

1. 用 `patches/` 里的补丁构建 `langgenius/dify-api:1.10.1-seekdb`（**只需几秒**，不重装依赖）；
2. 起一个 seekdb 容器（数据落 `runtime/seekdb`）；
3. 用 `deploy/docker-compose.seekdb.yaml` 覆盖 api/worker 镜像后启动整套 Dify；
4. 等控制台就绪。

**为什么要打补丁**：Dify 1.10.1 的 OceanBase 向量适配器用 `int(row[6])` 读取
`ob_vector_memory_limit_percentage`，而 seekdb 1.4.0 的 `SHOW PARAMETERS` 列顺序不同（第 7 列是
`scope='CLUSTER'`），建表阶段就会抛 `invalid literal for int() with base 10: 'CLUSTER'`。
`patches/oceanbase_vector.py` 把这一处改成按列名取值、取不到再按位置兜底，其余逻辑与官方一致。
补丁文件、Dockerfile 与所基于的官方镜像 tag 都在仓库里，可直接核对。

把宿主机服务接进 Dify 的 SSRF 代理白名单（squid 默认只允许 localhost）：

```bash
docker cp deploy/ssrf-allow.conf dify-seekdb-kb-ssrf_proxy-1:/etc/squid/conf.d/zz-allow-local.conf
docker restart dify-seekdb-kb-ssrf_proxy-1
```

### 5.4 准备 Embedding 模型与三个本地服务

```bash
python scripts/download_embedding_model.py          # 约 100 MB，落到 models/
# 终端 1：PowerContext（先按 5.5 装好）
powercontext server run --env-file deploy/powercontext.env
# 终端 2、3：Embedding 与侦探社后端
python scripts/run_services.py --env-file .env --only embed
python scripts/run_services.py --env-file .env --only bridge
```

浏览器打开 `http://127.0.0.1:18080` → 能登录即说明 Dify 就绪。

### 5.5 PowerContext（跨会话记忆）

```bash
uv tool install --force "powercontext[cli,server]==1.5.0"
```

`deploy/powercontext.env` 用它的默认本地存储（SQLite），记忆目录由 `POWERCONTEXT_HOME` 指定：

```dotenv
POWERCONTEXT_SERVER_HTTP_HOST=127.0.0.1
POWERCONTEXT_SERVER_HTTP_PORT=8011
POWERCONTEXT_SERVER_DASHBOARD_ENABLED=true
POWERCONTEXT_SERVER_ACCESS_MODE=disabled
POWERCONTEXT_HOME=<仓库内 runtime/powercontext 或你自己的目录>
```

> 为什么记忆不用 seekdb：PowerContext 1.5.0 的 OceanBase 维护流程要求"MySQL 用户租户"，
> 单机 seekdb 只有 sys 租户；它的 embedded seekdb 后端也不支持 Windows。
> 因此本作品让 seekdb 专职承担知识库存储与检索，记忆交给 PowerContext 自己的存储——
> 两者职责清晰，也符合"同时使用两者时说明各自分工"的要求。

### 5.6 初始化 Dify：装插件、配模型、建知识库、导入并发布 DSL

```bash
python scripts/init_dify.py --env-file .env
```

它按顺序完成（幂等，可重复执行）：

1. 自托管初始化校验 + 创建管理员（用 `INIT_PASSWORD`）；
2. 安装模型插件 `langgenius/deepseek`（对话）与 `langgenius/openai`（Embedding）；
3. 写入两家模型凭据（DeepSeek 官方 key；OpenAI 兼容端点指向本地 8013）；
4. 建知识库「离谱侦探社·案卷库」，嵌入模型 `text-embedding-3-small`（本地服务实际返回 512 维 bge 向量），
   检索方式 hybrid_search、向量 0.7 / 关键词 0.3；
5. 上传 12 份案卷并等待索引完成（seekdb 里建表 + 建向量索引 + 全文索引）；
6. 用当前知识库 ID 渲染 DSL（`scripts/build_dsl.py`），导入、发布，并创建一个应用 API Key；
7. 把 `app_id`、`app_api_key`、`dataset_id` 写到 `runtime/dify-access.json`（已在 `.gitignore` 中）。

体验地址会打印在输出里：`http://127.0.0.1:18080/chat/<app-api-key>`。

> **只想手工导入 DSL？** 也可以：控制台 → 创建空白应用（Chatflow）→ 导入 `dsl/casefile-detective.yml`，
> 再把环境变量 `DETECTIVE_API_KEY` 改成你的后端密钥、把知识库检索节点的数据集换成你自己的知识库，保存并发布。
> 注意 DSL 里写死的是渲染时的 dataset id，换环境请用 `scripts/build_dsl.py --dataset-id <你的知识库 ID>` 重新生成。

### 5.7 验证

```bash
python scripts/verify_flow.py --env-file .env     # 端到端 9 项断言
python scripts/show_checks.py                     # 打印断言结果
python scripts/seekdb_probe.py --password "$OCEANBASE_VECTOR_PASSWORD"   # seekdb 侧证据
python scripts/capture_evidence.py                # 汇总证据到 evidence/
```

详细步骤与手工体验样例见 [`samples/SCENARIO.md`](samples/SCENARIO.md)。

## 6. DSL 结构（18 节点）

```
玩家发言 → ① 解析意图 → ② 读记忆并开场(HTTP) → ③ 拆开场回执 → ④ 这次是不是指控
                                   ├─ 否 → ④A 组装信息回复 → ④B 信息回复
                                   └─ 是 → ⑤ 检索案卷库(seekdb, hybrid)
                                            → ⑥ 整理检索片段
                                            → ⑦ 林默 / 阿鬼 / 妙音（三个并发 LLM 节点）
                                            → ⑦' 清洗台词（去掉 <think>）
                                            → ⑧ 判定并写回记忆(HTTP)
                                            → ⑨ 拆判定回执
                                            → ⑩ 勘结是否成立
                                                 ├─ 成立 → ⑪ 勘结成立
                                                 └─ 驳回 → ⑫ 勘结被驳回
```

- 只有"指控"才消耗回合；开场/提示/战绩走信息链（`④` 分支）。
- 三位侦探是三个独立 LLM 节点，图引擎并发执行；判定节点等三条入边都完成才跑。
- 判定与记忆写入都在一次 HTTP 调用里完成，写入失败会显式报错，不会假装已归档。

## 7. 后端接口（Dify 只调用其中两个）

| 接口 | 作用 |
| --- | --- |
| `POST /session/open` | 读记忆、选案卷（未结案则保持同一份）、给检索兜底片段、签回合票据 |
| `POST /session/judge` | 确定性判定 + 写回记忆，返回判定、决定性证据、记忆凭证 |
| `GET /session/recap` | 读某位玩家的记忆（样例与评审核对用） |
| `GET /health` | 服务与 PowerContext 健康状态 |
| `GET /catalog` | 12 份案卷与三位侦探的设定 |

## 8. 已知限制

- **网络**：本机到 Docker Hub 极不稳定，镜像是通过镜像站 + 断点分段取回后 `docker load` 的；
  评审环境若能直连 Docker Hub，直接 `docker pull` 官方 tag 即可，无需本仓库的任何打包脚本。
- **Dify 版本**：只在 1.10.1 上验证。DSL `0.5.0` 与本作品的 seekdb 补丁都针对这个版本；
  换版本需重新确认 `SHOW PARAMETERS` 的列位置与 DSL 字段。
- **模型**：三位侦探的台词依赖对话模型，接入不同模型时风格会变，但**判定结果不受影响**（规则判定）。
  真实运行时模型可能带出 `<think>` 推理片段，流水线里有一步清洗节点专门处理。
- **检索**：Dify 的 knowledge-retrieval 在多数据集场景可能回退到默认检索；本作品只有一个知识库，
  命中即为 seekdb 的向量 + 全文混合结果。若想只验检索链路，用 `seekdb_probe.py` 直接查库更直观。
- **记忆规模**：每个玩家一个 Scope、每轮写一条快照（保留最近 6 轮细节）。长期使用需要清理策略。
- **单实例**：后端为单进程演示服务，用进程内锁串行化同一玩家的回合；多实例部署需要外部锁。

## 9. 参考与致谢

- [Dify 1.10.1 源码与许可证](https://github.com/langgenius/dify/tree/1.10.1)
- [Dify HTTP Request 节点文档](https://docs.dify.ai/en/use-dify/nodes/http-request)
- [seekdb 与 Dify 集成文档](https://docs.seekdb.ai/seekdb/zh-CN/dify)
- [PowerContext 文档（含 HTTP API）](https://powercontext.oceanbase.io/en/docs/)
- 案例与文案为本作品原创；`bge-small-zh-v1.5` 模型来自 BAAI（MIT），经 Xenova 转为 ONNX。
