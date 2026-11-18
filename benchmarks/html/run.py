"""Live benchmark for deterministic URL/HTML metadata extraction.

Measures the no-LLM path only:

    fetch -> _extract_html_head_metadata

for the URLs in ``manifest.jsonl``. Zotero, DSPy, and PageIndex are never
invoked, so scores reflect what a bare CiteIndex install can extract from
``<head>`` metadata (Highwire Press, Dublin Core, OpenGraph, Prism, JSON-LD).

Usage:
    uv run python benchmarks/html/run.py
    uv run python benchmarks/html/run.py --json benchmarks/html/results/$(date +%F).json

Each manifest line:
    {"kind": "scholarly|reference|blog|news", "url": "...",
     "title_contains": "...", "doi": "10...." | null,
     "container": "..." | null, "min_authors": 0, "date": true|false}

Notes:
* ``min_authors: 0`` means authors are not scored for that item.
* ``doi``/``container`` set to null means "not expected", not "must be absent".
* Fetch failures (403, captcha, timeouts) are reported separately and excluded
  from extraction accuracy.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from citeindex.ingestion.pipelines import url_article  # noqa: E402


def norm(value: object) -> str:
    if value is None:
        return ""
    text = re.sub(r"<[^>]+>", "", str(value))
    return re.sub(r"\s+", " ", text).strip().casefold()


# Interstitials that mean "we did not get the article", not "the extractor failed".
BLOCK_MARKERS = (
    "checking your browser",
    "just a moment",
    "client challenge",
    "verification required",
    "captcha",
    "access denied",
)


def is_blocked(html: str) -> bool:
    match = re.search(r"<title[^>]*>(.*?)</title>", html[:8000], re.S | re.I)
    title = re.sub(r"\s+", " ", match.group(1)).strip().casefold() if match else ""
    if any(marker in title for marker in BLOCK_MARKERS):
        return True
    # Interstitials without a meaningful <title> are short and script-only.
    return len(html) < 25_000 and any(marker in html[:4000].casefold() for marker in BLOCK_MARKERS)


def evaluate(case: dict) -> dict:
    started = time.time()
    row = {
        "url": case["url"],
        "kind": case.get("kind", "scholarly"),
        "fetch": False,
        "blocked": False,
        "title": False,
        "doi": None,
        "authors": 0,
        "date": False,
        "container": None,
        "fields": [],
        "seconds": None,
        "error": None,
    }
    html = None
    for attempt in (1, 2):
        try:
            _final_url, html, _redirects = url_article._fetch_article(case["url"])
            break
        except Exception as exc:  # noqa: BLE001 - benchmark records fetch failures
            row["error"] = f"{type(exc).__name__}: {exc}"
            html = None
            if attempt == 1:
                time.sleep(1.5)
    if html is None:
        return row

    row["fetch"] = True
    row["seconds"] = round(time.time() - started, 2)
    if is_blocked(html):
        row["blocked"] = True
        row["error"] = "bot interstitial"
        return row

    meta = url_article._extract_html_head_metadata(html)
    row["fields"] = sorted(meta)
    title = norm(meta.get("title"))
    row["title"] = bool(title) and norm(case.get("title_contains")) in title
    authors = meta.get("author")
    row["authors"] = len(authors) if isinstance(authors, list) else 0
    row["date"] = bool(meta.get("date"))

    expected_doi = case.get("doi")
    if expected_doi is not None:
        doi = meta.get("DOI")
        row["doi"] = bool(doi) and str(doi).strip().casefold() == expected_doi.casefold()

    expected_container = case.get("container")
    if expected_container is not None:
        container = norm(meta.get("container-title"))
        row["container"] = bool(container) and norm(expected_container) in container
    return row


def rate(hits: int, total: int) -> str:
    return f"{hits}/{total} ({100 * hits / total:.0f}%)" if total else "0/0"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(Path(__file__).with_name("manifest.jsonl")))
    parser.add_argument("--json", dest="json_path", help="write full results to this path")
    args = parser.parse_args()

    cases = [json.loads(line) for line in Path(args.manifest).read_text().splitlines() if line.strip()]
    rows = []
    for index, case in enumerate(cases):
        if index:
            time.sleep(0.5)
        rows.append(evaluate(case))
    for case, row in zip(cases, rows, strict=True):
        row["authors_ok"] = row["authors"] >= case.get("min_authors", 0)
        row["date_ok"] = (not case.get("date", False)) or row["date"]
        if case.get("doi") is not None:
            row["doi_ok"] = row["doi"]
        if case.get("container") is not None:
            row["container_ok"] = row["container"]

    reachable = [row for row in rows if row["fetch"] and not row["blocked"]]
    blocked = [row for row in rows if row["blocked"]]
    scholarly = [row for row in reachable if row["kind"] == "scholarly"]

    def fmt(value: object) -> str:
        return "ok" if value else ("-" if value is None else "MISS")

    print(f"{'#':>2}  {'kind':<9} {'fetch':<5} {'title':<5} {'doi':<5} "
          f"{'auth':<5} {'date':<5} {'cont':<5} url")
    for index, row in enumerate(rows, 1):
        fetch_state = "BLCK" if row["blocked"] else fmt(row["fetch"])
        print(f"{index:>2}  {row['kind']:<9} {fetch_state:<5} {fmt(row['title']):<5} "
              f"{fmt(row.get('doi_ok')):<5} {fmt(row.get('authors_ok')):<5} "
              f"{fmt(row.get('date_ok')):<5} {fmt(row.get('container_ok')):<5} {row['url'][:70]}")
        if row["error"]:
            print(f"      error: {row['error']}")

    summary = {
        "manifest": args.manifest,
        "cases": len(rows),
        "fetched": len(reachable),
        "blocked": len(blocked),
        "title": rate(sum(row["title"] for row in reachable), len(reachable)),
        "scholarly_title": rate(sum(row["title"] for row in scholarly), len(scholarly)),
        "scholarly_doi": rate(
            sum(bool(row.get("doi_ok")) for row in scholarly),
            sum(1 for row in scholarly if row.get("doi_ok") is not None),
        ),
        "scholarly_author": rate(sum(bool(row.get("authors_ok")) for row in scholarly), len(scholarly)),
        "scholarly_date": rate(sum(bool(row.get("date_ok")) for row in scholarly), len(scholarly)),
        "scholarly_container": rate(
            sum(bool(row.get("container_ok")) for row in scholarly),
            sum(1 for row in scholarly if row.get("container_ok") is not None),
        ),
    }
    print("\nSUMMARY")
    for key, value in summary.items():
        print(f"  {key:<22} {value}")

    payload = {"summary": summary, "rows": rows}
    if args.json_path:
        target = Path(args.json_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"\nJSON: {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
