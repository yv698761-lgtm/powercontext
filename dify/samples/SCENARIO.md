# 离谱侦探社 · 验证样例（评审按此逐步体验）

> 一句话：三位性格不同的 AI 侦探会对同一份案卷各说各话，你能不能盖章，只看案卷 canon 里的那一条决定性证据。
> 跨会话之后，档案员老砚还记得你上回栽在哪里。

## 0. 前置

按 `README.md` 完成部署后，确认三件事：

| 检查 | 命令 | 期望 |
| --- | --- | --- |
| 侦探社后端 | `curl http://127.0.0.1:8012/health` | `cases: 12, chunks: 153, powercontext.ok: true` |
| 本地 Embedding | `curl http://127.0.0.1:8013/health` | `dimension: 512` |
| PowerContext | `curl http://127.0.0.1:8011/v1/server-info` | `package_version: 1.5.0` |
| Dify 控制台 | 浏览器打开 `http://127.0.0.1:18080` | 能登录，应用列表里有「离谱侦探社」 |

体验入口：`http://127.0.0.1:18080/chat/<app-api-key>`，key 在 `runtime/dify-access.json` 的 `app_api_key`。

## 1. 样例 A：谣言结案必然被驳回（证明判定不靠模型）

1. 新开一个会话，发送：`开场`
   - 期望：回复给出案卷标题、现场与线索，末尾提示"直接写下你的指控"。
2. 打开同一份案卷，故意用坊间说法结案，例如案卷是「紫藤巷的铜铃案」时发送：
   `真凶是野猫。谣传一：据说铜铃是野猫踩翻的，守夜更夫私下也这么讲。`
   - 期望：`### 勘结：❌ 被驳回`，并显示
     - **案卷里的决定性证据**（撬痕朝向院内…）
     - **可以成立的勘结要点**（官府勘结原文）
     - **你这次踩中的谣言**：野猫
   - 判定依据不是模型打分，而是 `app/caseflow.py::judge()` 对 canon 断言与谣言关键词的确定性检查。

## 2. 样例 B：换一个新会话，档案员仍然记得你（跨会话记忆）

1. 在**同一个浏览器用户**下，点「新对话」（或直接刷新后新建会话）。
2. 发送：`开场`
   - 期望：开头一句是老砚的复述，形如
     > 又是你。上回你想拿「野猫。谣传一：…」结案，被驳回了——紫藤巷的铜铃案的批注还在呢。这次先看清楚再开口。
3. 发送：`战绩`
   - 期望：显示等级、办过的案卷、以及一行 `记忆凭证：PowerContext art_…`。

判定要点：新会话的 `conversation_id` 与上一轮不同，但 `user` 不变；记忆来自 PowerContext，不来自 Dify 的会话历史。
可用下面的命令直接核对（`art_…` 应等于页面上显示的凭证）：

```bash
curl "http://127.0.0.1:8012/session/recap?player=dify%3A<app_id>%3A<user>" \
  -H "Authorization: Bearer $DETECTIVE_API_KEY"
```

## 3. 样例 C：决定性证据结案成立（证明知识与检索真的参与了）

用同一份案卷，发送一条包含决定性证据的指控，例如：

- 紫藤巷的铜铃案：`真凶是姚娘子，铜铃是被撬棍从门轴上顶落的，撬痕朝院内，说明人是从院里出去的。`
- 渡口的空船：`关键证据是渡绳被人割断后留下的整齐绳头，船底的磕损朝船尾，船主鞋底还沾着渡口的黄泥。`

期望：

- `### 勘结：✅ 成立`
- **命中的已勘结事实**：至少一条
- **踩中的谣言**：无
- 页脚 `📚 知识库检索：本轮回捞 N 条案卷片段（seekdb 向量 + 全文加权融合）`，N ≥ 1

## 4. 样例 D：身份隔离

换一个浏览器（或用 API 换 `user`）发送 `战绩`：等级应为「实习记录员 / 已办 0 份」，且看不到上一个玩家的案卷。

## 5. 一键脚本（等价于上面 A–D）

```bash
python scripts/verify_flow.py --env-file .env
python scripts/show_checks.py
python scripts/seekdb_probe.py --password "$OCEANBASE_VECTOR_PASSWORD"
python scripts/capture_evidence.py
```

`verify_flow.py` 会新建两个随机玩家，跑 5 轮对话，逐条断言并写入 `evidence/verify-flow.json`；
`seekdb_probe.py` 直接连 seekdb 打印向量表行数、`VECTOR KEY`/`FULLTEXT KEY` 与两类检索的真实命中；
`capture_evidence.py` 汇总环境、应用节点、记忆档案。

## 6. 恢复初始状态

- 只重置某个玩家：换一个浏览器（新 `user`）即可，或在 Dify 里新开一个会话并用新的 API `user`。
- 清空全部记忆：停 PowerContext，删除 `POWERCONTEXT_HOME` 目录后重启（见 README）。
- 重建知识库：`python scripts/reindex_corpus.py`（删掉 12 份文档后重新上传并等待索引完成）。
