import pymupdf
import pytest

from citeindex.ingestion.pdf_classifier import (
    DocumentKind,
    PageKind,
    _get_bitmap_coverage,
    classify_pdf,
    pdf_kind,
)


BODY = "The existing text layer contains readable book content and should be reused. " * 4


def make_pdf(path, pages):
    """Build small PDFs with real image objects and visible or hidden text."""
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 100), False)
    pix.clear_with(220)
    with pymupdf.open() as doc:
        for text, image, hidden in pages:
            page = doc.new_page(width=400, height=600)
            if image:
                page.insert_image(page.rect, pixmap=pix, keep_proportion=False)
            if text:
                assert page.insert_textbox(
                    pymupdf.Rect(40, 80, 360, 500), text,
                    fontsize=11, render_mode=3 if hidden else 0,
                ) >= 0
        doc.save(path)
    return str(path)


@pytest.mark.parametrize("hidden", [False, True])
def test_readable_text_over_full_page_image_is_reusable(tmp_path, hidden):
    path = make_pdf(tmp_path / "searchable.pdf", [(BODY, True, hidden)])
    result = classify_pdf(path)
    assert result.document_kind == DocumentKind.DIGITAL_PDF
    assert result.pages[0].kind == PageKind.MIXED
    assert result.pages[0].has_invisible_text == hidden
    assert result.digital_ratio == 1
    assert result.scanned_ratio == 0
    assert pdf_kind(path) == "digital_pdf"


@pytest.mark.parametrize("image_pages, expected", [(9, "digital_pdf"), (10, "digital_pdf"), (11, "scanned_pdf")])
def test_image_only_page_tolerance(tmp_path, image_pages, expected):
    pages = [(BODY, True, True)] * (100 - image_pages) + [("", True, False)] * image_pages
    path = make_pdf(tmp_path / "book.pdf", pages)
    assert pdf_kind(path) == expected


def test_blank_pages_do_not_count_against_text_coverage(tmp_path):
    path = make_pdf(tmp_path / "blank.pdf", [(BODY, False, False)] + [("", False, False)] * 12)
    assert classify_pdf(path).document_kind == DocumentKind.DIGITAL_PDF


@pytest.mark.parametrize("text", ["", "12", "?~!" * 80])
def test_missing_sparse_or_garbled_overlays_still_need_ocr(tmp_path, text):
    path = make_pdf(tmp_path / "scan.pdf", [(text, True, True)])
    assert pdf_kind(path) == "scanned_pdf"


def test_garbled_minority_is_not_treated_as_illustrations(tmp_path):
    path = make_pdf(tmp_path / "bad.pdf", [(BODY, True, True)] * 19 + [("?~!" * 80, True, True)])
    assert pdf_kind(path) == "scanned_pdf"


def test_force_flags_and_explicit_ocr_layer_rejection(tmp_path):
    path = make_pdf(tmp_path / "searchable.pdf", [(BODY, True, True)])
    assert pdf_kind(path, "force_ocr") == "scanned_pdf"
    assert pdf_kind(path, "force_digital") == "digital_pdf"
    assert classify_pdf(path, strip_existing_ocr=True).document_kind == DocumentKind.SCANNED_PDF


def test_image_coverage_is_counted_once_and_clipped():
    pix = pymupdf.Pixmap(pymupdf.csRGB, pymupdf.IRect(0, 0, 100, 100), False)
    pix.clear_with(220)
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=600)
        page.insert_image(pymupdf.Rect(-100, 0, 200, 600), pixmap=pix, keep_proportion=False)
        coverage, images = _get_bitmap_coverage(page)
        assert coverage == pytest.approx(0.5)
        assert images == 1


def test_orchestrator_honors_text_reuse_and_explicit_rejection(tmp_path):
    from citeindex.ingestion.master import CiteIndexIngestionOrchestrator
    from citeindex.ingestion.models import IngestionConfig

    path = make_pdf(tmp_path / "book.pdf", [(BODY, True, True)])
    orchestrator = CiteIndexIngestionOrchestrator(str(tmp_path / "corpus"))
    assert orchestrator.detect_resource_type(path)[0] == "digital_pdf"
    assert orchestrator.detect_resource_type(path, IngestionConfig(strip_existing_ocr=True))[0] == "scanned_pdf"
    assert orchestrator.detect_resource_type(path, IngestionConfig(force_pdf_kind="force_digital", strip_existing_ocr=True))[0] == "digital_pdf"


def test_layout_recovers_text_when_model_returns_empty_boxes(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from citeindex.ingestion.pipelines import layout
    from pymupdf4llm.helpers import document_layout

    path = make_pdf(tmp_path / "book.pdf", [(BODY, True, True), ("", True, False)])

    def empty_layout(doc, **kwargs):
        assert kwargs["use_ocr"] is False
        assert kwargs["force_text"] is True
        return SimpleNamespace(pages=[
            SimpleNamespace(page_number=i + 1, boxes=[], width=p.rect.width, height=p.rect.height)
            for i, p in enumerate(doc)
        ])

    monkeypatch.setattr(document_layout, "parse_document", empty_layout)
    results = layout.analyze_document_layout_pymupdf4llm(path)
    assert len(results) == 2
    assert "readable book content" in results[0]["ordered_text"]
    assert not results[1]["ordered_text"].strip()


def test_layout_recovers_missing_pages(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from citeindex.ingestion.pipelines import layout
    from pymupdf4llm.helpers import document_layout

    path = make_pdf(tmp_path / "book.pdf", [(BODY, True, True), ("", True, False)])
    monkeypatch.setattr(document_layout, "parse_document", lambda *args, **kwargs: SimpleNamespace(pages=[]))
    results = layout.analyze_document_layout_pymupdf4llm(path)
    assert [page["page_number"] for page in results] == [1, 2]
    assert "readable book content" in results[0]["ordered_text"]


def test_searchable_book_digital_pipeline_preserves_text_without_ocr(tmp_path, monkeypatch):
    from citeindex.ingestion.master import CiteIndexIngestionOrchestrator
    from citeindex.ingestion.models import IngestionConfig
    from citeindex.ingestion.pipelines import common, digital_pdf, scanned_pdf
    from pymupdf4llm.helpers import document_layout

    pages = [(f"Chapter {i + 1}. " + BODY, True, True) for i in range(9)]
    path = make_pdf(tmp_path / "book.pdf", pages + [("", True, False)])

    def unexpected_ocr(*args, **kwargs):
        pytest.fail("Digital text reuse must not invoke OCR")

    monkeypatch.setattr(document_layout, "select_ocr_function", unexpected_ocr)
    monkeypatch.setattr(scanned_pdf, "run", unexpected_ocr)
    def unexpected_grobid(*args, **kwargs):
        pytest.fail("DSPy is the default digital-PDF citation engine")

    monkeypatch.setattr(digital_pdf, "_run_grobid", unexpected_grobid)
    monkeypatch.setattr(digital_pdf, "extract_pdf_images", lambda *args: [])
    monkeypatch.setattr(common, "enrich_csl_with_citation_cascade", lambda **kwargs: kwargs["base_csl"])
    orchestrator = CiteIndexIngestionOrchestrator(str(tmp_path / "corpus"))
    cfg = IngestionConfig(use_pageindex=False, doc_type_override="book")
    kind, normalized = orchestrator.detect_resource_type(path, cfg)
    result = orchestrator.route_to_pipeline(kind, normalized, cfg)
    assert result.resource_type == "digital_pdf"
    output_pages = result.document_json["structure"]["pages"]
    assert len(output_pages) == 10
    assert not output_pages[-1]["paragraphs"]
    for page in output_pages[:-1]:
        assert "readable book content" in " ".join(p["text"] for p in page["paragraphs"])
