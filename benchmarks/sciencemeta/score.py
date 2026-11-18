"""Score CiteIndex CSL predictions against ScienceMetaBench gold rows.

Join key and prediction schema follow ``benchmarks/citations/run.py``: a
prediction row is ``{"id": ..., "status": "ok"|..., "csl": {...}}`` and a field is
only read when ``status == "ok"``. A bare CiteIndex ``csl.json`` object
(``{"id": ..., "type": ..., ...}``) is also accepted and treated as ``ok``.

Evaluated fields are the five fields this track can label — ``title``,
``author``, ``publisher``, ``issued``, ``ISBN`` — minus anything the manifest
marks ``illegible``/``outside_scope``. A record with no evaluated field is not
scored (it has no denominator).

Metrics (each with n and a Wilson 95% CI):

* strict record accuracy — normalized exact equality on every evaluated field,
  with person fields (``author``/``editor``/``translator``) compared by
  canonical name tokens so a gold literal and a family/given split agree;
* soft record accuracy — case/punctuation/space-insensitive equality (person
  fields use the same canonical name tokens);
* Levenshtein record accuracy — per-field ``difflib`` ratio >= 0.8;
* per-field precision/recall/F1 under the strict rule;
* instance recall — every *gold-present* field correct under soft matching.

Adapter rules (scorer version ``sciencemeta-v3``) keep the comparison at the
shape the gold actually carries, so a right answer in another shape is not a
miss. Each applied rule is counted in ``report["adapter"]`` whenever it flips a
comparison:

* ``title_composed`` — ``title`` + ``subtitle`` are composed on both sides,
  because a gold row may keep the subtitle inside ``title``;
* ``publisher_split`` — a gold imprint (``White Plains, NY : Longman``) is also
  compared against the publisher part after its last separator;
* ``date_year_only`` / ``date_precision`` — a date is compared at the gold's
  precision and never at a greater one.

Usage:
    python score.py --manifest manifest.jsonl --predictions predictions.jsonl \
        --out-dir reports --label textbook-20251022

Writes ``<out-dir>/report.json`` and ``<out-dir>/report.md``; the label is the
report title, so use a distinct ``--out-dir`` per label to keep both.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import math
import re
import unicodedata
from pathlib import Path

from citeindex.ingestion.csl import NAME_FIELDS, split_publisher

FIELDS = ("title", "author", "publisher", "issued", "ISBN")
# Person-valued CSL fields compare by canonical name tokens in strict and soft
# mode: a gold literal and the pipeline's family/given split name one person.
PERSON_FIELDS = ("author", "editor", "translator")
EXCLUDED_STATUSES = {"illegible", "outside_scope"}
LEVENSHTEIN_THRESHOLD = 0.8
Z95 = 1.959963984540054
SCORER_VERSION = "sciencemeta-v3"
MODES = ("strict", "soft", "fuzzy")


# --- normalization -----------------------------------------------------------
# _norm, _composed_title and the name-literal conventions are copied from
# benchmarks/citations/run.py so both tracks agree on what "exact" means.
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


def _composed_title(csl: dict) -> str:
    """Title with subtitle absorbed, matching CSL export semantics."""
    title = str(csl.get("title") or "")
    subtitle = csl.get("subtitle")
    if not title or not isinstance(subtitle, str):
        return title
    sub = str(_norm(subtitle, "subtitle"))
    return title if not sub or sub in str(_norm(title, "title")) else f"{title}: {subtitle}"


def _name_text(value: object) -> str:
    """A CSL name as one comparable string: literal, else given + family parts."""
    if isinstance(value, dict):
        literal = value.get("literal")
        if literal:
            return str(literal)
        return " ".join(str(value.get(part) or "") for part in
                        ("given", "non-dropping-particle", "family", "suffix")).strip()
    return str(value or "")


def _strip_punctuation(text: str) -> str:
    return "".join(" " if unicodedata.category(char).startswith("P") else char for char in text)


_NAME_PARTS = ("family", "given", "literal", "non-dropping-particle", "dropping-particle", "suffix")
_NAME_SPLIT = re.compile(r"[\s,.\-]+")


def _fold_name_text(value: object) -> str:
    """Casefold and strip diacritics so ``José`` and ``Jose`` compare equal."""
    decomposed = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()


def _name_tokens(name: object) -> frozenset[str]:
    """Canonical token set for one CSL name (literal, or its component parts)."""
    if isinstance(name, dict):
        text = " ".join(_fold_name_text(name.get(part)) for part in _NAME_PARTS)
    else:
        text = _fold_name_text(name)
    return frozenset(token for token in _NAME_SPLIT.split(text) if token)


def canonical_names(value: object) -> list[frozenset[str]]:
    """Token sets for a CSL name list, or for a single name object.

    Name order is preserved: the returned list is never sorted, because author
    order is meaningful.
    """
    names = value if isinstance(value, list) else [value]
    return [_name_tokens(name) for name in names]


def _names_equal(left: object, right: object) -> bool:
    """Equal when both name lists have the same length and aligned token sets."""
    left_names, right_names = canonical_names(left), canonical_names(right)
    return len(left_names) == len(right_names) and all(
        expected == actual for expected, actual in zip(left_names, right_names, strict=True))


def _soft(value: object, field: str = "") -> object:
    """Case/punctuation/space-insensitive form, built on top of ``_norm``."""
    if isinstance(value, list):
        if field in NAME_FIELDS:
            return [_soft(_name_text(item), field) for item in value]
        return [_soft(item, field) for item in value]
    if isinstance(value, dict):
        return {key: _soft(item, field) for key, item in sorted(value.items())}
    return " ".join(_strip_punctuation(str(_norm(value, field))).split())


def _ratio_text(value: object, field: str = "") -> str:
    """Flat text for difflib: names become literals, dates become year(-month)."""
    if isinstance(value, list):
        if field in NAME_FIELDS:
            return " ; ".join(_ratio_text(_name_text(item), field) for item in value)
        return " ; ".join(_ratio_text(item, field) for item in value)
    if isinstance(value, dict):
        if set(value) == {"date-parts"}:
            return "-".join(str(part) for group in value["date-parts"] for part in group)
        return " ; ".join(_ratio_text(item, field) for _, item in sorted(value.items()))
    return " ".join(str(_norm(value, field)).split())


def ratio(left: str, right: str) -> float:
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    return difflib.SequenceMatcher(None, left, right).ratio()


def _strict_match(field: str, left: object, right: object) -> bool:
    """Strict equality; person fields compare canonical name tokens."""
    if field in PERSON_FIELDS:
        return _names_equal(left, right)
    return _norm(left, field) == _norm(right, field)


def _soft_match(field: str, left: object, right: object) -> bool:
    """Soft equality; person fields compare canonical name tokens."""
    if field in PERSON_FIELDS:
        return _names_equal(left, right)
    return _soft(left, field) == _soft(right, field)


def _match_one(field: str, expected: object, actual: object, mode: str) -> bool:
    """One comparison under ``strict``, ``soft`` or ``fuzzy``."""
    if expected is None or actual is None:
        return False
    if mode == "strict":
        return _strict_match(field, expected, actual)
    if mode == "soft":
        return _soft_match(field, expected, actual)
    if mode == "fuzzy":
        return ratio(_ratio_text(expected, field), _ratio_text(actual, field)) >= LEVENSHTEIN_THRESHOLD
    raise ValueError(f"unknown comparison mode: {mode}")


# --- adapter -----------------------------------------------------------------
def _date_parts(value: object) -> list | None:
    parts = value.get("date-parts") if isinstance(value, dict) else None
    if not (isinstance(parts, list) and len(parts) == 1 and isinstance(parts[0], list)
            and parts[0]):
        return None
    return parts[0]


def _date_precision(value: object) -> int | None:
    """Number of printed date parts (1 = year only), or None when not a date."""
    parts = _date_parts(value)
    return len(parts) if parts is not None else None


def _at_precision(value: dict, precision: int) -> dict:
    return {"date-parts": [list(_date_parts(value) or [])[:precision]]}


def _adapted(field: str, gold: object, actual: object, gold_csl: dict, pred_csl: dict
             ) -> tuple[list, object, str | None]:
    """Gold values, adapted prediction and the adapter rule that changed them.

    The rule is reported only when it rewrote one side of the comparison, so a
    count in ``report["adapter"]`` always marks a case the raw comparison would
    have read differently.
    """
    if field == "title":
        composed_gold = _composed_title(gold_csl)
        composed_actual = _composed_title(pred_csl)
        rule = "title_composed" if (composed_gold != gold or composed_actual != actual) else None
        return [composed_gold], composed_actual, rule
    if field == "publisher":
        accepted = [gold]
        split = split_publisher(gold) if isinstance(gold, str) else None
        if split:
            accepted.append(split[1])
        return accepted, actual, "publisher_split" if split else None
    if field == "issued":
        precision = _date_precision(gold)
        if precision is None or _date_precision(actual) is None:
            return [gold], actual, None
        adapted = _at_precision(actual, precision)
        if adapted == actual:
            return [gold], actual, None
        return [gold], adapted, "date_year_only" if precision == 1 else "date_precision"
    return [gold], actual, None


def _match(field: str, accepted: list, actual: object, mode: str) -> bool:
    return any(_match_one(field, expected, actual, mode) for expected in accepted)


# --- row evaluation ----------------------------------------------------------
def evaluated_fields(row: dict) -> set[str]:
    """Gold-labeled fields this row is scored on; outside_scope is excluded."""
    status = row.get("field_status") or {}
    csl = row.get("csl") or {}
    return {field for field in FIELDS
            if (field in csl or field in status) and status.get(field) not in EXCLUDED_STATUSES}


def _gold(row: dict, field: str) -> object:
    """Raw gold value; the adapter composes titles when it compares."""
    return (row.get("csl") or {}).get(field)


def _predicted(prediction: dict, field: str) -> object:
    if prediction.get("status") != "ok":
        return None
    return (prediction.get("csl") or {}).get(field)


def _csl_of(row_or_prediction: dict, *, prediction: bool) -> dict:
    if prediction and row_or_prediction.get("status") != "ok":
        return {}
    return row_or_prediction.get("csl") or {}


def field_correct(row: dict, prediction: dict, field: str, mode: str = "strict") -> bool:
    """Compare one field; ``mode`` is ``strict``, ``soft`` or ``fuzzy``."""
    gold, actual = _gold(row, field), _predicted(prediction, field)
    if gold is None or actual is None:
        return False
    accepted, adapted, _ = _adapted(field, gold, actual,
                                    _csl_of(row, prediction=False),
                                    _csl_of(prediction, prediction=True))
    return _match(field, accepted, adapted, mode)


def record_correct(row: dict, prediction: dict, mode: str) -> bool | None:
    """All evaluated fields correct; None when the row has nothing to score."""
    fields = evaluated_fields(row)
    if not fields:
        return None
    return all(field_correct(row, prediction, field, mode) for field in fields)


# --- statistics --------------------------------------------------------------
def wilson(successes: float, n: int, z: float = Z95) -> tuple[float | None, float | None]:
    """Wilson score interval for successes/n; (None, None) when n == 0."""
    if not n:
        return None, None
    proportion = successes / n
    denominator = 1 + z * z / n
    centre = (proportion + z * z / (2 * n)) / denominator
    margin = z * math.sqrt(proportion * (1 - proportion) / n + z * z / (4 * n * n)) / denominator
    # Boundary cases are exact by definition; clamping also removes float noise.
    low = 0.0 if successes <= 0 else max(0.0, centre - margin)
    high = 1.0 if successes >= n else min(1.0, centre + margin)
    return low, high


def metric(successes: float, n: int) -> dict:
    low, high = wilson(successes, n)
    return {"hits": successes, "n": n, "value": successes / n if n else None, "ci95": [low, high]}


# --- scoring -----------------------------------------------------------------
def field_counts(gold: dict[str, dict], predictions: dict[str, dict], field: str) -> dict:
    tp = fp = fn = evaluated = 0
    for source_id, row in gold.items():
        if field not in evaluated_fields(row):
            continue
        evaluated += 1
        prediction = predictions.get(source_id, {})
        expected = _gold(row, field)
        actual = _predicted(prediction, field)
        if field_correct(row, prediction, field, "strict"):
            tp += 1
        else:
            fp += actual is not None
            fn += expected is not None
    f1 = 2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else None
    return {
        "tp": tp, "fp": fp, "fn": fn, "n": evaluated,
        "precision": metric(tp, tp + fp),
        "recall": metric(tp, tp + fn),
        # F1 is not a binomial proportion: this Wilson interval is approximate,
        # computed on the point estimate with the field's decision count.
        "f1": metric(f1 * (2 * tp + fp + fn), 2 * tp + fp + fn) if f1 is not None else metric(0, 0),
        "value": f1,
    }


def adapter_counts(rows: list[dict], predictions: dict[str, dict]) -> dict:
    """Per field and rule, how many comparisons the adapter rules flipped.

    A rule is counted only where it changed an outcome, per mode, so the v3
    delta over the raw comparison is auditable field by field.
    """
    counts: dict = {}
    for row in rows:
        fields = evaluated_fields(row)
        if not fields:
            continue
        prediction = predictions.get(row["id"], {})
        for field in sorted(fields):
            gold, actual = _gold(row, field), _predicted(prediction, field)
            if gold is None or actual is None:
                continue
            accepted, adapted, rule = _adapted(field, gold, actual,
                                               _csl_of(row, prediction=False),
                                               _csl_of(prediction, prediction=True))
            if rule is None:
                continue
            rescued = [mode for mode in MODES
                       if _match(field, accepted, adapted, mode)
                       and not _match_one(field, gold, actual, mode)]
            if not rescued:
                continue
            entry = counts.setdefault(field, {}).setdefault(rule, dict.fromkeys(MODES, 0))
            for mode in rescued:
                entry[mode] += 1
    return counts


def score(rows: list[dict], predictions: dict[str, dict]) -> dict:
    gold = {row["id"]: row for row in rows}
    predictions = {source_id: row for source_id, row in predictions.items() if source_id in gold}
    scored = [row for row in rows if evaluated_fields(row)]
    report: dict = {
        "sources": len(rows),
        "scored_sources": len(scored),
        "rows_without_evaluated_fields": len(rows) - len(scored),
        "attempted": len(predictions),
        "completed": sum(row.get("status") == "ok" for row in predictions.values()),
        "failed": sum(row.get("status") != "ok" for row in predictions.values()),
        "not_attempted": len(rows) - len(predictions),
        "fields": list(FIELDS),
        "metrics": {},
        "per_field": {},
        "outside_scope": {field: sum((row.get("field_status") or {}).get(field) in EXCLUDED_STATUSES
                                     for row in rows) for field in FIELDS},
    }
    for mode, name in (("strict", "strict_record_accuracy"),
                       ("soft", "soft_record_accuracy"),
                       ("fuzzy", "levenshtein_record_accuracy")):
        hits = sum(bool(record_correct(row, predictions.get(source_id, {}), mode)) for source_id, row in gold.items()
                   if evaluated_fields(row))
        report["metrics"][name] = metric(hits, len(scored))
    instance_hits = instance_n = 0
    for source_id, row in gold.items():
        present = {field for field in evaluated_fields(row) if _gold(row, field) is not None}
        if not present:
            continue
        instance_n += 1
        prediction = predictions.get(source_id, {})
        instance_hits += all(field_correct(row, prediction, field, "soft") for field in present)
    report["metrics"]["instance_recall"] = metric(instance_hits, instance_n)
    for field in FIELDS:
        report["per_field"][field] = field_counts(gold, predictions, field)
    report["adapter"] = adapter_counts(rows, predictions)
    report["scorer_version"] = SCORER_VERSION
    report["levenshtein_threshold"] = LEVENSHTEIN_THRESHOLD
    return report


# --- rendering ---------------------------------------------------------------
def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def _ci(item: dict) -> str:
    low, high = item["ci95"]
    if low is None or high is None:
        return "95% CI n/a"
    return f"95% CI {100 * low:.1f}–{100 * high:.1f}"


def _line(label: str, item: dict) -> str:
    return f"| {label} | n={item['n']} | {item['hits']:g} | {_percent(item['value'])} | {_ci(item)} |"


def render_markdown(report: dict, label: str) -> str:
    lines = [
        f"# ScienceMetaBench citations — {label}",
        "",
        f"- manifest: `{report.get('manifest', '')}` (sha256 `{report.get('manifest_sha256', '')}`)",
        f"- predictions: `{report.get('predictions', '')}`",
        f"- scorer: `{report['scorer_version']}`; Levenshtein threshold "
        f"{report['levenshtein_threshold']:.2f} (`difflib` ratio)",
        f"- sources: {report['sources']}; scored: {report['scored_sources']}; "
        f"attempted: {report['attempted']}; completed: {report['completed']}; "
        f"failed: {report['failed']}; not attempted: {report['not_attempted']}",
        "",
        "## Record metrics",
        "",
        "| metric | n | hits | rate | interval |",
        "| --- | --- | --- | --- | --- |",
    ]
    for name, item in report["metrics"].items():
        lines.append(_line(name.replace("_", " "), item))
    lines += ["", "## Per-field (strict matching)", "",
              "| field | n | precision | recall | f1 |",
              "| --- | --- | --- | --- | --- |"]
    unscored = [field for field, counts in report["per_field"].items() if counts["n"] == 0]
    for field, counts in report["per_field"].items():
        if counts["n"] == 0:
            continue
        lines.append(
            f"| {field} | n={counts['n']} | {_percent(counts['precision']['value'])} "
            f"({_ci(counts['precision'])}) | {_percent(counts['recall']['value'])} "
            f"({_ci(counts['recall'])}) | {_percent(counts['value'])} ({_ci(counts['f1'])}) |")
    if unscored:
        lines += ["", f"No evaluated rows for: {', '.join(unscored)} "
                      f"(every row marks them outside_scope)."]
    lines += ["", "## Adapter rules (v3)", ""]
    if report.get("adapter"):
        lines += ["| field | rule | strict rescues | soft rescues | levenshtein rescues |",
                  "| --- | --- | --- | --- | --- |"]
        for field, rules in report["adapter"].items():
            for rule, counts in rules.items():
                lines.append(f"| {field} | `{rule}` | {counts['strict']} | "
                             f"{counts['soft']} | {counts['fuzzy']} |")
        lines += ["", "Each count is a field comparison the raw values would have failed and "
                      "the adapter rule made correct. Counts are measured against the stored "
                      "CSL values, so a rule an earlier scorer version already applied can "
                      "show a count larger than that version's delta."]
    else:
        lines += ["No adapter rule changed a comparison on this manifest."]
    lines += ["", "## Excluded gold-absent fields", "",
              "| field | outside_scope rows |", "| --- | --- |"]
    for field, count in report["outside_scope"].items():
        lines.append(f"| {field} | {count} |")
    lines += [
        "",
        "## Notes",
        "",
        "Record metrics count every manifest row with at least one evaluated field; "
        "missing or failed predictions are incorrect, never dropped. The F1 interval "
        "is an approximate Wilson interval on the point estimate, not a binomial CI.",
        "Fields marked `outside_scope` in the manifest are excluded from every "
        "denominator and cannot fail a record; with n=0 the interval is undefined "
        "and printed as `95% CI n/a`.",
        "The v3 adapter compares each field at the shape the gold carries: titles are "
        "composed with their subtitle, a MARC imprint is also read as its publisher "
        "part, and dates are compared at the gold precision, never above it.",
        "",
    ]
    return "\n".join(lines)


def main(manifest_path: Path, predictions_path: Path, out_dir: Path, label: str) -> int:
    rows = [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").split("\n") if line.strip()]
    predictions = _prediction_rows(
        [json.loads(line) for line in predictions_path.read_text(encoding="utf-8").split("\n") if line.strip()])
    gold = {row["id"]: row for row in rows}
    mismatched = [source_id for source_id, row in predictions.items()
                  if source_id in gold and row.get("source_sha256") is not None
                  and row["source_sha256"] != gold[source_id].get("source_sha256")]
    if mismatched:
        raise SystemExit(f"prediction source digest differs from reviewed source: {mismatched}")
    report = score(rows, predictions)
    report["label"] = label
    report["manifest"] = str(manifest_path)
    report["manifest_sha256"] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    report["predictions"] = str(predictions_path)
    report["predictions_sha256"] = hashlib.sha256(predictions_path.read_bytes()).hexdigest()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out_dir / "report.md").write_text(render_markdown(report, label), encoding="utf-8")
    for name, item in report["metrics"].items():
        print(f"{name}: n={item['n']} hits={item['hits']:g} rate={_percent(item['value'])} {_ci(item)}")
    print(f"wrote {out_dir / 'report.json'} and {out_dir / 'report.md'}")
    return 0


def _prediction_rows(rows: list[dict]) -> dict[str, dict]:
    """Index predictions by id; bare csl objects count as successful."""
    indexed: dict[str, dict] = {}
    for row in rows:
        source_id = row.get("id") or (row.get("csl") or {}).get("id")
        if not source_id:
            continue
        if "csl" in row or "status" in row:
            indexed[source_id] = row
        else:
            indexed[source_id] = {"id": source_id, "status": "ok", "csl": row}
    return indexed


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--predictions", required=True)
    parser.add_argument("--out-dir", default="reports")
    parser.add_argument("--label", default="sciencemeta")
    arguments = parser.parse_args()
    raise SystemExit(main(Path(arguments.manifest), Path(arguments.predictions),
                          Path(arguments.out_dir), arguments.label))
