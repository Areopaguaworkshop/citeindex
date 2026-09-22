import json

import fitz

from citeindex.ingestion.markdown_export import write_library_markdown
from citeindex.ingestion.master import CiteIndexIngestionOrchestrator
from citeindex.ingestion.models import IngestionConfig
from citeindex.ingestion.pipelines import digital_pdf
from citeindex.ingestion.storage import store_corpus_artifacts


def test_layout_pdf_keeps_raw_blocks_for_citation_extraction(tmp_path, monkeypatch):
    pdf_path = tmp_path / "paper.pdf"
    document = fitz.open()
    page = document.new_page()
    page.insert_text((72, 72), "Host Title\\nJane Doe\\n2024")
    document.save(pdf_path)
    document.close()
    captured = {}
    monkeypatch.setattr(digital_pdf, "analyze_document_layout_pymupdf4llm", lambda *_: [{
        "page_number": 1, "columns": [{"paragraphs": [{"text": "Host Title"}]}], "ordered_text": "Host Title",
    }])
    monkeypatch.setattr(digital_pdf, "extract_pdf_images", lambda *_: [])
    monkeypatch.setattr("citeindex.ingestion.pipelines.common.enrich_csl_with_citation_cascade", lambda **kwargs: captured.update(kwargs) or kwargs["base_csl"])

    result = digital_pdf.run(str(pdf_path), config=IngestionConfig(use_pageindex=False))

    assert result.extra["source_blocks"]
    assert "Host Title" in captured["source_blocks"][0]["text"]


def test_content_identity_includes_merkle_root(tmp_path):
    orchestrator = CiteIndexIngestionOrchestrator(str(tmp_path / "corpus"))
    csl = {"title": "Same metadata", "type": "book"}
    first = orchestrator.standardize_csl_json(csl, {"root": "one"}, "digital_pdf")
    second = orchestrator.standardize_csl_json(csl, {"root": "two"}, "digital_pdf")

    assert first["id"] != second["id"]


def test_reingestion_removes_stale_optional_artifacts(tmp_path):
    doc_dir = store_corpus_artifacts(str(tmp_path), "doc", {
        "document_json": {"version": "old"}, "pageindex_tree": {"old": True},
        "citation_verification": {"status": "verified"},
    })
    store_corpus_artifacts(str(tmp_path), "doc", {"document_json": {"version": "new"}})

    assert json.loads((tmp_path / "doc" / "document.json").read_text())["version"] == "new"
    assert not (tmp_path / "doc" / "pageindex_tree.json").exists()
    assert not (tmp_path / "doc" / "citation_verification.json").exists()
    assert doc_dir == str(tmp_path / "doc")


def test_custom_corpus_root_image_paths_are_relative(tmp_path):
    corpus_root = tmp_path / "custom-corpus"
    csl = {"title": "Illustrated", "type": "book", "content_hash": "abc"}
    document = {"structure": {"pages": [{"paragraphs": [{"text": "caption", "type": "image_caption", "image_path": "images/figure.png"}]}]}}

    markdown_path = write_library_markdown(str(corpus_root), csl, document, None, "digital_pdf")

    assert "../custom-corpus/" in (tmp_path / "library" / markdown_path.split("/")[-1]).read_text()


def test_failed_url_batch_is_blocked(tmp_path, monkeypatch):
    import citeindex.ingestion.url_crawler as crawler

    monkeypatch.setattr(crawler, "discover_article_urls", lambda *_args, **_kwargs: ["https://example.org/a"])
    monkeypatch.setattr(CiteIndexIngestionOrchestrator, "ingest", lambda *_args, **_kwargs: {"status": "blocked"})
    summary = CiteIndexIngestionOrchestrator(str(tmp_path / "corpus")).ingest_all_urls("https://example.org")

    assert summary["status"] == "blocked"
    assert summary["failed"] == 1
