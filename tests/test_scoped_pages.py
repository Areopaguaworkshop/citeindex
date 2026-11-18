"""Scoped, file-anchored ``--pages`` ingestion.

Unset ``pages`` must stay byte-identical to the pre-change identity::

    content_hash = hash_payload({"csl", "merkle_root", "source_type"})

With ``pages`` set, only that window is read and the page Merkle root no longer
describes the file, so identity moves to the source digest plus the canonical
window::

    content_hash = hash_payload({"csl", "source_sha256", "page_scope", "source_type"})

Nodes and the Merkle tree are then not computed at all.
"""

import copy
import json
from pathlib import Path

import fitz
import pytest

import citeindex.ingestion.master as master
from citeindex import cli
from citeindex.ingestion.master import (
    CiteIndexIngestionOrchestrator,
    file_sha256,
    resolve_page_scope,
)
from citeindex.ingestion.models import IngestionConfig, PipelineResult
from citeindex.ingestion.pipelines import digital_pdf, mineru, scanned_common

CSL = {
    "type": "book",
    "title": "Scoped Book",
    "author": [{"family": "Doe", "given": "Jane"}],
    "issued": {"date-parts": [[2019]]},
    "publisher": "Test Press",
}

MERKLE_ROOT = "root-hash"

# Hash of the pre-change payload for CSL above:
#   hash_payload({"csl": normalize_cataloguing_fields(CSL),
#                 "merkle_root": "root-hash", "source_type": "digital_pdf"})
# Pinned so any accidental change to the unscoped formula fails loudly: the
# stored gold manifests and every existing csl.json depend on it.
PINNED_UNSCOPED_HASH = "b49b561cc75e391aca6885b67f0508180bd17cfd3157cfc5f1e15222b10b17b1"


def _make_pdf(path: Path, *, pages: int = 12, label: str = "Alpha") -> Path:
    """Write a real PDF so page counting, hashing and extraction all run."""
    doc = fitz.open()
    for number in range(1, pages + 1):
        page = doc.new_page()
        page.insert_text((72, 72), f"{label} page {number}")
    doc.save(str(path))
    doc.close()
    return path


def _pipeline_result(*, merkle_tree=None, extra=None) -> PipelineResult:
    return PipelineResult(
        status="ok",
        source_id="source",
        resource_type="digital_pdf",
        csl_json=copy.deepcopy(CSL),
        document_json={"metadata": {"title": "Scoped Book"}, "structure": {"pages": []}},
        merkle_tree=merkle_tree,
        extra=copy.deepcopy(extra or {}),
    )


def _ingest(
    monkeypatch,
    tmp_path,
    *,
    source: Path,
    pages: str | None,
    corpus: str,
    output_mode: str = "csl_only",
    merkle_tree=None,
    extra=None,
    resource_type: str = "digital_pdf",
):
    orchestrator = CiteIndexIngestionOrchestrator(str(tmp_path / corpus))
    monkeypatch.setattr(
        orchestrator,
        "detect_resource_type",
        lambda *_a, **_k: (resource_type, str(source)),
    )
    monkeypatch.setattr(
        orchestrator,
        "route_to_pipeline",
        lambda *_a, **_k: _pipeline_result(merkle_tree=merkle_tree, extra=extra),
    )
    monkeypatch.setattr(CiteIndexIngestionOrchestrator, "_layout_features", lambda *_a, **_k: None)
    monkeypatch.setattr(master, "write_library_markdown", lambda **_k: "unused.md")
    config = IngestionConfig(pages=pages, output_mode=output_mode)
    output = orchestrator.ingest(str(source), config)
    assert output["status"] == "ok", output
    return output


def _names(document_path: str) -> set[str]:
    return {entry.name for entry in Path(document_path).iterdir()}


# ---------------------------------------------------------------------------
# Identity: unset ``pages`` is byte-identical to today
# ---------------------------------------------------------------------------


def test_unset_pages_keeps_the_pinned_pre_change_hash(monkeypatch, tmp_path):
    source = _make_pdf(tmp_path / "book.pdf")
    output = _ingest(
        monkeypatch,
        tmp_path,
        source=source,
        pages=None,
        corpus="plain",
        merkle_tree={"root": MERKLE_ROOT},
    )
    csl = json.loads(Path(output["document_path"], "csl.json").read_text(encoding="utf-8"))

    assert csl["content_hash"] == PINNED_UNSCOPED_HASH
    assert csl["id"] == PINNED_UNSCOPED_HASH[:16]
    assert csl["merkle_root"] == MERKLE_ROOT
    # The file-anchored fields belong to the scoped mode only.
    assert "source_sha256" not in csl
    assert "page_scope" not in csl


def test_standardize_keeps_the_old_payload_when_pages_are_unset(tmp_path):
    orchestrator = CiteIndexIngestionOrchestrator(str(tmp_path / "corpus"))

    standardized = orchestrator.standardize_csl_json(copy.deepcopy(CSL), {"root": MERKLE_ROOT}, "digital_pdf")

    assert standardized["content_hash"] == PINNED_UNSCOPED_HASH
    assert "source_sha256" not in standardized
    assert "page_scope" not in standardized


# ---------------------------------------------------------------------------
# Identity: scoped runs
# ---------------------------------------------------------------------------


def test_scoped_hash_covers_source_digest_and_page_scope(monkeypatch, tmp_path):
    from citeindex.ingestion.csl import normalize_cataloguing_fields
    from citeindex.ingestion.deterministic import hash_payload

    source = _make_pdf(tmp_path / "book.pdf")
    output = _ingest(monkeypatch, tmp_path, source=source, pages="1-6", corpus="scoped")
    csl = json.loads(Path(output["document_path"], "csl.json").read_text(encoding="utf-8"))

    expected = hash_payload(
        {
            "csl": normalize_cataloguing_fields(copy.deepcopy(CSL)),
            "source_sha256": file_sha256(str(source)),
            "page_scope": "1-6",
            "source_type": "digital_pdf",
        }
    )
    assert csl["content_hash"] == expected
    assert csl["source_sha256"] == file_sha256(str(source))
    assert csl["page_scope"] == "1-6"
    assert len(csl["id"]) == 16
    int(csl["id"], 16)


def test_same_file_and_scope_produce_the_same_id(monkeypatch, tmp_path):
    source = _make_pdf(tmp_path / "book.pdf")
    first = _ingest(monkeypatch, tmp_path, source=source, pages="1-6", corpus="run-a")
    second = _ingest(monkeypatch, tmp_path, source=source, pages="1-6", corpus="run-b")

    assert first["standardized_csl_json"]["id"] == second["standardized_csl_json"]["id"]
    assert first["standardized_csl_json"]["content_hash"] == second["standardized_csl_json"]["content_hash"]


def test_different_scope_changes_the_id(monkeypatch, tmp_path):
    source = _make_pdf(tmp_path / "book.pdf")
    first = _ingest(monkeypatch, tmp_path, source=source, pages="1-6", corpus="six")
    second = _ingest(monkeypatch, tmp_path, source=source, pages="1-10", corpus="ten")

    assert first["standardized_csl_json"]["id"] != second["standardized_csl_json"]["id"]
    assert first["standardized_csl_json"]["source_sha256"] == second["standardized_csl_json"]["source_sha256"]
    assert second["standardized_csl_json"]["page_scope"] == "1-10"


def test_changed_file_bytes_change_the_id(monkeypatch, tmp_path):
    original = _make_pdf(tmp_path / "book.pdf", label="Alpha")
    revised = _make_pdf(tmp_path / "book-revised.pdf", label="Beta")

    first = _ingest(monkeypatch, tmp_path, source=original, pages="1-6", corpus="original")
    second = _ingest(monkeypatch, tmp_path, source=revised, pages="1-6", corpus="revised")

    assert first["standardized_csl_json"]["page_scope"] == second["standardized_csl_json"]["page_scope"] == "1-6"
    assert first["standardized_csl_json"]["source_sha256"] != second["standardized_csl_json"]["source_sha256"]
    assert first["standardized_csl_json"]["id"] != second["standardized_csl_json"]["id"]


def test_page_scope_is_canonical_and_clamped(tmp_path):
    source = _make_pdf(tmp_path / "book.pdf", pages=8)

    assert resolve_page_scope(str(source), "1-6") == "1-6"
    assert resolve_page_scope(str(source), "3,1-2") == "1-3"
    assert resolve_page_scope(str(source), "1-99") == "1-8"
    assert resolve_page_scope(str(source), "-3") == "6-8"


# ---------------------------------------------------------------------------
# Scoped runs carry no Merkle proof, no tree, no PageIndex
# ---------------------------------------------------------------------------


def test_scoped_record_has_null_merkle_root_and_no_merkle_json(monkeypatch, tmp_path):
    source = _make_pdf(tmp_path / "book.pdf")
    output = _ingest(
        monkeypatch,
        tmp_path,
        source=source,
        pages="1-6",
        corpus="no-merkle",
        merkle_tree={"root": MERKLE_ROOT},
    )
    document = Path(output["document_path"])
    csl = json.loads((document / "csl.json").read_text(encoding="utf-8"))

    assert output["standardized_csl_json"]["merkle_root"] is None
    assert csl["merkle_root"] is None
    assert not (document / "merkle.json").exists()


def test_scoped_run_writes_no_pageindex_tree(monkeypatch, tmp_path):
    seen: dict[str, bool] = {}

    def spy(_resource_type, _normalized, config):
        seen["use_pageindex"] = config.use_pageindex
        return PipelineResult(
            status="ok",
            source_id="source",
            resource_type="digital_pdf",
            csl_json=copy.deepcopy(CSL),
            document_json={"metadata": {"title": "Scoped Book"}, "structure": {"pages": []}},
            merkle_tree={"root": MERKLE_ROOT},
            extra={"pageindex_tree": {"level_1": []}},
        )

    source = _make_pdf(tmp_path / "book.pdf")
    orchestrator = CiteIndexIngestionOrchestrator(str(tmp_path / "corpus"))
    monkeypatch.setattr(orchestrator, "detect_resource_type", lambda *_a, **_k: ("digital_pdf", str(source)))
    monkeypatch.setattr(orchestrator, "route_to_pipeline", spy)
    monkeypatch.setattr(CiteIndexIngestionOrchestrator, "_layout_features", lambda *_a, **_k: None)
    monkeypatch.setattr(master, "write_library_markdown", lambda **_k: "unused.md")

    config = IngestionConfig(pages="1-6", output_mode="full", use_pageindex=True)
    output = orchestrator.ingest(str(source), config)

    assert output["status"] == "ok", output
    # The stage is switched off for routing, and the caller's config is intact.
    assert seen["use_pageindex"] is False
    assert config.use_pageindex is True
    assert not Path(output["document_path"], "pageindex_tree.json").exists()


# ---------------------------------------------------------------------------
# Lane wiring: digital extraction window, MinerU page range
# ---------------------------------------------------------------------------


def test_digital_extraction_reads_only_the_scoped_pages(monkeypatch, tmp_path):
    source = _make_pdf(tmp_path / "book.pdf", pages=12)
    calls: list[list[int] | None] = []
    original = digital_pdf._extract_pages

    def spy(pdf_path, pages=None):
        calls.append(pages)
        return original(pdf_path, pages)

    monkeypatch.setattr(digital_pdf, "_extract_pages", spy)
    monkeypatch.setattr(
        "citeindex.ingestion.pipelines.common.enrich_csl_with_citation_cascade",
        lambda **_kwargs: {},
    )

    result = digital_pdf.run(
        str(source),
        config=IngestionConfig(pages="1-6", use_layout_analysis=False, use_pageindex=False),
    )

    assert calls == [[1, 2, 3, 4, 5, 6]]
    pages = result.document_json["structure"]["pages"]
    assert [page["page_number"] for page in pages] == [1, 2, 3, 4, 5, 6]
    # Physical numbering is preserved: page 1 of the artifact is PDF page 1.
    assert pages[0]["physical_page_index"] == 0
    assert "Alpha page 1" in json.dumps(pages[0])
    assert "Alpha page 7" not in json.dumps(result.document_json)
    # Nodes and Merkle exist only to feed the root, so neither is built.
    assert result.document_json["nodes"] == []
    assert result.merkle_tree is None


def test_scanned_lane_forwards_the_range_to_mineru(monkeypatch, tmp_path):
    source = _make_pdf(tmp_path / "scan.pdf", pages=12)
    captured: dict[str, object] = {}

    def fake_chunked(pdf_path, **kwargs):
        captured["pdf_path"] = pdf_path
        captured["kwargs"] = kwargs
        return {"content_list": [{"type": "text", "text": "Alpha page 1", "page_idx": 0}]}

    monkeypatch.delenv("CITEINDEX_BENCHMARK_SCAN_PAGES", raising=False)
    monkeypatch.setattr(mineru, "run_mineru_chunked", fake_chunked)
    monkeypatch.setattr(mineru, "extract_pdf_images", lambda *_a, **_k: [])
    monkeypatch.setattr(
        scanned_common,
        "build_scanned_pipeline_result",
        lambda **kwargs: PipelineResult(
            status="ok",
            source_id="source",
            resource_type="scanned_pdf",
            csl_json=copy.deepcopy(CSL),
            merkle_tree=None,
            extra={},
        ),
    )

    config = IngestionConfig(pages="1-6", output_mode="csl_only")
    mineru.run(str(source), "scanned_pdf", config)

    assert captured["pdf_path"] == str(source)
    assert captured["kwargs"]["benchmark_page_range"] == "1-6"


def test_scanned_lane_keeps_the_benchmark_env_switch(monkeypatch, tmp_path):
    source = _make_pdf(tmp_path / "scan.pdf", pages=12)
    captured: dict[str, object] = {}

    def fake_chunked(_pdf_path, **kwargs):
        captured["kwargs"] = kwargs
        return {"content_list": [{"type": "text", "text": "Alpha page 1", "page_idx": 0}]}

    monkeypatch.setenv("CITEINDEX_BENCHMARK_SCAN_PAGES", "1-5, -3")
    monkeypatch.setattr(mineru, "run_mineru_chunked", fake_chunked)
    monkeypatch.setattr(mineru, "extract_pdf_images", lambda *_a, **_k: [])
    monkeypatch.setattr(
        scanned_common,
        "build_scanned_pipeline_result",
        lambda **kwargs: PipelineResult(
            status="ok",
            source_id="source",
            resource_type="scanned_pdf",
            csl_json=copy.deepcopy(CSL),
            merkle_tree=None,
            extra={},
        ),
    )

    mineru.run(str(source), "scanned_pdf", IngestionConfig(output_mode="csl_only"))

    assert captured["kwargs"]["benchmark_page_range"] == "1-5, -3"
    assert captured["kwargs"]["benchmark_sample_pages"] is False


# ---------------------------------------------------------------------------
# Validation and CLI
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["", "   ", "abc", "6-1", "0", "1-6, foo", None])
def test_invalid_pages_are_rejected_at_config_construction(bad):
    if bad is None:
        assert IngestionConfig().pages is None
        return
    with pytest.raises(ValueError, match="pages"):
        IngestionConfig(pages=bad)


def test_cli_passes_pages_through(monkeypatch):
    captured = {}

    class Orchestrator:
        def __init__(self, **kwargs):
            captured["orchestrator"] = kwargs

        def ingest(self, input_ref, *, config):
            captured["config"] = config
            return {"status": "ok"}

    monkeypatch.setattr(cli, "CiteIndexIngestionOrchestrator", Orchestrator)
    monkeypatch.setattr("sys.argv", ["citeindex", "source.pdf", "--pages", "1-6"])
    with pytest.raises(SystemExit, match="0"):
        cli.main()

    assert captured["config"].pages == "1-6"


# ---------------------------------------------------------------------------
# Artifact sets: csl_only drops everything, full keeps the rest but no proof
# ---------------------------------------------------------------------------


def test_csl_only_with_pages_writes_only_the_csl_exports_and_summary(monkeypatch, tmp_path):
    source = _make_pdf(tmp_path / "book.pdf")
    output = _ingest(
        monkeypatch,
        tmp_path,
        source=source,
        pages="1-6",
        corpus="csl-only",
        output_mode="csl_only",
        extra={"source_blocks": [{"id": "block-1", "text": "Alpha page 1"}]},
    )
    document = Path(output["document_path"])

    # Provenance (the enrichment decision record) survives every mode.
    assert _names(str(document)) == {
        "csl.json",
        "csl-export.json",
        "ingestion_output.json",
        "online_enrichment.json",
    }
    assert not (tmp_path / "library").exists()


def test_full_with_pages_keeps_other_artifacts_with_a_null_merkle_root(monkeypatch, tmp_path):
    source = _make_pdf(tmp_path / "book.pdf")
    output = _ingest(
        monkeypatch,
        tmp_path,
        source=source,
        pages="1-6",
        corpus="full",
        output_mode="full",
        merkle_tree={"root": MERKLE_ROOT},
        extra={
            "source_blocks": [{"id": "block-1", "text": "Alpha page 1"}],
            "pageindex_tree": {"level_1": []},
        },
    )
    document = Path(output["document_path"])
    names = _names(str(document))

    assert {"csl.json", "csl-export.json", "ingestion_output.json", "document.json", "source_blocks.json"} <= names
    assert not names & {"merkle.json", "pageindex_tree.json"}
    stored = json.loads((document / "csl.json").read_text(encoding="utf-8"))
    assert stored["merkle_root"] is None
    assert stored["page_scope"] == "1-6"


# ---------------------------------------------------------------------------
# URL and media lanes ignore the option
# ---------------------------------------------------------------------------


def test_url_lane_ignores_pages(monkeypatch, tmp_path):
    from citeindex.ingestion.csl import normalize_cataloguing_fields
    from citeindex.ingestion.deterministic import hash_payload

    source = tmp_path / "article.pdf"
    source.write_bytes(b"not a pdf")
    output = _ingest(
        monkeypatch,
        tmp_path,
        source=source,
        pages="1-6",
        corpus="url",
        merkle_tree={"root": MERKLE_ROOT},
        resource_type="url_article",
    )
    csl = output["standardized_csl_json"]

    # No page window was resolved, so identity stays the pre-change formula.
    expected = hash_payload(
        {
            "csl": normalize_cataloguing_fields(copy.deepcopy(CSL)),
            "merkle_root": MERKLE_ROOT,
            "source_type": "url_article",
        }
    )
    assert csl["content_hash"] == expected
    assert csl["merkle_root"] == MERKLE_ROOT
    assert "source_sha256" not in csl
    assert "page_scope" not in csl
