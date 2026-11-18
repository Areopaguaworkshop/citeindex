"""Unit tests for the parallel ScienceMetaBench census runner.

No network, no LLM, no real CiteIndex run: every source is a tiny local script
wired in through ``census.census(command_factory=...)``.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import census

# Keys ``benchmarks/citations/ingest.py`` writes; the census adds ``protocol``.
INGEST_KEYS = {
    "id", "attempt", "modality", "status", "seconds", "csl", "error", "returncode",
    "document_path", "source_blocks", "retrieval_metadata", "redirect_snapshots",
    "citation_verification", "online_enrichment", "config", "logs", "tokens",
    "cost_usd", "source_sha256",
}

FAKE_CLI = """
import json, sys, time
source, mode = sys.argv[1], sys.argv[2]
if mode == "hang":
    time.sleep(120)
if mode == "fail":
    print("source failed: " + source, file=sys.stderr)
    sys.exit(3)
print(json.dumps({"status": "ok", "standardized_csl_json": {"type": "book", "title": source}}))
"""


# --- helpers -----------------------------------------------------------------
def write_manifest(tmp_path: Path, ids: list[str]) -> list[dict]:
    pdf = tmp_path / "pdf"
    pdf.mkdir(parents=True, exist_ok=True)
    rows = []
    for index, source_id in enumerate(ids):
        path = pdf / f"{source_id}.pdf"
        path.write_bytes(b"%PDF-1.4 fake " + source_id.encode())
        rows.append({
            "id": source_id,
            "modality": "textbook" if index % 2 == 0 else "ebook",
            "source_path": str(path),
            "source_sha256": source_id,
            "csl": {"type": "book", "title": f"Book {source_id}"},
            "field_status": {"title": "present", "author": "outside_scope",
                             "publisher": "outside_scope", "issued": "outside_scope",
                             "ISBN": "outside_scope"},
        })
    return rows


def factory(script: Path, modes: dict[str, str], invoked: list[str]):
    def build(row: dict, work: Path) -> list[str]:
        invoked.append(row["id"])
        return [sys.executable, str(script), row["id"], modes.get(row["id"], "ok")]
    return build


def run(tmp_path: Path, rows: list[dict], *, modes: dict[str, str] | None = None, workers: int = 2,
        milestone: int = 0, retry_failed: bool = False, timeout: float = 60,
        page_range: str = "1-6", predictions: Path | None = None) -> tuple[int, list[dict], list[str]]:
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE_CLI, encoding="utf-8")
    invoked: list[str] = []
    path = predictions or tmp_path / "census.jsonl"
    code = census.census(rows, path, workers=workers, page_range=page_range, timeout=timeout,
                         out_dir=tmp_path / "reports", milestone=milestone,
                         retry_failed=retry_failed,
                         command_factory=factory(script, modes or {}, invoked))
    return code, census.read_predictions(path), invoked


def write_predictions(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def is_dead(pid: int, wait: float = 5.0) -> bool:
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
        except OSError:
            return True
        if stat.rsplit(")", 1)[-1].split()[0] == "Z":
            return True
        time.sleep(0.05)
    return False


# --- resume ------------------------------------------------------------------
def test_resume_skips_ok_rows(tmp_path):
    rows = write_manifest(tmp_path, ["a", "b"])
    path = tmp_path / "census.jsonl"
    write_predictions(path, [{"id": "a", "attempt": 1, "status": "ok", "csl": {"title": "a"}}])
    code, predictions, invoked = run(tmp_path, rows, predictions=path)

    assert invoked == ["b"]  # "a" already succeeded
    assert code == 0
    assert [row["id"] for row in predictions] == ["a", "b"]
    # append-only: the preserved first attempt is untouched
    assert predictions[0]["attempt"] == 1 and predictions[1]["attempt"] == 1


def test_resume_retries_failed_rows(tmp_path):
    rows = write_manifest(tmp_path, ["a"])
    path = tmp_path / "census.jsonl"
    write_predictions(path, [{"id": "a", "attempt": 1, "status": "failed", "csl": {},
                              "error": "boom"}])
    code, predictions, invoked = run(tmp_path, rows, predictions=path)

    assert invoked == ["a"]
    assert code == 0
    assert [row["attempt"] for row in predictions] == [1, 2]
    assert predictions[-1]["status"] == "ok"


def test_retry_failed_flag_reruns_ok_rows(tmp_path):
    rows = write_manifest(tmp_path, ["a"])
    path = tmp_path / "census.jsonl"
    write_predictions(path, [{"id": "a", "attempt": 1, "status": "ok", "csl": {}}])

    code, predictions, invoked = run(tmp_path, rows, predictions=path, retry_failed=True)
    assert invoked == ["a"]
    assert code == 0
    assert predictions[-1]["attempt"] == 2


def test_completed_census_is_a_noop(tmp_path):
    rows = write_manifest(tmp_path, ["a", "b"])
    path = tmp_path / "census.jsonl"
    write_predictions(path, [{"id": "a", "attempt": 1, "status": "ok", "csl": {}},
                             {"id": "b", "attempt": 1, "status": "ok", "csl": {}}])
    before = path.read_text()

    code, _, invoked = run(tmp_path, rows, predictions=path)
    assert invoked == []
    assert code == 0
    assert path.read_text() == before


# --- pool behaviour ----------------------------------------------------------
def test_failed_source_does_not_abort_pool(tmp_path):
    rows = write_manifest(tmp_path, ["a", "b", "c", "d"])
    code, predictions, invoked = run(tmp_path, rows, modes={"b": "fail"}, workers=4)

    assert sorted(invoked) == ["a", "b", "c", "d"]
    statuses = {row["id"]: row["status"] for row in predictions}
    assert statuses == {"a": "ok", "b": "failed", "c": "ok", "d": "ok"}
    failed = next(row for row in predictions if row["id"] == "b")
    assert failed["returncode"] == 3
    assert failed["error"] == "exit code 3"
    assert failed["csl"] == {}
    assert code == 1


def test_rows_never_interleave(tmp_path):
    ids = [f"source-{index:02d}" for index in range(16)]
    rows = write_manifest(tmp_path, ids)
    path = tmp_path / "census.jsonl"
    code, predictions, _ = run(tmp_path, rows, workers=4, predictions=path)

    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 16
    assert sorted(json.loads(line)["id"] for line in lines) == sorted(ids)
    assert len({row["id"] for row in predictions}) == 16
    assert code == 0


def test_timeout_kills_process_group(tmp_path):
    script = tmp_path / "hang.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "open(sys.argv[1], 'w').write(str(child.pid))\n"
        "time.sleep(120)\n",
        encoding="utf-8")
    pidfile = tmp_path / "grandchild.pid"

    started = time.monotonic()
    returncode, error = census.run_source([sys.executable, str(script), str(pidfile)], 1.5,
                                          tmp_path / "stdout.json", tmp_path / "stderr.log")

    assert error == "timeout after 1.5s"
    assert returncode not in (0, None)
    assert time.monotonic() - started < 30
    assert pidfile.exists()
    assert is_dead(int(pidfile.read_text()))  # the grandchild died with the session


def test_timeout_is_recorded_as_failed_attempt(tmp_path):
    rows = write_manifest(tmp_path, ["slow"])
    code, predictions, _ = run(tmp_path, rows, modes={"slow": "hang"}, timeout=1.5, workers=1)

    assert code == 1
    assert predictions[0]["status"] == "failed"
    assert predictions[0]["error"] == "timeout after 1.5s"
    assert (tmp_path / "census-artifacts").is_dir()


# --- milestone ---------------------------------------------------------------
def test_milestone_fires_every_n(tmp_path):
    ids = [f"m{index}" for index in range(5)]
    rows = write_manifest(tmp_path, ids)
    code, predictions, _ = run(tmp_path, rows, workers=2, milestone=2)

    progress = (tmp_path / "reports" / "progress.md").read_text(encoding="utf-8")
    assert progress.count("completed=") == 2  # exactly at 2 and 4, not at 5
    assert "completed=2 " in progress and "completed=4 " in progress
    assert "completed=5 " not in progress
    assert "remaining=3" in progress and "remaining=1" in progress
    assert "strict_record_accuracy" in progress
    assert "levenshtein_record_accuracy" in progress
    assert "instance_recall" in progress
    assert "per-modality" in progress and "| textbook |" in progress
    assert "per-field recall (strict):" in progress
    assert code == 0 and len(predictions) == 5


def test_milestone_scoring_error_does_not_crash(tmp_path, monkeypatch):
    rows = write_manifest(tmp_path, ["a", "b"])

    def boom(*args, **kwargs):
        raise RuntimeError("scorer exploded")

    monkeypatch.setattr(census, "score_rows", boom)
    code, predictions, _ = run(tmp_path, rows, workers=2, milestone=1)

    progress = (tmp_path / "reports" / "progress.md").read_text(encoding="utf-8")
    assert "scoring_error: RuntimeError: scorer exploded" in progress
    assert code == 0 and len(predictions) == 2  # sources still completed


def test_milestone_zero_disables_progress(tmp_path):
    rows = write_manifest(tmp_path, ["a", "b"])
    code, _, _ = run(tmp_path, rows, milestone=0)
    assert code == 0
    assert not (tmp_path / "reports" / "progress.md").exists()


# --- row schema --------------------------------------------------------------
def test_prediction_schema_matches_ingest(tmp_path):
    rows = write_manifest(tmp_path, ["a"])
    _, predictions, _ = run(tmp_path, rows)
    assert set(predictions[0]) == INGEST_KEYS | {"protocol"}


def test_protocol_recorded(tmp_path):
    rows = write_manifest(tmp_path, ["a"])
    _, predictions, _ = run(tmp_path, rows, workers=2, timeout=42, page_range="7-9")

    row = predictions[0]
    assert row["protocol"] == {"page_range": "7-9", "pages": None, "workers": 2, "timeout": 42}
    assert row["config"]["page_range"] == "7-9"
    assert row["status"] == "ok"
    assert row["csl"] == {"type": "book", "title": "a"}
    assert row["source_sha256"] is not None
    assert Path(row["logs"], "stdout.json").is_file()
    assert Path(row["logs"], "stderr.log").is_file()


# --- exit codes --------------------------------------------------------------
def test_exit_codes(tmp_path):
    healthy = write_manifest(tmp_path / "healthy", ["a", "b"])
    assert run(tmp_path / "healthy", healthy)[0] == 0

    broken = write_manifest(tmp_path / "broken", ["a", "b"])
    assert run(tmp_path / "broken", broken, modes={"b": "fail"})[0] == 1

    partial = tmp_path / "partial"
    partial.mkdir()
    rows = write_manifest(partial, ["a"])
    path = partial / "census.jsonl"
    write_predictions(path, [{"id": "a", "attempt": 1, "status": "failed", "csl": {}}])
    # a failing source is retried and succeeds -> complete again
    assert run(partial, rows, predictions=path)[0] == 0
