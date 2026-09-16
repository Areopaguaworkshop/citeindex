"""Run source-metadata ingestion with append-only attempts and a 90-minute deadline."""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import psutil


def parse_cli_output(text: str) -> dict:
    """Accept one JSON object with harmless stdout warnings before it."""
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


def run_source(command: list[str], timeout: float, stdout_path: Path, stderr_path: Path) -> tuple[int, str | None]:
    """Track descendants (including MinerU's separate session) and bound cleanup."""
    children = {}
    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=stdout,
                                   stderr=stderr, start_new_session=True)
        parent = psutil.Process(process.pid)
        started = heartbeat = time.monotonic()
        error = None
        try:
            while process.poll() is None:
                try:
                    for child in parent.children(recursive=True):
                        children[child.pid] = child
                except psutil.NoSuchProcess:
                    pass
                elapsed = time.monotonic() - started
                if elapsed >= timeout:
                    error = f"timeout after {timeout}s"
                    break
                if time.monotonic() - heartbeat >= 30:
                    print(f"  running pid={process.pid}, elapsed={elapsed:.0f}s; log={stderr_path}", flush=True)
                    heartbeat = time.monotonic()
                time.sleep(min(0.5, max(0.01, timeout - elapsed)))
        finally:
            # psutil retains process identity, so cleanup never targets a reused PID.
            for child in reversed(list(children.values())):
                try:
                    child.kill()
                except psutil.NoSuchProcess:
                    pass
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait(timeout=5)
    return process.returncode, error


def main(manifest_path: str, predictions_path: str, retry_failed: bool = False,
         timeout_seconds: int = 5400, citation_engine: str | None = None,
         media_asr_backend: str | None = None, wenbi_asr_provider: str = "funasr",
         wenbi_python: str | None = None, wenbi_speaker_labels: bool = False) -> None:
    if timeout_seconds <= 0:
        raise ValueError("timeout must be positive")
    rows = [json.loads(line) for line in Path(manifest_path).read_text().splitlines() if line.strip()]
    ids = [row["id"] for row in rows]
    if len(ids) != len(set(ids)):
        raise ValueError("duplicate manifest IDs")
    output_path = Path(predictions_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # ponytail: one writer per predictions file; separate files permit independent configurations.
    with output_path.with_suffix(output_path.suffix + ".lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another runner owns this predictions file; do not start a duplicate.")
        prior = [json.loads(line) for line in output_path.read_text().splitlines() if line.strip()] if output_path.exists() else []
        latest = {row["id"]: row for row in prior}
        pending = [row for row in rows if row["id"] not in latest or
                   (retry_failed and latest[row["id"]].get("status") != "ok")]
        print(f"{len(pending)} pending / {len(rows)} sources; {len(prior)} preserved attempts", flush=True)
        with output_path.open("a", encoding="utf-8") as output:
            for row in pending:
                source_id, source_path = row["id"], row["source_path"]
                attempt = 1 + sum(item["id"] == source_id for item in prior)
                # IDs are labels, never directory paths supplied by the manifest.
                key = hashlib.sha256(source_id.encode()).hexdigest()[:16]
                work = output_path.parent / (output_path.stem + "-artifacts") / key / str(attempt)
                work.mkdir(parents=True, exist_ok=True)
                cli_args = list(row.get("cli_args", []))
                if not cli_args and source_path.casefold().endswith(".pdf"):
                    cli_args = ["--no-layout", "--no-pageindex"]
                if citation_engine:
                    if "--citation-engine" in cli_args:
                        raise ValueError("set engine in the manifest or on the runner, not both")
                    cli_args += ["--citation-engine", citation_engine]
                if row.get("modality") == "media" and media_asr_backend:
                    if "--media-asr-backend" in cli_args:
                        raise ValueError("set media ASR in the manifest or on the runner, not both")
                    cli_args += ["--media-asr-backend", media_asr_backend]
                    if media_asr_backend == "wenbi":
                        cli_args += ["--wenbi-asr-provider", wenbi_asr_provider]
                        if wenbi_python:
                            cli_args += ["--wenbi-python", wenbi_python]
                        if wenbi_speaker_labels:
                            cli_args.append("--wenbi-speaker-labels")
                command = [sys.executable, "-m", "citeindex.cli", source_path,
                           "--corpus-root", str(work / "corpus"), *cli_args]
                started = time.monotonic()
                deadline = int(row.get("timeout_seconds", timeout_seconds))
                if deadline <= 0:
                    raise ValueError("source timeout must be positive")
                print(f"starting {source_id} attempt={attempt} deadline={deadline}s", flush=True)
                interrupted = False
                result, returncode, error = {}, None, None
                try:
                    returncode, error = run_source(command, deadline, work / "stdout.json", work / "stderr.log")
                    if not error:
                        try:
                            result = parse_cli_output((work / "stdout.json").read_text())
                        except ValueError as exc:
                            error = f"invalid CLI JSON: {exc}; see {work / 'stderr.log'}"
                except KeyboardInterrupt:
                    interrupted, error = True, "interrupted"
                except OSError as exc:
                    error = str(exc)
                status = "ok" if returncode == 0 and result.get("status") == "ok" and not error else "failed"
                pipeline = result.get("sub_pipeline_outputs") or {}
                prediction = {
                    "id": source_id, "attempt": attempt, "modality": row.get("modality"),
                    "status": status, "seconds": round(time.monotonic() - started, 3),
                    "csl": result.get("standardized_csl_json", {}) if status == "ok" else {},
                    "error": error or result.get("error_message") or result.get("error"),
                    "returncode": returncode, "document_path": result.get("document_path"),
                    "source_blocks": pipeline.get("source_blocks", []),
                    "citation_verification": result.get("citation_verification"),
                    "config": {"cli_args": cli_args, "timeout_seconds": deadline},
                    "logs": str(work), "tokens": None, "cost_usd": None,
                }
                original = Path(source_path)
                if not original.is_file() and result.get("document_path"):
                    original = Path(result["document_path"]) / "source.html"
                prediction["source_sha256"] = None
                if original.is_file():
                    with original.open("rb") as source:
                        prediction["source_sha256"] = hashlib.file_digest(source, "sha256").hexdigest()
                output.write(json.dumps(prediction, ensure_ascii=False, sort_keys=True) + "\n")
                output.flush()
                os.fsync(output.fileno())
                prior.append(prediction)
                print(f"finished {source_id}: {status} ({prediction['seconds']}s)", flush=True)
                if interrupted:
                    raise KeyboardInterrupt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest")
    parser.add_argument("predictions")
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument("--timeout-seconds", type=int, default=5400)
    parser.add_argument("--citation-engine", choices=("dspy", "grobid"))
    parser.add_argument("--media-asr-backend", choices=("whisperx", "wenbi"))
    parser.add_argument("--wenbi-asr-provider", choices=("funasr", "whisper", "gladia"), default="funasr")
    parser.add_argument("--wenbi-python")
    parser.add_argument("--wenbi-speaker-labels", action="store_true")
    args = parser.parse_args()
    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)
    main(args.manifest, args.predictions, args.retry_failed, args.timeout_seconds, args.citation_engine,
         args.media_asr_backend, args.wenbi_asr_provider, args.wenbi_python, args.wenbi_speaker_labels)
