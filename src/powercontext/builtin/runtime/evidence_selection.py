# Copyright (c) 2026 OceanBase.
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at http://www.apache.org/licenses/LICENSE-2.0

"""Offline query-directed selection of literal Memory sentences.

Only request text and supplied candidates are used. No corpus, model, network,
filesystem resource, or persisted selection state is required.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from powercontext.artifacts import ArtifactAddress
from powercontext.builtin.runtime.errors import PreparedContextInvariantError
from powercontext.builtin.runtime.models import PrepareContextRequest, PreparedContext
from powercontext.builtin.runtime.prepared_context import (
    MemoryEntryAddress,
    PreparedContextBuild,
    PreparedContextOmissions,
    PreparedMemoryCandidates,
    PreparedMemoryFragment,
)
from powercontext.builtin.runtime.prepared_text import TRUST_POLICY

_STOP = frozenset([
    "a",
    "an",
    "the",
    "and",
    "or",
    "of",
    "to",
    "in",
    "on",
    "at",
    "by",
    "for",
    "with",
    "from",
    "as",
    "is",
    "are",
    "was",
    "were",
    "be",
    "been",
    "being",
    "this",
    "that",
    "these",
    "those",
    "it",
    "its",
    "we",
    "our",
    "their",
    "they",
    "has",
    "have",
    "had",
    "do",
    "does",
    "did",
    "can",
    "could",
    "may",
    "might",
    "will",
    "would",
    "should",
    "not",
    "no",
    "than",
    "then",
    "into",
    "through",
    "during",
    "using",
    "used",
    "also",
    "both",
    "each",
    "between",
    "among",
    "which",
    "who",
    "how",
    "what",
    "when",
    "where",
    "whether",
    "such",
    "there",
    "here",
])
_TOKEN = re.compile(r"[^\W_]+", re.UNICODE)
_BOUNDARY = re.compile(r"[。！？]+[\"\'”’)]*|[.!?]+[\"\'”’)]*(?=\s|$)|[\r\n]+")  # noqa: RUF001
_ABBREVIATIONS = frozenset({"e.g", "i.e", "et al", "fig", "dr", "vs", "approx", "prof"})


def _terms(text: str) -> Counter[str]:
    result: Counter[str] = Counter()
    for match in _TOKEN.finditer(text.casefold()):
        word = match.group()
        if re.search(r"[\u3400-\u9fff]", word):
            # CJK has no obligatory word separators; shared bigrams permit a
            # short query to match a longer sentence without changing its span.
            result.update(word[index : index + 2] for index in range(len(word) - 1))
        if word in _STOP:
            continue
        # Conservative morphology, keeping biomedical names and numbers intact.
        if len(word) > 5 and word.endswith("ies"):
            word = word[:-3] + "y"
        elif len(word) > 5 and word.endswith("ing"):
            word = word[:-3]
        elif len(word) > 4 and word.endswith("ed"):
            word = word[:-2]
        elif len(word) > 4 and word.endswith("s") and not word.endswith(("ss", "is", "us")):
            word = word[:-1]
        result[word] += 1
    return result


def _sentences(text: str) -> list[tuple[int, int]]:
    spans = []
    start = 0
    for match in _BOUNDARY.finditer(text):
        end = match.end()
        prefix = text[start : match.start()].rstrip().casefold()
        last = prefix.rsplit(" ", 1)[-1]
        if match.group().startswith(".") and (last in _ABBREVIATIONS or prefix.endswith("et al")):
            continue
        if text[start:end].strip():
            spans.append((start, end))
        start = end
    if text[start:].strip():
        spans.append((start, len(text)))
    return spans


@dataclass(frozen=True)
class _Sentence:
    document: int
    start: int
    end: int
    terms: Counter[str]
    score: float


@dataclass(frozen=True)
class _Document:
    address: MemoryEntryAddress
    text: str


def _fragments(documents: Sequence[_Document], sentences: Sequence[_Sentence]) -> tuple[PreparedMemoryFragment, ...]:
    intervals: list[tuple[int, int, int]] = []
    for sentence in sorted(sentences, key=lambda s: (s.document, s.start, s.end)):
        doc, start, end = sentence.document, sentence.start, sentence.end
        if intervals and intervals[-1][0] == doc and not documents[doc].text[intervals[-1][2] : start].strip():
            previous = intervals[-1]
            intervals[-1] = (doc, previous[1], max(previous[2], end))
        else:
            intervals.append((doc, start, end))
    return tuple(
        PreparedMemoryFragment(documents[doc].address, start, end, documents[doc].text[start:end])
        for doc, start, end in intervals
    )


def _render(fragments: Sequence[PreparedMemoryFragment]) -> str:
    items = []
    for fragment in fragments:
        address = fragment.address
        items.append({
            "citation": {
                "scope_id": address.memory.scope_id,
                "memory_ref": address.memory.artifact.model_dump(mode="json"),
                "entry_id": address.entry_id,
                "entry_version_id": address.entry_version_id,
            },
            "content": fragment.text,
            "start": fragment.start,
            "end": fragment.end,
        })
    payload = json.dumps({"trust": "untrusted_history", "items": items}, ensure_ascii=False, separators=(",", ":"))
    return (
        TRUST_POLICY
        + "\n\nBEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1\n"
        + payload
        + "\nEND_POWERCONTEXT_PREPARED_CONTEXT_V1"
    )


def _documents(groups: Sequence[PreparedMemoryCandidates]) -> list[_Document]:
    """Validate exact identities before scoring or deduplicating any evidence."""
    documents: list[_Document] = []
    seen = set()
    for group in groups:
        if group.hits and group.memory_ref is None:
            raise PreparedContextInvariantError("memory-ref-missing")
        for hit in group.hits:
            if hit.memory_ref != group.memory_ref:
                raise PreparedContextInvariantError("memory-ref-mismatch")
            address = MemoryEntryAddress(
                ArtifactAddress(scope_id=group.scope_id, artifact=hit.memory_ref), hit.entry_id, hit.entry_version_id
            )
            identity = (
                group.scope_id,
                hit.memory_ref.family,
                hit.memory_ref.artifact_id,
                hit.memory_ref.revision,
                hit.entry_id,
                hit.entry_version_id,
            )
            if identity in seen or not hit.text.strip() or not hit.entry_id.strip() or not hit.entry_version_id.strip():
                continue
            seen.add(identity)
            documents.append(_Document(address, hit.text))
    return documents


def _rank_sentences(query_text: str, documents: Sequence[_Document]) -> list[_Sentence]:
    query = _terms(query_text)
    document_terms = [_terms(document.text) for document in documents]
    frequency = Counter(term for terms in document_terms for term in terms)
    weights = {term: 1 + math.log(1 + len(documents) / (1 + frequency[term])) for term in query}
    total_weight = sum(weights.values()) or 1
    doc_scores = [sum(weights[t] for t in query if t in terms) / total_weight for terms in document_terms]
    sentences = []
    for doc, document in enumerate(documents):
        for start, end in _sentences(document.text):
            terms = _terms(document.text[start:end])
            overlap = sum(
                weight * (1 + 0.15 * math.log(terms[term])) for term, weight in weights.items() if terms[term]
            )
            if not overlap:
                continue
            coverage = overlap / total_weight
            # Document support disambiguates a shared term in unrelated papers;
            # a mild length normalization does not punish complete result sentences.
            score = coverage * (0.55 + 0.45 * doc_scores[doc]) / (1 + len(terms) / 100)
            sentences.append(_Sentence(doc, start, end, terms, score))

    # Repeated boilerplate cannot buy utility by appearing many times. Identity
    # remains attached to its original scope, even when its text is duplicated.
    unique = {}
    for sentence in sentences:
        key = documents[sentence.document].text[sentence.start : sentence.end].strip()
        if key not in unique or sentence.score > unique[key].score:
            unique[key] = sentence
    return sorted(unique.values(), key=lambda s: (-s.score, s.document, s.start))


def _pack(
    documents: Sequence[_Document], sentences: Sequence[_Sentence], max_bytes: int
) -> tuple[PreparedMemoryFragment, ...]:
    # Each state records selected sentences, utility, and its *complete* encoding.
    # Keep both utility and low-cost representatives in each byte bucket.
    states: list[tuple[tuple[_Sentence, ...], float, int]] = [((), 0.0, 0)]
    for sentence in sentences:
        expanded = list(states)
        for selected, value, _ in states:
            if len(selected) >= 12:
                continue
            fragments = _fragments(documents, (*selected, sentence))
            size = len(_render(fragments).encode("utf-8"))
            if size > max_bytes:
                continue
            redundancy = max(
                (
                    sum((sentence.terms & other.terms).values()) / max(1, sum(sentence.terms.values()))
                    for other in selected
                ),
                default=0,
            )
            gain = sentence.score * (1 - 0.4 * redundancy)
            expanded.append(((*selected, sentence), value + gain, size))
        buckets: dict[int, list[tuple[tuple[_Sentence, ...], float, int]]] = {}
        for state in expanded:
            buckets.setdefault(state[2] // 16, []).append(state)
        diverse = []
        for bucket in buckets.values():
            best = max(bucket, key=lambda state: (state[1], -state[2]))
            cheapest = min(bucket, key=lambda state: (state[2], -state[1]))
            diverse.append(best)
            if cheapest is not best:
                diverse.append(cheapest)
        ranked = sorted(diverse, key=lambda state: (-state[1], state[2]))
        reserve = sorted(diverse, key=lambda state: (state[2], -state[1]))[:32]
        states = ranked[:96] + [state for state in reserve if state not in ranked[:96]]
    selected, _, _ = max(states, key=lambda state: (state[1], -state[2]))
    return _fragments(documents, selected)


def select_evidence(request: PrepareContextRequest, groups: Sequence[PreparedMemoryCandidates]) -> PreparedContextBuild:
    """Select whole sentences with an exact serialized-byte, bounded beam search."""
    documents = _documents(groups)
    ranked = _rank_sentences(request.query, documents)
    fitting = [
        sentence
        for sentence in ranked
        if len(_render(_fragments(documents, (sentence,))).encode("utf-8")) <= request.max_bytes
    ]
    fragments = _pack(documents, fitting[:96], request.max_bytes)
    selected_ranges = [(fragment.address, fragment.start, fragment.end) for fragment in fragments]
    dropped = sum(
        not any(
            address == documents[sentence.document].address and start <= sentence.start and end >= sentence.end
            for address, start, end in selected_ranges
        )
        for sentence in ranked
    )
    omissions = PreparedContextOmissions(dropped_items=dropped, dropped_no_fitting_truncation=dropped)
    if not fragments:
        return PreparedContextBuild(
            PreparedContext(status="empty", content=None, content_bytes=0), (), omissions=omissions
        )
    content = _render(fragments)
    return PreparedContextBuild(
        context=PreparedContext(status="ready", content=content, content_bytes=len(content.encode("utf-8"))),
        origins=tuple(fragment.address for fragment in fragments),
        omissions=omissions,
        fragments=fragments,
    )
