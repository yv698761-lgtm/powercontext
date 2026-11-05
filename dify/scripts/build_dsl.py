"""生成《离谱侦探社》的 Dify DSL。

DSL 里与环境相关的只有：知识库数据集 ID、后端地址、模型供应商与模型名、
后端 API Key 的占位值。这些都由脚本注入，换环境时重新生成一次即可，
不需要手改 YAML：

    python scripts/build_dsl.py --dataset-id <dataset_uuid> \
        --llm-provider langgenius/openai_api_compatible/openai_api_compatible \
        --llm-model deepseek-chat \
        --bridge-url http://host.docker.internal:8012

产物：dsl/casefile-detective.yml
"""

from __future__ import annotations

import argparse
import io
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "dsl" / "casefile-detective.yml"

DETECTIVES = [
    {
        "id": "linmo",
        "name": "林默",
        "persona": "物证派侦探，只认物证，讨厌没有痕迹支撑的说法，说话短、硬，像在念笔录。",
        "style": "不超过 70 字，先判断手里有没有物证，再指出一处可疑的痕迹细节。",
        "y": 300,
    },
    {
        "id": "agui",
        "name": "阿鬼",
        "persona": "人心派侦探，偏爱动机与关系网，喜欢问「谁得利」，容易被利益关系带偏。",
        "style": "不超过 70 字，从动机和利害关系切入，可以点名嫌疑人，但要说明为什么。",
        "y": 480,
    },
    {
        "id": "miaoyin",
        "name": "妙音",
        "persona": "风闻派侦探，满脑子坊间传闻与时间线，会引用流言，也可能把流言当成线索。",
        "style": "不超过 70 字，引用一到两条坊间说法或时间点，语气轻快。",
        "y": 660,
    },
]

PARSE_CODE = '''import re

COMMANDS = {
    "open": ["开场", "新案", "换案", "换一个", "下一案", "再来一份", "来一份"],
    "hint": ["提示", "给点线索", "看不懂", "太难了"],
    "rank": ["战绩", "等级", "我是谁", "档案"],
    "help": ["帮助", "怎么玩", "玩法", "说明"],
}
ACCUSE_PREFIXES = ["指控", "结案", "我断定", "真凶是", "凶手是", "定案", "我认定"]

def main(query: str, user: str, app: str) -> dict:
    text = re.sub(r"\\s+", " ", (query or "").strip())[:1500]
    intent = "accuse" if text else "open"
    for key, words in COMMANDS.items():
        if any(text == word or text.startswith(word) for word in words):
            intent = key
            break
    accusation = text
    for word in ACCUSE_PREFIXES:
        if accusation.startswith(word):
            accusation = accusation[len(word):].lstrip(":：,， ")
    if intent == "accuse" and not accusation:
        intent = "open"
    player = "dify:" + (app or "local") + ":" + (user or "anonymous")
    safe = text.replace('"', "'")
    return {
        "intent": intent,
        "query": text,
        "accusation": accusation,
        "player": player,
        "player_input": '{"player": "' + player + '", "app_id": "' + (app or "") + '", "user_input": "' + safe + '"}',
    }
'''

UNPACK_CODE = '''import json

FIELDS = ["ok", "error", "kb_query", "case_title", "case_id", "ticket", "briefing", "document",
          "archivist_line", "rank_title", "rank_note", "memory_id", "memory_summary",
          "trust_text", "seen_text", "hint"]

def main(body: str) -> dict:
    out = {key: "" for key in FIELDS}
    out["seq"] = 0
    try:
        data = json.loads(body)
    except Exception:
        out["error"] = (body or "")[:400]
        return out
    case = data.get("case") or {}
    memory = data.get("memory") or {}
    rank = memory.get("rank") or {}
    trust = memory.get("trust") or {}
    seen = memory.get("seen_cases") or []
    title = case.get("title") or ""
    document = case.get("document") or ""
    out.update({
        "ok": str(bool(data.get("ticket"))),
        "kb_query": (title + " " + document[:120]).strip(),
        "case_title": title,
        "case_id": case.get("case_id") or "",
        "ticket": data.get("ticket") or "",
        "briefing": case.get("briefing") or "",
        "document": document,
        "archivist_line": (data.get("archivist") or {}).get("line") or "",
        "rank_title": rank.get("title") or "",
        "rank_note": rank.get("note") or "",
        "memory_id": memory.get("artifact_id") or "",
        "hint": data.get("hint") or "",
        "seq": int(memory.get("seq") or 0),
    })
    out["memory_summary"] = ("等级 " + (rank.get("title") or "实习记录员")
                             + "；已办 " + str(len(seen)) + " 份案卷；上次判定 "
                             + (memory.get("last_verdict") or "无"))
    out["trust_text"] = "；".join([name + " " + str(value) for name, value in trust.items()]) or "暂无"
    out["seen_text"] = "、".join(seen) or "（还没有办过案卷）"
    return out
'''

PACK_CODE = '''def main(segments: list, briefing: str, document: str) -> dict:
    lines = []
    for item in (segments or [])[:6]:
        if isinstance(item, dict):
            content = (item.get("content") or "").strip().replace("\\n", " ")
            if content:
                lines.append("【" + (item.get("title") or "案卷") + "】" + content[:220])
        elif isinstance(item, str):
            lines.append(item[:220])
    return {
        "evidence": "\\n".join(lines) if lines else "（知识库没有检索到片段）",
        "case_context": (document or briefing or "")[:1400],
        "hit_count": len(lines),
    }
'''

JUDGE_CODE = '''import json

def main(body: str) -> dict:
    out = {"ok": "False", "error": "", "pass": False, "verdict": "", "evidence": "", "expected": "",
           "memory_id": "", "rank_title": "", "rank_note": "", "canon_report": "",
           "rumour_report": "", "verdict_line": "", "trust_text": "", "seq": 0}
    try:
        data = json.loads(body)
    except Exception:
        out["error"] = (body or "")[:400]
        return out
    hits = data.get("canon_hits") or []
    rumours = data.get("rumour_hits") or []
    rank = data.get("rank") or {}
    trust = data.get("trust") or {}
    out.update({
        "ok": "True",
        "pass": bool(data.get("pass")),
        "verdict": data.get("verdict") or "",
        "evidence": (data.get("evidence_fragment") or {}).get("text") or "",
        "expected": data.get("expected_answer") or "",
        "memory_id": data.get("memory_id") or "",
        "seq": int(data.get("seq") or 0),
        "rank_title": rank.get("title") or "",
        "rank_note": rank.get("note") or "",
        "canon_report": "；".join([item.get("claim", "") for item in hits]) or "（没有命中任何已勘结事实）",
        "rumour_report": "、".join(rumours) or "无",
        "verdict_line": data.get("verdict_line") or "",
        "trust_text": "；".join([name + " " + str(value) for name, value in trust.items()]) or "暂无",
    })
    return out
'''

INFO_CODE = '''def main(intent: str, case_title: str, briefing: str, archivist_line: str, hint: str,
         rank_title: str, rank_note: str, memory_summary: str, seen_text: str,
         memory_id: str, seq: int) -> dict:
    head = "🏮 **离谱侦探社** · 档案员老砚：\\n" + (archivist_line or "档案室今天很安静。") + "\\n\\n"
    if intent == "hint":
        body = ("### 案卷：「" + case_title + "」\\n" + (briefing or "") + "\\n\\n"
                "**老砚压低声**：这句别写进勘结，但你可以照着找——\\n> "
                + (hint or "先看现场，再看矛盾那一栏。") + "\\n\\n"
                "看完写下你的指控，例如「真凶是某某，因为某处痕迹只能这样解释」。")
    elif intent == "rank":
        body = ("### 你的档案\\n"
                "等级：**" + (rank_title or "实习记录员") + "**（" + (rank_note or "") + "）\\n"
                "- " + (memory_summary or "还没有记录") + "\\n"
                "- 记忆凭证：PowerContext `" + (memory_id or "（尚未建立）") + "`\\n"
                "- 办过的案卷：" + (seen_text or "（无）") + "\\n\\n"
                "输入「开场」开始下一份案卷。")
    elif intent == "help":
        body = ("### 怎么玩\\n"
                "1. 输入「开场」领一份案卷，读现场、线索、矛盾三部分。\\n"
                "2. 三位侦探会各有说法：林默只看物证，阿鬼只问谁得利，妙音爱传流言——他们说错话是常态。\\n"
                "3. 你写下指控，点名凶手并给出决定性的那处证据；判定按案卷 canon 执行，AI 说了不算。\\n"
                "4. 「提示」找老砚要线索，「战绩」看档案。换新会话回来，档案员仍然记得你。")
    else:
        body = ("### 案卷：「" + (case_title or "（还没抽到）") + "」\\n"
                + (briefing or "") + "\\n\\n"
                "三位侦探已经就位。读完材料直接写下指控，例如：\\n"
                "> 真凶是姚娘子，铜铃是被撬棍从门轴上顶落的，撬痕朝院内，说明她是从院里出来的人。\\n\\n"
                "输入「提示」可以让老砚给一条线索。")
    return {"info_text": head + body}
'''


CLEAN_CODE = """import re

THINK = re.compile(r"<think[^>]*>.*?</think>|<!--.*?-->", re.S)


def _clean(text: str) -> str:
    value = THINK.sub(" ", text or "")
    value = re.sub(r"\s+", " ", value).strip()
    return value[:260]


def main(linmo: str, agui: str, miaoyin: str) -> dict:
    return {
        "linmo": _clean(linmo) or "（林默翻了翻笔录，没说话）",
        "agui": _clean(agui) or "（阿鬼耸了耸肩）",
        "miaoyin": _clean(miaoyin) or "（妙音还在打听街上的说法）",
    }
"""


def node(node_id: str, title: str, data: dict, x: int, y: int, desc: str = "", height: int = 120) -> dict:
    payload = {"type": data.pop("type"), "title": title, "desc": desc, "selected": False, **data}
    return {
        "id": node_id,
        "type": "custom",
        "position": {"x": x, "y": y},
        "width": 244,
        "height": height,
        "sourcePosition": "right",
        "targetPosition": "left",
        "data": payload,
    }


def edge(source: str, target: str, source_type: str, target_type: str, handle: str = "source") -> dict:
    return {
        "id": f"{source}-{handle}-{target}",
        "source": source,
        "sourceHandle": handle,
        "target": target,
        "targetHandle": "target",
        "type": "custom",
        "zIndex": 0,
        "data": {"isInIteration": False, "isInLoop": False, "sourceType": source_type, "targetType": target_type},
    }


def build(
    dataset_id: str,
    provider: str,
    model: str,
    bridge_url: str,
    api_key_placeholder: str,
    embedding_provider: str,
    embedding_model: str,
) -> dict:
    base = bridge_url.rstrip("/")
    nodes: list[dict] = []
    edges: list[dict] = []

    nodes.append(
        node(
            "start",
            "玩家发言",
            {"type": "start", "variables": []},
            40,
            320,
            "同一个 Dify 终端用户（sys.user_id）在侦探社里永远对应同一份档案。",
        )
    )
    nodes.append(
        node(
            "parse",
            "① 解析意图",
            {
                "type": "code",
                "code_language": "python3",
                "code": PARSE_CODE,
                "variables": [
                    {"variable": "query", "value_selector": ["sys", "query"]},
                    {"variable": "user", "value_selector": ["sys", "user_id"]},
                    {"variable": "app", "value_selector": ["sys", "app_id"]},
                ],
                "outputs": {
                    "intent": {"type": "string", "children": None},
                    "query": {"type": "string", "children": None},
                    "accusation": {"type": "string", "children": None},
                    "player": {"type": "string", "children": None},
                    "player_input": {"type": "string", "children": None},
                },
            },
            300,
            320,
            "把「开场 / 指控 / 提示 / 战绩 / 玩法」分开；player 由 app_id + user_id 拼成，跨会话稳定。",
        )
    )
    nodes.append(
        node(
            "open",
            "② 读记忆并开场",
            {
                "type": "http-request",
                "method": "post",
                "url": base + "/session/open",
                "authorization": {"type": "no-auth"},
                "headers": "Content-Type:application/json\nAuthorization:Bearer {{#env.DETECTIVE_API_KEY#}}",
                "params": "",
                "body": {"type": "json", "data": [{"key": "", "type": "text", "value": "{{#parse.player_input#}}"}]},
                "timeout": {"connect": 10, "read": 60, "write": 20},
                "ssl_verify": True,
                "retry_config": {"retry_enabled": False, "max_retries": 0, "retry_interval": 1000},
            },
            560,
            320,
            "PowerContext 读：把这位玩家的等级、侦探信任度、黑历史读回来，并抽一份案卷。",
            140,
        )
    )
    nodes.append(
        node(
            "unpack",
            "③ 拆开场回执",
            {
                "type": "code",
                "code_language": "python3",
                "code": UNPACK_CODE,
                "variables": [{"variable": "body", "value_selector": ["open", "body"]}],
                "outputs": {
                    key: {"type": "number" if key == "seq" else "string", "children": None}
                    for key in (
                        "ok",
                        "error",
                        "kb_query",
                        "case_title",
                        "case_id",
                        "ticket",
                        "briefing",
                        "document",
                        "archivist_line",
                        "rank_title",
                        "rank_note",
                        "seq",
                        "memory_id",
                        "memory_summary",
                        "trust_text",
                        "seen_text",
                        "hint",
                    )
                },
            },
            820,
            320,
        )
    )
    nodes.append(
        node(
            "gate",
            "④ 这次是不是指控",
            {
                "type": "if-else",
                "logical_operator": "and",
                "cases": [
                    {
                        "case_id": "true",
                        "logical_operator": "and",
                        "conditions": [
                            {
                                "variable_selector": ["parse", "intent"],
                                "comparison_operator": "is",
                                "value": "accuse",
                            }
                        ],
                    }
                ],
            },
            1080,
            320,
            "只有「指控」才进判定链；开场、提示、战绩走信息链，不消耗回合。",
        )
    )
    nodes.append(
        node(
            "info",
            "④A 组装信息回复",
            {
                "type": "code",
                "code_language": "python3",
                "code": INFO_CODE,
                "variables": [
                    {"variable": "intent", "value_selector": ["parse", "intent"]},
                    {"variable": "case_title", "value_selector": ["unpack", "case_title"]},
                    {"variable": "briefing", "value_selector": ["unpack", "briefing"]},
                    {"variable": "archivist_line", "value_selector": ["unpack", "archivist_line"]},
                    {"variable": "hint", "value_selector": ["unpack", "hint"]},
                    {"variable": "rank_title", "value_selector": ["unpack", "rank_title"]},
                    {"variable": "rank_note", "value_selector": ["unpack", "rank_note"]},
                    {"variable": "memory_summary", "value_selector": ["unpack", "memory_summary"]},
                    {"variable": "seen_text", "value_selector": ["unpack", "seen_text"]},
                    {"variable": "memory_id", "value_selector": ["unpack", "memory_id"]},
                    {"variable": "seq", "value_selector": ["unpack", "seq"]},
                ],
                "outputs": {"info_text": {"type": "string", "children": None}},
            },
            1340,
            60,
        )
    )
    nodes.append(
        node(
            "answer_info",
            "④B 信息回复",
            {"type": "answer", "answer": "{{#info.info_text#}}", "variables": []},
            1600,
            60,
        )
    )
    nodes.append(
        node(
            "kb",
            "⑤ 检索案卷库（seekdb）",
            {
                "type": "knowledge-retrieval",
                "query_variable_selector": ["unpack", "kb_query"],
                "dataset_ids": [dataset_id],
                "retrieval_mode": "multiple",
                "multiple_retrieval_config": {
                    "top_k": 6,
                    "score_threshold": None,
                    "reranking_mode": "weighted_score",
                    "reranking_enable": True,
                    "reranking_model": None,
                    "weights": {
                        "vector_setting": {
                            "vector_weight": 0.7,
                            "embedding_provider_name": embedding_provider,
                            "embedding_model_name": embedding_model,
                        },
                        "keyword_setting": {"keyword_weight": 0.3},
                    },
                },
                "metadata_filtering_mode": "disabled",
                "metadata_filtering_conditions": None,
                "vision": {"enabled": False},
            },
            1340,
            480,
            "Dify 原生知识库检索：seekdb 同一张表上有向量索引和全文索引，这里按 0.7 / 0.3 加权融合。",
        )
    )
    nodes.append(
        node(
            "pack",
            "⑥ 整理检索片段",
            {
                "type": "code",
                "code_language": "python3",
                "code": PACK_CODE,
                "variables": [
                    {"variable": "segments", "value_selector": ["kb", "result"]},
                    {"variable": "briefing", "value_selector": ["unpack", "briefing"]},
                    {"variable": "document", "value_selector": ["unpack", "document"]},
                ],
                "outputs": {
                    "evidence": {"type": "string", "children": None},
                    "case_context": {"type": "string", "children": None},
                    "hit_count": {"type": "number", "children": None},
                },
            },
            1600,
            480,
        )
    )

    for detective in DETECTIVES:
        nodes.append(
            node(
                detective["id"],
                f"⑦ 侦探 · {detective['name']}",
                {
                    "type": "llm",
                    "model": {
                        "provider": provider,
                        "name": model,
                        "mode": "chat",
                        "completion_params": {"temperature": 0.85, "max_tokens": 220},
                    },
                    "prompt_template": [
                        {
                            "role": "system",
                            "text": (
                                f"你是清代小城侦探社的{detective['name']}。{detective['persona']}\n"
                                "你只根据给出的案卷材料发言，不编造型号、人名或物证。\n"
                                "材料里既有官府勘结的真相与决定性证据，也有坊间流言：流言不能当成事实，"
                                "但按你的性格，可以替它说句话并注明「听来的」。\n"
                                f"输出要求：{detective['style']}只输出台词本身，不要标题，不要解释你是否在扮演。"
                            ),
                        },
                        {
                            "role": "user",
                            "text": (
                                "【案卷材料】\n{{#pack.case_context#}}\n\n"
                                "【知识库检索片段】\n{{#pack.evidence#}}\n\n"
                                "【玩家这次的说法】\n{{#parse.query#}}\n\n"
                                "给出一句判断，并说明你依据的是材料里的哪处痕迹或哪条传闻。"
                            ),
                        },
                    ],
                    "prompt_config": {"jinja2_variables": []},
                    "memory": None,
                    "context": {"enabled": False, "variable_selector": []},
                    "vision": {"enabled": False},
                    "reasoning_format": "separated",
                },
                1860,
                detective["y"],
                detective["persona"],
            )
        )

    nodes.append(
        node(
            "clean",
            "\u2466' \u6e05\u6d17\u53f0\u8bcd",
            {
                "type": "code",
                "code_language": "python3",
                "code": CLEAN_CODE,
                "variables": [
                    {"variable": "linmo", "value_selector": ["linmo", "text"]},
                    {"variable": "agui", "value_selector": ["agui", "text"]},
                    {"variable": "miaoyin", "value_selector": ["miaoyin", "text"]},
                ],
                "outputs": {
                    "linmo": {"type": "string", "children": None},
                    "agui": {"type": "string", "children": None},
                    "miaoyin": {"type": "string", "children": None},
                },
            },
            1990,
            480,
            "\u53bb\u6389\u6a21\u578b\u81ea\u5e26\u7684 <think> \u601d\u7ef4\u6807\u7b7e\uff0c\u53ea\u4fdd\u7559\u53f0\u8bcd\u3002",
        )
    )
    nodes.append(
        node(
            "judge",
            "⑧ 判定并写回记忆",
            {
                "type": "http-request",
                "method": "post",
                "url": base + "/session/judge",
                "authorization": {"type": "no-auth"},
                "headers": "Content-Type:application/json\nAuthorization:Bearer {{#env.DETECTIVE_API_KEY#}}",
                "params": "",
                "body": {
                    "type": "json",
                    "data": [
                        {
                            "key": "",
                            "type": "text",
                            "value": (
                                '{"ticket": "{{#unpack.ticket#}}", '
                                '"accusation": "{{#parse.accusation#}}", '
                                '"speeches": {"linmo": "{{#linmo.text#}}", '
                                '"agui": "{{#agui.text#}}", "miaoyin": "{{#miaoyin.text#}}"}}'
                            ),
                        }
                    ],
                },
                "timeout": {"connect": 10, "read": 60, "write": 20},
                "ssl_verify": True,
                "retry_config": {"retry_enabled": False, "max_retries": 0, "retry_interval": 1000},
            },
            2120,
            480,
            "确定性判定 + PowerContext 写回：AI 侦探可以胡说，盖章必须按案卷 canon。",
            140,
        )
    )
    nodes.append(
        node(
            "verdict",
            "⑨ 拆判定回执",
            {
                "type": "code",
                "code_language": "python3",
                "code": JUDGE_CODE,
                "variables": [{"variable": "body", "value_selector": ["judge", "body"]}],
                "outputs": {
                    **{
                        key: {"type": "number" if key == "seq" else "string", "children": None}
                        for key in (
                            "ok",
                            "error",
                            "verdict",
                            "evidence",
                            "expected",
                            "memory_id",
                            "seq",
                            "rank_title",
                            "rank_note",
                            "canon_report",
                            "rumour_report",
                            "verdict_line",
                            "trust_text",
                        )
                    },
                    "pass": {"type": "boolean", "children": None},
                },
            },
            2380,
            480,
        )
    )
    nodes.append(
        node(
            "pass_gate",
            "⑩ 勘结是否成立",
            {
                "type": "if-else",
                "logical_operator": "and",
                "cases": [
                    {
                        "case_id": "true",
                        "logical_operator": "and",
                        "conditions": [
                            {
                                "variable_selector": ["verdict", "pass"],
                                "comparison_operator": "is",
                                "value": "true",
                            }
                        ],
                    }
                ],
            },
            2640,
            480,
        )
    )

    head = (
        "🏮 **离谱侦探社** · 第 {{#verdict.seq#}} 轮\n"
        "档案员老砚：{{#unpack.archivist_line#}}\n\n"
        "### 本次案卷\n"
        "**{{#unpack.case_title#}}**\n"
        "{{#unpack.briefing#}}\n\n"
        "### 三位侦探的说法\n"
        "- **林默（物证派）**：{{#clean.linmo#}}\n"
        "- **阿鬼（人心派）**：{{#clean.agui#}}\n"
        "- **妙音（风闻派）**：{{#clean.miaoyin#}}\n\n"
        "### 你的指控\n"
        "{{#parse.accusation#}}\n\n"
    )
    tail = (
        "\n\n---\n"
        "🧾 **记忆凭证**：PowerContext `{{#verdict.memory_id#}}` · 累计 {{#verdict.seq#}} 轮 · "
        "等级 {{#verdict.rank_title#}}（{{#verdict.rank_note#}}）\n"
        "📚 **知识库检索**：本轮回捞 {{#pack.hit_count#}} 条案卷片段（seekdb 向量 + 全文加权融合）\n"
        "🎭 **侦探信任度**：{{#verdict.trust_text#}}\n"
        "下一步：输入「开场」领下一份案卷，「战绩」看档案，「提示」让档案员给线索。"
    )
    nodes.append(
        node(
            "answer_pass",
            "⑪ 勘结成立",
            {
                "type": "answer",
                "answer": (
                    head
                    + "### 勘结：✅ 成立\n"
                    "{{#verdict.verdict_line#}}\n\n"
                    "**决定性证据**：{{#verdict.evidence#}}\n\n"
                    "**命中的已勘结事实**：{{#verdict.canon_report#}}\n"
                    "**踩中的谣言**：{{#verdict.rumour_report#}}"
                    + tail
                ),
                "variables": [],
            },
            2900,
            380,
        )
    )
    nodes.append(
        node(
            "answer_fail",
            "⑫ 勘结被驳回",
            {
                "type": "answer",
                "answer": (
                    head
                    + "### 勘结：❌ 被驳回\n"
                    "{{#verdict.verdict_line#}}\n\n"
                    "**案卷里的决定性证据**：{{#verdict.evidence#}}\n\n"
                    "**可以成立的勘结要点**：{{#verdict.expected#}}\n"
                    "**你这次踩中的谣言**：{{#verdict.rumour_report#}}"
                    + tail
                ),
                "variables": [],
            },
            2900,
            620,
        )
    )

    edges.extend(
        [
            edge("start", "parse", "start", "code"),
            edge("parse", "open", "code", "http-request"),
            edge("open", "unpack", "http-request", "code"),
            edge("unpack", "gate", "code", "if-else"),
            edge("gate", "kb", "if-else", "knowledge-retrieval", "true"),
            edge("gate", "info", "if-else", "code", "false"),
            edge("info", "answer_info", "code", "answer"),
            edge("kb", "pack", "knowledge-retrieval", "code"),
            edge("pack", "linmo", "code", "llm"),
            edge("pack", "agui", "code", "llm"),
            edge("pack", "miaoyin", "code", "llm"),
            edge("linmo", "clean", "llm", "code"),
            edge("agui", "clean", "llm", "code"),
            edge("miaoyin", "clean", "llm", "code"),
            edge("clean", "judge", "code", "http-request"),
            edge("judge", "verdict", "http-request", "code"),
            edge("verdict", "pass_gate", "code", "if-else"),
            edge("pass_gate", "answer_pass", "if-else", "answer", "true"),
            edge("pass_gate", "answer_fail", "if-else", "answer", "false"),
        ]
    )

    return {
        "kind": "app",
        "version": "0.5.0",
        "dependencies": [],
        "app": {
            "name": "离谱侦探社",
            "description": (
                "Dify × seekdb × PowerContext：三位性格不同的 AI 侦探各说各话，"
                "你只能用知识库里的案卷证据盖章；档案员会记住你上次栽在哪里。"
            ),
            "mode": "advanced-chat",
            "icon": "🕵️",
            "icon_background": "#E0F2FE",
            "use_icon_as_answer_icon": True,
        },
        "workflow": {
            "conversation_variables": [],
            "environment_variables": [
                {
                    "id": "9f2a1f7c-1c1e-4c3a-9c11-1a2b3c4d5e01",
                    "name": "DETECTIVE_API_KEY",
                    "value": api_key_placeholder,
                    "value_type": "secret",
                    "selector": ["env", "DETECTIVE_API_KEY"],
                    "description": "与后端 .env 里的 DETECTIVE_API_KEY 一致",
                }
            ],
            "features": {
                "opening_statement": (
                    "🕵️ 离谱侦探社开门了。\n\n"
                    "镇上的案卷太多，三位侦探又常常互相矛盾：林默只认物证，阿鬼只问谁得利，妙音满嘴流言。\n"
                    "真正靠得住的凭据，是档案室里的案卷本身。\n\n"
                    "输入「开场」领一份案卷；读完写下你的指控（例如「真凶是姚娘子，撬痕朝院内，说明人是院里出去的」）；"
                    "「提示」让档案员给一条线索；「战绩」看你的档案。\n"
                    "关掉页面、甚至换个新会话回来，档案员仍然还记得你。"
                ),
                "suggested_questions": [
                    "开场",
                    "战绩",
                    "真凶是姚娘子，铜铃是被撬棍从门轴上顶落的",
                    "提示",
                ],
                "suggested_questions_after_answer": {"enabled": False},
                "retriever_resource": {"enabled": True},
                "speech_to_text": {"enabled": False},
                "text_to_speech": {"enabled": False},
                "sensitive_word_avoidance": {"enabled": False},
                "file_upload": {"enabled": False},
            },
            "graph": {
                "nodes": nodes,
                "edges": edges,
                "viewport": {"x": 0, "y": 0, "zoom": 0.5},
            },
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-id", default="REPLACE_WITH_DATASET_ID")
    parser.add_argument("--llm-provider", default="langgenius/openai_api_compatible/openai_api_compatible")
    parser.add_argument("--llm-model", default="deepseek-chat")
    parser.add_argument("--bridge-url", default="http://host.docker.internal:8012")
    parser.add_argument("--embedding-provider", default="langgenius/openai_api_compatible/openai_api_compatible")
    parser.add_argument("--embedding-model", default="bge-small-zh-v1.5")
    parser.add_argument("--api-key-placeholder", default="REPLACE_WITH_DETECTIVE_API_KEY")
    parser.add_argument("--output", default=str(OUTPUT))
    args = parser.parse_args()

    document = build(
        dataset_id=args.dataset_id,
        provider=args.llm_provider,
        model=args.llm_model,
        bridge_url=args.bridge_url,
        api_key_placeholder=args.api_key_placeholder,
        embedding_provider=args.embedding_provider,
        embedding_model=args.embedding_model,
    )
    target = Path(args.output)
    target.parent.mkdir(parents=True, exist_ok=True)
    with io.open(target, "w", encoding="utf-8", newline="\n") as handle:
        yaml.safe_dump(document, handle, allow_unicode=True, sort_keys=False, width=120)
    graph = document["workflow"]["graph"]
    print(f"wrote {target} ({len(graph['nodes'])} nodes, {len(graph['edges'])} edges)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
