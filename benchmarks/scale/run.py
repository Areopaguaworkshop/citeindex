"""Throughput benchmark: ingest a corpus of PDFs and record pages/min + failure modes.

Usage:
    python benchmarks/scale/run.py --corpus <dir-or-glob> [--limit N] [--workers W] [--timeout-seconds S]

The parent process writes one JSONL row per document plus a summary JSON to
benchmarks/runs/. Each document runs in a child process with a hard timeout so
one hang cannot stall the whole run; --workers runs several documents at once
(each still in its own process). Corpus files are read from wherever they live;
nothing is copied into the repo. Results are the pipeline's real output
(default routing and configuration).
"""

import argparse
import glob
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DEFAULT_RUNS_DIR = os.path.join(REPO_ROOT, "benchmarks", "runs")


def _sha256(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _page_count(path: str) -> int | None:
    try:
        import pymupdf

        with pymupdf.open(path) as doc:
            return doc.page_count
    except Exception as exc:  # benchmark: record, don't crash
        print(f"warning: page count failed for {path}: {exc}", file=sys.stderr)
        return None


def _git_sha() -> str:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, text=True
        ).strip()
    except Exception:  # benchmark: record, don't crash
        return "unknown"


def _run_single(path: str, scratch_root: str) -> dict:
    """Ingest one document in this process and return a result row."""
    from citeindex.ingestion import CiteIndexIngestionOrchestrator

    row: dict = {"path": os.path.abspath(path)}
    start = time.monotonic()
    try:
        orchestrator = CiteIndexIngestionOrchestrator(
            corpus_root=scratch_root, schema_version="1.0.0"
        )
        output = orchestrator.ingest(path)
        row["status"] = output.get("status", "unknown")
        for key in ("error_code", "stage", "next_action"):
            if output.get(key):
                row[key] = output[key]
    except Exception as exc:  # benchmark: record, don't crash
        row["status"] = "exception"
        row["error_code"] = type(exc).__name__
        row["error_message"] = str(exc)[:300]
    row["seconds"] = round(time.monotonic() - start, 3)
    return row


def _collect_documents(corpus: str) -> list[str]:
    pattern = os.path.join(corpus, "*.pdf") if os.path.isdir(corpus) else corpus
    return sorted(glob.glob(pattern))


def _run_one(path: str, index: int, scratch_root: str, timeout: float, keep: bool) -> dict:
    """Run one document in a child process with a hard timeout."""
    pages = _page_count(path)
    doc_scratch = os.path.join(scratch_root, f"doc-{index:03d}")
    cmd = [sys.executable, os.path.abspath(__file__), "--single", path, "--scratch", doc_scratch]
    row: dict
    try:
        proc = subprocess.run(
            cmd, cwd=REPO_ROOT, capture_output=True, text=True, timeout=timeout, check=False
        )
        out_lines = [line for line in proc.stdout.splitlines() if line.strip()]
        row = (
            json.loads(out_lines[-1])
            if out_lines
            else {"path": os.path.abspath(path), "status": "no-output"}
        )
        row["exit_code"] = proc.returncode
        err_lines = proc.stderr.strip().splitlines()
        if err_lines:
            row["stderr_tail"] = err_lines[-1][:200]
    except subprocess.TimeoutExpired:
        row = {"path": os.path.abspath(path), "status": "timeout", "seconds": timeout}
    row["index"] = index
    row["pages"] = pages
    row["bytes"] = os.path.getsize(path)
    row["sha256"] = _sha256(path)
    if not keep:
        shutil.rmtree(doc_scratch, ignore_errors=True)
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Citation-corpus throughput benchmark")
    parser.add_argument("--corpus", help="Directory (all *.pdf) or glob of PDFs")
    parser.add_argument("--limit", type=int, default=0, help="Process only the first N documents")
    parser.add_argument("--timeout-seconds", type=float, default=900.0, help="Hard per-document timeout")
    parser.add_argument("--workers", type=int, default=1, help="Documents processed in parallel")
    parser.add_argument("--runs-dir", default=DEFAULT_RUNS_DIR)
    parser.add_argument("--keep-artifacts", action="store_true", help="Keep per-document corpus output")
    parser.add_argument("--single", help=argparse.SUPPRESS)
    parser.add_argument("--scratch", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)

    if args.single:
        row = _run_single(
            args.single, args.scratch or tempfile.mkdtemp(prefix="citeindex-single-")
        )
        print(json.dumps(row))
        return 0

    if not args.corpus:
        parser.error("--corpus is required")

    documents = _collect_documents(args.corpus)
    if args.limit > 0:
        documents = documents[: args.limit]
    if not documents:
        parser.error(f"no PDFs matched: {args.corpus}")

    run_id = f"{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}-{_git_sha()}"
    os.makedirs(args.runs_dir, exist_ok=True)
    ledger_path = os.path.join(args.runs_dir, f"{run_id}.jsonl")
    summary_path = os.path.join(args.runs_dir, f"{run_id}.summary.json")
    scratch_root = tempfile.mkdtemp(prefix=f"citeindex-scale-{run_id}-")

    print(
        f"run {run_id}: {len(documents)} documents, {args.workers} worker(s),"
        f" timeout {args.timeout_seconds:.0f}s/doc"
    )
    rows: list[dict] = []
    started = time.monotonic()
    with (
        open(ledger_path, "w", encoding="utf-8") as ledger,
        ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool,
    ):
        futures = [
            pool.submit(
                _run_one, path, index, scratch_root, args.timeout_seconds, args.keep_artifacts
            )
            for index, path in enumerate(documents, start=1)
        ]
        for future in as_completed(futures):
            row = future.result()
            ledger.write(json.dumps(row) + "\n")
            ledger.flush()
            rows.append(row)
            pages_text = str(row.get("pages")) if row.get("pages") is not None else "?"
            print(
                f"[{len(rows)}/{len(documents)}] {row.get('status', '?'):>9}"
                f" {row.get('seconds', 0.0):8.1f}s {pages_text:>5} pages"
                f"  {os.path.basename(row['path'])}"
            )
    if not args.keep_artifacts:
        shutil.rmtree(scratch_root, ignore_errors=True)

    ok_rows = [r for r in rows if r.get("status") == "ok"]
    ok_pages = sum((r.get("pages") or 0) for r in ok_rows)
    ok_seconds = sum(r.get("seconds", 0.0) for r in ok_rows)
    elapsed = time.monotonic() - started
    statuses = sorted({str(r.get("status", "unknown")) for r in rows})
    summary = {
        "run_id": run_id,
        "git_sha": _git_sha(),
        "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "documents": len(rows),
        "ok_documents": len(ok_rows),
        "total_pages_all": sum((r.get("pages") or 0) for r in rows),
        "ok_pages": ok_pages,
        "wall_seconds": round(elapsed, 1),
        "pages_per_minute_wall": round(ok_pages / (elapsed / 60.0), 1) if elapsed else None,
        "pages_per_minute_ok_only": round(ok_pages / (ok_seconds / 60.0), 1) if ok_seconds else None,
        "by_status": {s: sum(1 for r in rows if str(r.get("status")) == s) for s in statuses},
        "failures": [
            {
                "path": r["path"],
                "status": r.get("status"),
                "error_code": r.get("error_code"),
                "stage": r.get("stage"),
            }
            for r in rows
            if r.get("status") != "ok"
        ],
    }
    with open(summary_path, "w", encoding="utf-8") as handle:
        json.dump(summary, handle, indent=2)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
