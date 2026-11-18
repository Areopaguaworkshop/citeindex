"""Fetch a deterministic ScienceMetaBench sample: gold rows plus their PDFs.

Dataset: https://huggingface.co/datasets/opendatalab/ScienceMetaBench

The repository layout is discovered from the Hub tree API rather than assumed
(verified 2026-10-27):

    data/{batch}/{kind}_{suffix}.jsonl
    pdf/{batch}/{kind}_{suffix}/{sha256}.pdf

``--limit`` applies per kind, so ``--kind textbook,ebook --limit 12`` writes 24
rows and downloads 24 PDFs. Selection is reproducible: candidate rows of a kind
are sorted by ``sha256`` and ``random.Random(seed).sample`` picks ``--limit`` of
them.

Failures never abort the batch: each one is appended to
``data/fetch-failures.jsonl`` and the remaining files are still attempted.

Usage:
    python benchmarks/sciencemeta/fetch.py --kind textbook,ebook --limit 12 \
        --seed 7 --out benchmarks/sciencemeta/data

Stdlib only (urllib.request, json, random).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DATASET = "opendatalab/ScienceMetaBench"
REPO = f"https://huggingface.co/datasets/{DATASET}"
API = f"https://huggingface.co/api/datasets/{DATASET}"
BATCHES = ("20250806", "20251022")
KINDS = ("paper", "textbook", "ebook")
TIMEOUT = 60
HEADERS = {"User-Agent": "citeindex-sciencemeta-benchmark/1"}


def _get(url: str) -> bytes:
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return response.read()


def _next_link(header: str) -> str:
    for part in header.split(","):
        if 'rel="next"' in part:
            return part.split(";")[0].strip().strip("<>")
    return ""


def _tree(path: str) -> list[dict]:
    """List one Hub tree path, following pagination links."""
    url = f"{API}/tree/main/{path}"
    entries: list[dict] = []
    while url:
        request = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            entries.extend(json.loads(response.read().decode("utf-8")))
            url = _next_link(response.headers.get("Link") or "")
    return entries


def _kind_of(name: str) -> str:
    """``ebook_0806`` -> ``ebook``; ``textbook.jsonl`` -> ``textbook``."""
    return name.split("_")[0].split(".")[0]


def batch_row_files(batch: str) -> dict[str, str]:
    """Map kind -> repository path of that batch's JSONL, from the API listing."""
    files = {}
    for entry in _tree(f"data/{batch}"):
        if entry.get("type") != "file" or not entry.get("path", "").endswith(".jsonl"):
            continue
        files[_kind_of(entry["path"].rsplit("/", 1)[-1])] = entry["path"]
    return files


def pdf_directory(batch: str, kind: str) -> str:
    """Repository directory holding this batch/kind's PDFs.

    The kind subdirectory name carries a batch suffix (``textbook_0806``), so it
    is matched by prefix. If the batch keeps PDFs directly under ``pdf/{batch}``,
    that directory is used instead.
    """
    directories = [entry["path"] for entry in _tree(f"pdf/{batch}") if entry.get("type") == "directory"]
    if not directories:
        return f"pdf/{batch}"
    for path in directories:
        if _kind_of(path.rsplit("/", 1)[-1]) == kind:
            return path
    raise LookupError(f"no pdf directory for kind={kind} under pdf/{batch}: {directories}")


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _record_failure(log, stage: str, url: str, error: str, **context) -> None:
    entry = {"stage": stage, "url": url, "error": error, "at": _now(), **context}
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _download(url: str, destination: Path, expected_sha256: str) -> str:
    """Download to ``.part``, verify the digest, then publish. Returns a status."""
    if destination.exists():
        return "skip"
    partial = destination.parent / (destination.name + ".part")
    try:
        request = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response, partial.open("wb") as stream:
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                stream.write(chunk)
        digest = _sha256(partial)
        if digest != expected_sha256:
            partial.unlink(missing_ok=True)
            raise ValueError(f"digest mismatch: got {digest}, expected {expected_sha256}")
        partial.replace(destination)
    except (urllib.error.URLError, OSError, ValueError) as error:
        partial.unlink(missing_ok=True)
        raise RuntimeError(f"{type(error).__name__}: {error}") from error
    return "ok"


def select(rows: list[dict], limit: int, seed: int) -> list[dict]:
    """Deterministic sample: sort by sha256, then seeded sample."""
    ordered = sorted(rows, key=lambda row: row["sha256"])
    if limit >= len(ordered):
        return ordered
    return random.Random(seed).sample(ordered, limit)


def collect(kind: str, batches: tuple[str, ...], limit: int, seed: int, log, failures: list[str]) -> list[dict]:
    candidates: dict[str, dict] = {}
    for batch in batches:
        try:
            files = batch_row_files(batch)
            path = files.get(kind)
            if not path:
                failures.append(f"{batch}/{kind}: no JSONL listed for kind")
                continue
            url = f"{REPO}/resolve/main/{path}"
            payload = _get(url).decode("utf-8")
        except (urllib.error.URLError, OSError, ValueError, LookupError) as error:
            message = f"{type(error).__name__}: {error}"
            _record_failure(log, "rows", f"{REPO}/resolve/main/data/{batch}/{kind}", message, kind=kind, batch=batch)
            failures.append(f"{batch}/{kind}: {message}")
            continue
        count = 0
        for line in payload.split("\n"):
            if not line.strip():
                continue
            row = json.loads(line)
            sha256 = str(row.get("sha256") or "").strip().lower()
            if len(sha256) != 64:
                continue
            count += 1
            candidates.setdefault(sha256, {"row": row, "sha256": sha256, "kind": kind, "batch": batch})
        print(f"rows {kind}/{batch}: {count} candidates ({path})")
    return select(list(candidates.values()), limit, seed)


def main(kinds: list[str], limit: int, seed: int, out: Path, batches: tuple[str, ...] = BATCHES) -> int:
    out.mkdir(parents=True, exist_ok=True)
    pdf_dir = out / "pdf"
    pdf_dir.mkdir(parents=True, exist_ok=True)
    log = out / "fetch-failures.jsonl"
    log.unlink(missing_ok=True)
    failures: list[str] = []
    selected: list[dict] = []
    for kind in kinds:
        if kind not in KINDS:
            raise SystemExit(f"unknown kind {kind!r}; choose from {', '.join(KINDS)}")
        selected.extend(collect(kind, batches, limit, seed, log, failures))

    rows_path = out / "rows.jsonl"
    with rows_path.open("w", encoding="utf-8") as stream:
        for item in sorted(selected, key=lambda item: (item["kind"], item["batch"], item["sha256"])):
            stream.write(json.dumps({**item["row"], "kind": item["kind"], "batch": item["batch"]},
                                    ensure_ascii=False, sort_keys=True) + "\n")
    print(f"wrote {rows_path} ({len(selected)} rows)")

    downloaded = skipped = 0
    for item in sorted(selected, key=lambda item: (item["kind"], item["batch"], item["sha256"])):
        sha256, kind, batch = item["sha256"], item["kind"], item["batch"]
        destination = pdf_dir / f"{sha256}.pdf"
        try:
            directory = pdf_directory(batch, kind)
        except (urllib.error.URLError, OSError, ValueError, LookupError) as error:
            message = f"{type(error).__name__}: {error}"
            _record_failure(log, "pdf-listing", f"{API}/tree/main/pdf/{batch}", message, sha256=sha256, kind=kind, batch=batch)
            failures.append(f"{sha256[:12]} {kind}/{batch}: {message}")
            print(f"fail {sha256[:12]} {kind}/{batch} listing: {message}")
            continue
        url = f"{REPO}/resolve/main/{directory}/{sha256}.pdf"
        try:
            status = _download(url, destination, sha256)
        except RuntimeError as error:
            _record_failure(log, "pdf", url, str(error), sha256=sha256, kind=kind, batch=batch)
            failures.append(f"{sha256[:12]} {kind}/{batch}: {error}")
            print(f"fail {sha256[:12]} {kind}/{batch} {error}")
            continue
        downloaded += status == "ok"
        skipped += status == "skip"
        size = destination.stat().st_size if destination.exists() else 0
        print(f"{status} {sha256[:12]} {kind}/{batch} {destination} ({size} bytes)")

    print(f"summary: {len(selected)} rows, {downloaded} downloaded, {skipped} skipped, {len(failures)} failures")
    if failures:
        print(f"failures logged to {log}")
    return 0 if selected else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--kind", default="textbook", help="comma-separated kinds (paper, textbook, ebook)")
    parser.add_argument("--limit", type=int, default=12, help="rows per kind")
    parser.add_argument("--seed", type=int, default=7, help="sampling seed")
    parser.add_argument("--batch", default=",".join(BATCHES), help="comma-separated dataset batches")
    parser.add_argument("--out", default="benchmarks/sciencemeta/data", help="output directory")
    arguments = parser.parse_args()
    kinds = [kind.strip() for kind in arguments.kind.split(",") if kind.strip()]
    batches = tuple(batch.strip() for batch in arguments.batch.split(",") if batch.strip())
    sys.exit(main(kinds, arguments.limit, arguments.seed, Path(arguments.out), batches))
