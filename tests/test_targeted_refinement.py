"""Phase 7: one focused second question for fields the main pass left empty.

The main extraction sometimes returns nothing for a printed field. The targeted
pass re-asks only those fields, only inside the blocks whose zone owns the field,
and only when an LM is configured. Every test stubs DSPy, so no model call
leaves the process.
"""

from contextlib import nullcontext
from types import SimpleNamespace

from citeindex.ingestion.pipelines import dspy_extract, refinement

TITLE = "The Book of Evidence"
AUTHOR = "Jane Doe"
PRESS = "Test Press"
CIP_TEXT = (
    "图书在版编目（CIP）数据\n"
    "视觉SLAM十四讲：从理论到实践 / 高翔等著.—北京：电子工业出版社，2017.3\n"
    "ISBN 978-7-121-31104-8"
)
BODY = (
    "This is a long body paragraph about the transmission of the text, its "
    "manuscripts, marginalia and the long history of its printed editions."
)

BLOCKS = [
    {"id": "p1_b1", "text": TITLE, "physical_page_index": 0},
    {"id": "p1_b2", "text": AUTHOR, "physical_page_index": 0},
    {"id": "p2_b1", "text": f"Copyright © 2011 {PRESS}", "physical_page_index": 1},
]
ZONES = {"p1_b1": "title", "p1_b2": "statement_of_responsibility", "p2_b1": "copyright"}


def _blocks(pages):
    """pages: iterable of (page_number, text) tuples, in reading order."""
    blocks, counts = [], {}
    for page, text in pages:
        counts[page] = counts.get(page, 0) + 1
        blocks.append({"id": f"p{page}_b{counts[page]}", "text": text,
                       "physical_page_index": page - 1})
    return blocks


def _full_record():
    return {
        "title": TITLE,
        "author": [{"literal": AUTHOR}],
        "issued": {"date-parts": [[2011]]},
    }


def _state(accepted=None, drops=None):
    return {
        "accepted": dict(accepted or {}),
        "evidence": {},
        "drops": {field: dict(drop) for field, drop in (drops or {}).items()},
        "confidences": {},
        "reasons": {},
        "sources": {},
    }


class _TargetedStub:
    """Fake ``dspy.Predict`` factory for ``RefineMissingField``."""

    def __init__(self, answers):
        self.answers = list(answers)
        self.calls = []
        self.constructed = 0

    def __call__(self, signature):
        assert signature is dspy_extract.RefineMissingField
        self.constructed += 1

        def predict(**kwargs):
            self.calls.append(kwargs)
            reply = self.answers[len(self.calls) - 1]
            if isinstance(reply, Exception):
                raise reply
            return SimpleNamespace(**reply)

        return predict


def _run_targeted(monkeypatch, answers, state, blocks=BLOCKS, zones=ZONES,
                  evaluated=("title", "publisher", "issued", "author")):
    stub = _TargetedStub(answers)
    monkeypatch.setattr(dspy_extract.dspy, "Predict", stub)
    monkeypatch.setattr(dspy_extract.dspy, "context", lambda **kwargs: nullcontext())
    calls = dspy_extract._refine_empty_fields(
        object(), state["accepted"], state["evidence"], state["drops"],
        state["confidences"], state["reasons"], state["sources"],
        blocks, zones, set(evaluated))
    return calls, stub


# ---------------------------------------------------------------------------
# Selection rules (pure)
# ---------------------------------------------------------------------------


def test_priority_order_is_title_publisher_issued_author():
    assert refinement.TARGETED_FIELDS == ("title", "publisher", "issued", "author")
    assert refinement.empty_fields({}, {"title", "publisher", "issued", "author", "type"}) == [
        "title", "publisher", "issued", "author"]


def test_only_evaluated_and_absent_fields_are_pending():
    accepted = {"title": TITLE, "publisher": PRESS}
    assert refinement.empty_fields(accepted, {"title", "publisher", "issued"}) == ["issued"]
    # A field outside the document profile is never re-asked.
    assert refinement.empty_fields({}, {"title", "author", "type"}) == ["title", "author"]


def test_owning_blocks_are_zone_scoped_in_priority_order():
    blocks = [
        {"id": "body", "text": BODY},
        {"id": "imprint", "text": PRESS},
        {"id": "cover", "text": TITLE},
        {"id": "title", "text": TITLE},
    ]
    zones = {"body": "body", "imprint": "imprint", "cover": "cover", "title": "title"}
    assert [block["id"] for block in refinement.owning_blocks("title", blocks, zones)] == [
        "title", "cover"]
    assert [block["id"] for block in refinement.owning_blocks("publisher", blocks, zones)] == [
        "imprint"]
    assert [block["id"] for block in refinement.owning_blocks("issued", blocks, zones)] == [
        "imprint"]
    assert [block["id"] for block in refinement.owning_blocks("author", blocks, zones)] == [
        "title", "cover"]


# ---------------------------------------------------------------------------
# One focused answer
# ---------------------------------------------------------------------------


def test_one_empty_publisher_produces_exactly_one_call_and_fills_it(monkeypatch):
    state = _state(_full_record())
    calls, stub = _run_targeted(
        monkeypatch, [{"value": PRESS, "block_id": "p2_b1", "quote": PRESS}], state)

    assert calls == 1
    assert [call["field"] for call in stub.calls] == ["publisher"]
    assert stub.calls[0]["source_blocks"][0]["id"] == "p2_b1"
    assert state["accepted"]["publisher"] == PRESS


def test_rejected_answer_keeps_the_drop_and_marks_the_attempt(monkeypatch):
    state = _state(_full_record(), {"publisher": {"reason": "not_found", "quote": "Some Press"}})
    calls, _ = _run_targeted(
        monkeypatch,
        [{"value": PRESS, "block_id": "p2_b1", "quote": "Invented Press"}],
        state)

    assert calls == 1
    assert "publisher" not in state["accepted"]
    assert state["drops"]["publisher"] == {
        "reason": "not_found", "quote": "Some Press", "targeted": "rejected"}
    assert "publisher" not in state["sources"]


def test_accepted_field_records_source_confidence_and_clears_the_drop(monkeypatch):
    state = _state(_full_record(), {"publisher": {"reason": "not_found", "quote": None}})
    _run_targeted(monkeypatch, [{"value": PRESS, "block_id": "p2_b1", "quote": PRESS}], state)

    assert state["sources"] == {"publisher": "targeted"}
    # Exact quote in a zone that owns the field: 0.95 on the Phase 6 ladder.
    assert state["confidences"]["publisher"] == 0.95
    assert state["reasons"]["publisher"] == "exact"
    assert state["evidence"]["publisher"]["block_id"] == "p2_b1"
    assert "publisher" not in state["drops"]


def test_normalised_quote_scores_090(monkeypatch):
    state = _state(_full_record())
    _run_targeted(monkeypatch,
                  [{"value": PRESS, "block_id": "p2_b1", "quote": "Test  Press"}], state)

    assert state["confidences"]["publisher"] == 0.90


def test_two_empty_fields_make_at_most_two_calls(monkeypatch):
    state = _state({"title": TITLE})
    calls, stub = _run_targeted(monkeypatch, [
        {"value": PRESS, "block_id": "p2_b1", "quote": PRESS},
        {"value": {"date-parts": [[2011]]}, "block_id": "p2_b1", "quote": "2011"},
    ], state)

    assert calls == refinement.TARGETED_MAX_CALLS == 2
    assert [call["field"] for call in stub.calls] == ["publisher", "issued"]
    assert state["accepted"]["publisher"] == PRESS
    assert state["accepted"]["issued"] == {"date-parts": [[2011]]}


def test_a_third_empty_field_is_left_alone_by_the_cap(monkeypatch):
    state = _state({"title": TITLE})
    calls, stub = _run_targeted(monkeypatch, [
        {"value": PRESS, "block_id": "p2_b1", "quote": PRESS},
        {"value": {"date-parts": [[2011]]}, "block_id": "p2_b1", "quote": "2011"},
    ], state)

    assert calls == 2
    assert [call["field"] for call in stub.calls] == ["publisher", "issued"]
    assert "author" not in state["accepted"]
    assert "author" not in state["sources"]
    assert "author" not in state["drops"]


def test_no_empty_field_makes_zero_calls(monkeypatch):
    state = _state(_full_record() | {"publisher": PRESS})
    calls, stub = _run_targeted(monkeypatch, [], state)

    assert calls == 0
    assert stub.constructed == 0
    assert stub.calls == []


def test_empty_field_without_an_owning_zone_makes_zero_calls(monkeypatch):
    blocks = _blocks([(1, BODY), (2, BODY)])
    state = _state({"title": TITLE})
    calls, stub = _run_targeted(monkeypatch, [], state, blocks=blocks,
                                zones={block["id"]: "body" for block in blocks})

    assert calls == 0
    assert stub.calls == []


def test_a_filled_field_is_never_re_asked(monkeypatch):
    state = _state({"title": TITLE, "publisher": PRESS, "author": [{"literal": AUTHOR}]})
    calls, stub = _run_targeted(monkeypatch, [
        {"value": {"date-parts": [[2011]]}, "block_id": "p2_b1", "quote": "2011"},
    ], state)

    assert calls == 1
    assert [call["field"] for call in stub.calls] == ["issued"]


def test_priority_order_holds_for_the_two_calls(monkeypatch):
    state = _state({})
    calls, stub = _run_targeted(monkeypatch, [
        {"value": TITLE, "block_id": "p1_b1", "quote": TITLE},
        {"value": PRESS, "block_id": "p2_b1", "quote": PRESS},
    ], state)

    assert calls == 2
    assert [call["field"] for call in stub.calls] == ["title", "publisher"]


def test_a_quote_from_outside_the_owning_zones_is_rejected(monkeypatch):
    blocks = BLOCKS + [{"id": "p3_b1", "text": f"Published by {PRESS}", "physical_page_index": 2}]
    zones = ZONES | {"p3_b1": "body"}
    state = _state(_full_record())
    _run_targeted(monkeypatch,
                  [{"value": PRESS, "block_id": "p3_b1", "quote": PRESS}], state,
                  blocks=blocks, zones=zones)

    assert "publisher" not in state["accepted"]
    assert state["drops"]["publisher"] == {"reason": "wrong_role", "targeted": "rejected"}


def test_an_exception_in_a_targeted_call_leaves_the_record_untouched(monkeypatch):
    state = _state(_full_record())
    calls, _ = _run_targeted(monkeypatch, [RuntimeError("lm down")], state)

    assert calls == 1
    assert "publisher" not in state["accepted"]
    assert state["sources"] == {}
    assert "publisher" not in state["drops"]


# ---------------------------------------------------------------------------
# Inside the extraction pipeline
# ---------------------------------------------------------------------------


def _run_pipeline(monkeypatch, blocks, main_csl, main_evidence, targeted_answers,
                  lm_available=True, total_pages=2):
    targeted = _TargetedStub(targeted_answers)
    main_calls = {"n": 0}
    first = blocks[0]
    quote = first["text"].splitlines()[0]

    def locate_predict(**kwargs):
        return SimpleNamespace(
            document_type="book",
            type_evidence=[{"block_id": first["id"], "quote": quote}],
            selected_pages=[{"page": first["physical_page_index"] + 1, "role": "title",
                             "block_id": first["id"], "quote": quote}])

    def extract_predict(**kwargs):
        main_calls["n"] += 1
        return SimpleNamespace(csl=dict(main_csl), field_evidence=dict(main_evidence))

    def make_predict(signature):
        if signature is dspy_extract.RefineMissingField:
            return targeted(signature)
        if signature is dspy_extract.LocateBibliographicPages:
            return locate_predict
        return extract_predict

    monkeypatch.setattr(dspy_extract, "get_llm_model",
                        lambda *a, **k: object() if lm_available else None)
    monkeypatch.setattr(dspy_extract.dspy, "Predict", make_predict)
    monkeypatch.setattr(dspy_extract.dspy, "context", lambda **kwargs: nullcontext())
    result = dspy_extract._run_dspy_extraction(
        "", "book", source_blocks=blocks, total_pages=total_pages)
    return result, targeted


def _main_evidence():
    return {
        "title": {"block_id": "p1_b1", "quote": TITLE},
        "author": {"block_id": "p1_b2", "quote": AUTHOR},
        "publisher": {"block_id": "p2_b1", "quote": PRESS},
        "issued": {"block_id": "p2_b1", "quote": "2011"},
    }


def test_pipeline_fills_an_empty_publisher_from_the_copyright_zone(monkeypatch):
    blocks = _blocks([(1, TITLE), (1, AUTHOR), (2, f"Copyright © 2011 {PRESS}")])
    result, targeted = _run_pipeline(
        monkeypatch, blocks,
        main_csl={"title": TITLE, "author": [{"literal": AUTHOR}]},
        main_evidence=_main_evidence(),
        targeted_answers=[{"value": PRESS, "block_id": "p2_b1", "quote": PRESS}])

    assert [call["field"] for call in targeted.calls] == ["publisher"]
    assert result["publisher"] == PRESS
    assert result["_field_source"] == {"publisher": "targeted"}
    assert result["_field_confidence"]["publisher"] == 0.95
    assert "publisher" not in result.get("_field_drops", {})
    assert not [entry for entry in result["_review_queue"] if entry["field"] == "publisher"]


def test_pipeline_makes_no_targeted_call_when_every_field_is_present(monkeypatch):
    blocks = _blocks([(1, TITLE), (1, AUTHOR), (2, f"Copyright © 2011 {PRESS}")])
    result, targeted = _run_pipeline(
        monkeypatch, blocks,
        main_csl={"title": TITLE, "author": [{"literal": AUTHOR}],
                  "publisher": PRESS, "issued": {"date-parts": [[2011]]}},
        main_evidence=_main_evidence(),
        targeted_answers=[])

    assert targeted.calls == []
    assert "_field_source" not in result


def test_pipeline_makes_no_targeted_call_without_a_configured_lm(monkeypatch):
    blocks = _blocks([(1, TITLE), (1, AUTHOR), (2, f"Copyright © 2011 {PRESS}")])
    result, targeted = _run_pipeline(
        monkeypatch, blocks,
        main_csl={"title": TITLE, "author": [{"literal": AUTHOR}]},
        main_evidence=_main_evidence(),
        targeted_answers=[],
        lm_available=False)

    assert targeted.calls == []
    assert result["title"] == TITLE
    assert "publisher" not in result


def test_pipeline_returns_the_record_when_the_targeted_call_raises(monkeypatch):
    blocks = _blocks([(1, TITLE), (1, AUTHOR), (2, f"Copyright © 2011 {PRESS}")])
    result, targeted = _run_pipeline(
        monkeypatch, blocks,
        main_csl={"title": TITLE, "author": [{"literal": AUTHOR}]},
        main_evidence=_main_evidence(),
        targeted_answers=[RuntimeError("lm down")])

    assert len(targeted.calls) == 1
    assert result["title"] == TITLE
    assert "publisher" not in result
    assert "_field_source" not in result
