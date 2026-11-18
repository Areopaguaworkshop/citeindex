"""Oracle audit for ScienceMetaBench predictions: a second opinion off the scored path.

Two modes, so the harness is usable whether or not GROBID is installed:

* **Mode A — two prediction sets.** Compare two CiteIndex runs field by field
  (``--predictions-a`` vs ``--predictions-b``), with per-field agreement rates,
  presence splits and, when a manifest is supplied, gold-relative counts.
* **Mode B — GROBID TEI.** Read a directory of GROBID ``*.tei.xml`` (or
  ``*.xml``) files (``--tei-dir``), map the TEI header to the same five CSL
  fields, and compare that against ``--predictions-a``. File stems are the
  record ids, so name ``<id>.tei.xml`` after the manifest/prediction id.

Nothing here feeds the scorer: an oracle disagreement is evidence to inspect,
never a correction. Per-field agreement is normalized equality over the fields
both sides supply; ``both absent`` is reported but excluded from the rate, so
two empty extractions cannot look like agreement.

Each comparison also runs five detectors over the disagreement set —
``publisher_in_author``, ``title_prefix``, ``date_precision``, ``name_order``
and ``subtitle_split`` — reporting counts plus one example id each. Detectors
overlap by design (a subtitle split is also a title prefix).

Gold-relative columns use the public ``score.field_correct`` helper, so
``a_right_b_wrong`` means the same thing the scored path means. Without a
manifest those columns are omitted rather than guessed.

Usage:
    python oracle.py --manifest manifest.jsonl --predictions-a a.jsonl \
        --predictions-b b.jsonl --out-dir reports --label "run a vs run b"
    python oracle.py --predictions-a a.jsonl --tei-dir grobid-tei \
        --out-dir reports --label "run a vs grobid"

Exits 2 with a one-line message when neither mode is usable or the two sides
share no ids; it never writes a report it cannot support.

Writes ``<out-dir>/report.json`` and ``<out-dir>/report.md``. Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
import unicodedata
import xml.etree.ElementTree as ET
from pathlib import Path

try:  # Public scorer helpers, so gold counts share the scored path's semantics.
    from . import score as scorer
except ImportError:  # Run as a script: benchmarks/sciencemeta is already on sys.path.
    import score as scorer

FIELDS = ("title", "author", "publisher", "issued", "ISBN")
ORACLE_VERSION = "sciencemeta-oracle-v1"
ISBN_SEPARATORS = re.compile(r"[\s-]")
TOKEN_SPLIT = re.compile(r"[\s,.\-]+")
TEI_WHEN = re.compile(r"(?<!\d)(\d{4})(?:-(\d{2})(?:-(\d{2}))?)?")
NO_ORACLE = (
    "no oracle available: Mode A takes --predictions-b (two prediction sets) and "
    "Mode B takes --tei-dir (GROBID TEI; {grobid})"
)
EMPTY_TEI = (
    "no *.tei.xml or *.xml files in {directory}: Mode B needs GROBID TEI; "
    "Mode A compares --predictions-a against --predictions-b"
)


class OracleUnavailable(RuntimeError):
    """Raised when an oracle source exists but cannot supply a usable comparison."""


# --- normalization -----------------------------------------------------------
def normalize(value: object, field: str = "") -> object:
    """Comparable form: NFC, casefolded, whitespace collapsed (score semantics)."""
    if isinstance(value, list):
        return [normalize(item, field) for item in value]
    if isinstance(value, dict):
        return {key: normalize(item, field) for key, item in sorted(value.items())}
    text = " ".join(unicodedata.normalize("NFC", str(value or "")).casefold().split())
    if field == "ISBN":
        text = ISBN_SEPARATORS.sub("", text)
    return text


def composed_title(csl: dict) -> str:
    """Title with its subtitle absorbed, matching the scorer's title adapter."""
    title = str(csl.get("title") or "")
    subtitle = csl.get("subtitle")
    if not title or not isinstance(subtitle, str):
        return title
    sub = str(normalize(subtitle, "subtitle"))
    return title if not sub or sub in str(normalize(title, "title")) else f"{title}: {subtitle}"


def name_text(name: object) -> str:
    """One CSL name as comparable text: literal, else its component parts."""
    if isinstance(name, dict):
        if name.get("literal"):
            return str(normalize(name["literal"]))
        return str(normalize(" ".join(
            str(name.get(part) or "") for part in ("given", "non-dropping-particle", "family", "suffix"))))
    return str(normalize(name))


def author_names(value: object) -> list[str]:
    """Ordered comparable name strings; author order stays meaningful."""
    names = value if isinstance(value, list) else [value]
    return [text for text in (name_text(name) for name in names) if text]


def date_parts_of(value: object) -> list | None:
    """The first ``date-parts`` group as ints where possible, or None."""
    parts = value.get("date-parts") if isinstance(value, dict) else None
    if not (isinstance(parts, list) and len(parts) == 1 and isinstance(parts[0], list) and parts[0]):
        return None
    converted: list = []
    for part in parts[0]:
        text = str(part).strip()
        converted.append(int(text) if text.isdigit() else text)
    return converted


def field_key(csl: dict, field: str) -> object | None:
    """Comparable value for one field, or None when the side supplies nothing."""
    if not isinstance(csl, dict):
        return None
    value = csl.get(field)
    if value is None or value == "" or value == [] or value == {}:
        return None
    if field == "title":
        return normalize(composed_title(csl), "title") or None
    if field == "author":
        names = author_names(value)
        return tuple(names) if names else None
    if field == "issued":
        parts = date_parts_of(value)
        return tuple(parts) if parts is not None else normalize(value, field)
    return normalize(value, field) or None


def _excerpt(value: object, limit: int = 120) -> str:
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, sort_keys=True)
    else:
        text = str(value or "")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3] + "..."


# --- comparison --------------------------------------------------------------
def _prediction_csl(row: dict | None) -> dict:
    if not row or row.get("status") != "ok":
        return {}
    csl = row.get("csl")
    return csl if isinstance(csl, dict) else {}


def audit(predictions_a: dict[str, dict], predictions_b: dict[str, dict], ids: list[str],
          manifest_rows: dict[str, dict] | None = None) -> dict:
    """Per-field counts, disagreement records and pattern counts for shared ids."""
    per_field: dict[str, dict] = {
        field: {"n": 0, "agreements": 0, "a_only": 0, "b_only": 0, "both_absent": 0,
                "gold": None if manifest_rows is None else
                {"judged": 0, "both_right": 0, "a_right_b_wrong": 0,
                 "a_wrong_b_right": 0, "both_wrong": 0}}
        for field in FIELDS
    }
    disagreements: list[dict] = []
    for source_id in ids:
        a_csl = _prediction_csl(predictions_a.get(source_id))
        b_csl = _prediction_csl(predictions_b.get(source_id))
        gold_row = (manifest_rows or {}).get(source_id)
        differing: list[str] = []
        for field in FIELDS:
            counts = per_field[field]
            a_key, b_key = field_key(a_csl, field), field_key(b_csl, field)
            if a_key is None and b_key is None:
                counts["both_absent"] += 1
                continue
            if a_key is None:
                counts["b_only"] += 1
                continue
            if b_key is None:
                counts["a_only"] += 1
                continue
            counts["n"] += 1
            if a_key == b_key:
                counts["agreements"] += 1
                continue
            differing.append(field)
            if gold_row is not None:
                counts_gold = counts["gold"]
                if (field in scorer.evaluated_fields(gold_row)
                        and (gold_row.get("csl") or {}).get(field) is not None):
                    a_ok = scorer.field_correct(
                        gold_row, {"id": source_id, "status": "ok", "csl": a_csl}, field, "strict")
                    b_ok = scorer.field_correct(
                        gold_row, {"id": source_id, "status": "ok", "csl": b_csl}, field, "strict")
                    counts_gold["judged"] += 1
                    if a_ok and b_ok:
                        counts_gold["both_right"] += 1
                    elif a_ok:
                        counts_gold["a_right_b_wrong"] += 1
                    elif b_ok:
                        counts_gold["a_wrong_b_right"] += 1
                    else:
                        counts_gold["both_wrong"] += 1
        if differing:
            disagreements.append({"id": source_id, "fields": tuple(differing), "a": a_csl, "b": b_csl})
    for counts in per_field.values():
        low, high = scorer.wilson(counts["agreements"], counts["n"])
        counts["rate"] = counts["agreements"] / counts["n"] if counts["n"] else None
        counts["ci95"] = [low, high]
    patterns = detect_patterns(disagreements)
    explained = {record["id"] for record in disagreements
                 if any(detector(record) for _, detector in DETECTORS)}
    unexplained = [record["id"] for record in disagreements if record["id"] not in explained]
    return {"per_field": per_field, "patterns": patterns,
            "disagreements": disagreements, "unexplained": unexplained}


# --- systematic-pattern detectors -------------------------------------------
def _detect_publisher_in_author(record: dict) -> dict | None:
    if not {"publisher", "author"} & set(record["fields"]):
        return None
    for publisher_side, author_side in ((record["a"], record["b"]), (record["b"], record["a"])):
        publisher = field_key(publisher_side, "publisher")
        author = " ".join(author_names(author_side.get("author")))
        if (isinstance(publisher, str) and len(publisher) >= 3 and author
                and publisher != author and publisher in author):
            return {"field": "publisher/author", "a": _excerpt(publisher), "b": _excerpt(author)}
    return None


def _detect_title_prefix(record: dict) -> dict | None:
    if "title" not in record["fields"]:
        return None
    a, b = field_key(record["a"], "title"), field_key(record["b"], "title")
    if not (isinstance(a, str) and isinstance(b, str)):
        return None
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    if len(shorter) >= 3 and longer.startswith(shorter) and shorter != longer:
        return {"field": "title", "a": _excerpt(a), "b": _excerpt(b)}
    return None


def _detect_date_precision(record: dict) -> dict | None:
    if "issued" not in record["fields"]:
        return None
    a = date_parts_of((record["a"] or {}).get("issued"))
    b = date_parts_of((record["b"] or {}).get("issued"))
    if a and b and a[0] == b[0] and len(a) != len(b):
        return {"field": "issued", "a": _excerpt(a), "b": _excerpt(b)}
    return None


def _detect_name_order(record: dict) -> dict | None:
    if "author" not in record["fields"]:
        return None
    def tokens(side: dict) -> list[str]:
        return [token for name in author_names(side.get("author")) for token in TOKEN_SPLIT.split(name) if token]
    a, b = tokens(record["a"]), tokens(record["b"])
    if len(a) >= 2 and a != b and sorted(a) == sorted(b):
        return {"field": "author", "a": _excerpt(" ".join(a)), "b": _excerpt(" ".join(b))}
    return None


def _detect_subtitle_split(record: dict) -> dict | None:
    if "title" not in record["fields"]:
        return None
    a, b = field_key(record["a"], "title"), field_key(record["b"], "title")
    if not (isinstance(a, str) and isinstance(b, str)):
        return None
    def base(title: str) -> str:
        return re.split(r":\s|：", title, maxsplit=1)[0].strip()
    if base(a) and base(a) == base(b) and a != b:
        return {"field": "title", "a": _excerpt(a), "b": _excerpt(b)}
    return None


DETECTORS = (
    ("publisher_in_author", _detect_publisher_in_author),
    ("title_prefix", _detect_title_prefix),
    ("date_precision", _detect_date_precision),
    ("name_order", _detect_name_order),
    ("subtitle_split", _detect_subtitle_split),
)


def detect_patterns(disagreements: list[dict]) -> dict:
    """Count each detector's matches and keep one example id per detector."""
    patterns: dict[str, dict] = {}
    for name, detector in DETECTORS:
        matches = [(record["id"], detail) for record in disagreements if (detail := detector(record))]
        patterns[name] = {
            "count": len(matches),
            "example": {"id": matches[0][0], **matches[0][1]} if matches else None,
        }
    return patterns


# --- GROBID TEI (Mode B) ------------------------------------------------------
def _local(tag: object) -> str:
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _children(node: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in list(node) if _local(child.tag) == name]


def _all_path(node: ET.Element, *names: str) -> list[ET.Element]:
    nodes = [node]
    for name in names:
        nodes = [child for parent in nodes for child in _children(parent, name)]
    return nodes


def _first_path(node: ET.Element, *names: str) -> ET.Element | None:
    nodes = _all_path(node, *names)
    return nodes[0] if nodes else None


def _all_named(node: ET.Element, name: str) -> list[ET.Element]:
    return [element for element in node.iter() if _local(element.tag) == name]


def _element_text(node: ET.Element) -> str:
    return " ".join("".join(node.itertext()).split())


def _tei_name(node: ET.Element) -> dict:
    pers = _first_path(node, "persName")
    if pers is None:
        pers = next(iter(_all_named(node, "persName")), None)
    if pers is not None:
        given = _first_path(pers, "forename")
        family = _first_path(pers, "surname")
        if given is not None or family is not None:
            name: dict = {}
            if given is not None and _element_text(given):
                name["given"] = _element_text(given)
            if family is not None and _element_text(family):
                name["family"] = _element_text(family)
            if name:
                return name
    return {"literal": _element_text(node)}


def _tei_date(imprint: ET.Element) -> dict | None:
    for node in _children(imprint, "date"):
        text = node.get("when") or node.get("when-custom") or _element_text(node)
        match = TEI_WHEN.search(text)
        if not match:
            continue
        parts = [int(match.group(1))]
        if match.group(2):
            parts.append(int(match.group(2)))
        if match.group(3):
            parts.append(int(match.group(3)))
        return {"date-parts": [parts]}
    return None


def parse_tei(text: str) -> dict:
    """Map one GROBID TEI header to the five CSL-like fields used for comparison."""
    root = ET.fromstring(text)
    header = next(iter(_all_named(root, "teiHeader")), root)
    csl: dict = {}
    title_node = _first_path(header, "fileDesc", "titleStmt", "title")
    if title_node is None:
        title_node = next(iter(_all_path(
            header, "fileDesc", "sourceDesc", "biblStruct", "analytic", "title")), None)
    if title_node is not None and _element_text(title_node):
        csl["title"] = _element_text(title_node)

    authors: list[ET.Element] = []
    for path in (("fileDesc", "sourceDesc", "biblStruct", "analytic", "author"),
                 ("fileDesc", "sourceDesc", "biblStruct", "monogr", "author")):
        authors.extend(_all_path(header, *path))
    if not authors:
        bibl = _first_path(header, "fileDesc", "sourceDesc", "biblStruct")
        if bibl is not None:
            authors.extend(_all_path(bibl, "author"))
    if authors:
        csl["author"] = [_tei_name(author) for author in authors]

    publisher_node = None
    for path in (("fileDesc", "sourceDesc", "biblStruct", "monogr", "imprint", "pubPublisher"),
                 ("fileDesc", "sourceDesc", "biblStruct", "analytic", "imprint", "pubPublisher")):
        publisher_node = _first_path(header, *path)
        if publisher_node is not None and _element_text(publisher_node):
            break
    if publisher_node is None:
        publisher_node = next(iter(_all_named(header, "pubPublisher")), None)
    if publisher_node is not None and _element_text(publisher_node):
        csl["publisher"] = _element_text(publisher_node)

    for imprint in _all_named(header, "imprint"):
        issued = _tei_date(imprint)
        if issued is not None:
            csl["issued"] = issued
            break

    for identifier in _all_named(header, "idno"):
        if (identifier.get("type") or "").strip().lower() != "isbn":
            continue
        text = _element_text(identifier)
        if text:
            csl["ISBN"] = text.split(",")[0].strip()
            break
    return csl


def _tei_id(path: Path) -> str:
    name = path.name
    for suffix in (".tei.xml", ".xml"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return path.stem


def load_tei_dir(directory: Path) -> dict[str, dict]:
    """Parse ``<id>.tei.xml`` / ``<id>.xml`` into prediction-shaped rows."""
    if not directory.is_dir():
        raise OracleUnavailable(f"TEI directory not found: {directory} (Mode B needs GROBID TEI; "
                                "Mode A takes --predictions-b)")
    files = sorted(directory.glob("*.tei.xml"))
    files += [path for path in sorted(directory.glob("*.xml")) if not path.name.endswith(".tei.xml")]
    if not files:
        raise OracleUnavailable(EMPTY_TEI.format(directory=directory))
    rows: dict[str, dict] = {}
    for path in files:
        try:
            csl = parse_tei(path.read_text(encoding="utf-8"))
        except (ET.ParseError, UnicodeDecodeError) as exc:
            raise OracleUnavailable(f"cannot read TEI {path.name}: {exc}") from exc
        source_id = _tei_id(path)
        rows[source_id] = {"id": source_id, "status": "ok", "csl": csl}
    return rows


# --- input/output ------------------------------------------------------------
def load_predictions(path: Path) -> tuple[dict[str, dict], int]:
    """Index predictions by id (last row wins); return them and the unreadable-line count."""
    predictions: dict[str, dict] = {}
    unreadable = 0
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                unreadable += 1  # A file still being appended may end mid-record.
                continue
            source_id = row.get("id") or (row.get("csl") or {}).get("id")
            if not source_id:
                continue
            if "csl" not in row and "status" not in row:
                row = {"id": source_id, "status": "ok", "csl": row}
            predictions[source_id] = row
    return predictions, unreadable


def load_manifest(path: Path) -> dict[str, dict]:
    rows, _ = load_predictions(path)
    return rows


def _percent(value: float | None) -> str:
    return "n/a" if value is None else f"{100 * value:.1f}%"


def _interval(low: float | None, high: float | None) -> str:
    if low is None or high is None:
        return "95% CI n/a"
    return f"95% CI {100 * low:.1f}-{100 * high:.1f}"


def render_markdown(report: dict) -> str:
    coverage = report["coverage"]
    gold = report["per_field"][FIELDS[0]]["gold"] is not None
    mode = ("A - two prediction sets" if report["mode"] == "predictions"
            else "B - GROBID TEI")
    lines = [
        f"# ScienceMetaBench oracle audit - {report['label']}",
        "",
        f"- oracle: `{report['oracle_version']}`",
        f"- mode: **{mode}**",
        f"- predictions A: `{report['sources']['predictions_a']}`",
        f"- predictions B: `{report['sources']['predictions_b'] or '-'}`",
        f"- TEI directory: `{report['sources']['tei_dir'] or '-'}`",
        f"- manifest: `{report['sources']['manifest'] or '-'}`",
        f"- ids: A={coverage['predictions_a_ids']}; B={coverage['predictions_b_ids']}; "
        f"compared={coverage['compared_ids']}; only in A={coverage['only_in_a']}; "
        f"only in B={coverage['only_in_b']}",
    ]
    if report["sources"]["manifest"]:
        lines.append(f"- manifest rows={coverage['manifest_rows']}; "
                     f"matched to compared ids={coverage['manifest_matched']}")
    unreadable = coverage["unreadable_lines"]
    lines += [
        f"- unreadable prediction lines: A={unreadable['a']}; B={unreadable['b']}",
        "",
        "## Per-field agreement",
        "",
        "Agreement is normalized equality on fields both sides supply. `n` counts "
        "fields where at least one side has a value; `both absent` is reported but "
        "excluded from the rate, so two empty extractions never look like agreement.",
        "",
        "| field | n | agreement | 95% CI | a only | b only | both absent |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for field in FIELDS:
        counts = report["per_field"][field]
        lines.append(f"| {field} | n={counts['n']} | {_percent(counts['rate'])} | "
                     f"{_interval(*counts['ci95'])} | {counts['a_only']} | "
                     f"{counts['b_only']} | {counts['both_absent']} |")
    if gold:
        lines += [
            "",
            "## Gold-relative counts on disagreeing fields",
            "",
            "Counted where both sides supply the field and the manifest labels gold; "
            "`judged` is that denominator.",
            "",
            "| field | judged | both wrong | a right / b wrong | a wrong / b right | both right |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for field in FIELDS:
            counts = report["per_field"][field]["gold"]
            lines.append(f"| {field} | n={counts['judged']} | {counts['both_wrong']} | "
                         f"{counts['a_right_b_wrong']} | {counts['a_wrong_b_right']} | "
                         f"{counts['both_right']} |")
    lines += [
        "",
        "## Systematic patterns",
        "",
        f"{report['disagreement_records']} records have at least one disagreeing field. "
        "Detectors overlap; a count is the number of records a detector explains.",
        "",
        "| pattern | count | example id | detail |",
        "| --- | --- | --- | --- |",
    ]
    for name, finding in report["patterns"].items():
        example = finding["example"]
        if example is None:
            lines.append(f"| `{name}` | 0 | - | no match |")
        else:
            detail = f"{example['field']}: {example['a']} / {example['b']}"
            lines.append(f"| `{name}` | {finding['count']} | `{example['id']}` | {detail} |")
    unexplained = report["unexplained"]
    examples = ", ".join(f"`{source_id}`" for source_id in unexplained["examples"]) or "-"
    lines += [
        "",
        f"No detector explains n={unexplained['count']} disagreement records "
        f"(first ids: {examples}).",
        "",
        "## Notes",
        "",
        "- The oracle never feeds the scored path; inspect disagreements, never copy a value.",
        "- `both_wrong` means both runs disagree with gold, not that gold is right.",
        "- Gold-relative counts use `score.field_correct` (strict mode), so they match the "
        "scored benchmark's adapter semantics.",
        "- Mode B reads GROBID TEI headers only; a file is addressed by its stem, so "
        "`<id>.tei.xml` must match the prediction id.",
        "- A line still being appended by a running census is counted as unreadable and skipped.",
        "",
    ]
    return "\n".join(lines)


def _fail(message: str) -> int:
    print(f"oracle: {message}", file=sys.stderr)
    return 2


def _grobid_note() -> str:
    return "grobid found on PATH" if shutil.which("grobid") else "grobid not found on PATH"


def run(*, predictions_a: Path, predictions_b: Path | None = None, tei_dir: Path | None = None,
        manifest: Path | None = None, out_dir: Path = Path("reports"),
        label: str = "sciencemeta") -> int:
    """Run one audit; returns 0 on a written report and 2 when nothing is usable."""
    if predictions_b is not None and tei_dir is not None:
        return _fail("choose one oracle source: Mode A takes --predictions-b (two prediction sets), "
                     "Mode B takes --tei-dir (GROBID TEI)")
    if predictions_b is None and tei_dir is None:
        return _fail(NO_ORACLE.format(grobid=_grobid_note()))
    if not predictions_a.is_file():
        return _fail(f"predictions A not found: {predictions_a} (Mode A needs --predictions-b; "
                     "Mode B needs --tei-dir)")
    predictions_a_rows, unreadable_a = load_predictions(predictions_a)
    unreadable_b = 0
    if predictions_b is not None:
        if not predictions_b.is_file():
            return _fail(f"predictions B not found: {predictions_b} (Mode A compares two prediction sets)")
        predictions_b_rows, unreadable_b = load_predictions(predictions_b)
        mode = "predictions"
    else:
        try:
            predictions_b_rows = load_tei_dir(tei_dir)
        except OracleUnavailable as exc:
            return _fail(str(exc))
        mode = "tei"
    manifest_rows = None
    if manifest is not None:
        if not manifest.is_file():
            return _fail(f"manifest not found: {manifest}")
        manifest_rows = load_manifest(manifest)
    ids = sorted(set(predictions_a_rows) & set(predictions_b_rows))
    if not ids:
        return _fail(f"no shared ids between predictions A and the oracle "
                     f"(A={len(predictions_a_rows)}, B={len(predictions_b_rows)}); "
                     "check that TEI filenames or prediction ids agree")
    result = audit(predictions_a_rows, predictions_b_rows, ids, manifest_rows)
    per_field = result["per_field"]
    coverage = {
        "predictions_a_ids": len(predictions_a_rows),
        "predictions_b_ids": len(predictions_b_rows),
        "compared_ids": len(ids),
        "only_in_a": len(set(predictions_a_rows) - set(predictions_b_rows)),
        "only_in_b": len(set(predictions_b_rows) - set(predictions_a_rows)),
        "manifest_rows": len(manifest_rows) if manifest_rows is not None else None,
        "manifest_matched": len(set(ids) & set(manifest_rows)) if manifest_rows is not None else None,
        "unreadable_lines": {"a": unreadable_a, "b": unreadable_b},
    }
    report = {
        "oracle_version": ORACLE_VERSION,
        "label": label,
        "mode": mode,
        "fields": list(FIELDS),
        "sources": {
            "predictions_a": str(predictions_a),
            "predictions_b": str(predictions_b) if predictions_b is not None else "",
            "tei_dir": str(tei_dir) if tei_dir is not None else "",
            "manifest": str(manifest) if manifest is not None else "",
        },
        "coverage": coverage,
        "per_field": per_field,
        "patterns": result["patterns"],
        "disagreement_records": len(result["disagreements"]),
        "unexplained": {"count": len(result["unexplained"]),
                        "examples": result["unexplained"][:3]},
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (out_dir / "report.md").write_text(render_markdown(report), encoding="utf-8")
    for field in FIELDS:
        counts = per_field[field]
        print(f"{field}: n={counts['n']} agreement={_percent(counts['rate'])} "
              f"{_interval(*counts['ci95'])} a_only={counts['a_only']} "
              f"b_only={counts['b_only']} both_absent={counts['both_absent']}")
    matched = [(name, finding["count"]) for name, finding in report["patterns"].items()
               if finding["count"]]
    if not matched:
        print("top pattern: none of the detectors matched")
    else:
        best = max(count for _, count in matched)
        leaders = [name for name, count in matched if count == best]
        summary = ", ".join(f"{name}={best} example={report['patterns'][name]['example']['id']}"
                            for name in leaders)
        print(f"top pattern{'s' if len(leaders) > 1 else ''}: {summary}"
              f"{' (tie)' if len(leaders) > 1 else ''}")
    print(f"wrote {out_dir / 'report.json'} and {out_dir / 'report.md'}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--predictions-a", required=True)
    parser.add_argument("--predictions-b")
    parser.add_argument("--tei-dir")
    parser.add_argument("--manifest")
    parser.add_argument("--out-dir", default="reports")
    parser.add_argument("--label", default="sciencemeta")
    arguments = parser.parse_args(argv)
    return run(
        predictions_a=Path(arguments.predictions_a),
        predictions_b=Path(arguments.predictions_b) if arguments.predictions_b else None,
        tei_dir=Path(arguments.tei_dir) if arguments.tei_dir else None,
        manifest=Path(arguments.manifest) if arguments.manifest else None,
        out_dir=Path(arguments.out_dir),
        label=arguments.label,
    )


if __name__ == "__main__":
    raise SystemExit(main())
