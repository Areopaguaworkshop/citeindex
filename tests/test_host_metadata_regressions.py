import hashlib
import importlib.util
import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from citeindex.ingestion.citation_verification import validate_block_evidence
from citeindex.ingestion.master import CiteIndexIngestionOrchestrator
from citeindex.ingestion.models import PipelineResult
from citeindex.ingestion.pipelines import dspy_extract


def benchmark_module(name):
    path = Path(__file__).parents[1] / "benchmarks" / "citations" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"benchmark_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_host_blocks_and_bounded_extraction(monkeypatch):
    blocks = dspy_extract.build_source_blocks([(42, ["Host title\nJane Doe\n2015"]), (43, ["References: Other title"])])
    assert blocks[0]["physical_page_index"] == 0
    assert blocks[0]["printed_page_label"] == "42"
    calls = []
    def predict(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(csl={"title": "Host title", "DOI": "10.9999/invented"},
                               field_evidence={"title": {"block_id": "p1_b1", "quote": "Host title"},
                                               "DOI": {"block_id": "p1_b1", "quote": "Host title"}})
    monkeypatch.setattr(dspy_extract, "get_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(dspy_extract.dspy, "Predict", lambda *a: predict)
    result = dspy_extract._run_dspy_extraction("", "book", source_blocks=blocks)
    assert result["title"] == "Host title" and "DOI" not in result
    assert 1 <= len(calls) <= 2
    assert all("References:" not in b["text"] for c in calls for b in c["source_blocks"])
    assert result["_field_evidence"]["title"]["locator"]["physical_page_index"] == 0


def test_repair_rejects_stale_digest_wrong_locator_and_unsupported_value(tmp_path):
    source = tmp_path / "source.pdf"
    source.write_bytes(b"original")
    blocks = dspy_extract.build_source_blocks([(1, ["Host title"])])
    evidence = validate_block_evidence("title", "Host title", {"block_id": "p1_b1", "quote": "Host title"}, blocks)
    result = PipelineResult(status="ok", source_id="s", resource_type="digital_pdf",
                            csl_json={"title": "Draft"}, extra={"source_blocks": blocks})
    orchestrator = CiteIndexIngestionOrchestrator(str(tmp_path / "corpus"))
    proposal = {"source_sha256": hashlib.sha256(b"original").hexdigest(), "proposals": [{
        "status": "proposed", "field": "title", "old_value": "Draft", "proposed_value": "Host title",
        "quote": "Host title", "locator": evidence["locator"], "reason": "Host title page"}]}
    path = tmp_path / "repair.json"
    def apply(payload):
        path.write_text(json.dumps(payload))
        return orchestrator._apply_repair_proposal(result.csl_json, str(path), str(source), result)
    assert apply(proposal)["title"] == "Host title"
    for mutate in (
        lambda p: p.update(source_sha256="0" * 64),
        lambda p: p["proposals"][0]["locator"].update(physical_page_index=999),
        lambda p: p["proposals"][0].update(proposed_value="Invented"),
        lambda p: p["proposals"][0].update(old_value="Stale"),
        lambda p: p["proposals"][0].update(field="_cited_references"),
    ):
        bad = json.loads(json.dumps(proposal))
        mutate(bad)
        with pytest.raises(ValueError):
            apply(bad)


def test_retry_preserves_history_and_records_blocked(tmp_path, monkeypatch):
    ingest = benchmark_module("ingest")
    source = tmp_path / "source.pdf"
    source.write_bytes(b"pdf")
    manifest, predictions = tmp_path / "manifest.jsonl", tmp_path / "predictions.jsonl"
    manifest.write_text(json.dumps({"id": "one", "source_path": str(source)}) + "\n")
    first = {"id": "one", "status": "failed", "error": "old failure"}
    predictions.write_text(json.dumps(first) + "\n")
    def run(command, timeout, stdout, stderr):
        assert timeout == 5400
        stdout.write_text(json.dumps({"status": "blocked", "error_message": "service unavailable"}))
        return 0, None
    monkeypatch.setattr(ingest, "run_source", run)
    ingest.main(str(manifest), str(predictions), retry_failed=True)
    rows = [json.loads(line) for line in predictions.read_text().splitlines()]
    assert rows[0] == first and len(rows) == 2
    assert rows[1]["status"] == "failed" and rows[1]["error"] == "service unavailable"


def test_runner_accepts_stdout_warning_before_cli_json():
    ingest = benchmark_module("ingest")
    payload = {"status": "ok", "standardized_csl_json": {"title": "Host"}}
    assert ingest.parse_cli_output("warning: deprecated API\n" + json.dumps(payload, indent=2)) == payload
    with pytest.raises(ValueError, match="does not contain"):
        ingest.parse_cli_output("warning only")


def test_digital_pdf_doc_type_is_not_shadowed():
    from citeindex.ingestion.pipelines import digital_pdf
    assert callable(digital_pdf.determine_doc_type)
    assert callable(digital_pdf.doc_type_to_csl_type)
    assert "determine_doc_type" not in digital_pdf.run.__code__.co_varnames


def test_timeout_and_output_lock(tmp_path):
    import fcntl
    ingest = benchmark_module("ingest")
    started = time.monotonic()
    _, error = ingest.run_source([sys.executable, "-c", "import time; time.sleep(30)"], .1,
                                  tmp_path / "out", tmp_path / "err")
    assert "timeout" in error and time.monotonic() - started < 5
    manifest, predictions = tmp_path / "manifest", tmp_path / "predictions.jsonl"
    manifest.write_text("")
    with predictions.with_suffix(".jsonl.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(SystemExit, match="Another runner"):
            ingest.main(str(manifest), str(predictions))


def test_host_scorer_counts_failures_and_unknown_cost():
    scorer = benchmark_module("run")
    gold = {"one": {"csl": {"title": "Host"}}, "two": {"csl": {"title": "Second"}}}
    prediction = {"one": {"id": "one", "status": "failed", "csl": {"title": "Host"}, "seconds": 5}}
    result = scorer.score(gold, prediction, list(prediction.values()))
    assert result["fields"]["title"]["fn"] == 2
    assert result["attempted"] == 1 and result["not_attempted"] == 1
    assert result["cost"]["cost_usd"] is None
    assert "references" not in result and "marker_links" not in result
    assert scorer._norm("https://doi.org/10.1/ABC", "DOI") == "10.1/abc"
    assert scorer._norm([{"family": "A"}, {"family": "B"}]) != scorer._norm([{"family": "B"}, {"family": "A"}])


def test_timeout_cleans_child_in_separate_session(tmp_path):
    import psutil
    ingest = benchmark_module("ingest")
    script = ("import subprocess,sys,time; "
              "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'],start_new_session=True); "
              "print(p.pid,flush=True); time.sleep(30)")
    _, error = ingest.run_source([sys.executable, "-c", script], 1.2, tmp_path / "out", tmp_path / "err")
    assert error
    pid = int((tmp_path / "out").read_text().strip())
    for _ in range(20):
        if not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE:
            break
        time.sleep(.05)
    else:
        pytest.fail("detached source child survived timeout")


def test_pageindex_ranges_rank_original_colophon(monkeypatch):
    blocks = [{"id": "body", "text": "x" * 12000, "physical_page_index": 0},
              {"id": "colophon", "text": "Host title", "physical_page_index": 100}]
    calls = []
    def predict(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(csl={}, field_evidence={})
    monkeypatch.setattr(dspy_extract, "get_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(dspy_extract.dspy, "Predict", lambda *a: predict)
    dspy_extract._run_dspy_extraction("", "book", source_blocks=blocks,
        candidate_regions=[{"role": "imprint", "start_page": 101, "end_page": 101}])
    assert calls[0]["source_blocks"][0]["id"] == "colophon"
    assert len(calls) <= 2


def test_explicit_grobid_failure_does_not_become_success(monkeypatch):
    from citeindex.ingestion.pipelines import digital_pdf, grobid
    monkeypatch.setattr(grobid, "is_grobid_available", lambda: True)
    monkeypatch.setattr(grobid, "extract_document_metadata_grobid", lambda *a: {})
    with pytest.raises(RuntimeError, match="no host metadata"):
        digital_pdf._run_grobid("source.pdf")
