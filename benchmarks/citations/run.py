"""Score host metadata only, against independently reviewed labels."""
from __future__ import annotations
import hashlib
import json
import re
import sys
import unicodedata
from pathlib import Path

from citeindex.ingestion.csl import HOST_FIELDS, NAME_FIELDS, evaluation_fields
from citeindex.ingestion.citation_verification import validate_block_evidence

FIELDS = HOST_FIELDS


def evaluated_fields(row: dict) -> set[str]:
    return (evaluation_fields(row.get("csl", {}), row.get("modality", ""))
            | set(row.get("field_status", {}))
            | (set(row.get("csl", {})) & set(FIELDS)))


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


def _observed_accessed(prediction: dict) -> dict | None:
    """Access date belongs to this retrieval, not an earlier annotation run."""
    blocks = prediction.get("source_blocks") or []
    dates = {block.get("text") for block in blocks if block.get("metadata_key") == "accessed"}
    if len(dates) != 1:
        return None
    value = dates.pop()
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return None
    from citeindex.ingestion.csl import valid_host_value
    parsed = {"date-parts": [[int(part) for part in value.split("-")]]}
    return parsed if valid_host_value("accessed", parsed) else None


def _expected(row: dict, prediction: dict, field: str) -> object:
    if field == "accessed" and row.get("modality") == "url_article":
        return _observed_accessed(prediction) or row.get("csl", {}).get(field)
    return row.get("csl", {}).get(field)


def _composed_title(csl: dict) -> str:
    """Title with subtitle absorbed, matching CSL export semantics."""
    title = str(csl.get("title") or "")
    subtitle = csl.get("subtitle")
    if not title or not isinstance(subtitle, str):
        return title
    sub = str(_norm(subtitle, "subtitle"))
    return title if not sub or sub in str(_norm(title, "title")) else f"{title}: {subtitle}"


def _title_pair(gold_csl: dict, pred_csl: dict) -> tuple[object, object]:
    """Normalized composed titles; missing titles stay None to preserve missing/extra semantics."""
    expected = None if gold_csl.get("title") is None else _norm(_composed_title(gold_csl), "title")
    actual = None if pred_csl.get("title") is None else _norm(_composed_title(pred_csl), "title")
    return expected, actual


def _equiv_text(value: object, field: str) -> object:
    if isinstance(value, list):
        return [_equiv_text(item, field) for item in value]
    if isinstance(value, dict):
        return {key: _equiv_text(part, field) for key, part in sorted(value.items())}
    text = _norm(value, field)
    if not isinstance(text, str):
        return text
    # Typographic glyphs are convention, not content: dashes fold to '-', and
    # every quote mark folds to '"' (printed 'X' vs recorded “X”).
    text = text.translate(str.maketrans({"‘": '"', "’": '"', "“": '"', "”": '"',
                                         "'": '"', "–": "-", "—": "-"}))
    text = re.sub(r"\b([a-z])\.\s+(?=[a-z]\.)", r"\1.", text)
    # Equivalence conventions (2026-09-26 two-type plan, user-approved):
    # honorific author prefixes and leading-'The' publishers are convention;
    # joint-publisher '; ' vs ' and ' joins are the same set of presses.
    if field in NAME_FIELDS:
        text = re.sub(r"^saint\s+", "", text)
        # Ecclesiastical honorific prefixes are convention, not content
        # (aimilianos: 'Archimandrite Aimilianos of Simonopetra' ==
        # 'Aimilianos of Simonopetra'; the title page prints the office).
        text = re.sub(r"^(?:archimandrite|fr\.|fr)\s+", "", text)
    if field == "publisher":
        text = re.sub(r"\s*&\s*", " and ", text)
        text = re.sub(r"^the\s+", "", text)
        parts = [p for p in re.split(r"\s*(?:;| and )\s*", text) if p]
        if len(parts) > 1:
            return sorted(set(parts))
    return text


def _equivalent(row: dict, prediction: dict, field: str) -> bool:
    expected, actual = row.get("csl", {}), prediction.get("csl", {})
    if field == "URL":
        retrieval = prediction.get("retrieval_metadata") or {
            block.get("metadata_key"): block.get("text") for block in prediction.get("source_blocks", [])
            if block.get("metadata_key") in {"URL", "requested_url"}}
        if expected.get("URL") == retrieval.get("requested_url") and retrieval.get("URL"):
            return actual.get("URL") == retrieval.get("URL")
    if field in {"title", "subtitle"}:
        return _equiv_text(_composed_title(expected), "title") == _equiv_text(_composed_title(actual), "title")
    if field in NAME_FIELDS:
        def names(value: object) -> object:
            if not isinstance(value, list):
                return value
            return [_equiv_text(name.get("literal") or " ".join(str(name.get(part, "")) for part in
                    ("given", "non-dropping-particle", "family", "suffix")).strip(), field)
                    if isinstance(name, dict) else name for name in value]
        return names(_expected(row, prediction, field)) == names(actual.get(field))
    return _equiv_text(_expected(row, prediction, field), field) == _equiv_text(actual.get(field), field)


def _score_values(gold: dict, predictions: dict, field: str) -> dict:
    tp = fp = fn = evaluated = 0
    for source_id, item in gold.items():
        state = item.get("field_status", {}).get(field)
        if field not in evaluated_fields(item) or state in {"illegible", "outside_scope"}:
            continue
        evaluated += 1
        prediction = predictions.get(source_id, {})
        if field == "title":
            expected, actual = _title_pair(item.get("csl", {}),
                                           prediction.get("csl", {}) if prediction.get("status") == "ok" else {})
            matched = expected is not None and actual is not None and expected == actual
        else:
            expected = _expected(item, prediction, field)
            actual = prediction.get("csl", {}).get(field) if prediction.get("status") == "ok" else None
            matched = expected is not None and actual is not None and _norm(expected, field) == _norm(actual, field)
        if matched:
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
    return [json.loads(line) for line in path.read_text().split("\n") if line.strip()]


def _percentile(values: list[float], quantile: float) -> float | None:
    values = sorted(float(value) for value in values if value is not None)
    return values[min(len(values) - 1, round((len(values) - 1) * quantile))] if values else None


def _redirect_only(prediction: dict) -> bool:
    text = " ".join(block.get("text", "") for block in prediction.get("source_blocks", [])
                    if not block.get("metadata_key"))
    return bool(text and len(text) < 300 and "redirecting" in text.casefold())


def _exact_field_equal(row: dict, prediction: dict, field: str) -> bool:
    """Raw record-field equality, with title compared through the composed title."""
    gold_csl, pred_csl = row.get("csl", {}), prediction.get("csl", {})
    if field == "title":
        return _norm(_composed_title(gold_csl), "title") == _norm(_composed_title(pred_csl), "title")
    if field == "subtitle" and not gold_csl.get("subtitle") and pred_csl.get("subtitle"):
        return _norm(_composed_title(gold_csl), "title") == _norm(_composed_title(pred_csl), "title")  # ponytail: absorbed by composed title
    return _norm(_expected(row, prediction, field), field) == _norm(pred_csl.get(field), field)


# Category-aware core requirements (two-type model, 2026-09-26):
# article = type, author, title, container-title, present numerics;
# book = type, present author-side name, title, publisher, issued.
CORE_BY_TYPE = {
    "article-journal": ("type", "title", "author", "container-title", "volume", "issue", "page"),
    "chapter": ("type", "title", "container-title", "author", "editor", "page", "publisher", "issued"),
    "entry-encyclopedia": ("type", "title", "container-title", "author", "editor", "page", "publisher", "issued"),
}


def _core_fields(row: dict) -> set[str]:
    """Core fields a source must get right, per its category.

    Only fields live in the supplied source count: gold values marked
    illegible/outside_scope in field_status are excluded — a number not
    printed in the scan is not required of extraction.
    """
    csl = row.get("csl", {})
    status = row.get("field_status", {})
    def live(field: str) -> bool:
        return csl.get(field) is not None and status.get(field) not in {"illegible", "outside_scope"}
    default = ("type", "title", "author", "editor", "translator", "publisher", "issued")
    return {f for f in CORE_BY_TYPE.get(csl.get("type"), default) if live(f)}


def score(gold: dict, predictions: dict, attempts: list[dict]) -> dict:
    predictions = {key: row for key, row in predictions.items() if key in gold}
    attempts = [row for row in attempts if row["id"] in gold]
    report = {"sources": len(gold), "attempted": len(predictions), "attempts": len(attempts),
              "completed": sum(row.get("status") == "ok" for row in predictions.values()),
              "failed": sum(row.get("status") != "ok" for row in predictions.values()),
              "not_attempted": len(gold) - len(predictions),
              "fields": {field: _score_values(gold, predictions, field) for field in FIELDS}}
    report["redirect_only_snapshots"] = sum(
        row.get("modality") == "url_article" and _redirect_only(predictions.get(key, {}))
        for key, row in gold.items())
    report["exact_record_accuracy"] = sum(
        predictions.get(key, {}).get("status") == "ok" and not _redirect_only(predictions[key]) and all(
            _exact_field_equal(row, predictions[key], field)
            for field in evaluated_fields(row) if row.get("field_status", {}).get(field) not in {"illegible", "outside_scope"})
        for key, row in gold.items()) / len(gold) if gold else None
    report["equivalent_record_accuracy"] = sum(
        predictions.get(key, {}).get("status") == "ok" and not _redirect_only(predictions[key]) and all(
            _equivalent(row, predictions[key], field)
            for field in evaluated_fields(row) if row.get("field_status", {}).get(field) not in {"illegible", "outside_scope"}
        )
        for key, row in gold.items()) / len(gold) if gold else None
    core_records = {}
    for key, row in gold.items():
        fields = _core_fields(row)
        if not fields:
            continue
        prediction = predictions.get(key, {})
        core_records[key] = bool(prediction.get("status") == "ok" and not _redirect_only(prediction)
                                  and all(_equivalent(row, prediction, f) for f in fields))
    report["core_record_accuracy"] = sum(core_records.values()) / len(core_records) if core_records else None
    report["core_records"] = core_records
    report["accessed_gold_date_differs_from_run"] = sum(
        row.get("modality") == "url_article" and (observed := _observed_accessed(predictions.get(key, {}))) is not None
        and _norm(observed, "accessed") != _norm(row.get("csl", {}).get("accessed"), "accessed")
        for key, row in gold.items())
    errors: dict[str, dict[str, int]] = {}
    for key, row in gold.items():
        prediction = predictions.get(key, {})
        for field in evaluated_fields(row):
            if row.get("field_status", {}).get(field) in {"illegible", "outside_scope"}:
                continue
            expected = _expected(row, prediction, field)
            actual = prediction.get("csl", {}).get(field) if prediction.get("status") == "ok" else None
            if field == "title":
                expected, actual = _title_pair(row.get("csl", {}),
                                              prediction.get("csl", {}) if prediction.get("status") == "ok" else {})
                if expected == actual:
                    continue
            elif _norm(expected, field) == _norm(actual, field):
                continue
            reason = "failed_source" if prediction.get("status") != "ok" else "missing" if actual is None else "extra" if expected is None else "different"
            errors.setdefault(field, {}).setdefault(reason, 0)
            errors[field][reason] += 1
    report["field_errors"] = errors
    gold_evidence = {"checked": 0, "unresolved": 0, "external_or_visual": 0,
                     "different_retrieval_date": 0, "quote_not_in_block": 0,
                     "value_not_literal": 0, "unresolved_source_ids": []}
    for key, row in gold.items():
        blocks = predictions.get(key, {}).get("source_blocks", [])
        for field, item in row.get("field_evidence", {}).items():
            if row.get("field_status", {}).get(field) != "present" or not isinstance(item, dict):
                continue
            block_id = item.get("block_id") or item.get("locator", {}).get("block_id")
            if not block_id:
                gold_evidence["external_or_visual"] += 1
                continue
            gold_evidence["checked"] += 1
            if not validate_block_evidence(field, row.get("csl", {}).get(field), {**item, "block_id": block_id}, blocks):
                gold_evidence["unresolved"] += 1
                block = next((b for b in blocks if b.get("id") == block_id), None)
                if field == "accessed" and _observed_accessed(predictions.get(key, {})) != row.get("csl", {}).get("accessed"):
                    gold_evidence["different_retrieval_date"] += 1
                elif not block or item.get("quote", "") not in block.get("text", ""):
                    gold_evidence["quote_not_in_block"] += 1
                else:
                    gold_evidence["value_not_literal"] += 1
                if key not in gold_evidence["unresolved_source_ids"]:
                    gold_evidence["unresolved_source_ids"].append(key)
    report["gold_evidence_against_run_blocks"] = gold_evidence
    evidence = {"supplied": 0, "valid_span": 0, "invalid_span": 0, "value_matches_gold": 0,
                "matches_reviewed_attribution": 0, "needs_attribution_review": 0}
    for key, prediction in predictions.items():
        blocks = {block["id"]: block for block in prediction.get("source_blocks", [])}
        for field, item in prediction.get("csl", {}).get("_field_evidence", {}).items():
            evidence["supplied"] += 1
            resolved = validate_block_evidence(field, prediction.get("csl", {}).get(field), item, list(blocks.values()))
            valid = resolved is not None and resolved["locator"] == item.get("locator")
            evidence["valid_span" if valid else "invalid_span"] += 1
            if valid and _norm(prediction["csl"].get(field), field) == _norm(_expected(gold[key], prediction, field), field):
                evidence["value_matches_gold"] += 1
            reviewed = gold[key].get("field_evidence", {}).get(field)
            reviewed_locator = reviewed.get("locator", {}) if isinstance(reviewed, dict) else {}
            item_locator = item.get("locator", {})
            reviewed_block_id = (reviewed.get("block_id") or reviewed_locator.get("block_id")) if isinstance(reviewed, dict) else None
            item_block_id = item.get("block_id") or item_locator.get("block_id")
            matches = (valid and isinstance(reviewed, dict) and
                       item.get("quote") == reviewed.get("quote") and
                       item_block_id == reviewed_block_id and
                       item_locator == reviewed_locator and
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
    if any(row.get("pilot_selection") is not None for row in rows):
        rows = [row for row in rows if row.get("pilot_selection") != "excluded"]
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
    report["scorer_version"] = "host-v6-core-equivalence"
    report["by_modality"] = {modality: score({k: r for k, r in gold.items() if r["modality"] == modality}, predictions, attempts)
                             for modality in sorted({r["modality"] for r in rows})}
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main(*sys.argv[1:])
