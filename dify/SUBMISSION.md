# 提交信息

| 项 | 值 |
| --- | --- |
| 作品 | 离谱侦探社（Dify × seekdb × PowerContext） |
| 比赛仓库 | `oceanbase/powercontext` |
| 目标基线 | 分支 `2026-competition-ecosystem`，commit `d647275c308e09b7b623f322a26f5c4e85734fb0` |
| 资产目录 | `dify/`（本目录） |
| 唯一 DSL | `dsl/casefile-detective.yml`（Dify 1.10.1，DSL `0.5.0`，Chatflow，18 节点） |
| 运行说明 | `README.md` |
| 验证样例 | `samples/SCENARIO.md`，一键脚本 `scripts/verify_flow.py` |
| 实跑证据 | `evidence/`，索引见 `ACCEPTANCE.md` |
| 需要打补丁的组件 | Dify 1.10.1 的 OceanBase 向量适配器（`patches/`，仅 seekdb 兼容性修复） |

## 提交到测评平台

平台要求填 **Commit id** 与 **Branch**，仓库是报名时登记的团队仓库（`yv698761-lgtm/powercontext`）。
作品必须落在该仓库 `2026-competition-ecosystem` 分支（或其提交谱系）下的 `dify/` 目录。

### 方式一：直接推到团队仓库

```bash
git clone https://github.com/yv698761-lgtm/powercontext.git
cd powercontext
git fetch https://github.com/oceanbase/powercontext.git 2026-competition-ecosystem
git checkout -b dify/casefile-detective FETCH_HEAD
# 把本目录（dify/）覆盖进来，然后：
git add dify
git commit -m "feat(dify): 离谱侦探社 —— Dify 编排 + seekdb 知识库 + PowerContext 跨会话记忆"
git push -u origin dify/casefile-detective
```

平台填：

- **Branch**：`dify/casefile-detective`
- **Commit id**：仓库里执行 `git rev-parse HEAD` 得到的 40 位哈希（本作品交付时的提交见平台填报值；提交后哈希会变，不要照抄文档）

若平台只接受 `2026-competition-ecosystem` 分支本身，则把上面的分支直接推成该分支：

```bash
git push origin dify/casefile-detective:2026-competition-ecosystem
```

### 方式二：离线 bundle

`dify.casefile-detective.bundle` 是包含全部提交的 Git bundle：

```bash
git clone https://github.com/yv698761-lgtm/powercontext.git pctx
cd pctx
git fetch /path/to/dify.casefile-detective.bundle dify/casefile-detective:dify/casefile-detective
git checkout dify/casefile-detective
git push -u origin dify/casefile-detective
```

## 材料清单（对应四类提交产物）

| 题目要求 | 本作品对应文件 |
| --- | --- |
| Dify DSL | `dsl/casefile-detective.yml`（单文件即可导入；无需其它 DSL 的顺序依赖） |
| 运行说明 | `README.md`（创意/玩法、技术分工、环境与版本、从零启动、网络条件、已知限制） |
| 验证样例与证据 | `samples/SCENARIO.md`、`scripts/verify_flow.py`、`evidence/*.json`、`ACCEPTANCE.md` |
| 使用 seekdb 的补充材料 | seekdb 版本与部署方式（`scripts/deploy_dify.py`、`deploy/docker-compose.seekdb.yaml`）、Dify 适配补丁与源码（`patches/`）、实际读写验证（`scripts/seekdb_probe.py` + `evidence/seekdb-probe.json`） |
| 预置记忆/互动状态 | 首次开场自动创建 PowerContext Scope；`scripts/recap_player.py` 可查看任意玩家档案；恢复方式见 `samples/SCENARIO.md` 第 6 节 |
| 现成插件 | Dify 官方插件 `langgenius/deepseek` 0.0.26（对话）、`langgenius/openai` 1.1.0（Embedding 端点），均从 Dify Marketplace 安装 |
| 自定义后端 | `app/`（FastAPI），依赖见 `requirements.txt`，启动见 `scripts/run_services.py` |
| 数据与初始化 | 12 份案卷在 `app/data/cases.py`，`scripts/init_dify.py` / `scripts/reindex_corpus.py` 负责入库与索引 |

## 凭据说明

仓库里**没有任何真实凭据**：`.env`、`runtime/`、`models/` 都被 `.gitignore` 排除；
`.env.example` 里全部是占位值。运行前必须自己填写：

- `DIFY_ADMIN_PASSWORD`、`INIT_PASSWORD`（Dify 控制台）
- `DEEPSEEK_API_KEY`（或你自己的 OpenAI 兼容模型 key）
- `DETECTIVE_API_KEY`、`DETECTIVE_SIGNING_KEY`（后端，>=16 位随机串，且与 DSL 环境变量一致）
- `OCEANBASE_VECTOR_PASSWORD`（seekdb root 口令）

## 已知的验证边界

- 本作品只声明对 **Dify 1.10.1** 的兼容；其它版本的 DSL 字段与 OceanBase 适配器实现可能不同。
- 实跑环境是 Windows 11 + Docker Desktop；Linux/macOS 上的步骤一致，只是脚本路径写法不同。
- `verify_flow.py` 的 9 条断言里，涉及自然语言的部分（例如"档案员复述上一轮"）用关键词判断，
  模型更换后文案会变，但判定与记忆行为不变。
