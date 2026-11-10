from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from powercontext.artifacts import ArtifactAddress, ArtifactRef
from powercontext.builtin.artifacts.memory import MemoryHit
from powercontext.builtin.runtime.errors import PreparedContextInvariantError
from powercontext.builtin.runtime.models import PrepareContextRequest
from powercontext.builtin.runtime.prepared_context import (
    MemoryEntryAddress,
    PreparedContextBuilder,
    PreparedMemoryCandidates,
)

REF = ArtifactRef(family="memory", artifact_id="revision", revision=1)


def hit(entry_id: str, text: str, *, memory_ref: ArtifactRef = REF, version: str = "v1") -> MemoryHit:
    return MemoryHit(
        memory_ref=memory_ref,
        entry_id=entry_id,
        entry_version_id=version,
        text=text,
        score=1.0,
        matched_by=("fts",),
    )


def build(
    query: str,
    hits: tuple[MemoryHit, ...],
    *,
    scope: str = "scope-a",
    max_bytes: int = 8000,
    selection: bool = True,
    **kwargs,
):
    request = PrepareContextRequest(
        query=query,
        max_bytes=max_bytes,
        evidence_selection=selection,
        **kwargs,
    )
    return PreparedContextBuilder().build_scopes_result(
        request=request,
        current_scope_id=scope,
        memory_candidates=(PreparedMemoryCandidates(scope_id=scope, memory_ref=REF, hits=hits),),
    )


def items(result):
    assert result.context.content is not None
    return json.loads(result.context.content.splitlines()[-2])["items"]


def test_query_switches_to_late_selected_tail():
    hits = (
        hit("one", "General administrative notes." * 30 + " Cobalt sensor calibrated at 37 degrees."),
        hit("two", "General administrative notes." * 30 + " Amber valve opens at 19 degrees."),
    )
    cobalt = build("cobalt sensor calibrated", hits, max_bytes=1200)
    amber = build("amber valve opens", hits, max_bytes=1200)
    assert any("Cobalt sensor" in fragment.text for fragment in cobalt.fragments)
    assert any("Amber valve" in fragment.text for fragment in amber.fragments)
    assert not any("Amber valve" in fragment.text for fragment in cobalt.fragments)


def test_nonadjacent_sentences_from_one_entry_merge_only_when_gap_is_whitespace():
    text = "Alpha finding is important. unrelated filler. Beta finding is important."
    result = build("alpha beta finding", (hit("one", text),))
    assert len(result.fragments) == 2
    assert [fragment.text for fragment in result.fragments] == [
        "Alpha finding is important.",
        " Beta finding is important.",
    ]
    assert all(fragment.address.entry_id == "one" for fragment in result.fragments)


def test_cross_scope_duplicate_text_is_selected_once_with_consistent_address_and_json():
    text = "The same addressable finding is shared across scopes."
    groups = (
        PreparedMemoryCandidates("scope-a", REF, (hit("a", text),)),
        PreparedMemoryCandidates("scope-b", REF, (hit("b", text),)),
    )
    request = PrepareContextRequest(query="shared finding", max_bytes=1200, evidence_selection=True)
    result = PreparedContextBuilder().build_scopes_result(
        request=request, current_scope_id="scope-a", memory_candidates=groups
    )
    assert len(result.fragments) == len(result.origins) == 1
    fragment = result.fragments[0]
    assert fragment.address == MemoryEntryAddress(ArtifactAddress(scope_id="scope-a", artifact=REF), "a", "v1")
    assert items(result)[0]["citation"] == {
        "scope_id": "scope-a",
        "memory_ref": REF.model_dump(mode="json"),
        "entry_id": "a",
        "entry_version_id": "v1",
    }


def test_unicode_emoji_crlf_quotes_backslashes_are_exact_slices_and_bytes():
    text = '海洋观测 🌊 says "C:\\data\\海洋".\r\nA second line has the target.'
    result = build("海洋观测 data", (hit("unicode", text),))
    assert result.fragments
    assert result.context.content_bytes == len(result.context.content.encode("utf-8"))
    for fragment in result.fragments:
        assert fragment.text == text[fragment.start : fragment.end]
        assert items(result)[result.fragments.index(fragment)]["content"] == fragment.text
    assert "🌊" in result.fragments[0].text and "\\data" in result.fragments[0].text


def test_512_byte_budget_is_valid_and_can_return_empty():
    result = build("target", (hit("one", "target " + "x" * 2000),), max_bytes=512)
    assert result.context.status == "empty"
    assert result.context.content is None
    assert result.context.content_bytes == 0
    assert result.fragments == ()


def test_exact_output_budget_n_fits_and_n_minus_one_does_not():
    candidate = (hit("one", "A precise target sentence with enough useful evidence."),)
    generous = build("precise target", candidate, max_bytes=2000)
    n = generous.context.content_bytes
    exact = build("precise target", candidate, max_bytes=n)
    below = build("precise target", candidate, max_bytes=max(512, n - 1))
    assert n >= 512
    assert exact.context.content == generous.context.content
    assert exact.context.content_bytes == n
    if n - 1 >= 512:
        assert below.context.status == "empty"


def test_default_false_has_no_fragments_and_is_repeatable():
    hits = (hit("one", "A target sentence."),)
    first = build("target", hits, selection=False)
    second = build("target", hits, selection=False)
    assert first.fragments == second.fragments == ()
    assert first.context.content == second.context.content
    assert first.context.content_bytes == second.context.content_bytes


def test_true_selection_takes_precedence_over_assembly_and_include_code():
    request = PrepareContextRequest(
        query="target",
        max_bytes=1200,
        evidence_selection=True,
        include_code=True,
        assembly={"sections": [{"family": "memory", "limit": 1}]},
    )
    result = PreparedContextBuilder().build_scopes_result(
        request=request,
        current_scope_id="scope-a",
        memory_candidates=(PreparedMemoryCandidates("scope-a", REF, (hit("one", "The target is literal evidence."),)),),
    )
    assert result.fragments and result.context.content is not None
    assert "BEGIN_POWERCONTEXT_PREPARED_CONTEXT_V1" in result.context.content


@pytest.mark.parametrize("bad", [None, "true", 1, 0])
def test_evidence_selection_requires_strict_bool(bad):
    with pytest.raises(ValidationError):
        PrepareContextRequest.model_validate({"query": "target", "evidence_selection": bad})


def test_memory_ref_mismatch_and_missing_ref_are_invariants():
    other = ArtifactRef(family="memory", artifact_id="other", revision=1)
    with pytest.raises(PreparedContextInvariantError, match="memory-ref-mismatch"):
        build("target", (hit("one", "target", memory_ref=other),))
    with pytest.raises(PreparedContextInvariantError, match="memory-ref-missing"):
        PreparedContextBuilder().build_scopes_result(
            request=PrepareContextRequest(query="target", evidence_selection=True),
            current_scope_id="scope-a",
            memory_candidates=(PreparedMemoryCandidates("scope-a", None, (hit("one", "target"),)),),
        )


def test_memory_candidate_limit_is_enforced_before_selection():
    hits = tuple(hit(str(index), f"target sentence {index}.") for index in range(17))
    with pytest.raises(PreparedContextInvariantError, match="memory-candidate-limit"):
        build("target", hits)


def test_decimal_and_abbreviation_do_not_split_sentences_early():
    text = "Dr. Smith measured approx. 3.14 units. The target conclusion follows."
    result = build("Smith 3.14", (hit("one", text),))
    assert result.fragments
    assert result.fragments[0].text == "Dr. Smith measured approx. 3.14 units."


def test_unspaced_chinese_can_select_late_sentence_and_unpunctuated_tail():
    text = "前文无关。" * 100 + "目标结果成立。尾部结论有效"
    result = build("目标结果", (hit("one", text),), max_bytes=800)
    assert [fragment.text for fragment in result.fragments] == ["目标结果成立。"]
    tail = build("尾部结论", (hit("one", text),), max_bytes=800)
    assert tail.fragments[0].end == len(text)
    assert tail.fragments[0].text == "尾部结论有效"


def test_unfitting_high_score_sentences_do_not_hide_short_fitting_evidence():
    text = " ".join("alpha beta " + "x" * 600 + str(index) + "." for index in range(96)) + " alpha."
    result = build("alpha beta", (hit("one", text),), max_bytes=800)
    assert result.fragments and result.fragments[0].text.strip() == "alpha."


def test_budget_search_retains_cheaper_combination_in_same_byte_bucket():
    hits = tuple(
        hit(str(index), text) for index, text in enumerate(("alpha beta gamma.", "alpha beta.", "gamma delta."))
    )
    result = build("alpha beta gamma delta epsilon", hits, max_bytes=743)
    assert [fragment.address.entry_id for fragment in result.fragments] == ["1", "2"]


def test_probe_reports_budget_drops_consistently_with_build():
    request = PrepareContextRequest(query="target", max_bytes=512, evidence_selection=True)
    groups = (PreparedMemoryCandidates("scope-a", REF, (hit("one", "target " + "x" * 2000),)),)
    builder = PreparedContextBuilder()
    result = builder.build_scopes_result(request=request, current_scope_id="scope-a", memory_candidates=groups)
    probe = builder.probe_budget(request=request, current_scope_id="scope-a", memory_candidates=groups)
    assert probe.delivered_items == len(result.fragments) == 0
    assert probe.dropped_items == result.omissions.dropped_items == 1
    assert probe.unused_bytes == 512


@pytest.mark.asyncio
async def test_runtime_prepare_selects_memory_even_with_empty_assembly_and_code_enabled():
    from powercontext.builtin.artifacts.memory import MemoryCandidateRequest, MemoryEntryInput
    from powercontext.builtin.runtime import BuiltinConfig, CaptureSource, open_builtin_runtime
    from powercontext.builtin.scope import ScopeDraft
    from powercontext.builtin.sources import ContentSource

    class LiteralPipeline:
        async def extract(self, request: MemoryCandidateRequest, /):
            return tuple(
                MemoryEntryInput(kind="document", text=source.content, sources=(source,))
                for source in request.sources
                if isinstance(source, ContentSource)
            )

    async with open_builtin_runtime(BuiltinConfig(), candidate_pipeline=LiteralPipeline()) as runtime:
        scope = await runtime.scopes.create(ScopeDraft(title="Evidence", summary="Literal", idempotency_key="evidence"))
        source_text = "Unrelated background. " * 70 + "Cobalt sensor calibrated at 37 degrees."
        await runtime.sources.for_scope(scope.scope_id).capture(CaptureSource(source_id="one", content=source_text, metadata={}))
        while (await runtime.memory.for_scope(scope.scope_id).flush()).processed:
            pass
        context = await runtime.context.for_scope(scope.scope_id).prepare(
            PrepareContextRequest(
                query="cobalt sensor calibrated",
                max_bytes=1200,
                evidence_selection=True,
                assembly={"sections": []},
                include_code=True,
            )
        )
        assert context.status == "ready"
        item = json.loads(context.content.splitlines()[-2])["items"][0]
        assert "37 degrees" in item["content"]
        assert item["content"] == source_text[item["start"] : item["end"]]
        assert item["citation"]["scope_id"] == scope.scope_id
        assert context.content_bytes == len(context.content.encode("utf-8")) <= 1200
