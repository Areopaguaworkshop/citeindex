"""Evidence normalisation and the config-owned extraction window.

Change 1: ``locate_bibliographic_pages`` answers only the document type; the
blocks handed to extraction are every block in ``config.page_range``.
Change 2: quotes are normalised before the containment gate, cross-block
quotes must join adjacent blocks on one page, and every rejected field is
recorded in ``_field_drops``.
"""

from types import SimpleNamespace

from citeindex.ingestion.citation_verification import evaluate_block_evidence, validate_block_evidence
from citeindex.ingestion.models import IngestionConfig
from citeindex.ingestion.pipelines import dspy_extract

TITLE = "The Book of Evidence"


def _blocks(pages):
    """pages: iterable of (page_number, text) tuples on distinct pages."""
    return [{"id": f"p{page}_b1", "text": text, "physical_page_index": page - 1}
            for page, text in pages]


def _locate(document_type="book", pages=None, evidence=None):
    pages = pages or []
    evidence = evidence if evidence is not None else [
        {"block_id": f"p{page}_b1", "quote": quote} for page, quote in pages]
    return SimpleNamespace(
        document_type=document_type,
        type_evidence=evidence,
        selected_pages=[{"page": page, "role": "title", "block_id": f"p{page}_b1", "quote": quote}
                        for page, quote in pages])


def _run_extraction(monkeypatch, blocks, total_pages, locate, extract, config=None):
    """Fake the locator and the extractor; return (result, extract_calls)."""
    calls = []

    def locate_predict(**kwargs):
        return locate

    def extract_predict(**kwargs):
        calls.append(kwargs)
        return extract

    queue = [locate_predict, extract_predict, extract_predict, extract_predict]
    monkeypatch.setattr(dspy_extract, "get_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(dspy_extract.dspy, "Predict", lambda *args: queue.pop(0))
    result = dspy_extract._run_dspy_extraction(
        "", "book", config=config, source_blocks=blocks, total_pages=total_pages)
    return result, calls


# ---------------------------------------------------------------------------
# Change 2: evidence normalisation before the gate
# ---------------------------------------------------------------------------


def test_double_space_quote_still_passes():
    blocks = [{"id": "p1_b1", "text": "The Book of Evidence", "physical_page_index": 0}]
    resolved = validate_block_evidence(
        "title", TITLE, {"block_id": "p1_b1", "quote": "The  Book  of Evidence"}, blocks)
    assert resolved
    assert resolved["locator"]["char_start"] == 0


def test_full_width_punctuation_passes():
    blocks = [{"id": "p1_b1", "text": "A Study in Origins！", "physical_page_index": 0}]
    resolved = validate_block_evidence(
        "title", "A Study in Origins!",
        {"block_id": "p1_b1", "quote": "A Study in Origins!"}, blocks)
    assert resolved


def test_em_dash_and_minus_match_hyphen():
    blocks = [{"id": "p1_b1", "text": "Studies 1949—1950 and 3−4", "physical_page_index": 0}]
    assert validate_block_evidence(
        "title", "Studies 1949-1950 and 3-4",
        {"block_id": "p1_b1", "quote": "Studies 1949-1950 and 3-4"}, blocks)
    assert validate_block_evidence(
        "title", "Studies 1949-1950 and 3-4",
        {"block_id": "p1_b1", "quote": "Studies 1949—1950 and 3−4"}, blocks)


def test_curly_quotes_and_soft_hyphens_normalise():
    blocks = [{"id": "p1_b1", "text": "The “Real” Ti\u00adtle", "physical_page_index": 0}]
    resolved = validate_block_evidence(
        "title", 'The "Real" Title', {"block_id": "p1_b1", "quote": 'The "Real" Title'}, blocks)
    assert resolved


def test_two_block_quote_on_one_page_passes():
    blocks = [
        {"id": "p3_b1", "text": "The Book of", "physical_page_index": 2},
        {"id": "p3_b2", "text": "Evidence", "physical_page_index": 2},
    ]
    spans = [{"block_id": "p3_b1", "quote": "The Book of"},
             {"block_id": "p3_b2", "quote": "Evidence"}]
    resolved = validate_block_evidence("title", TITLE, {"spans": spans}, blocks)
    assert resolved and len(resolved["spans"]) == 2


def test_single_span_quote_joined_across_adjacent_blocks_passes():
    blocks = [
        {"id": "p3_b1", "text": "The Book of", "physical_page_index": 2},
        {"id": "p3_b2", "text": "Evidence", "physical_page_index": 2},
    ]
    resolved = validate_block_evidence(
        "title", TITLE, {"block_id": "p3_b1", "quote": TITLE}, blocks)
    assert resolved and len(resolved["spans"]) == 2


def test_two_block_quote_across_pages_fails():
    blocks = [
        {"id": "p3_b1", "text": "The Book of", "physical_page_index": 2},
        {"id": "p4_b1", "text": "Evidence", "physical_page_index": 3},
    ]
    spans = [{"block_id": "p3_b1", "quote": "The Book of"},
             {"block_id": "p4_b1", "quote": "Evidence"}]
    assert validate_block_evidence("title", TITLE, {"spans": spans}, blocks) is None
    assert validate_block_evidence(
        "title", TITLE, {"block_id": "p3_b1", "quote": TITLE}, blocks) is None


def test_rejected_field_records_quote_mismatch_with_quote(monkeypatch):
    blocks = [{"id": "p1_b1", "text": TITLE, "physical_page_index": 0}]
    result, _ = _run_extraction(
        monkeypatch, blocks, 1,
        _locate(pages=[(1, TITLE)]),
        SimpleNamespace(csl={"title": "Invented Title"},
                        field_evidence={"title": {"block_id": "p1_b1", "quote": "Invented quote"}}))
    assert result["_field_drops"]["title"] == {"reason": "quote_mismatch", "quote": "Invented quote"}


def test_wrong_role_recorded_for_platform_publisher(monkeypatch):
    blocks = [{"id": "p1_b1", "text": "Some Press", "physical_page_index": 0,
               "metadata_key": "platform"}]
    result, _ = _run_extraction(
        monkeypatch, blocks, 1,
        _locate(pages=[(1, "Some Press")]),
        SimpleNamespace(csl={"publisher": "Some Press"},
                        field_evidence={"publisher": {"block_id": "p1_b1", "quote": "Some Press"}}))
    assert result["_field_drops"]["publisher"] == {"reason": "wrong_role", "quote": "Some Press"}


def test_every_evaluated_field_has_evidence_or_drop(monkeypatch):
    blocks = [{"id": "p1_b1", "text": TITLE, "physical_page_index": 0}]
    result, _ = _run_extraction(
        monkeypatch, blocks, 1,
        _locate(pages=[(1, TITLE)]),
        SimpleNamespace(
            csl={"title": TITLE, "publisher": "Invented Press",
                 "subtitle": "   ", "issued": {"date-parts": [[1999]]}},
            field_evidence={"title": {"block_id": "p1_b1", "quote": TITLE}}))
    evidence = result.get("_field_evidence", {})
    drops = result.get("_field_drops", {})
    for field in ("title", "publisher", "subtitle", "issued"):
        assert field in evidence or field in drops, field
    assert result["title"] == TITLE
    assert drops["publisher"]["reason"] == "not_found"
    assert drops["subtitle"]["reason"] == "empty"
    assert drops["issued"]["reason"] == "not_found"


def test_drop_reasons_are_closed_set(monkeypatch):
    blocks = [{"id": "p1_b1", "text": TITLE, "physical_page_index": 0}]
    result, _ = _run_extraction(
        monkeypatch, blocks, 1,
        _locate(pages=[(1, TITLE)]),
        SimpleNamespace(csl={"publisher": "Invented Press"}, field_evidence={}))
    _, reason = evaluate_block_evidence("publisher", "Invented Press", None, blocks)
    assert reason == "not_found"
    assert result["_field_drops"]["publisher"]["reason"] in {
        "not_found", "quote_mismatch", "empty", "wrong_role"}


# ---------------------------------------------------------------------------
# Change 1: the extraction window comes from config.page_range
# ---------------------------------------------------------------------------


def test_extraction_sees_block_on_page_locator_did_not_select(monkeypatch):
    blocks = _blocks([(page, f"Page {page} text") for page in range(1, 6)])
    result, calls = _run_extraction(
        monkeypatch, blocks, 5,
        _locate(pages=[(1, "Page 1 text")]),
        SimpleNamespace(csl={"title": "Page 4 text"},
                        field_evidence={"title": {"block_id": "p4_b1", "quote": "Page 4 text"}}))
    assert result["_bibliographic_selection"]["selected_pages"][0]["page"] == 1
    assert any("Page 4 text" in block["text"] for block in calls[0]["source_blocks"])
    assert result["title"] == "Page 4 text"


def test_unknown_type_still_extracts_over_window(monkeypatch):
    blocks = _blocks([(1, TITLE), (2, "A second page of the same book")])
    result, calls = _run_extraction(
        monkeypatch, blocks, 2,
        _locate(document_type="unknown", pages=[]),
        SimpleNamespace(csl={"title": TITLE},
                        field_evidence={"title": {"block_id": "p1_b1", "quote": TITLE}}))
    assert result["_bibliographic_selection"]["document_type"] == "unknown"
    assert result["title"] == TITLE
    assert calls[0]["doc_type"] == "book"
    assert "type" not in {key for key in result if not key.startswith("_")}


def test_locator_failure_still_extracts_over_window(monkeypatch):
    blocks = _blocks([(1, TITLE)])
    raised = []

    def predict(**kwargs):
        if not raised:
            raised.append(True)
            raise RuntimeError("locator unavailable")
        return SimpleNamespace(csl={"title": TITLE},
                               field_evidence={"title": {"block_id": "p1_b1", "quote": TITLE}})

    monkeypatch.setattr(dspy_extract, "get_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(dspy_extract.dspy, "Predict", lambda *args: predict)
    result = dspy_extract._run_dspy_extraction(
        "", "book", source_blocks=blocks, total_pages=1)
    assert result["_bibliographic_selection"] == {"status": "failed"}
    assert result["title"] == TITLE


def test_audit_reports_locator_chosen_pages(monkeypatch):
    blocks = _blocks([(page, f"Page {page} text") for page in range(1, 7)])
    result, calls = _run_extraction(
        monkeypatch, blocks, 6,
        _locate(pages=[(2, "Page 2 text")]),
        SimpleNamespace(csl={}, field_evidence={}))
    chosen = result["_bibliographic_selection"]["selected_pages"]
    assert [item["page"] for item in chosen] == [2]
    assert any("Page 4 text" in block["text"] for block in calls[0]["source_blocks"])


def test_extraction_window_excludes_pages_outside_config(monkeypatch):
    blocks = _blocks([(1, "Page 1 text"), (2, "Page 2 text"), (10, "Page 10 text")])
    result, calls = _run_extraction(
        monkeypatch, blocks, 20,
        _locate(pages=[(1, "Page 1 text")]),
        SimpleNamespace(csl={}, field_evidence={}),
        config=IngestionConfig(evidence_window="range", page_range="1-2"))
    texts = [block["text"] for call in calls for block in call["source_blocks"]]
    assert any("Page 1 text" in text for text in texts)
    assert all("Page 10 text" not in text for text in texts)
    assert result["_bibliographic_selection"]["selected_pages"][0]["page"] == 1
