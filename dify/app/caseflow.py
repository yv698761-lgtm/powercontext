"""案卷语料切分、检索兜底与确定性判定。"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .data.cases import CASES

# 每条案卷里，只有"真相/关键证据"是决定性事实，其余是现场、线索、矛盾与坊间谣言。
DECISIVE_KINDS = ("真相", "证据")
RUMOR_KIND = "谣言"


@dataclass
class Chunk:
    chunk_id: str
    case_id: str
    title: str
    kind: str
    heading: str
    factual: bool
    text: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Case:
    case_id: str
    title: str
    document: str
    rumors: list[str]
    canon_assertions: list[tuple[str, list[str]]]
    decisive_keywords: list[str]
    answer_line: str
    red_herring: str
    fragments: list[Chunk] = field(default_factory=list)


def _split_sentences(text: str) -> list[str]:
    return [piece.strip() for piece in re.split(r"(?<=。)", text) if piece.strip()]


def _add_fragments(
    out: list[Chunk],
    case: dict,
    text: str,
    kind: str,
    heading: str,
    factual: bool,
) -> None:
    case_id = case["case_id"]
    used = sum(1 for chunk in out if chunk.case_id == case_id)
    for offset, sentence in enumerate(_split_sentences(text), start=1):
        out.append(
            Chunk(
                chunk_id=f"{case_id}-{kind[:1]}{used + offset:02d}",
                case_id=case_id,
                title=case["title"],
                kind=kind,
                heading=heading,
                factual=factual,
                text=sentence,
            )
        )


def build_chunks() -> list[Chunk]:
    """把 12 份案卷拆成知识库片段（含真假混杂的坊间谣言）。"""
    chunks: list[Chunk] = []
    for case in CASES:
        doc = case["document"]
        scene = re.search(r"【现场】(.*?)(?=【)", doc, re.S)
        if scene:
            _add_fragments(chunks, case, scene.group(1), "现场", "现场记录", True)
        for name, body in re.findall(r"【线索·([^】]+)】(.*?)(?=【)", doc, re.S):
            _add_fragments(chunks, case, body, "线索", f"线索·{name}", True)
        conflict = re.search(r"【矛盾】(.*?)(?=【)", doc, re.S)
        if conflict:
            _add_fragments(chunks, case, conflict.group(1), "现场", "记录中的矛盾", True)
        truth = re.search(r"【真相】(.*?)(?=【)", doc, re.S)
        if truth:
            _add_fragments(chunks, case, truth.group(1), "真相", "官府勘结·真相", True)
        evidence = re.search(r"【关键证据】(.*)$", doc, re.S)
        if evidence:
            _add_fragments(chunks, case, evidence.group(1), "证据", "决定性证据", True)
        for rumor in case["rumors"]:
            index = sum(1 for chunk in chunks if chunk.case_id == case["case_id"] and chunk.kind == RUMOR_KIND) + 1
            chunks.append(
                Chunk(
                    chunk_id=f"{case['case_id']}-r{index:02d}",
                    case_id=case["case_id"],
                    title=case["title"],
                    kind=RUMOR_KIND,
                    heading="坊间流言",
                    factual=False,
                    text=rumor,
                )
            )
    return chunks


def case_by_id(case_id: str) -> dict:
    for case in CASES:
        if case["case_id"] == case_id:
            return case
    raise KeyError(case_id)


def case_by_title(title: str) -> dict | None:
    for case in CASES:
        if case["title"] in title or title in case["title"]:
            return case
    return None


def fallback_retrieval(case_id: str, top_k: int = 4) -> list[Chunk]:
    """离线检索兜底：真相与证据优先。

    线上由 Dify 的知识库检索节点（seekdb 向量 + 全文混合）完成检索；这个函数只用于
    在没有 Dify 的情况下让 `samples/` 的离线样例也能跑，并在 README 中注明区别。
    """
    chunks = [chunk for chunk in build_chunks() if chunk.case_id == case_id and chunk.kind != RUMOR_KIND]
    ranked = sorted(chunks, key=lambda chunk: (chunk.kind not in DECISIVE_KINDS, len(chunk.text)))
    return ranked[:top_k]


def export_corpus(dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("w", encoding="utf-8", newline="\n") as handle:
        for chunk in build_chunks():
            handle.write(json.dumps(chunk.to_dict(), ensure_ascii=False) + "\n")
    return dest


def judge(case: dict, accusation: str) -> dict:
    """确定性判定：指控是否命中真相，是否踩中谣言。"""
    text = accusation or ""
    hits = []
    for claim, keywords in case["canon_assertions"]:
        matched = [kw for kw in keywords if kw in text]
        if matched:
            hits.append({"claim": claim, "matched": matched})
    rumor_hits = [keyword for keyword in false_keywords(case["case_id"]) if _asserted(text, keyword)]
    score = min(len(hits), 2) * 2 - len(rumor_hits)
    return {
        "case_id": case["case_id"],
        "canon_hits": hits,
        "rumour_hits": rumor_hits,
        "score": score,
        "pass": bool(hits) and not rumor_hits,
        "evidence_keywords": [kw for kw in case["decisive_keywords"] if kw in text],
    }


# 各案卷中只属于"错误读法"的短语，玩家指控里出现即视为被谣言带偏。
FALSE_KEYWORDS = {
    "case-001": ["野猫", "风吹", "野猫撞落"],
    "case-002": ["骗保", "电机老化", "潮水导致", "潮水高"],
    "case-003": ["盗贼", "从院门进的"],
    "case-004": ["乌鸦撞", "寒潮"],
    "case-005": ["闹鬼", "自己会唱"],
    "case-006": ["醉汉吃了", "晒化"],
    "case-007": ["游医", "糊涂抓错"],
    "case-008": ["夜风吹动", "老鼠", "监守自盗"],
    "case-009": ["漂木撞", "木头撞走"],
    "case-010": ["灯芯自爆", "骗保"],
    "case-011": ["抄近道", "抄了近道"],
    "case-012": ["小孩当球踢", "本来就是空白"],
}


NEGATIONS = ("不是", "并非", "而非", "没有", "不能", "不算", "排除")


def _asserted(text: str, keyword: str) -> bool:
    """判断关键词是否被"肯定地"说了出来。

    玩家常会写"不是被木头撞走"这种否定句，直接做子串匹配会把它误判成被谣言带偏，
    所以关键词前面出现否定词时不算命中。
    """
    start = 0
    while True:
        index = text.find(keyword, start)
        if index < 0:
            return False
        prefix = text[max(0, index - 4) : index]
        if not any(negation in prefix for negation in NEGATIONS):
            return True
        start = index + len(keyword)


def false_keywords(case_id: str) -> list[str]:
    return FALSE_KEYWORDS.get(case_id, [])


def grade(seq: int, passes: int) -> dict:
    """由累计通过数给出侦探等级，用于跨会话身份感。"""
    levels = [
        (0, "实习记录员", "还在给档案员端茶倒水。"),
        (1, "见习侦探", "第一次让案卷自己开口说话。"),
        (3, "正式侦探", "镇上的巡捕开始主动递烟。"),
        (5, "首席侦探", "档案员开始偷偷抄你的批注。"),
        (8, "神探", "案卷在你手上会自己招供。"),
    ]
    title, note = levels[0]
    for need, name, remark in levels:
        if passes >= need:
            title, note = name, remark
    return {"title": title, "note": note, "rounds": seq, "passes": passes}
