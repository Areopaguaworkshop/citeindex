"""Score enrichment against independent source and registry gold; no network calls."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from collections import Counter
from pathlib import Path

from citeindex.ingestion.csl import HOST_FIELDS

try:
    from .run import _core_fields, _norm, _percentile
except ImportError:  # Direct script invocation, like the existing benchmark runner.
    from run import _core_fields, _norm, _percentile


def candidate_digest(csl: dict) -> str:
    return hashlib.sha256(json.dumps(csl, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def mask_candidate(csl: dict, fields: list[str], no_identifiers: bool = False) -> dict:
    """Mask values and evidence together; never carry reports or registry caches."""
    masked = set(fields) | ({"DOI", "ISBN"} if no_identifiers else set())
    if masked - set(HOST_FIELDS):
        raise ValueError("unknown masked CSL fields")
    result = {field: copy.deepcopy(value) for field, value in csl.items()
              if field in HOST_FIELDS and field not in masked}
    for key in ("_field_status", "_field_evidence"):
        retained = {field: copy.deepcopy(value) for field, value in csl.get(key, {}).items()
                    if field in result}
        if retained:
            result[key] = retained
    return result


def _rate(numerator: int, denominator: int) -> dict:
    return {"numerator": numerator, "denominator": denominator,
            "rate": numerator / denominator if denominator else None}


def _same(left: object, right: object, field: str) -> bool:
    return _norm(left, field) == _norm(right, field)


def _present(value: object) -> bool:
    return value is not None and value != "" and value != [] and value != {}


def _gold_rows(rows: list[dict]) -> dict:
    gold = {}
    for row in rows:
        key = row["id"]
        if key in gold:
            raise ValueError(f"duplicate gold ID: {key}")
        if (row.get("source_review_status") != "reviewed" or
                row.get("registry_review_status") != "reviewed" or
                row.get("registry_gold_origin") != "independent_review"):
            raise ValueError(f"{key}: independent reviewed source and registry gold required")
        if not isinstance(row.get("source_csl"), dict) or not isinstance(row.get("registry_csl"), dict):
            raise ValueError(f"{key}: source_csl and registry_csl must be separate objects")
        digest = row.get("source_sha256", "")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError(f"{key}: source SHA-256 required")
        if (row.get("split") not in {"development", "heldout"} or
                not row.get("work_family") or row.get("script") not in {"CJK", "Latin", "mixed"} or
                not isinstance(row.get("eligible"), bool)):
            raise ValueError(f"{key}: frozen split, work_family, script and eligibility required")
        if not set(row.get("masked_fields", [])) <= set(row["registry_csl"]):
            raise ValueError(f"{key}: masked fields need reviewed registry gold")
        gold[key] = row
    families = {}
    for row in rows:
        previous = families.setdefault(row["work_family"], row["split"])
        if previous != row["split"]:
            raise ValueError("work family occurs in development and heldout")
    return gold


def score(rows: list[dict], predictions: list[dict]) -> dict:
    gold = _gold_rows(rows)
    latest = {row["id"]: row for row in predictions}
    unknown = set(latest) - set(gold)
    if unknown:
        raise ValueError(f"predictions without reviewed gold: {sorted(unknown)}")
    counts = Counter()
    telemetry = Counter()
    latencies, costs = [], []
    changed_cohorts = Counter()
    facets = {name: {} for name in ("modality", "script", "work_type", "provider", "tier")}
    for key, row in gold.items():
        prediction = latest.get(key, {})
        if prediction and prediction.get("source_sha256") != row["source_sha256"]:
            raise ValueError(f"{key}: prediction source digest mismatch")
        before = prediction.get("before_csl")
        report = prediction.get("online_enrichment") or {}
        if before is None:
            if prediction and "candidate_input_snapshot" not in report:
                raise ValueError(f"{key}: pre-enrichment snapshot required")
            before = report.get("candidate_input_snapshot", {})
        if not isinstance(before, dict):
            raise ValueError(f"{key}: before_csl must be an object")
        succeeded = prediction.get("status") == "ok"
        after = prediction.get("csl", {}) if succeeded else before
        expected = row["registry_csl"]
        fields = set(expected) | set(row.get("registry_absent_fields", []))
        changes = {field for field in set(before) | set(after)
                   if field in HOST_FIELDS and not _same(before.get(field), after.get(field), field)}
        if changes - fields:
            raise ValueError(f"{key}: changed fields lack registry adjudication: {sorted(changes - fields)}")
        counts["inputs"] += 1
        counts["failed_or_missing_inputs"] += not succeeded
        counts["eligible"] += row["eligible"]
        counts["abstentions"] += row["eligible"] and not changes
        counts["changed_records"] += bool(changes)
        identity = row.get("change_review", {})
        reviewed_identity = (identity.get("review_status") == "reviewed" and
                             identity.get("prediction_digest") == candidate_digest(after) and
                             isinstance(identity.get("same_work_and_edition"), bool))
        counts["identity_reviewed"] += bool(changes) and reviewed_identity
        wrong_identity = bool(changes) and reviewed_identity and not identity["same_work_and_edition"]
        counts["wrong_work_or_edition"] += wrong_identity
        correct_identity = reviewed_identity and identity["same_work_and_edition"]
        if changes:
            changed_cohorts[row["script"]] += 1
        decisions = {item["field"]: item for item in report.get("decisions", [])
                     if item.get("field") and item.get("action") in {"fill", "overwrite"}}
        providers = {item.get("provenance", {}).get("provider", "unknown") for item in report.get("attempts", []) + list(decisions.values())}
        cohorts = {"modality": {row.get("modality", "unknown")}, "script": {row["script"]},
                   "work_type": {row["source_csl"].get("type", "unknown")},
                   "provider": providers or {"unattempted"},
                   "tier": {item.get("tier", "unknown") for item in decisions.values()} or {"unapplied"}}
        for facet, values in cohorts.items():
            for value in values:
                item = facets[facet].setdefault(value, Counter())
                item["inputs"] += 1
                item["eligible"] += row["eligible"]
                item["abstentions"] += row["eligible"] and not changes
                item["changed_records"] += bool(changes)
        for field in fields:
            was_correct = _present(before.get(field)) and _same(before.get(field), expected.get(field), field)
            counts["previously_correct_fields"] += was_correct
            if field not in changes:
                continue
            decision = decisions.get(field, {})
            correct = correct_identity and _same(after.get(field), expected.get(field), field)
            correction = _present(before.get(field))
            counts["applied_fields"] += 1
            counts["correct_applied_fields"] += correct
            counts["corrections" if correction else "fills"] += 1
            counts["correct_corrections" if correction else "correct_fills"] += correct
            counts["harmful_overwrites"] += correction and was_correct and not correct
            source_status = before.get("_field_status", {}).get(field)
            counts["source_overwrites"] += correction and (field in before.get("_field_evidence", {}) or
                                                            source_status in {"source-supported", "verified", "source-repaired"})
            counts["source_repair_reversals"] += correction and (source_status == "source-repaired" or
                                                                 field in report.get("source_repaired_fields", []))
            provenance = decision.get("provenance") or {}
            counts["unprovenanced_writes"] += not all(provenance.get(name) for name in ("provider", "response_digest", "request_identifier"))
            chain = [item for item in report.get("decisions", []) if item.get("field") == field]
            previous = before.get(field)
            invalid_chain = not chain
            for step in chain:
                invalid_chain |= not _same(step.get("old_value"), previous, field)
                previous = step.get("new_value")
            counts["audit_value_mismatches"] += invalid_chain or not _same(previous, after.get(field), field)
            counts["forbidden_overwrites"] += correction and decision.get("tier") not in {"T1", "T2"}
            for facet, value in (("modality", row.get("modality", "unknown")),
                                 ("script", row["script"]), ("work_type", row["source_csl"].get("type", "unknown")),
                                 ("provider", provenance.get("provider", "unknown")), ("tier", decision.get("tier", "unknown"))):
                item = facets[facet].setdefault(value, Counter())
                item["applied_fields"] += 1
                item["correct_fields"] += correct
                item["corrections" if correction else "fills"] += 1
                item["correct_corrections" if correction else "correct_fills"] += correct
                item["harmful_overwrites"] += correction and was_correct and not correct
                item["wrong_work_or_edition_fields"] += wrong_identity
        for field in row.get("masked_fields", []):
            counts["masked_fields"] += 1
            counts["baseline_recovered"] += _same(before.get(field), expected[field], field)
            counts["recovered"] += succeeded and (field not in changes or correct_identity) and _same(after.get(field), expected[field], field)
        core = _core_fields({"csl": row["source_csl"], "field_status": row.get("source_field_status", {})})
        if core:
            counts["core_records"] += 1
            counts["baseline_core_correct"] += all(_same(before.get(field), row["source_csl"][field], field) for field in core)
            counts["core_correct"] += succeeded and all(_same(after.get(field), row["source_csl"][field], field) for field in core)
        transport = report.get("transport", report)
        for name in ("request_count", "cache_hits", "cache_misses", "offline_requests", "unsafe_url_fetches", "failed_proposal_successes"):
            telemetry[name] += transport.get(name, 0)
        telemetry["measured_records"] += bool(transport)
        telemetry["cache_measured_records"] += "cache_misses" in transport
        telemetry["budget_violations"] += transport.get("request_count", 0) > 12 or report.get("elapsed_seconds", 0) > report.get("budget_seconds", 20)
        for name, amount in transport.get("provider_errors", {}).items():
            telemetry[f"provider_error:{name}"] += amount
        if isinstance(report.get("elapsed_seconds"), (int, float)):
            latencies.append(report["elapsed_seconds"])
        if isinstance(report.get("ai_cost_usd"), (int, float)):
            costs.append(report["ai_cost_usd"])
    metrics = {name: _rate(counts[num], counts[den]) for name, num, den in (
        ("recovery", "recovered", "masked_fields"), ("baseline_recovery", "baseline_recovered", "masked_fields"),
        ("applied_precision", "correct_applied_fields", "applied_fields"), ("fill_precision", "correct_fills", "fills"),
        ("correction_precision", "correct_corrections", "corrections"), ("harmful_overwrites", "harmful_overwrites", "previously_correct_fields"),
        ("wrong_work_or_edition", "wrong_work_or_edition", "changed_records"), ("abstention", "abstentions", "eligible"),
        ("core_accuracy", "core_correct", "core_records"), ("baseline_core_accuracy", "baseline_core_correct", "core_records"))}
    # Exact one-sided binomial upper bound for zero observed failures.
    n = counts["changed_records"]
    counts["unreviewed_changed_records"] = n - counts["identity_reviewed"]
    if counts["unreviewed_changed_records"]:
        metrics["wrong_work_or_edition"]["rate"] = None
    upper = -math.expm1(math.log(.05) / n) if n and counts["identity_reviewed"] == n and not counts["wrong_work_or_edition"] else None
    gain = ((metrics["recovery"]["rate"] or 0) - (metrics["baseline_recovery"]["rate"] or 0))
    gates = {
        "heldout_only": bool(rows) and all(row["split"] == "heldout" for row in rows),
        "reviewed_300_changed_zero_wrong_identity": n >= 300 and counts["identity_reviewed"] == n and not counts["wrong_work_or_edition"],
        "CJK_50_changed": changed_cohorts["CJK"] >= 50,
        "Latin_50_changed": changed_cohorts["Latin"] >= 50,
        "fill_precision_99pct": (metrics["fill_precision"]["rate"] or 0) >= .99,
        "correction_precision_99pct": (metrics["correction_precision"]["rate"] or 0) >= .99,
        "applied_precision_99pct": (metrics["applied_precision"]["rate"] or 0) >= .99,
        "recovery_gain_10pp": counts["masked_fields"] > 0 and gain >= .10 - 1e-12,
        "no_core_regression": counts["core_records"] > 0 and counts["core_correct"] >= counts["baseline_core_correct"],
        "no_unprovenanced_or_forbidden_writes": not any(counts[name] for name in ("unprovenanced_writes", "forbidden_overwrites", "audit_value_mismatches")),
        "safety_checks_reviewed": bool(rows) and all(row.get("safety_review_status") == "reviewed" and
                                                    row.get("safety_checks_passed") is True for row in rows),
        "no_transport_violations": not any(telemetry[name] for name in ("offline_requests", "unsafe_url_fetches", "failed_proposal_successes")),
        "observed_budget_respected": not telemetry["budget_violations"],
    }
    return {"scorer_version": "enrichment-v1", "counts": dict(counts), "metrics": metrics,
            "changed_by_script": dict(changed_cohorts), "by": facets, "telemetry": dict(telemetry),
            "cache_hit_rate": (_rate(telemetry["cache_hits"], telemetry["cache_hits"] + telemetry["cache_misses"])
                               if telemetry["measured_records"] and telemetry["cache_measured_records"] == telemetry["measured_records"] else None),
            "added_latency_seconds": {"measured_records": len(latencies), "p50": _percentile(latencies, .5), "p95": _percentile(latencies, .95)},
            "ai_cost_usd": sum(costs) if costs else None, "ai_cost_measured_records": len(costs),
            "wrong_identity_95pct_upper_bound_zero_failures": upper,
            "recovery_gain_percentage_points": gain * 100, "release_gates": gates,
            "default_on_release_eligible": all(gates.values())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("gold")
    parser.add_argument("predictions")
    args = parser.parse_args()
    def read(path):
        return [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
    report = score(read(args.gold), read(args.predictions))
    report["gold_sha256"] = hashlib.sha256(Path(args.gold).read_bytes()).hexdigest()
    report["predictions_sha256"] = hashlib.sha256(Path(args.predictions).read_bytes()).hexdigest()
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
