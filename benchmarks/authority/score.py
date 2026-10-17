"""Score the authority gold set (P2 phase 3 / D5): precision, recall, F1 per type.

Two systems are compared on the same gold rows:

- ``new`` — the P2 pipeline: candidate pools from the same searches, ranked
  by the scored policy (anchors, embeddings when installed, ambiguity
  margin, script-mismatch skip). Works use the production work matcher
  (``select_candidate``, 0.90 title threshold), which P2 deliberately
  left untouched.
- ``baseline`` — what today's SequenceMatcher path would do with the same
  candidate pool: stdlib fuzzy top-hit at the same threshold, no anchors,
  no ambiguity margin, no script handling.

Outcome rules (the no-match rate is a headline number, not a hidden
failure):

- present label, linked with the labelled id → tp; linked with another
  id → fp + fn; no_match → fn.
- absent label, no_match → tn; linked → fp (a fabricated link).

Run: ``.venv/bin/python benchmarks/authority/score.py [--types person,place,...]``
First run is live (paced); re-runs hit ``cache/`` (7-day TTL on hits).
GeoNames joins the place pool only when ``GEONAMES_USERNAME`` is set.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path

AUTHORITY_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(AUTHORITY_DIR.parent.parent))

from citeindex.ingestion.authority.matching import (  # noqa: E402
    ENTITY_THRESHOLDS,
    fuzzy_similarity,
    select_authority_candidate,
)
from citeindex.ingestion.metadata_registry import RegistryClient  # noqa: E402
from citeindex.ingestion.models import IngestionConfig  # noqa: E402
from citeindex.ingestion.online_enrichment import select_candidate  # noqa: E402

GOLD = AUTHORITY_DIR / "gold.jsonl"
CACHE = AUTHORITY_DIR / "cache"
REPORTS = AUTHORITY_DIR / "reports"


def bench_config() -> IngestionConfig:
    return IngestionConfig(
        online_enrich=True,
        online_enrich_providers=("crossref", "orcid", "wikidata", "geonames"),
        online_enrich_timeout=30.0,
        online_enrich_cache_ttl=30 * 86400,
    )


def _client() -> RegistryClient:
    CACHE.mkdir(exist_ok=True)
    return RegistryClient(bench_config(), CACHE)


def _person_surface(source: dict) -> tuple[str, str | None]:
    if source.get("family"):
        return source["family"], source.get("given") or None
    return str(source.get("literal") or ""), None


def _work_csl(source: dict) -> dict:
    csl: dict = {"title": source.get("title") or "", "type": "article-journal", "_field_status": {}}
    if source.get("author"):
        csl["author"] = [{"family": source["author"]}]
        csl["_field_status"]["author"] = "source-supported"  # gold fields ARE the source evidence
    if source.get("year"):
        csl["issued"] = {"date-parts": [[int(source["year"])]]}
        csl["_field_status"]["issued"] = "source-supported"
    return csl


def candidate_pools(row: dict) -> dict[str, list[dict]]:
    """One search pass per row; both systems score the same pools."""
    client = _client()
    entity_type, source = row["entity_type"], row["source"]
    pools: dict[str, list[dict]] = {}
    if entity_type == "person":
        family, given = _person_surface(source)
        pools["orcid"] = client.search("orcid", family, author=given)
    elif entity_type == "place":
        surface = str(source.get("literal") or "")
        pools["wikidata"] = client.search("wikidata", surface)
        if client.search("geonames", surface)[0].get("status") != "skipped":
            pools["geonames"] = client.search("geonames", surface)
    elif entity_type == "journal":
        pools["crossref"] = client.search("crossref", source.get("title") or "")
    elif entity_type == "work":
        pools["crossref"] = client.search(
            "crossref", source.get("title") or "", author=source.get("author") or None, year=source.get("year")
        )
    return pools


def _pooled_results(pools: dict[str, list[dict]]) -> list[dict]:
    return [result for results in pools.values() for result in results]


def _entity_surfaces(row: dict, candidate: dict) -> tuple[str, str]:
    source = row["source"]
    if row["entity_type"] == "person":
        family, given = _person_surface(source)
        left = " ".join(part for part in (given, family) if part)
        right = " ".join(str(part) for part in (candidate.get("given"), candidate.get("family")) if part)
        return left or str(source.get("literal") or ""), right
    left = str(source.get("literal") or source.get("title") or "")
    return left, str(candidate.get("label") or candidate.get("container-title") or "")


def _work_winner(pools: dict[str, list[dict]], source: dict) -> str | None:
    """select_candidate returns the result wrapper; the DOI lives in its candidate."""
    winner, _ = select_candidate(_work_csl(source), _pooled_results(pools))
    return (winner or {}).get("candidate", {}).get("DOI")


def run_baseline(row: dict, pools: dict[str, list[dict]]) -> dict:
    """SequenceMatcher top-hit at the same threshold: today's approach."""
    if row["entity_type"] == "work":
        doi = _work_winner(pools, row["source"])
        return {"status": "linked", "id": doi} if doi else {"status": "no_match"}
    threshold = ENTITY_THRESHOLDS.get(row["entity_type"], 0.90)
    best_score, best = 0.0, None
    for result in _pooled_results(pools):
        if result.get("status") != "found":
            continue
        candidate = result.get("candidate") or {}
        left, right = _entity_surfaces(row, candidate)
        score = fuzzy_similarity(left, right, engine="stdlib")
        if score > best_score:
            best_score, best = score, candidate
    if best is None or best_score < threshold:
        return {"status": "no_match", "reason": "below_threshold" if best else "no_candidates"}
    return {"status": "linked", "id": (best.get("authority") or {}).get("id"), "score": round(best_score, 4)}


def run_new(row: dict, pools: dict[str, list[dict]]) -> dict:
    """The P2 policy; works go through the untouched production matcher."""
    if row["entity_type"] == "work":
        doi = _work_winner(pools, row["source"])
        return {"status": "linked", "id": doi} if doi else {"status": "no_match"}
    source = (
        row["source"]
        if row["entity_type"] == "person"
        else str(row["source"].get("literal") or row["source"].get("title") or "")
    )
    decision = select_authority_candidate(row["entity_type"], source, _pooled_results(pools))
    return {
        "status": decision["status"],
        "id": (decision.get("authority") or {}).get("id"),
        "reason": decision.get("reason"),
        "score": decision.get("score"),
    }


def score_row(outcome: dict, row: dict) -> str:
    """tp/fp/fn/tn for one decision against one gold label."""
    linked = outcome["status"] == "linked"
    correct_id = linked and str(outcome.get("id") or "") == str(row["label"]["id"])
    if row["label_status"] == "present":
        return "tp" if correct_id else ("fp" if linked else "fn")
    return "tn" if not linked else "fp"


def aggregate(outcomes: list[tuple[str, dict, dict]]) -> dict:
    """Per-type metrics + the headline no-match rate."""
    metrics: dict = {}
    for entity_type in {row["entity_type"] for _, _, row in outcomes}:
        rows = [(decision, row) for _, decision, row in outcomes if row["entity_type"] == entity_type]
        present = [(decision, row) for decision, row in rows if row["label_status"] == "present"]
        absent = [(decision, row) for decision, row in rows if row["label_status"] == "absent"]
        counts = defaultdict(int)
        for decision, row in rows:
            counts[score_row(decision, row)] += 1
        tp, fp, fn = counts["tp"], counts["fp"], counts["fn"]
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / len(present) if present else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        metrics[entity_type] = {
            "rows": len(rows),
            "present": len(present),
            "absent": len(absent),
            "tp": tp,
            "fp": fp,
            "fn": fn,
            "tn": counts["tn"],
            "precision": round(precision, 4),
            "recall": round(recall, 4),
            "f1": round(f1, 4),
            "no_match_rate": round(sum(1 for decision, _ in rows if decision["status"] != "linked") / len(rows), 4),
            "correct_no_match_rate": round(counts["tn"] / len(absent), 4) if absent else None,
        }
    return metrics


def evaluate(types: list[str]) -> dict:
    rows = [json.loads(line) for line in open(GOLD, encoding="utf-8") if line.strip()]
    # Journal authority generation is deferred (D2 record): the gold set keeps
    # journal labels, but no candidate generator exists, so scoring them would
    # measure a gap we already documented, not matching quality.
    if "journal" in types:
        types = [t for t in types if t != "journal"]
        print(
            "note: journal rows are in gold.jsonl but scoring is deferred until a journal candidate generator exists (see plan D2 record)"
        )
    rows = [row for row in rows if row["entity_type"] in types]
    outcomes: list[tuple[str, dict, dict]] = []
    started = time.monotonic()
    for index, row in enumerate(rows, 1):
        pools = candidate_pools(row)
        for system in ("new", "baseline"):
            outcome = run_new(row, pools) if system == "new" else run_baseline(row, pools)
            outcomes.append((system, outcome, row))
        if index % 50 == 0:
            print(f"  … {index}/{len(rows)} rows", flush=True)
    elapsed = time.monotonic() - started
    by_system: dict[str, dict] = {}
    for system in ("new", "baseline"):
        by_system[system] = aggregate([o for o in outcomes if o[0] == system])
    return {
        "gold_rows": len(rows),
        "latency_seconds_per_100": round(elapsed * 100 / len(rows), 2),
        "systems": by_system,
        "outcomes": outcomes,
    }


def print_report(report: dict) -> None:
    print(f"\ngold rows: {report['gold_rows']}  |  latency: {report['latency_seconds_per_100']}s per 100 entities")
    for system, metrics in report["systems"].items():
        print(f"\n=== {system} ===")
        print(f"{'type':10} {'rows':>5} {'P':>7} {'R':>7} {'F1':>7} {'no-match':>9} {'TN/absent':>10}")
        for entity_type, m in sorted(metrics.items()):
            tn = f"{m['correct_no_match_rate']}" if m["correct_no_match_rate"] is not None else "-"
            print(
                f"{entity_type:10} {m['rows']:>5} {m['precision']:>7} {m['recall']:>7} "
                f"{m['f1']:>7} {m['no_match_rate']:>9} {tn:>10}"
            )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--types", default="person,place,work", help="entity types to score (default: person,place,work)"
    )
    args = parser.parse_args()
    types = [t.strip() for t in args.types.split(",") if t.strip()]
    report = evaluate(types)
    print_report(report)
    REPORTS.mkdir(exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    path = REPORTS / f"{stamp}.json"
    payload = {key: value for key, value in report.items() if key != "outcomes"}
    payload["outcome_rows"] = [
        {
            "id": row["id"],
            "entity_type": row["entity_type"],
            "system": system,
            "label_status": row["label_status"],
            "label_id": row["label"]["id"],
            "outcome": decision,
        }
        for system, decision, row in report["outcomes"]
    ]
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"\nreport: {path}")


if __name__ == "__main__":
    main()
