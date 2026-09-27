"""Phase 2 wiring tests: deterministic candidate merge in _run_dspy_extraction.

Window union, evidence-gated CIP/LoC seeding, watermark filtering, and the
seed-vs-LLM conflict audit. The LLM is monkeypatched exactly as in
tests/test_host_metadata_regressions.py.
"""

from types import SimpleNamespace

from citeindex.ingestion.models import IngestionConfig
from citeindex.ingestion.pipelines import dspy_extract


CIP_TEXT = (
    "图书在版编目（CIP）数据\n"
    "视觉SLAM十四讲：从理论到实践 / 高翔等著.—北京：电子工业出版社，2017.3\n"
    "ISBN 978-7-121-31104-8\n"
    "Ⅰ.①视… Ⅱ.①高… Ⅲ.①人工智能－视觉跟踪－研究 Ⅳ.①TP18\n"
    "中国版本图书馆CIP数据核字（2017）第053910号"
)


def _pages(n):
    return [{"id": f"p{page}_b1", "text": f"Body text page {page} filler.",
             "physical_page_index": page - 1} for page in range(1, n + 1)]


def _patch_llm(monkeypatch, csl=None, field_evidence=None, capture=None):
    """Predict returns nothing useful; deterministic seeds must survive."""
    def predict(**kwargs):
        if capture is not None:
            capture.append(kwargs)
        return SimpleNamespace(csl=dict(csl or {}), field_evidence=dict(field_evidence or {}))
    monkeypatch.setattr(dspy_extract, "get_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(dspy_extract.dspy, "Predict", lambda *args: predict)


def _locate_result(document_type, pages):
    """Fake LocateBibliographicPages output via dspy.Predict for locate."""
    return document_type, pages


def test_window_union_adds_cip_page(monkeypatch):
    blocks = _pages(100)
    blocks[49] = {"id": "p50_b1", "text": CIP_TEXT, "physical_page_index": 49}
    seen = []
    def predict(**kwargs):
        seen.extend(kwargs["page_blocks"])
        return SimpleNamespace(
            document_type="book",
            type_evidence=[{"block_id": "p50_b1", "quote": "图书在版编目（CIP）数据"}],
            selected_pages=[{"page": 50, "role": "copyright",
                              "block_id": "p50_b1", "quote": "图书在版编目（CIP）数据"}])
    monkeypatch.setattr(dspy_extract, "get_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(dspy_extract.dspy, "Predict", lambda *args: predict)
    selected, audit = dspy_extract.locate_bibliographic_pages(
        blocks, 100, IngestionConfig(), None, "book")
    assert 50 in audit["search_pages"]
    assert any("图书在版编目" in b["text"] for b in seen)


def test_cip_seeding_with_selection(monkeypatch):
    """Full path: locate selects book; CIP seeds beat LLM garbage."""
    blocks = dspy_extract.build_source_blocks([
        (1, ["视觉SLAM十四讲：从理论到实践", "高翔等著"]), (2, [CIP_TEXT])])
    calls = {"n": 0}

    def locate_predict(**kwargs):
        calls["n"] += 1
        return SimpleNamespace(
            document_type="book",
            type_evidence=[{"block_id": "p2_b1", "quote": "图书在版编目（CIP）数据"}],
            selected_pages=[{"page": 2, "role": "copyright",
                              "block_id": "p2_b1", "quote": "图书在版编目（CIP）数据"}])

    def extract_predict(**kwargs):
        return SimpleNamespace(csl={"title": "LLM Garbage Title"},
                                field_evidence={})

    predict_seq = [locate_predict, extract_predict]
    monkeypatch.setattr(dspy_extract, "get_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(dspy_extract.dspy, "Predict",
                        lambda *args: predict_seq.pop(0))
    result = dspy_extract._run_dspy_extraction(
        "", "book", source_blocks=blocks, total_pages=2)
    assert result.get("title") == "视觉SLAM十四讲"
    assert result.get("publisher") == "电子工业出版社"
    assert result.get("issued") == {"date-parts": [[2017]]}
    assert result.get("author") == [{"literal": "高翔等"}]
    assert result.get("ISBN") in {"9787121311048", "978-7-121-31104-8"}
    assert "_metadata_conflicts" not in result or isinstance(
        result.get("_metadata_conflicts"), list)


def test_chapter_seeds_only_imprint_fields(monkeypatch):
    blocks = dspy_extract.build_source_blocks([
        (1, ["Chapter One", "视觉SLAM十四讲：从理论到实践", "高翔等著"]),
        (2, [CIP_TEXT])])

    def locate_predict(**kwargs):
        return SimpleNamespace(
            document_type="chapter",
            type_evidence=[{"block_id": "p2_b1", "quote": "图书在版编目（CIP）数据"}],
            selected_pages=[
                {"page": 1, "role": "chapter opening",
                 "block_id": "p1_b1", "quote": "Chapter One"},
                {"page": 2, "role": "copyright",
                 "block_id": "p2_b1", "quote": "图书在版编目（CIP）数据"}])

    def extract_predict(**kwargs):
        # LLM proposes the chapter title with real block evidence.
        return SimpleNamespace(
            csl={"title": "Chapter One", "container-title": "视觉SLAM十四讲"},
            field_evidence={"title": {"block_id": "p1_b1", "quote": "Chapter One"},
                            "container-title": {"block_id": "p1_b2", "quote": "视觉SLAM十四讲：从理论到实践"}})

    predict_seq = [locate_predict, extract_predict]
    monkeypatch.setattr(dspy_extract, "get_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(dspy_extract.dspy, "Predict",
                        lambda *args: predict_seq.pop(0))
    result = dspy_extract._run_dspy_extraction(
        "", "chapter", source_blocks=blocks, total_pages=2)
    # Container imprint fields seeded; CIP title/author are NOT the host's.
    assert result.get("publisher") == "电子工业出版社"
    assert result.get("issued") == {"date-parts": [[2017]]}
    assert result.get("title") == "Chapter One"
    assert "author" not in result


def test_watermark_filter_drops_body_repeats(monkeypatch):
    blocks = dspy_extract.build_source_blocks(
        [(1, ["Host Title", "Some author"])] +
        [(p, ["Crack by RAOGY."]) for p in range(2, 6)] +
        [(6, ["A real body paragraph."])])

    def locate_predict(**kwargs):
        return SimpleNamespace(
            document_type="book",
            type_evidence=[{"block_id": "p1_b1", "quote": "Host Title"}],
            selected_pages=[{"page": 1, "role": "title",
                              "block_id": "p1_b1", "quote": "Host Title"}])

    seen_blocks = []

    def extract_predict(**kwargs):
        seen_blocks.extend(kwargs["source_blocks"])
        # LLM proposes the title with real block evidence.
        return SimpleNamespace(csl={"title": "Host Title"},
                               field_evidence={"title": {"block_id": "p1_b1", "quote": "Host Title"}})

    predict_seq = [locate_predict, extract_predict]
    monkeypatch.setattr(dspy_extract, "get_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(dspy_extract.dspy, "Predict",
                        lambda *args: predict_seq.pop(0))
    result = dspy_extract._run_dspy_extraction(
        "", "book", source_blocks=blocks, total_pages=6)
    texts = [b["text"] for b in seen_blocks]
    assert texts, "extractor saw no blocks"
    assert all("Crack by RAOGY." not in t for t in texts)
    assert any("Host Title" in t for t in texts)
    assert result.get("title") == "Host Title"


def test_unanchored_cip_never_seeds(monkeypatch):
    # Main-line shape without 图书在版编目 anchor → confidence 0.9 → no seed.
    unanchored = "某书：副标题 / 张三著.—北京：人民出版社，2015.1"
    blocks = dspy_extract.build_source_blocks([(1, [unanchored, "ISBN 978-7-121-31104-8"])])

    def locate_predict(**kwargs):
        return SimpleNamespace(
            document_type="book",
            type_evidence=[{"block_id": "p1_b1", "quote": unanchored}],
            selected_pages=[{"page": 1, "role": "copyright",
                              "block_id": "p1_b1", "quote": unanchored}])

    def extract_predict(**kwargs):
        return SimpleNamespace(csl={}, field_evidence={})

    predict_seq = [locate_predict, extract_predict]
    monkeypatch.setattr(dspy_extract, "get_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(dspy_extract.dspy, "Predict",
                        lambda *args: predict_seq.pop(0))
    result = dspy_extract._run_dspy_extraction(
        "", "book", source_blocks=blocks, total_pages=1)
    assert "publisher" not in result
    assert "issued" not in result


def test_no_markers_behavior_unchanged(monkeypatch):
    blocks = _pages(20)
    locate_seen = []

    def locate_predict(**kwargs):
        locate_seen.extend(kwargs["page_blocks"])
        return SimpleNamespace(
            document_type="book",
            type_evidence=[{"block_id": "p1_b1", "quote": "Body text page 1 filler."}],
            selected_pages=[{"page": 1, "role": "title",
                              "block_id": "p1_b1", "quote": "Body text page 1 filler."}])

    def extract_predict(**kwargs):
        return SimpleNamespace(csl={}, field_evidence={})

    predict_seq = [locate_predict, extract_predict]
    monkeypatch.setattr(dspy_extract, "get_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(dspy_extract.dspy, "Predict",
                        lambda *args: predict_seq.pop(0))
    result = dspy_extract._run_dspy_extraction(
        "", "book", source_blocks=blocks, total_pages=20)
    assert "_metadata_conflicts" not in result
    assert {b["page"] for b in locate_seen} == {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 18, 19, 20}


def test_seed_conflict_recorded(monkeypatch):
    blocks = dspy_extract.build_source_blocks([
        (1, ["视觉SLAM十四讲：从理论到实践", "高翔等著"]), (2, [CIP_TEXT])])

    def locate_predict(**kwargs):
        return SimpleNamespace(
            document_type="book",
            type_evidence=[{"block_id": "p2_b1", "quote": "图书在版编目（CIP）数据"}],
            selected_pages=[{"page": 2, "role": "copyright",
                              "block_id": "p2_b1", "quote": "图书在版编目（CIP）数据"}])

    def extract_predict(**kwargs):
        return SimpleNamespace(csl={"title": "视觉SLAM十四讲：从理论到实践"},
                               field_evidence={})

    predict_seq = [locate_predict, extract_predict]
    monkeypatch.setattr(dspy_extract, "get_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(dspy_extract.dspy, "Predict",
                        lambda *args: predict_seq.pop(0))
    result = dspy_extract._run_dspy_extraction(
        "", "book", source_blocks=blocks, total_pages=2)
    # LLM agreed on title → equivalent → no conflict for title.
    conflicts = result.get("_metadata_conflicts", [])
    assert not any(c["field"] == "title" for c in conflicts)


def test_two_page_cip_span(monkeypatch):
    """CIP text split across two adjacent pages concatenates before parsing."""
    part1 = "图书在版编目（CIP）数据\n视觉SLAM十四讲：从理论到实践 / 高翔等著"
    part2 = ".—北京：电子工业出版社，2017.3\nISBN 978-7-121-31104-8"
    blocks = dspy_extract.build_source_blocks([(1, [part1]), (2, [part2])])

    def locate_predict(**kwargs):
        return SimpleNamespace(
            document_type="book",
            type_evidence=[{"block_id": "p1_b1", "quote": "图书在版编目（CIP）数据"}],
            selected_pages=[{"page": 1, "role": "copyright",
                              "block_id": "p1_b1", "quote": "图书在版编目（CIP）数据"}])

    def extract_predict(**kwargs):
        return SimpleNamespace(csl={}, field_evidence={})

    predict_seq = [locate_predict, extract_predict]
    monkeypatch.setattr(dspy_extract, "get_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(dspy_extract.dspy, "Predict",
                        lambda *args: predict_seq.pop(0))
    result = dspy_extract._run_dspy_extraction(
        "", "book", source_blocks=blocks, total_pages=2)
    assert result.get("publisher") == "电子工业出版社"
    assert result.get("issued") == {"date-parts": [[2017]]}


# ---------------------------------------------------------------------------
# Two-type taxonomy (2026-09-26): report/manuscript fold into book;
# entry-encyclopedia follows the chapter (article-category) profile.
# ---------------------------------------------------------------------------


def test_report_folds_to_book_in_selection(monkeypatch):
    blocks = dspy_extract.build_source_blocks([(1, ["A Standalone Report"])])

    def locate_predict(**kwargs):
        return SimpleNamespace(
            document_type="report",
            type_evidence=[{"block_id": "p1_b1", "quote": "A Standalone Report"}],
            selected_pages=[{"page": 1, "role": "title",
                             "block_id": "p1_b1", "quote": "A Standalone Report"}])

    def extract_predict(**kwargs):
        return SimpleNamespace(csl={}, field_evidence={})

    predict_seq = [locate_predict, extract_predict]
    monkeypatch.setattr(dspy_extract, "get_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(dspy_extract.dspy, "Predict",
                        lambda *args: predict_seq.pop(0))
    result = dspy_extract._run_dspy_extraction(
        "", "report", source_blocks=blocks, total_pages=1)
    assert result["type"] == "book"


def test_doc_type_mapping_folds_report_manuscript():
    from citeindex.ingestion.pipelines.common import doc_type_to_csl_type
    assert doc_type_to_csl_type("report") == "book"
    assert doc_type_to_csl_type("manuscript") == "book"
    assert doc_type_to_csl_type("thesis") == "thesis"
    assert doc_type_to_csl_type("entry-encyclopedia") == "entry-encyclopedia"


def test_entry_encyclopedia_profile_is_chapter_like():
    from citeindex.ingestion.csl import evaluation_fields
    fields = evaluation_fields({"type": "entry-encyclopedia"}, "digital_pdf")
    assert {"container-title", "page", "publisher", "editor"} <= fields


def test_cip_seeding_entry_encyclopedia_imprint_only(monkeypatch):
    """Entry profile: only container imprint fields seed from the CIP."""
    blocks = dspy_extract.build_source_blocks([
        (1, ["The Entry", "视觉SLAM十四讲：从理论到实践"]), (2, [CIP_TEXT])])

    def locate_predict(**kwargs):
        return SimpleNamespace(
            document_type="entry-encyclopedia",
            type_evidence=[{"block_id": "p2_b1", "quote": "图书在版编目（CIP）数据"}],
            selected_pages=[
                {"page": 1, "role": "entry opening",
                 "block_id": "p1_b1", "quote": "The Entry"},
                {"page": 2, "role": "copyright",
                 "block_id": "p2_b1", "quote": "图书在版编目（CIP）数据"}])

    def extract_predict(**kwargs):
        return SimpleNamespace(csl={"title": "The Entry", "container-title": "视觉SLAM十四讲"},
                               field_evidence={"title": {"block_id": "p1_b1", "quote": "The Entry"}})

    predict_seq = [locate_predict, extract_predict]
    monkeypatch.setattr(dspy_extract, "get_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(dspy_extract.dspy, "Predict",
                        lambda *args: predict_seq.pop(0))
    result = dspy_extract._run_dspy_extraction(
        "", "entry-encyclopedia", source_blocks=blocks, total_pages=2)
    assert result.get("publisher") == "电子工业出版社"
    assert result.get("title") == "The Entry"
    assert "author" not in result  # CIP author describes the host encyclopedia


def test_online_first_published_seeds_year(monkeypatch):
    """'First published online: YYYY' (last page) seeds issued for online entries."""
    blocks = dspy_extract.build_source_blocks([
        (1, ["Brill Encyclopedia of Early Christianity Online", "Cyprian of Carthage"]),
        (14, ["Cite this page", "First published online: 2018"])])

    def locate_predict(**kwargs):
        return SimpleNamespace(
            document_type="entry-encyclopedia",
            type_evidence=[{"block_id": "p1_b2", "quote": "Cyprian of Carthage"}],
            selected_pages=[{"page": 1, "role": "article_header",
                             "block_id": "p1_b2", "quote": "Cyprian of Carthage"}])

    def extract_predict(**kwargs):
        return SimpleNamespace(
            csl={"title": "Cyprian of Carthage",
                 "container-title": "Brill Encyclopedia of Early Christianity Online"},
            field_evidence={"title": {"block_id": "p1_b2", "quote": "Cyprian of Carthage"}})

    predict_seq = [locate_predict, extract_predict]
    monkeypatch.setattr(dspy_extract, "get_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(dspy_extract.dspy, "Predict",
                        lambda *args: predict_seq.pop(0))
    result = dspy_extract._run_dspy_extraction(
        "", "entry-encyclopedia", source_blocks=blocks, total_pages=14)
    assert result.get("issued") == {"date-parts": [[2018]]}
    assert result.get("title") == "Cyprian of Carthage"