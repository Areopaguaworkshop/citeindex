"""Run the whole ScienceMetaBench census in parallel, resumably.

Each manifest row is one CiteIndex ingestion. The runner is the parallel sibling
of ``benchmarks/citations/ingest.py``: it keeps the same append-only prediction
schema (so ``score.py`` reads the result unchanged), but executes ``--workers``
sources concurrently and appends every finished row through a single writer
thread, so lines never interleave.

Per attempt the CLI is invoked as::

    <python> -m citeindex.cli <source_path> --corpus-root <artifacts>/<key>/<attempt>/corpus \\
        -p <page-range> --no-layout --no-pageindex --no-online-enrich

stdout (the CLI's JSON object) and stderr go to ``stdout.json`` / ``stderr.log``
under ``<predictions-stem>-artifacts/<sha256(id)[:16]>/<attempt>/``. A source
that overruns ``--timeout`` is killed by process group (it runs in its own
session), so OCR helpers cannot outlive the attempt.

Resume semantics: the last attempt per id wins. Ids whose last attempt is
``ok`` are skipped; failures and unattempted ids are rerun, with the attempt
number incremented. ``--retry-failed`` forces even successful ids to rerun.

Every ``--milestone`` finished sources a progress block is appended to
``<out-dir>/progress.md`` and printed: counts plus a running score from
``score.py`` (strict/soft/Levenshtein/instance with Wilson intervals, a
per-modality split and per-field recall). Scoring is best-effort — an error is
recorded in the block instead of ending the run.

Exit status is 0 only when every manifest source's last attempt is ``ok`` (the
census is complete); 1 when anything is still missing or failed, so rerunning
the same command resumes exactly the remaining work.

Usage:
    python benchmarks/sciencemeta/census.py --manifest data/manifest-all.jsonl \\
        --predictions data/census.jsonl --workers 4 --page-range "1-6" \\
        --timeout 1800 --out-dir data/census-reports --milestone 10

Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import queue
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from score import score as score_rows  # noqa: E402

MODES = (("strict", "strict_record_accuracy"), ("soft", "soft_record_accuracy"),
         ("fuzzy", "levenshtein_record_accuracy"), ("soft", "instance_recall"))
MILESTONE_HEADER = "# ScienceMetaBench census progress"
PROGRESS_FILE = "progress.md"

# Running attempts, so an interrupt can kill their process groups instead of
# leaving OCR children behind for the full per-source timeout.
_RUNNING: set[subprocess.Popen] = set()
_RUNNING_LOCK = threading.Lock()


# --- CLI plumbing ------------------------------------------------------------
def parse_cli_output(text: str) -> dict:
    """Accept one JSON object, tolerating harmless warnings printed before it.

    Mirrors ``benchmarks/citations/ingest.py`` so both runners read the CLI the
    same way; only a trailing object (nothing but whitespace after it) counts.
    """
    decoder = json.JSONDecoder()
    for offset, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, end = decoder.raw_decode(text[offset:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and not text[offset + end:].strip():
            return value
    raise ValueError("CLI output does not contain one JSON object")


def kill_process_group(process: subprocess.Popen) -> None:
    """SIGKILL the whole session started for this attempt (child + descendants)."""
    try:
        os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


def run_source(command: list[str], timeout: float, stdout_path: Path, stderr_path: Path,
               env: dict[str, str] | None = None) -> tuple[int | None, str | None]:
    """Run one source in its own session; return (returncode, error).

    ``error`` is set only for a timeout, in which case the process group has
    been killed and reaped before returning.
    """
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open("w", encoding="utf-8") as stderr:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                   start_new_session=True, env=env)
        with _RUNNING_LOCK:
            _RUNNING.add(process)
        try:
            process.wait(timeout=timeout)
            return process.returncode, None
        except subprocess.TimeoutExpired:
            kill_process_group(process)
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            return process.returncode, f"timeout after {timeout}s"
        finally:
            with _RUNNING_LOCK:
                _RUNNING.discard(process)


def terminate_running() -> None:
    """Kill every in-flight attempt; used when the runner itself is interrupted."""
    with _RUNNING_LOCK:
        processes = list(_RUNNING)
    for process in processes:
        kill_process_group(process)


def build_command(row: dict, attempt_dir: Path, *, page_range: str,
                  cli_extra: list[str] | None = None,
                  python_executable: str = sys.executable) -> list[str]:
    """The one canonical census command; the manifest only supplies the path."""
    command = [python_executable, "-m", "citeindex.cli", str(row["source_path"]),
               "--corpus-root", str(attempt_dir / "corpus"), "-p", page_range,
               "--no-layout", "--no-pageindex", "--no-online-enrich"]
    return command + list(cli_extra or [])


# --- predictions -------------------------------------------------------------
def read_predictions(path: Path) -> list[dict]:
    """Read append-only predictions, ignoring a truncated final line."""
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def latest_attempts(predictions: list[dict]) -> dict[str, dict]:
    """Index the last attempt per id (rows are appended, so later wins)."""
    latest: dict[str, dict] = {}
    for prediction in predictions:
        source_id = prediction.get("id")
        if source_id:
            latest[source_id] = prediction
    return latest


def pending_rows(rows: list[dict], latest: dict[str, dict], retry_failed: bool) -> list[dict]:
    """Manifest rows that still need a run.

    A source whose last attempt succeeded is done; anything else (attempted and
    failed, or never attempted) is retried. ``retry_failed`` additionally reruns
    successful sources.
    """
    return [row for row in rows
            if retry_failed or row["id"] not in latest or latest[row["id"]].get("status") != "ok"]


def attempt_directory(base: Path, source_id: str, attempt: int) -> Path:
    """Artifact directory keyed by a hash, never by a manifest-supplied path."""
    key = hashlib.sha256(source_id.encode()).hexdigest()[:16]
    return base / key / str(attempt)


def source_digest(row: dict, result: dict) -> str | None:
    original = Path(str(row.get("source_path") or ""))
    document = result.get("document_path")
    if not original.is_file() and document:
        original = Path(str(document)) / "source.html"
    try:
        if original.is_file():
            with original.open("rb") as stream:
                return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        return None
    return None


def run_attempt(row: dict, *, attempt: int, attempt_dir: Path, page_range: str, timeout: float,
                workers: int, pages: str | None = None, cli_extra: list[str] | None = None,
                command_factory: Callable[[dict, Path], list[str]] | None = None,
                env: dict[str, str] | None = None) -> dict:
    """Run one source and return its prediction row; never raises for a bad source."""
    started = time.monotonic()
    result: dict = {}
    returncode: int | None = None
    error: str | None = None
    timed_out = False
    try:
        attempt_dir.mkdir(parents=True, exist_ok=True)
        stdout_path, stderr_path = attempt_dir / "stdout.json", attempt_dir / "stderr.log"
        if command_factory is None:
            command = build_command(row, attempt_dir, page_range=page_range, cli_extra=cli_extra)
        if pages and env is None:
            # The CLI has no page cap of its own; the OCR backend honours this
            # switch, and -p already limits the metadata window to the same range.
            env = os.environ.copy()
            env["CITEINDEX_BENCHMARK_SCAN_PAGES"] = pages
        else:
            command = command_factory(row, attempt_dir)
        returncode, error = run_source(command, timeout, stdout_path, stderr_path, env)
        timed_out = error is not None
        if error is None and returncode == 0:
            try:
                result = parse_cli_output(stdout_path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                error = f"invalid CLI JSON: {exc}; see {stderr_path}"
    except OSError as exc:
        error = f"could not run source: {exc}"
    except Exception as exc:  # a broken runner call must still yield a terminal row
        error = f"{type(exc).__name__}: {exc}"

    finished = result.get("status") == "ok" and returncode == 0 and error is None
    status = "ok" if finished else "failed"
    if not finished and error is None:
        error = (result.get("error_message") or result.get("error")
                 or (f"exit code {returncode}" if returncode not in (0, None) else "source reported failure"))
    pipeline = result.get("sub_pipeline_outputs") or {}
    return {
        "id": row["id"],
        "attempt": attempt,
        "modality": row.get("modality"),
        "status": status,
        "seconds": round(time.monotonic() - started, 3),
        "csl": result.get("standardized_csl_json", {}) if finished else {},
        "error": error,
        "returncode": returncode,
        "document_path": result.get("document_path"),
        "source_blocks": pipeline.get("source_blocks", []),
        "retrieval_metadata": pipeline.get("retrieval_metadata", {}),
        "redirect_snapshots": pipeline.get("redirect_snapshots", []),
        "citation_verification": result.get("citation_verification"),
        "online_enrichment": result.get("online_enrichment"),
        "config": {"cli_args": ["-p", page_range, "--no-layout", "--no-pageindex", "--no-online-enrich"],
                   "env": {"CITEINDEX_BENCHMARK_SCAN_PAGES": pages} if pages else {},
                   "timeout_seconds": timeout, "page_range": page_range,
                   "pages": pages, "timed_out": timed_out},
        "logs": str(attempt_dir),
        "tokens": None,
        "cost_usd": None,
        "source_sha256": source_digest(row, result),
        "protocol": {"page_range": page_range, "pages": pages, "workers": workers,
                     "timeout": timeout},
    }


# --- milestone rendering -----------------------------------------------------
def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def _interval(item: dict) -> str:
    low, high = item["ci95"]
    if low is None or high is None:
        return "95% CI n/a"
    return f"95% CI {100 * low:.1f}-{100 * high:.1f}"


def _metric_line(label: str, item: dict) -> str:
    return f"- {label}: n={item['n']} hits={item['hits']:g} rate={_percent(item['value'])} ({_interval(item)})"


def milestone_block(rows: list[dict], latest: dict[str, dict], *, total: int, page_range: str,
                    pages: str | None = None,
                    workers: int, timeout: float) -> str:
    """Render one milestone block; may raise (the caller logs the failure)."""
    processed = [row for row in rows if row["id"] in latest]
    predictions = {row["id"]: latest[row["id"]] for row in processed}
    report = score_rows(processed, predictions)
    completed = len(predictions)
    ok = sum(prediction.get("status") == "ok" for prediction in predictions.values())
    by_modality: dict[str, list[dict]] = {}
    for row in processed:
        by_modality.setdefault(str(row.get("modality")), []).append(row)

    lines = [
        f"## {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"completed={completed} ok={ok} failed={completed - ok} remaining={total - completed}",
        f"protocol: pages={pages or 'whole-document'} page_range={page_range} "
        f"workers={workers} timeout={timeout}",
        "",
    ]
    for _, name in MODES:
        lines.append(_metric_line(name, report["metrics"][name]))
    lines += ["", "per-modality (record accuracy):", "",
              "| modality | n | strict | soft | levenshtein | instance |",
              "| --- | --- | --- | --- | --- | --- |"]
    for modality in sorted(by_modality):
        subset = by_modality[modality]
        subset_report = score_rows(subset, {row["id"]: latest[row["id"]] for row in subset})
        cells = [f"n={subset_report['scored_sources']}"]
        for _, name in MODES:
            item = subset_report["metrics"][name]
            cells.append(f"{_percent(item['value'])} (n={item['n']})")
        lines.append(f"| {modality} | " + " | ".join(cells) + " |")
    lines += ["", "per-field recall (strict):"]
    for field, counts in report["per_field"].items():
        recall = counts["recall"]
        lines.append(f"- {field}: n={counts['n']} recall={_percent(recall['value'])} "
                     f"({_interval(recall)})")
    return "\n".join(lines) + "\n"


# --- single writer -----------------------------------------------------------
class PredictionWriter(threading.Thread):
    """Append finished rows and emit milestones; the only thread that writes."""

    def __init__(self, predictions_path: Path, work_queue: queue.Queue[dict | None], *,
                 rows: list[dict], latest: dict[str, dict], milestone: int, progress_path: Path,
                 page_range: str, workers: int, timeout: float,
                 pages: str | None = None) -> None:
        super().__init__(name="census-writer", daemon=True)
        self.predictions_path = predictions_path
        self.queue = work_queue
        self.rows = rows
        self.latest = dict(latest)
        self.milestone = milestone
        self.progress_path = progress_path
        self.page_range = page_range
        self.pages = pages
        self.workers = workers
        self.timeout = timeout
        self.finished = 0
        self.append_failed = False
        self.warnings: list[str] = []

    def _append(self, output, prediction: dict) -> None:
        output.write(json.dumps(prediction, ensure_ascii=False, sort_keys=True) + "\n")
        output.flush()
        os.fsync(output.fileno())

    def _report_milestone(self) -> None:
        """Best-effort progress block: never let reporting end the census."""
        try:
            block = milestone_block(self.rows, self.latest, total=len(self.rows),
                                    page_range=self.page_range, workers=self.workers,
                                    timeout=self.timeout, pages=self.pages)
        except Exception as exc:
            block = (f"## {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
                     f"completed={len(self.latest)} remaining={len(self.rows) - len(self.latest)}\n"
                     f"scoring_error: {type(exc).__name__}: {exc}\n")
            self._warn(f"milestone scoring failed: {exc}")
        try:
            self.progress_path.parent.mkdir(parents=True, exist_ok=True)
            new_file = not self.progress_path.exists()
            with self.progress_path.open("a", encoding="utf-8") as progress:
                if new_file:
                    progress.write(MILESTONE_HEADER + "\n\n")
                progress.write(block + "\n")
        except OSError as exc:
            self._warn(f"milestone write failed: {exc}")
        print(block, flush=True)

    def _warn(self, message: str) -> None:
        self.warnings.append(message)
        print(f"warning: {message}", file=sys.stderr, flush=True)

    def run(self) -> None:
        self.predictions_path.parent.mkdir(parents=True, exist_ok=True)
        with self.predictions_path.open("a", encoding="utf-8") as output:
            while True:
                item = self.queue.get()
                try:
                    if item is None:
                        return
                    try:
                        self._append(output, item)
                    except OSError as exc:
                        # The attempt is finished but not durable: force a rerun.
                        self.append_failed = True
                        self._warn(f"could not append prediction for {item.get('id')}: {exc}")
                        continue
                    self.latest[item["id"]] = item
                    self.finished += 1
                    if self.milestone and self.finished % self.milestone == 0:
                        self._report_milestone()
                except Exception as exc:
                    self._warn(f"{type(exc).__name__}: {exc}")
                finally:
                    self.queue.task_done()


# --- driver ------------------------------------------------------------------
def looks_scanned(path: str) -> bool:
    """Cheap probe: a source with no text layer will occupy the OCR lane."""
    try:
        import pymupdf

        with pymupdf.open(path) as doc:
            text = "".join(doc[i].get_text() for i in range(min(3, doc.page_count)))
        return len(text.strip()) < 200
    except Exception:
        return False


def census(rows: list[dict], predictions_path: Path, *, workers: int = 4, page_range: str = "1-6",
           pages: str | None = None, ocr_workers: int = 1, cli_extra: list[str] | None = None,
           timeout: float = 1800, out_dir: Path = Path("data/census-reports"), milestone: int = 10,
           retry_failed: bool = False,
           command_factory: Callable[[dict, Path], list[str]] | None = None) -> int:
    """Run pending sources; return 0 only when every source's last attempt is ok."""
    if workers < 1:
        raise ValueError("workers must be at least 1")
    if timeout <= 0:
        raise ValueError("timeout must be positive")
    if milestone < 0:
        raise ValueError("milestone must not be negative")
    if not page_range:
        raise ValueError("page range must not be empty")
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate manifest IDs")

    predictions_path = Path(predictions_path)
    out_dir = Path(out_dir)
    artifacts = predictions_path.parent / (predictions_path.stem + "-artifacts")
    predictions_path.parent.mkdir(parents=True, exist_ok=True)
    out_dir.mkdir(parents=True, exist_ok=True)

    prior = read_predictions(predictions_path)
    latest = latest_attempts(prior)
    pending = pending_rows(rows, latest, retry_failed)
    attempts = {source_id: 1 + sum(prediction.get("id") == source_id for prediction in prior)
                for source_id in ids}
    print(f"{len(pending)} pending / {len(rows)} sources; {len(prior)} preserved attempts", flush=True)

    work_queue: queue.Queue[dict | None] = queue.Queue()
    writer = PredictionWriter(predictions_path, work_queue, rows=rows, latest=latest,
                              milestone=milestone, progress_path=out_dir / PROGRESS_FILE,
                              page_range=page_range, workers=workers, timeout=timeout, pages=pages)

    ocr_semaphore = threading.Semaphore(max(1, ocr_workers))

    def task(row: dict) -> None:
        attempt = attempts[row["id"]]
        try:
            scanned = looks_scanned(str(row.get("source_path") or ""))
            if scanned:
                # Each OCR source starts its own MinerU process with its own
                # model copy, so serialise those or swap is exhausted.
                with ocr_semaphore:
                    prediction = run_attempt(
                        row, attempt=attempt, attempt_dir=attempt_directory(artifacts, row["id"], attempt),
                        page_range=page_range, timeout=timeout, workers=workers, pages=pages, cli_extra=cli_extra,
                        command_factory=command_factory)
            else:
                prediction = run_attempt(
                    row, attempt=attempt, attempt_dir=attempt_directory(artifacts, row["id"], attempt),
                    page_range=page_range, timeout=timeout, workers=workers, pages=pages, cli_extra=cli_extra,
                    command_factory=command_factory)
        except Exception as exc:  # the pool must survive any single source
            prediction = {"id": row["id"], "attempt": attempt, "modality": row.get("modality"),
                          "status": "failed", "seconds": 0.0, "csl": {},
                          "error": f"{type(exc).__name__}: {exc}", "returncode": None,
                          "document_path": None, "source_blocks": [], "retrieval_metadata": {},
                          "redirect_snapshots": [], "citation_verification": None,
                          "online_enrichment": None,
                          "config": {"page_range": page_range, "timeout_seconds": timeout},
                          "logs": None, "tokens": None, "cost_usd": None, "source_sha256": None,
                          "protocol": {"page_range": page_range, "workers": workers, "timeout": timeout}}
        work_queue.put(prediction)

    writer.start()
    try:
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="census") as pool:
            for future in [pool.submit(task, row) for row in pending]:
                future.result()
    finally:
        work_queue.put(None)
        writer.join()

    completed = sum(row["id"] in writer.latest for row in rows)
    ok = sum(writer.latest[row["id"]].get("status") == "ok" for row in rows if row["id"] in writer.latest)
    failed = completed - ok
    remaining = len(rows) - completed
    exit_code = 0 if completed == len(rows) and failed == 0 and not writer.append_failed else 1
    print(f"census: completed={completed}/{len(rows)} ok={ok} failed={failed} remaining={remaining} "
          f"exit={exit_code}", flush=True)
    return exit_code


def read_manifest(path: Path) -> list[dict]:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [row for row in rows if row.get("pilot_selection") != "excluded"]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--manifest", default="data/manifest-all.jsonl")
    parser.add_argument("--predictions", default="data/census.jsonl")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--cli-extra", action="append", default=[],
                        help="extra flag passed through to citeindex.cli (repeatable)")
    parser.add_argument("--ocr-workers", type=int, default=1,
                        help="how many scan-lane sources may run at once (each starts its own OCR process)")
    parser.add_argument("--page-range", default="1-6")
    parser.add_argument("--timeout", type=float, default=1800)
    parser.add_argument("--out-dir", default="data/census-reports")
    parser.add_argument("--milestone", type=int, default=10,
                        help="emit a progress block every N finished sources (0 disables)")
    parser.add_argument("--pages", default=None,
                        help="cap the run to these pages, e.g. '1-6' (sets "
                             "CITEINDEX_BENCHMARK_SCAN_PAGES for the OCR backend)")
    parser.add_argument("--retry-failed", action="store_true",
                        help="also rerun sources whose last attempt was ok (default: retry only failures)")
    arguments = parser.parse_args(argv)

    def stop(signum, frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop)
    try:
        return census(read_manifest(Path(arguments.manifest)), Path(arguments.predictions),
                      workers=arguments.workers, page_range=arguments.page_range,
                      pages=arguments.pages, ocr_workers=arguments.ocr_workers,
                      cli_extra=arguments.cli_extra,
                      timeout=arguments.timeout, out_dir=Path(arguments.out_dir),
                      milestone=arguments.milestone, retry_failed=arguments.retry_failed)
    except KeyboardInterrupt:
        terminate_running()
        print("interrupted; finished attempts are already in the predictions file", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
