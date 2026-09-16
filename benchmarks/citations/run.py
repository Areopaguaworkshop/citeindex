"""Score host metadata only, against independently reviewed labels."""
from __future__ import annotations
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path

FIELDS = ("type", "title", "subtitle", "author", "editor", "translator", "issued",
          "publisher", "publisher-place", "container-title", "collection-title",
          "edition", "volume", "issue", "page", "DOI", "ISBN", "URL")


def _norm(value: object, field: str = "") -> object:
    if isinstance(value, list):
        return [_norm(item, field) for item in value]  # author order is meaningful
    if isinstance(value, dict):
        return {key: _norm(item, field) for key, item in sorted(value.items())}
    text = " ".join(unicodedata.normalize("NFC", str(value or "")).casefold().split())
    if field == "DOI":
        text = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", text)
    if field == "ISBN":
        text = re.sub(r"[\s-]", "", text)
    if field == "page":
        text = text.replace("–", "-").replace("—", "-")
    return text


def _score_values(gold: dict, predictions: dict, field: str) -> dict:
    tp = fp = fn = evaluated = 0
    for source_id, item in gold.items():
        state = item.get("field_status", {}).get(field)
        if state in {"illegible", "outside_scope"}:
            continue
        evaluated += 1
        expected = item.get("csl", {}).get(field)
        prediction = predictions.get(source_id, {})
        actual = prediction.get("csl", {}).get(field) if prediction.get("status") == "ok" else None
        if expected is not None and actual is not None and _norm(expected, field) == _norm(actual, field):
            tp += 1
        else:
            fp += actual is not None
            fn += expected is not None
    return {"tp": tp, "fp": fp, "fn": fn, "evaluated": evaluated,
            "precision": tp / (tp + fp) if tp + fp else None,
            "recall": tp / (tp + fn) if tp + fn else None,
            "f1": 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None}


def _records(path: Path) -> dict[str, dict]:
    return {row["id"]: row for row in _attempts(path)}


def _attempts(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _percentile(values: list[float], quantile: float) -> float | None:
    values = sorted(float(value) for value in values if value is not None)
    return values[min(len(values) - 1, round((len(values) - 1) * quantile))] if values else None


def score(gold: dict, predictions: dict, attempts: list[dict]) -> dict:
    predictions = {key: row for key, row in predictions.items() if key in gold}
    attempts = [row for row in attempts if row["id"] in gold]
    report = {"sources": len(gold), "attempted": len(predictions), "attempts": len(attempts),
              "completed": sum(row.get("status") == "ok" for row in predictions.values()),
              "failed": sum(row.get("status") != "ok" for row in predictions.values()),
              "not_attempted": len(gold) - len(predictions),
              "fields": {field: _score_values(gold, predictions, field) for field in FIELDS}}
    report["exact_record_accuracy"] = sum(
        predictions.get(key, {}).get("status") == "ok" and all(
            _norm(row.get("csl", {}).get(field), field) == _norm(predictions[key].get("csl", {}).get(field), field)
            for field in FIELDS if row.get("field_status", {}).get(field) not in {"illegible", "outside_scope"})
        for key, row in gold.items()) / len(gold) if gold else None
    evidence = {"supplied": 0, "valid_span": 0, "invalid_span": 0,
                "matches_reviewed_attribution": 0, "needs_attribution_review": 0}
    for key, prediction in predictions.items():
        blocks = {block["id"]: block for block in prediction.get("source_blocks", [])}
        for field, item in prediction.get("csl", {}).get("_field_evidence", {}).items():
            evidence["supplied"] += 1
            locator = item.get("locator", {})
            block = blocks.get(item.get("block_id"), {})
            start, end = locator.get("char_start"), locator.get("char_end")
            valid = (bool(block) and locator.get("block_id") == block.get("id")
                     and type(start) is int and type(end) is int and 0 <= start < end <= len(block.get("text", ""))
                     and block["text"][start:end] == item.get("quote")
                     and any(k in block for k in ("physical_page_index", "section_index"))
                     and all(locator.get(k) == block.get(k) for k in ("physical_page_index", "section_index")))
            evidence["valid_span" if valid else "invalid_span"] += 1
            reviewed = gold[key].get("field_evidence", {}).get(field)
            matches = (valid and item == reviewed and
                       _norm(prediction["csl"].get(field), field) == _norm(gold[key]["csl"].get(field), field))
            evidence["matches_reviewed_attribution" if matches else "needs_attribution_review"] += 1
    report["evidence"] = evidence
    report["latency_seconds"] = {"p50": _percentile([r.get("seconds") for r in attempts], .5),
                                 "p95": _percentile([r.get("seconds") for r in attempts], .95)}
    report["cost"] = {field: sum(r[field] for r in attempts) if attempts and all(isinstance(r.get(field), (int, float)) for r in attempts) else None
                      for field in ("tokens", "cost_usd")}
    report["cost"]["measured_attempts"] = sum(r.get("cost_usd") is not None for r in attempts)
    return report


def main(manifest_path: str, predictions_path: str) -> None:
    from validate_manifest import validate_rows
    rows = _attempts(Path(manifest_path))
    errors = validate_rows(rows)
    if errors:
        raise SystemExit("\n".join(errors))
    gold = {row["id"]: row for row in rows}
    attempts = _attempts(Path(predictions_path))
    predictions = {row["id"]: row for row in attempts}
    mismatched = [key for key, row in predictions.items() if key in gold and row.get("source_sha256") != gold[key]["source_sha256"]]
    if mismatched:
        raise SystemExit(f"prediction source digest differs from reviewed source: {mismatched}")
    report = score(gold, predictions, attempts)
    report["manifest_sha256"] = hashlib.sha256(Path(manifest_path).read_bytes()).hexdigest()
    report["scorer_version"] = "host-v2"
    report["by_modality"] = {modality: score({k: r for k, r in gold.items() if r["modality"] == modality}, predictions, attempts)
                             for modality in sorted({r["modality"] for r in rows})}
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main(*sys.argv[1:])
