"""离谱侦探社 · 应用包。

Dify 负责编排（输入解析、记忆读取、知识库检索、三位侦探发言、判定、分支回答），
本包只提供 Dify 无法可靠完成的确定性部分：案卷语料切分、规则判定、
PowerContext 记忆读写，以及可选的本地 Embedding 服务。
"""

__all__ = ["caseflow", "main", "powercontext", "rules"]
__version__ = "1.0.0"
