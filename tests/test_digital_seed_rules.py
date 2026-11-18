"""Digital-path seed rules: unknown page type and block-based year/imprint.

Mirrors ``tests/test_scanned_seed_rules.py``. Two behaviours changed
(2026-10-27):
1. a page selection that reports ``unknown`` must not throw away the front
   matter it did locate — extraction continues with the caller's doc_type and
   no forced type;
2. the year and imprint seeds run whenever a text-block source exists (so the
   digital path, which passes ``content_list=None``, gets them too), and the
   year matcher accepts Chinese imprint year forms. Latin
   ``_COPYRIGHT_YEAR_RE`` / ``_IMPRINT_BLOCK_RE`` behaviour is untouched.
"""

from types import SimpleNamespace

from citeindex.ingestion.pipelines import dspy_extract
from citeindex.ingestion.pipelines.dspy_extract import (
    _COPYRIGHT_YEAR_RE,
    _IMPRINT_BLOCK_RE,
    _cjk_imprint,
    _cjk_year,
    _seed_deterministic_candidates,
)

TITLE = "THE LETTERS OF ST. ANTONY"
BLOCK_TEXT = f"{TITLE}\nSamuel Rubenson"


def _cl(*items):
    """content_list items: (page_idx, text, text_level)."""
    return [{"page_idx": page, "text": text, "text_level": level}
            for page, text, level in items]


def _blocks(content_list):
    return [{"id": f"ocr_{i}", "text": it["text"], "physical_page_index": it["page_idx"],
             "role": it.get("type", "text")}
            for i, it in enumerate(content_list) if it["text"].strip()]


def _seed(content_list, scanned=False, doc_type="book"):
    accepted, evidence = {}, {}
    _seed_deterministic_candidates(
        _blocks(content_list), [], [],
        {"document_type": doc_type, "selected_pages": [1]}, accepted, evidence,
        content_list=content_list if scanned else None)
    return accepted


# ---------------------------------------------------------------------------
# Fix 1: an 'unknown' document type keeps what the locator found
# ---------------------------------------------------------------------------


def _run_pipeline(monkeypatch, selection_type, csl, field_evidence, doc_type="book"):
    """Run the locate+extract cascade with a stubbed LLM; return (result, seen)."""
    seen: dict = {}

    def locate_predict(**kwargs):
        return SimpleNamespace(
            document_type=selection_type,
            type_evidence=[] if selection_type == "unknown"
            else [{"block_id": "b1", "quote": TITLE}],
            selected_pages=[{"page": 1, "role": "title_page",
                             "block_id": "b1", "quote": TITLE}])

    def extract_predict(**kwargs):
        seen["doc_type"] = kwargs.get("doc_type")
        return SimpleNamespace(csl=dict(csl), field_evidence=dict(field_evidence))

    queue = [locate_predict, extract_predict, extract_predict, extract_predict]
    monkeypatch.setattr(dspy_extract, "get_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(dspy_extract.dspy, "Predict", lambda *args: queue.pop(0))
    result = dspy_extract.extract_metadata_with_dspy_priority(
        content_list=[{"page_idx": 0, "text": BLOCK_TEXT, "text_level": None}],
        normalized_markdown=BLOCK_TEXT, doc_type=doc_type,
        source_blocks=[{"id": "b1", "text": BLOCK_TEXT, "physical_page_index": 0}],
        total_pages=1)
    return result, seen


def test_unknown_type_keeps_a_located_title(monkeypatch):
    # The caller used to keep the filename because the early return dropped
    # the extracted title whenever the page locator said 'unknown'.
    result, _ = _run_pipeline(
        monkeypatch, "unknown", {"title": TITLE}, {"title": [{"block_id": "b1", "quote": TITLE}]})
    assert result["title"] == TITLE
    assert "_citation_status" not in result


def test_unknown_type_does_not_force_a_type(monkeypatch):
    result, seen = _run_pipeline(
        monkeypatch, "unknown", {"title": TITLE}, {"title": [{"block_id": "b1", "quote": TITLE}]})
    assert "type" not in {k for k in result if not k.startswith("_")}
    assert result["_bibliographic_selection"]["document_type"] == "unknown"
    # No invented type: the caller's hint (not 'unknown') reaches the prompt.
    assert seen["doc_type"] == "book"


def test_known_type_mapping_still_drives_doc_type(monkeypatch):
    evidence = {"title": [{"block_id": "b1", "quote": TITLE}]}
    for selection_type, expected_doc_type in (("chapter", "bookchapter"),
                                              ("article-journal", "journal"),
                                              ("book", "book")):
        result, seen = _run_pipeline(monkeypatch, selection_type, {"title": TITLE}, evidence)
        assert seen["doc_type"] == expected_doc_type
        assert result["type"] == selection_type


def test_no_located_blocks_still_returns_incomplete(monkeypatch):
    queue = [SimpleNamespace(document_type="unknown", type_evidence=[], selected_pages=[])]
    monkeypatch.setattr(dspy_extract, "get_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(dspy_extract.dspy, "Predict", lambda *args: queue.pop(0))
    result = dspy_extract.extract_metadata_with_dspy_priority(
        content_list=[{"page_idx": 0, "text": BLOCK_TEXT, "text_level": None}],
        normalized_markdown=BLOCK_TEXT, doc_type="book",
        source_blocks=[{"id": "b1", "text": BLOCK_TEXT, "physical_page_index": 0}],
        total_pages=1)
    assert result["_citation_status"] == "incomplete"
    assert "title" not in result


# ---------------------------------------------------------------------------
# Fix 2: Chinese imprint year forms
# ---------------------------------------------------------------------------


def test_cjk_edition_year_forms():
    for text in ("1998年3月第1版", "1998年第1版", "第一版1998"):
        assert _cjk_year(text, "year2") == "1998", text
        assert _seed(_cl((1, text, None)))["issued"] == {"date-parts": [[1998]]}, text


def test_cjk_publication_year_forms():
    for text in ("1998年出版", "出版年份：1998"):
        assert _cjk_year(text, "copyright") == "1998", text
        assert _seed(_cl((1, text, None)))["issued"] == {"date-parts": [[1998]]}, text


def test_cjk_edition_year_beats_publication_year():
    accepted = _seed(_cl((1, "2005年出版", None), (1, "1998年第1版", None)))
    assert accepted["issued"] == {"date-parts": [[1998]]}


def test_cjk_block_without_year_is_ignored():
    accepted = _seed(_cl((2, "使徒教父著作/(古罗马)克莱门等著", None)))
    assert "issued" not in accepted
    assert "publisher" not in accepted
    assert _cjk_year("使徒教父著作", "year2") is None


def test_cjk_block_latin_copyright_year_is_still_ignored():
    # A ©-year inside a CJK block is often a reprint year: only a Chinese
    # year form lifts the CJK skip.
    assert "issued" not in _seed(_cl((1, "版权所有 ©2001 某某出版社", None)))


def test_latin_year_and_imprint_rules_are_unchanged():
    accepted = _seed(_cl((1, "©2001 Abbaye de Bellefontaine", None),
                         (1, "Copyright© 2006 St. Bede's Publications", None)))
    assert accepted["issued"] == {"date-parts": [[2006]]}
    # _COPYRIGHT_YEAR_RE keeps its own branch behaviour on Latin text.
    assert _COPYRIGHT_YEAR_RE.search("Copyright© 2006 St. Bede's Publications").group("year") == "2006"
    assert _COPYRIGHT_YEAR_RE.search("was first published in 1995").group("year2") == "1995"
    assert _COPYRIGHT_YEAR_RE.search("©2001 Abbaye de Bellefontaine").group("year3") == "2001"
    assert _COPYRIGHT_YEAR_RE.search("1998年3月第1版") is None
    # _IMPRINT_BLOCK_RE stays Latin-only.
    assert _IMPRINT_BLOCK_RE.match("1998年3月第1版") is None
    assert _IMPRINT_BLOCK_RE.match("中华书局") is None


# ---------------------------------------------------------------------------
# Fix 2: imprint seeds on both paths (digital passes content_list=None)
# ---------------------------------------------------------------------------


def test_digital_path_seeds_publisher_and_year_from_blocks():
    accepted = _seed(_cl((2, "CISTERCIAN PUBLICATIONS KALAMAZOO, MICHIGAN", None),
                         (2, "Copyright© 2006 St. Bede's Publications", None)))
    assert accepted["publisher"] == "CISTERCIAN PUBLICATIONS"
    assert accepted["publisher-place"] == "KALAMAZOO, MICHIGAN"
    assert accepted["issued"] == {"date-parts": [[2006]]}


def test_cjk_imprint_line_seeds_publisher_and_place():
    assert _cjk_imprint("商务印书馆") == ("商务印书馆", "")
    accepted = _seed(_cl((3, "北京：商务印书馆", None)))
    assert accepted["publisher"] == "商务印书馆"
    assert accepted["publisher-place"] == "北京"


def test_scanned_and_digital_paths_return_the_same_seed_values():
    content = _cl((1, "was first pub  \nlished in 1995. Copyright ©1990, 1995", None),
                  (474, "CISTERCIAN PUBLICATIONS KALAMAZOO, MICHIGAN", None))
    scanned = _seed(content, scanned=True)
    digital = _seed(content, scanned=False)
    assert scanned == digital
    assert scanned["issued"] == {"date-parts": [[1995]]}
    assert scanned["publisher"] == "CISTERCIAN PUBLICATIONS"


def test_non_string_block_text_does_not_crash_the_seeds():
    # Some OCR backends hand back structured text for a block. The year and
    # imprint seeds must skip such a block instead of calling .strip() on it.
    blocks = [
        {"id": "ocr_0", "text": {"content": "1998\u5e743\u6708\u7b2c1\u7248"}, "physical_page_index": 0},
        {"id": "ocr_1", "text": "\u4e2d\u534e\u4e66\u5c40", "physical_page_index": 1},
    ]
    accepted, evidence = {}, {}
    _seed_deterministic_candidates(
        blocks, [], [], {"document_type": "book", "selected_pages": [1]},
        accepted, evidence, content_list=None)
    assert accepted.get("publisher") == "\u4e2d\u534e\u4e66\u5c40"
