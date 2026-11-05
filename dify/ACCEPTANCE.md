# 验收记录（实跑）

本文件记录本作品在本机真实跑出来的结果，所有数字都能在 `evidence/` 里核对。
没有使用假的 Dify、假的 seekdb 或假的模型响应；模型是真实的 DeepSeek（`deepseek-flash`）。

- 环境：Windows 11 + Docker Desktop（Linux 容器），Python 3.12.16
- Dify 1.10.1（官方 Docker Compose + 本仓库的 seekdb 适配镜像）
- seekdb 1.4.0（`oceanbase/seekdb:1.4.0-100000172026082615`），作为 Dify 知识库的向量库
- PowerContext 1.5.0（HTTP Memory API），作为跨会话记忆
- 对话模型：DeepSeek `deepseek-flash`（Dify 插件 `langgenius/deepseek` 0.0.26）
- Embedding：本地 ONNX `bge-small-zh-v1.5`（512 维），Dify 里注册为 `text-embedding-3-small` 端点

## 1. 结论速览

| 验收项 | 结果 | 证据 |
| --- | --- | --- |
| DSL 可导入并发布 | ✅ 18 节点 / 19 连线，`completed` | `evidence/dify-app.json` |
| 应用能接收输入并返回结果 | ✅ 5 轮对话全部 200 | `evidence/verify-flow.json` |
| 完整玩法（开场 → 指控 → 判定 → 分支回答） | ✅ 9/9 断言通过 | `evidence/verify-flow.json` |
| 谣言结案被驳回（判定不靠模型） | ✅ | `verify-flow.json` `accuse-rumour` |
| 决定性证据结案成立 | ✅ | `verify-flow.json` `accuse-decisive` |
| 跨会话记忆（新 conversation 仍记得上一轮） | ✅ 新会话复述了上一轮的失败与案卷 | `verify-flow.json` `open-new-conversation` |
| 玩家隔离（另一个 user 看不到他人档案） | ✅ 等级实习、已办 0 份 | `verify-flow.json` `other-player-rank` |
| seekdb 真的存了向量并参与检索 | ✅ 108 行、`VECTOR KEY` + `FULLTEXT KEY`、两类检索均有命中 | `evidence/seekdb-probe.json` |
| PowerContext 真的存了记忆并可复用 | ✅ `seq=2`，两轮记录、信任度、黑历史 | `evidence/powercontext-recap.json` |

## 2. 逐项证据

### 2.1 Dify 编排

`evidence/dify-app.json` 里的 18 个节点：

```
start → ① 解析意图(code) → ② 读记忆并开场(http) → ③ 拆开场回执(code) → ④ 这次是不是指控(if-else)
        ├─ false → ④A 组装信息回复(code) → ④B 信息回复(answer)
        └─ true  → ⑤ 检索案卷库(knowledge-retrieval, seekdb)
                   → ⑥ 整理检索片段(code)
                   → ⑦ 林默 / 阿鬼 / 妙音（三个并发 LLM）
                   → ⑦' 清洗台词(code)
                   → ⑧ 判定并写回记忆(http)
                   → ⑨ 拆判定回执(code)
                   → ⑩ 勘结是否成立(if-else)
                        ├─ true  → ⑪ 勘结成立(answer)
                        └─ false → ⑫ 勘结被驳回(answer)
```

- 三位侦探是三个独立 LLM 节点，由 Dify 图引擎并发执行（回答里三段台词相互独立）；
- 判定节点等三条入边都完成后才执行；
- 只有"指控"消耗回合，开场/提示/战绩走信息链。

### 2.2 端到端断言（`evidence/verify-flow.json`）

本轮玩家：`dify:8b1e7247-d601-43ac-a7df-1599d6416330:verify-a-885483`，另一位：`verify-b-885483`。

| # | 断言 | 结果 |
| --- | --- | --- |
| 1 | 开场返回案卷 | ✅ 会走路的铜锁 |
| 2 | 开场包含案卷标题 | ✅ |
| 3 | 开场带出档案员的记忆台词 | ✅ 「新面孔。规矩只有一条：…」 |
| 4 | 谣言指控被驳回 | ✅ 踩中的谣言：野猫 |
| 5 | 新会话仍然是同一份档案 | ✅ 「又是你。上回你想拿…结案，被驳回了」 |
| 6 | 新会话 conversation_id 不同 | ✅ `4c58b5bd…` vs `4afc723e…` |
| 7 | 决定性证据使勘结成立 | ✅ 勘结：✅ 成立 |
| 8 | 新用户档案为空 | ✅ 实习记录员 / 已办 0 份 |
| 9 | 新用户看不到他人案卷 | ✅ |

其中第 5 条是本作品的技术重点：Dify 的两轮对话 `conversation_id` 不同，模型仍然说出了上一轮的具体内容，
说明上下文来自 PowerContext 而不是 Dify 的会话历史。

### 2.3 seekdb（`evidence/seekdb-probe.json`）

- 表：`vector_index_d432ad42_3851_495d_ac44_8daa4b145cd7_node`，**108 行**（12 份案卷分片后由 Dify 写入）
- 索引：`VECTOR KEY vector_index (vector) WITH (DISTANCE=L2, TYPE=HNSW)`、`FULLTEXT KEY fulltext_index_for_col_text (text)`，分词器 `ik`
- 向量检索：`ORDER BY l2_distance(vector, %s) APPROXIMATE LIMIT 3` 命中 3 条（含「关键证据】撬痕朝向院内…」）
- 全文检索：`MATCH(text) AGAINST('铜铃 撬痕 朝院内')` 命中 3 条，score 6.51 / 6.15 / 3.98

也就是说：Dify 的 knowledge-retrieval 节点检索时，向量与全文两条路都落在 seekdb 这张表上。

### 2.4 PowerContext（`evidence/powercontext-recap.json`）

```json
{
  "player": "dify:8b1e7247-…:verify-a-885483",
  "seq": 2,
  "artifact_id": "art_a3a5fd10ee8e4696afaebca4f50dac62",
  "rank": {"title": "见习侦探", "passes": 1, "rounds": 2},
  "trust": {"林默": 0, "阿鬼": 1, "妙音": 0},
  "rounds": [["会走路的铜锁", false], ["紫藤巷的铜铃案", true]]
}
```

- 每条记忆是 PowerContext 里一个真实的 Atomic Memory（`art_…`），kind = `detective-casefile-v1`；
- 应用重启后 `seq` 与黑历史仍在（桥接服务本身不落任何本地状态）；
- 「侦探信任度」由历史轮次里各侦探是否给出决定性依据统计得到，会随玩局演进。

### 2.5 实际运行的 DSL 副本

`evidence/casefile-detective.rendered.yml` 是本次实跑导入 Dify 的那一份（含本机知识库 ID
`d432ad42-3851-495d-ac44-8daa4b145cd7` 与环境变量已脱敏）。仓库里的 `dsl/casefile-detective.yml`
是同一个生成器的产物，只是把知识库 ID 与环境变量换成占位值——两者结构完全一致，
差别只在 `dataset_ids` 与 `DETECTIVE_API_KEY` 的取值。

## 3. 复跑方法

```bash
python scripts/verify_flow.py --env-file .env      # 重新跑 5 轮、9 条断言（每次用新玩家）
python scripts/show_checks.py                      # 打印断言
python scripts/seekdb_probe.py --password "$OCEANBASE_VECTOR_PASSWORD" --query "铜铃 撬痕 朝院内"
python scripts/recap_player.py <user_id>           # 取某位玩家的跨会话档案
python scripts/capture_evidence.py                 # 汇总环境/应用/检索/记忆证据
```

## 4. 过程中修掉的两个真实问题（记录备查）

1. **Dify 1.10.1 的 OceanBase 适配器与 seekdb 不兼容**：`int(row[6])` 在 seekdb 上读到 `scope='CLUSTER'`，
   建表即失败。补丁见 `patches/oceanbase_vector.py`，用官方镜像二次构建（`patches/Dockerfile.api-seekdb`）。
   没有这个补丁，知识库根本无法写入 seekdb。
2. **模型把 `<think>` 推理片段带进台词**：`deepseek-flash` 会在正文里输出思维链。
   流水线加了 `⑦' 清洗台词` 节点（正则剥离 `<think>`/注释并裁剪长度），避免把内部推理展示给玩家。

另外，判定逻辑最初用子串匹配识别"踩中谣言"，会把玩家写的"不是被木头撞走"误判成踩中谣言；
现在改为**否定词感知匹配**（`app/caseflow.py::_asserted`），否定的说法不再算命中。这也是端到端断言
从 8/9 变成 9/9 的原因。
