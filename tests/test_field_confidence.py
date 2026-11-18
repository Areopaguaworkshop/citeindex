"""Phase 6: per-field confidence and the review queue.

The ladder maps one printed-evidence rung to one number, so a dropped field and
a confidently wrong field stop looking identical. The queue is a report only —
nothing is dropped, repaired or re-extracted from its score. Every test stubs
dspy, so no model call leaves the process.
"""

from types import SimpleNamespace

import pytest

from citeindex.ingestion.models import IngestionConfig
from citeindex.ingestion.pipelines import dspy_extract
from citeindex.ingestion.pipelines.confidence import (
    CONFIDENCE_DEGENERATE_FALLBACK,
    CONFIDENCE_JOINED,
    CONFIDENCE_NON_OWNING_EXACT,
    CONFIDENCE_NORMALISED,
    CONFIDENCE_OWNING_EXACT,
    CONFIDENCE_SEED,
    CONFIDENCE_WEAK_SEED,
    DEFAULT_REVIEW_THRESHOLD,
    build_review_queue,
    confidence_for_evidence,
    field_confidence,
    review_threshold,
    seed_confidence,
    validate_review_threshold,
)

TITLE = "The Book of Evidence"
AUTHOR = "Jane Doe"
PRESS = "Test Press"
BODY = (
    "This is a long body paragraph about the transmission of the text, its "
    "manuscripts, marginalia and the long history of its printed editions, "
    "written to be longer than a title page line so the page is not a title page."
)
CIP_TEXT = (
    "图书在版编目（CIP）数据\n"
    "视觉SLAM十四讲：从理论到实践 / 高翔等著.—北京：电子工业出版社，2017.3\n"
    "ISBN 978-7-121-31104-8"
)
EVIDENCE = {"block_id": "p1_b1", "quote": TITLE,
            "locator": {"physical_page_index": 0, "block_id": "p1_b1"}}


def _blocks(pages):
    """pages: iterable of (page_number, text) tuples, in reading order."""
    blocks, counts = [], {}
    for page, text in pages:
        counts[page] = counts.get(page, 0) + 1
        blocks.append({"id": f"p{page}_b{counts[page]}", "text": text,
                       "physical_page_index": page - 1})
    return blocks


def _locate(document_type="book", pages=None, evidence=None):
    pages = pages or []
    evidence = evidence if evidence is not None else [
        {"block_id": f"p{page}_b1", "quote": quote} for page, quote in pages]
    return SimpleNamespace(
        document_type=document_type,
        type_evidence=evidence,
        selected_pages=[{"page": page, "role": "title", "block_id": f"p{page}_b1",
                         "quote": quote} for page, quote in pages])


def _run_extraction(monkeypatch, blocks, total_pages, locate, extract,
                    config=None, content_list=None):
    """Fake the locator and the extractor; return the extraction result."""
    created = {"n": 0}

    def make_predict(*args):
        created["n"] += 1
        reply = locate if created["n"] == 1 else extract
        return lambda **kwargs: reply

    monkeypatch.setattr(dspy_extract, "get_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(dspy_extract.dspy, "Predict", make_predict)
    return dspy_extract._run_dspy_extraction(
        "", "book", config=config, source_blocks=blocks, total_pages=total_pages,
        content_list=content_list)


# ---------------------------------------------------------------------------
# The ladder: one rung, one documented value
# ---------------------------------------------------------------------------


def test_exact_quote_in_an_owning_zone_scores_095():
    assert field_confidence("exact", EVIDENCE, "cover", field="title") == CONFIDENCE_OWNING_EXACT
    assert field_confidence("exact", EVIDENCE, "statement_of_responsibility",
                            field="title") == CONFIDENCE_OWNING_EXACT
    assert field_confidence("exact", EVIDENCE, "cip", field="publisher") == CONFIDENCE_OWNING_EXACT
    assert field_confidence("exact", EVIDENCE, "imprint", field="publisher") == CONFIDENCE_OWNING_EXACT


def test_quote_matched_after_normalisation_scores_090():
    blocks = [{"id": "p1_b1", "text": TITLE, "physical_page_index": 0}]
    evidence = {"block_id": "p1_b1", "quote": "The  Book  of Evidence"}
    assert field_confidence("normalised", evidence, "title", field="title") == CONFIDENCE_NORMALISED
    confidence, reason = confidence_for_evidence("title", evidence, blocks, {"p1_b1": "title"})
    assert (confidence, reason) == (CONFIDENCE_NORMALISED, "normalised")


def test_quote_assembled_from_two_adjacent_blocks_scores_085():
    blocks = [{"id": "p1_b1", "text": "The Book of", "physical_page_index": 0},
              {"id": "p1_b2", "text": "Evidence", "physical_page_index": 0}]
    evidence = {"spans": [
        {"block_id": "p1_b1", "quote": "The Book of", "locator": {"physical_page_index": 0}},
        {"block_id": "p1_b2", "quote": "Evidence", "locator": {"physical_page_index": 0}}],
        "quote": "The Book of Evidence"}
    assert field_confidence("joined", evidence, "title", field="title") == CONFIDENCE_JOINED
    confidence, reason = confidence_for_evidence("title", evidence, blocks, {"p1_b1": "title",
                                                                             "p1_b2": "title"})
    assert (confidence, reason) == (CONFIDENCE_JOINED, "joined")


def test_exact_quote_in_a_non_owning_zone_scores_080():
    assert field_confidence("exact", EVIDENCE, "imprint",
                            field="title") == CONFIDENCE_NON_OWNING_EXACT
    assert field_confidence("exact", EVIDENCE, "title",
                            field="publisher") == CONFIDENCE_NON_OWNING_EXACT


def test_degenerate_zone_fallback_scores_060():
    assert field_confidence("exact", EVIDENCE, "running_head",
                            field="title") == CONFIDENCE_DEGENERATE_FALLBACK
    assert field_confidence("exact", EVIDENCE, "contents",
                            field="publisher") == CONFIDENCE_DEGENERATE_FALLBACK
    assert field_confidence("degenerate_zone", EVIDENCE, "title",
                            field="title") == CONFIDENCE_DEGENERATE_FALLBACK


def test_weak_seed_scores_050_even_in_an_owning_zone():
    assert field_confidence("weak_marker", EVIDENCE, "imprint",
                            field="publisher") == CONFIDENCE_WEAK_SEED


def test_unknown_reason_or_missing_evidence_raises():
    with pytest.raises(ValueError, match="unknown confidence reason"):
        field_confidence("vibes", EVIDENCE, "title", field="title")
    with pytest.raises(ValueError, match="without evidence"):
        field_confidence("exact", None, "title", field="title")


def test_strong_cip_seed_scores_090_and_unmarked_seed_scores_050():
    assert seed_confidence("publisher", CIP_TEXT, "cip") == (CONFIDENCE_SEED, "seed")
    # A title page heading has no CIP marker but sits in an owning zone.
    assert seed_confidence("title", TITLE, "title") == (CONFIDENCE_SEED, "seed")
    # A heading read out of an unmarked body block is the weak case.
    assert seed_confidence("title", TITLE, "body") == (CONFIDENCE_WEAK_SEED, "weak_marker")
    # A degenerate-zone fallback outranks the weak marker on the ladder.
    assert seed_confidence("title", TITLE, "running_head") == (
        CONFIDENCE_DEGENERATE_FALLBACK, "degenerate_zone")


def test_threshold_defaults_and_accepts_a_config_override():
    assert DEFAULT_REVIEW_THRESHOLD == 0.80
    assert review_threshold(None) == DEFAULT_REVIEW_THRESHOLD
    assert review_threshold(IngestionConfig(evidence_window="range")) == DEFAULT_REVIEW_THRESHOLD
    assert review_threshold(SimpleNamespace(review_threshold=0.55)) == 0.55


@pytest.mark.parametrize("value", [-0.01, 1.01, "high", True, float("nan")])
def test_invalid_threshold_raises_a_clear_error(value):
    with pytest.raises(ValueError, match="review_threshold"):
        validate_review_threshold(value)


@pytest.mark.parametrize("value", [-0.01, 1.01])
def test_pipeline_rejects_an_invalid_configured_threshold(monkeypatch, value):
    config = IngestionConfig(evidence_window="range")
    config.review_threshold = value
    with pytest.raises(ValueError, match="review_threshold"):
        _run_extraction(monkeypatch, _blocks([(1, TITLE)]), 1, _locate(pages=[(1, TITLE)]),
                        SimpleNamespace(csl={}, field_evidence={}), config=config)


# ---------------------------------------------------------------------------
# Queue construction
# ---------------------------------------------------------------------------


def test_queue_is_sorted_by_confidence_then_field():
    queue = build_review_queue(
        {"title": 0.60, "publisher": 0.50, "author": 0.60},
        {"issued": {"reason": "not_found", "quote": None}},
        DEFAULT_REVIEW_THRESHOLD)
    assert [entry["field"] for entry in queue] == ["issued", "publisher", "author", "title"]


def test_threshold_changes_queue_membership():
    confidences = {"title": 0.60, "publisher": 0.90}
    assert [e["field"] for e in build_review_queue(confidences, {}, 0.80)] == ["title"]
    assert build_review_queue(confidences, {}, 0.55) == []


# ---------------------------------------------------------------------------
# The queue and the confidence map inside a real extraction
# ---------------------------------------------------------------------------


def test_high_confidence_record_produces_an_empty_queue(monkeypatch):
    blocks = [{"id": "p1_b1", "text": TITLE, "physical_page_index": 0},
              {"id": "p1_b2", "text": AUTHOR, "physical_page_index": 0},
              {"id": "p2_b1", "text": PRESS, "physical_page_index": 1}]
    result = _run_extraction(
        monkeypatch, blocks, 2, _locate(pages=[(1, TITLE)]),
        SimpleNamespace(csl={"title": TITLE, "author": [{"family": "Doe", "given": "Jane"}]},
                        field_evidence={
                            "title": {"block_id": "p1_b1", "quote": TITLE},
                            "author": {"block_id": "p1_b2", "quote": AUTHOR}}))
    assert result["_review_queue"] == []
    assert set(result["_field_confidence"]) == {"type", "title", "author", "publisher"}
    assert result["_field_confidence"]["title"] == CONFIDENCE_OWNING_EXACT
    assert result["_field_confidence"]["publisher"] == CONFIDENCE_SEED
    assert "publisher" not in result.get("_field_drops", {})


def test_accepted_fields_have_confidence_and_dropped_fields_do_not(monkeypatch):
    blocks = _blocks([(1, TITLE)])
    result = _run_extraction(
        monkeypatch, blocks, 1, _locate(pages=[(1, TITLE)]),
        SimpleNamespace(csl={"title": TITLE, "publisher": "Invented Press"},
                        field_evidence={"title": {"block_id": "p1_b1", "quote": TITLE}}))
    accepted = {key for key in result if not key.startswith("_")}
    assert accepted == {"type", "title"}
    assert set(result["_field_confidence"]) == accepted
    assert "publisher" not in result["_field_confidence"]


def test_review_queue_lists_a_dropped_field_with_its_reason(monkeypatch):
    blocks = _blocks([(1, TITLE)])
    result = _run_extraction(
        monkeypatch, blocks, 1, _locate(pages=[(1, TITLE)]),
        SimpleNamespace(csl={"publisher": PRESS},
                        field_evidence={"publisher": {"block_id": "missing", "quote": PRESS}}))
    entry = next(item for item in result["_review_queue"] if item["field"] == "publisher")
    assert entry["reason"] == "not_found"
    assert entry["confidence"] == 0.0
    assert entry["quote"] == PRESS


def test_review_queue_lists_a_degenerate_zone_fallback(monkeypatch):
    contents = f"Contents\nChapter One ........ 12\nPublished by {PRESS} ........ 3"
    blocks = _blocks([(1, TITLE), (2, contents)])
    result = _run_extraction(
        monkeypatch, blocks, 2, _locate(pages=[(1, TITLE)]),
        SimpleNamespace(csl={"title": TITLE, "publisher": PRESS},
                        field_evidence={
                            "title": {"block_id": "p1_b1", "quote": TITLE},
                            "publisher": {"block_id": "p2_b1", "quote": PRESS}}))
    assert result["_field_confidence"]["publisher"] == CONFIDENCE_DEGENERATE_FALLBACK
    entry = next(item for item in result["_review_queue"] if item["field"] == "publisher")
    assert entry["confidence"] == CONFIDENCE_DEGENERATE_FALLBACK
    assert entry["reason"] == "degenerate_zone"


def test_weak_seed_lands_in_the_queue_and_the_threshold_can_clear_it(monkeypatch):
    blocks = _blocks([(1, TITLE), (1, BODY)])
    content_list = [{"text": TITLE, "page_idx": 0, "text_level": 1},
                    {"text": BODY, "page_idx": 0}]

    def run(config):
        return _run_extraction(
            monkeypatch, blocks, 1, _locate(pages=[(1, TITLE)]),
            SimpleNamespace(csl={}, field_evidence={}),
            config=config, content_list=content_list)

    strict = IngestionConfig(evidence_window="range")
    strict.review_threshold = 0.80
    result = run(strict)
    assert result["title"] == TITLE
    assert result["_field_confidence"]["title"] == CONFIDENCE_WEAK_SEED
    assert [entry["field"] for entry in result["_review_queue"]] == ["title"]
    assert result["_review_queue"][0]["reason"] == "weak_marker"
    assert result["_review_queue"][0]["quote"] == TITLE

    relaxed = IngestionConfig(evidence_window="range")
    relaxed.review_threshold = 0.45
    assert run(relaxed)["_review_queue"] == []
