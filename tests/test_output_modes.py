"""The three output modes: full, csl_evidence, csl_only.

The contract is the artifact matrix in ``docs/2026-10-28-csl-only-plan.html``:
a reduced mode skips persistence (and the optional stages that only feed it),
never the CSL identity.  ``csl.json`` carries ``id``, ``content_hash`` and
``merkle_root``, so the Merkle tree and nodes are still computed.
"""

import copy
import json
import sys
from pathlib import Path

import pytest

import citeindex.ingestion.master as master
from citeindex import cli, plugins
from citeindex.ingestion.master import CiteIndexIngestionOrchestrator, apply_output_mode
from citeindex.ingestion.models import OUTPUT_MODES, IngestionConfig, PipelineResult
from citeindex.ingestion.storage import store_corpus_artifacts

CSL = {
    "id": "0123456789abcdef",
    "type": "book",
    "title": "Test Book",
    "author": [{"family": "Doe", "given": "Jane"}],
    "issued": {"date-parts": [[2019]]},
    "publisher": "Test Press",
    "content_hash": "62727a86de5843bf247d128ce7d04b40af6a0bf87724ed232e63a43e52fdab52",
    "merkle_root": "1f02086362d0fecd732dab163392730d10702d4886fbe9dd17a9574148ef9493",
}

LAYOUT = {"pages": [{"page_number": 1, "margins": {}}]}

# Artifact payload keys each mode refuses to persist.
REMOVED = {
    "full": frozenset(),
    "csl_evidence": frozenset({"document_json", "merkle_tree", "pageindex_tree"}),
    "csl_only": frozenset(
        {"document_json", "merkle_tree", "pageindex_tree", "source_blocks", "layout_features"}
    ),
}

REDUCED_MODES = ("csl_evidence", "csl_only")


def _artifacts() -> dict:
    return {
        "csl_json": copy.deepcopy(CSL),
        "document_json": {"structure": {"pages": []}},
        "merkle_tree": {"root": "root-hash", "nodes": [{"id": "node-1"}]},
        "pageindex_tree": {"level_1": []},
        "source_blocks": [{"id": "block-1", "text": "Test Book", "char_start": 0}],
        "layout_features": copy.deepcopy(LAYOUT),
    }


def _pipeline_result(extra: dict | None = None) -> PipelineResult:
    return PipelineResult(
        status="ok",
        source_id="source",
        resource_type="digital_pdf",
        csl_json=copy.deepcopy(CSL),
        document_json={"metadata": {"title": "Test Book"}, "structure": {"pages": []}},
        merkle_tree={"root": "root-hash", "nodes": [{"id": "node-1"}]},
        extra=copy.deepcopy(extra or {}),
    )


def _run(monkeypatch, tmp_path, mode: str, *, extra: dict | None = None, config=None) -> dict:
    """Ingest with a stubbed pipeline, so only master's routing is exercised."""
    path = tmp_path / "source.pdf"
    path.write_bytes(b"%PDF-1.4\n% stub\n")
    orchestrator = CiteIndexIngestionOrchestrator(str(tmp_path / "corpus"))
    monkeypatch.setattr(orchestrator, "detect_resource_type", lambda *_a, **_k: ("digital_pdf", str(path)))
    monkeypatch.setattr(orchestrator, "route_to_pipeline", lambda *_a, **_k: _pipeline_result(extra))
    monkeypatch.setattr(
        CiteIndexIngestionOrchestrator,
        "_layout_features",
        lambda *_a, **_k: copy.deepcopy(LAYOUT),
    )
    cfg = config if config is not None else IngestionConfig(output_mode=mode)
    output = orchestrator.ingest(str(path), cfg)
    assert output["status"] == "ok", output
    return output


def _names(document_path: str) -> set[str]:
    return {entry.name for entry in Path(document_path).iterdir()}


# ---------------------------------------------------------------------------
# apply_output_mode: the pure helper
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", OUTPUT_MODES)
def test_apply_output_mode_returns_exactly_the_mode_key_set(mode):
    artifacts = _artifacts()
    applied = apply_output_mode(artifacts, mode)

    assert set(applied) == set(artifacts) - REMOVED[mode]
    assert set(applied) <= set(artifacts)
    # The input payload is never mutated: the mode is a projection, not an edit.
    assert set(artifacts) == set(_artifacts())


@pytest.mark.parametrize("mode", OUTPUT_MODES)
def test_csl_json_survives_untouched_in_every_mode(mode):
    artifacts = _artifacts()
    applied = apply_output_mode(artifacts, mode)

    assert applied["csl_json"] == CSL
    assert applied["csl_json"] is artifacts["csl_json"]
    # Identity fields are computed upstream; no mode may strip or rewrite them.
    assert applied["csl_json"]["id"] == CSL["id"]
    assert applied["csl_json"]["content_hash"] == CSL["content_hash"]
    assert applied["csl_json"]["merkle_root"] == CSL["merkle_root"]


@pytest.mark.parametrize("bad", ["", "csl", "FULL", "evidence", None])
def test_invalid_output_mode_is_rejected_with_a_clear_message(bad):
    with pytest.raises(ValueError, match="output_mode"):
        IngestionConfig(output_mode=bad)
    with pytest.raises(ValueError, match="output_mode"):
        apply_output_mode(_artifacts(), bad)


def test_default_output_mode_is_full():
    assert IngestionConfig().output_mode == "full"


# ---------------------------------------------------------------------------
# CLI wiring
# ---------------------------------------------------------------------------


def _cli_config(monkeypatch, argv: list[str]) -> IngestionConfig:
    captured = {}

    class Orchestrator:
        def __init__(self, **kwargs):
            captured["orchestrator"] = kwargs

        def ingest(self, input_ref, *, config):
            captured["config"] = config
            return {"status": "ok"}

    monkeypatch.setattr(cli, "CiteIndexIngestionOrchestrator", Orchestrator)
    monkeypatch.setattr(sys, "argv", ["citeindex", "source.pdf", *argv])
    with pytest.raises(SystemExit, match="0"):
        cli.main()
    return captured["config"]


def test_cli_default_output_mode_is_full(monkeypatch):
    assert _cli_config(monkeypatch, []).output_mode == "full"


@pytest.mark.parametrize("mode", OUTPUT_MODES)
def test_cli_parses_each_output_mode(monkeypatch, mode):
    assert _cli_config(monkeypatch, ["--output-mode", mode]).output_mode == mode


# ---------------------------------------------------------------------------
# Orchestrator: what lands on disk per mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", OUTPUT_MODES)
def test_ingestion_output_is_written_in_every_mode(tmp_path, monkeypatch, mode):
    output = _run(monkeypatch, tmp_path, mode)
    document = Path(output["document_path"])

    assert (document / "csl.json").is_file()
    assert (document / "csl-export.json").is_file()
    ingestion_output = document / "ingestion_output.json"
    assert ingestion_output.is_file()
    assert json.loads(ingestion_output.read_text(encoding="utf-8"))["status"] == "ok"


def test_csl_evidence_writes_csl_blocks_and_layout(tmp_path, monkeypatch):
    output = _run(monkeypatch, tmp_path, "csl_evidence", extra={"source_blocks": _artifacts()["source_blocks"]})
    names = _names(output["document_path"])

    assert {"csl.json", "csl-export.json", "ingestion_output.json", "source_blocks.json", "layout.json"} <= names
    assert not names & {"document.json", "merkle.json", "pageindex_tree.json", "images"}
    assert not (tmp_path / "library").exists()


def test_csl_only_writes_only_csl_exports_and_the_run_summary(tmp_path, monkeypatch):
    output = _run(
        monkeypatch,
        tmp_path,
        "csl_only",
        extra={"source_blocks": _artifacts()["source_blocks"]},
    )
    names = _names(output["document_path"])

    assert {"csl.json", "csl-export.json", "ingestion_output.json"} <= names
    assert not names & {
        "document.json",
        "merkle.json",
        "pageindex_tree.json",
        "source_blocks.json",
        "layout.json",
        "images",
    }
    assert not (tmp_path / "library").exists()
    # The Merkle tree is still computed; only merkle.json is not written.
    stored_csl = json.loads((Path(output["document_path"]) / "csl.json").read_text(encoding="utf-8"))
    assert stored_csl["merkle_root"] == "root-hash"
    assert stored_csl["content_hash"]


# ---------------------------------------------------------------------------
# Byte equality of csl.json
# ---------------------------------------------------------------------------


def test_csl_json_bytes_are_identical_under_all_three_modes(tmp_path):
    written = {}
    for mode in OUTPUT_MODES:
        corpus = tmp_path / mode / "corpus"
        corpus.mkdir(parents=True)
        artifacts = apply_output_mode(_artifacts(), mode)
        document = Path(store_corpus_artifacts(str(corpus), "same_doc", artifacts))
        written[mode] = (document / "csl.json").read_bytes()

    assert len(set(written.values())) == 1, {mode: len(value) for mode, value in written.items()}


# ---------------------------------------------------------------------------
# Skipped work in reduced modes
# ---------------------------------------------------------------------------


def test_pageindex_is_not_computed_in_reduced_modes(tmp_path, monkeypatch):
    tree_calls: list[str] = []
    monkeypatch.setattr(
        "citeindex.ingestion.pipelines.pageindex_tree.run_pageindex_tree",
        lambda *_a, **_k: tree_calls.append("pdf"),
    )
    monkeypatch.setattr(
        "citeindex.ingestion.pipelines.pageindex_tree.run_pageindex_md_tree",
        lambda *_a, **_k: tree_calls.append("markdown"),
    )
    seen: dict[str, bool] = {}

    def spy(_self, _resource_type, _normalized, config):
        seen[config.output_mode] = config.use_pageindex
        return _pipeline_result()

    path = tmp_path / "source.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(CiteIndexIngestionOrchestrator, "_layout_features", lambda *_a, **_k: copy.deepcopy(LAYOUT))
    monkeypatch.setattr(CiteIndexIngestionOrchestrator, "detect_resource_type", lambda *_a, **_k: ("digital_pdf", str(path)))
    monkeypatch.setattr(CiteIndexIngestionOrchestrator, "route_to_pipeline", spy)

    caller_configs = {}
    for mode in REDUCED_MODES:
        caller_configs[mode] = IngestionConfig(output_mode=mode, use_pageindex=True)
        orchestrator = CiteIndexIngestionOrchestrator(str(tmp_path / mode / "corpus"))
        output = orchestrator.ingest(str(path), caller_configs[mode])
        assert output["status"] == "ok", output
        assert not Path(output["document_path"], "pageindex_tree.json").exists()

    for mode in REDUCED_MODES:
        assert seen[mode] is False
        # The caller's config is not mutated; routing gets a replaced copy.
        assert caller_configs[mode].use_pageindex is True
    assert tree_calls == []


def test_pageindex_still_runs_in_full_mode(tmp_path, monkeypatch):
    seen: dict[str, bool] = {}

    def spy(_resource_type, _normalized, config):
        seen[config.output_mode] = config.use_pageindex
        return _pipeline_result()

    path = tmp_path / "source.pdf"
    path.write_bytes(b"%PDF-1.4\n")
    orchestrator = CiteIndexIngestionOrchestrator(str(tmp_path / "corpus"))
    monkeypatch.setattr(orchestrator, "detect_resource_type", lambda *_a, **_k: ("digital_pdf", str(path)))
    monkeypatch.setattr(orchestrator, "route_to_pipeline", spy)
    monkeypatch.setattr(CiteIndexIngestionOrchestrator, "_layout_features", lambda *_a, **_k: copy.deepcopy(LAYOUT))

    assert orchestrator.ingest(str(path), IngestionConfig()).get("status") == "ok"
    assert seen["full"] is True


@pytest.mark.parametrize("mode", REDUCED_MODES)
def test_library_markdown_is_skipped_in_reduced_modes(tmp_path, monkeypatch, mode):
    calls: list[dict] = []
    monkeypatch.setattr(master, "write_library_markdown", lambda **kwargs: calls.append(kwargs) or "unused.md")

    _run(monkeypatch, tmp_path, mode)

    assert calls == []
    assert not (tmp_path / "library").exists()


def test_library_markdown_is_written_in_full_mode(tmp_path, monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(master, "write_library_markdown", lambda **kwargs: calls.append(kwargs) or "written.md")

    output = _run(monkeypatch, tmp_path, "full")

    assert len(calls) == 1
    assert output["library_md_path"] == "written.md"


@pytest.mark.parametrize("mode", REDUCED_MODES)
def test_image_extraction_is_skipped_in_reduced_modes(tmp_path, monkeypatch, mode):
    images_tmpdir = tmp_path / "images-tmp"
    (images_tmpdir / "images").mkdir(parents=True)
    (images_tmpdir / "images" / "figure-1.png").write_bytes(b"png")

    output = _run(
        monkeypatch,
        tmp_path,
        mode,
        extra={"_images_tmpdir": str(images_tmpdir), "_images_list": [{"filename": "figure-1.png"}]},
    )

    assert not (Path(output["document_path"]) / "images").exists()
    assert not images_tmpdir.exists()


# ---------------------------------------------------------------------------
# Re-ingestion: stale sidecars from a full record are cleared
# ---------------------------------------------------------------------------


def test_full_record_reingested_as_csl_only_leaves_no_stale_sidecars(tmp_path, monkeypatch):
    images_tmpdir = tmp_path / "images-tmp"
    (images_tmpdir / "images").mkdir(parents=True)
    (images_tmpdir / "images" / "figure-1.png").write_bytes(b"png")
    extra = {
        "pageindex_tree": {"level_1": []},
        "source_blocks": _artifacts()["source_blocks"],
        "_images_tmpdir": str(images_tmpdir),
        "_images_list": [{"filename": "figure-1.png"}],
    }

    full = _run(monkeypatch, tmp_path, "full", extra=copy.deepcopy(extra))
    document = Path(full["document_path"])
    library_md = tmp_path / "library" / f"{document.name}.md"
    assert (document / "document.json").is_file()
    assert (document / "merkle.json").is_file()
    assert (document / "pageindex_tree.json").is_file()
    assert (document / "images").is_dir()
    assert library_md.is_file()

    reingested = _run(monkeypatch, tmp_path, "csl_only", extra=copy.deepcopy(extra))

    assert reingested["document_path"] == str(document)
    names = _names(str(document))
    assert not names & {"document.json", "merkle.json", "pageindex_tree.json", "images"}
    assert not library_md.exists()
    assert list((tmp_path / "library").iterdir()) == []
    assert not images_tmpdir.exists()
    assert (document / "csl.json").is_file()
    assert (document / "ingestion_output.json").is_file()


# ---------------------------------------------------------------------------
# Plugins can still read a csl_only record
# ---------------------------------------------------------------------------


def test_plugin_input_is_readable_from_a_csl_only_record(tmp_path, monkeypatch):
    output = _run(monkeypatch, tmp_path, "csl_only")
    document = Path(output["document_path"])

    # The precondition plugins.py:185-190 enforces before dispatch.
    csl = plugins._read_json(document / "csl.json")
    ingestion = plugins._read_json(document / "ingestion_output.json")
    assert ingestion["status"] == "ok"
    assert csl["id"] == ingestion["standardized_csl_json"]["id"]
    assert csl["content_hash"] == ingestion["standardized_csl_json"]["content_hash"]


# ---------------------------------------------------------------------------
# full keeps writing everything it writes today
# ---------------------------------------------------------------------------


def test_full_mode_still_writes_every_artifact(tmp_path, monkeypatch):
    images_tmpdir = tmp_path / "images-tmp"
    (images_tmpdir / "images").mkdir(parents=True)
    (images_tmpdir / "images" / "figure-1.png").write_bytes(b"png")
    extra = {
        "pageindex_tree": {"level_1": []},
        "source_blocks": _artifacts()["source_blocks"],
        "_images_tmpdir": str(images_tmpdir),
        "_images_list": [{"filename": "figure-1.png"}],
    }

    output = _run(monkeypatch, tmp_path, "full", extra=extra)
    document = Path(output["document_path"])
    names = _names(str(document))

    assert {
        "csl.json",
        "csl-export.json",
        "ingestion_output.json",
        "source_blocks.json",
        "document.json",
        "merkle.json",
        "pageindex_tree.json",
        "layout.json",
        "images",
    } <= names
    assert (document / "images" / "figure-1.png").is_file()
    assert (tmp_path / "library" / f"{document.name}.md").is_file()
    assert output["library_md_path"].endswith(".md")
    tree = (document / "pageindex_tree.json").read_text(encoding="utf-8")
    assert '"level_0"' in tree
