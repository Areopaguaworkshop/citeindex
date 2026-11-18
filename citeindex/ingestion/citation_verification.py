"""Evidence-first citation metadata verification."""

from __future__ import annotations

import calendar
import hashlib
import json
import math
import os
import re
import unicodedata
from collections.abc import Iterable
from typing import Any, TypeGuard

from .csl import HOST_FIELDS, NAME_FIELDS, valid_host_value
from .metadata_registry import lookup_crossref_doi, lookup_openlibrary_isbn, normalize_doi, normalize_isbn

_RECONCILABLE_FIELDS = (
    "author", "title", "subtitle", "issued", "publisher", "publisher-place",
    "container-title", "DOI", "URL", "page",
)
_LOCATOR_KEYS = ("node_id", "char_start", "char_end", "bbox")

# Whitespace-free canonical form: printed titles are frequently split across
# OCR/extraction line breaks ("论三位一\n体", "A ND MACARIU\nS"); a quote that
# matches modulo whitespace (and CJK zero-width artifacts) is still verbatim
# support for the value. This is the plan v2 §4 "multi-line title" fix.
_WS_STRIP_RE = re.compile(r"[\s\u3000\u200b\ufeff]+")

# Presentation-only glyph differences (NFKC handles full-width forms; these
# remain after it) must not turn a verbatim quote into an invented one.
_EVIDENCE_TRANSLATION = str.maketrans({
    "\u2010": "-", "\u2011": "-", "\u2012": "-", "\u2013": "-", "\u2014": "-",
    "\u2015": "-", "\u2212": "-",
    "\u2018": "'", "\u2019": "'", "\u201a": "'", "\u201b": "'",
    "\u201c": '"', "\u201d": '"', "\u201e": '"', "\u201f": '"',
})

# Closed set of reasons a field can be dropped from verified CSL.
DROP_REASONS = ("not_found", "quote_mismatch", "empty", "wrong_role")

_SPAN_PAGE_KEYS = ("physical_page_index", "section_index", "segment_id")


def _compact(text: str) -> str:
    """Casefolded, whitespace-stripped form for tolerance comparisons."""
    return _WS_STRIP_RE.sub("", text.casefold())


def _normalise_units(text: str) -> Iterable[tuple[str, int]]:
    """Canonical characters paired with their index in the original ``text``.

    Applies NFKC (full-width punctuation, ligatures), maps dashes and curly
    quotes to ASCII, drops soft hyphens and collapses every whitespace run to
    a single space. Working character by character keeps original offsets
    recoverable for the locator.
    """
    pending_space: int | None = None
    emitted = False
    for position, char in enumerate(text):
        if char == "\u00ad":
            continue
        piece = unicodedata.normalize("NFKC", char).translate(_EVIDENCE_TRANSLATION)
        if not piece:
            continue
        if piece.isspace() or not piece.strip():
            if pending_space is None:
                pending_space = position
            continue
        if pending_space is not None and emitted:
            yield " ", pending_space
        pending_space = None
        for unit in piece:
            emitted = True
            yield unit, position


def _normalise_evidence(text: str) -> str:
    """Canonical text used on both sides of the evidence containment check."""
    if not isinstance(text, str):
        return ""
    return "".join(unit for unit, _ in _normalise_units(text))


def _normalised_index(text: str, target: int) -> int:
    """Original offset of canonical character ``target`` (approximate fallback)."""
    position = 0
    for _, original in _normalise_units(text):
        if position == target:
            return original
        position += 1
    return 0


def _quote_in_block(quote: str, block_text: str) -> int | None:
    """Index of ``quote`` in ``block_text``, presentation-tolerant, else ``None``.

    Fast path: exact substring (the common case — unchanged behavior).
    Slow paths: whitespace-insensitive scan, then full evidence normalisation
    (NFKC, dashes, curly quotes, soft hyphens, collapsed spaces).
    """
    if quote in block_text:
        return block_text.index(quote)
    compacted_quote = _compact(quote)
    if compacted_quote:
        compacted_block = _compact(block_text)
        idx = compacted_block.find(compacted_quote)
        if idx >= 0:
            # Map the compacted hit back to an original-text span: walk the
            # block skipping whitespace, counting non-space chars.
            char_count = 0
            for pos, ch in enumerate(block_text):
                if _WS_STRIP_RE.match(ch):
                    continue
                if char_count == idx:
                    return pos
                char_count += 1
    normalised_quote = _normalise_evidence(quote)
    if not normalised_quote:
        return None
    idx = _normalise_evidence(block_text).find(normalised_quote)
    if idx < 0:
        return None
    return _normalised_index(block_text, idx)


def _plain(text: str) -> str:
    return " ".join(re.findall(r"\w+", _normalise_evidence(text).casefold()))


def _supports_quote(field: str, value: Any, quote: str) -> bool:
    """Whether ``quote`` can support ``value`` for this field."""
    if field == "type" or _find_evidence(value, [{"quote": quote}]):
        return True
    # A page range is supported when a joined quote carries both endpoints
    # (``citation_firstpage`` + ``citation_lastpage``).
    if field == "page" and isinstance(value, str):
        parts = [part for part in re.split(r"\s*[-–—/]\s*", value) if part.strip()]
        if len(parts) > 1 and all(_compact(part) in _compact(quote) for part in parts):
            return True
    if field not in {*NAME_FIELDS, "title", "subtitle", "publisher", "publisher-place"}:
        return False
    normalized = _plain(quote)
    if all(_plain(part) in normalized for part in _value_strings(value)):
        return True
    # Multi-line OCR tolerance: value words may straddle line breaks in the
    # quote; whitespace-stripped containment still entails support.
    return all(_compact(part) in _compact(quote) for part in _value_strings(value))


def _empty_value(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return not value
    return False


def _same_span_page(left: dict, right: dict) -> bool:
    """Joins across pages are never allowed."""
    metadata_pair = bool(left["locator"].get("metadata_key") and right["locator"].get("metadata_key"))
    for key in _SPAN_PAGE_KEYS:
        if key == "section_index" and metadata_pair:
            # Metadata blocks share one source.html snapshot; section_index is
            # only their emit order inside <head>, not a physical page.
            continue
        first = left["locator"].get(key)
        second = right["locator"].get(key)
        if first is not None and second is not None and first != second:
            return False
    return True


def _adjacent_blocks(left_id: Any, right_id: Any, blocks: list[dict]) -> bool:
    ids = [block.get("id") for block in blocks]
    try:
        return ids.index(right_id) == ids.index(left_id) + 1
    except ValueError:
        return False


def _resolve_span(field: str, span: Any, blocks: list[dict]) -> tuple[dict | None, str | None]:
    """Resolve one span to a locator, or return ``(None, drop_reason)``."""
    if not isinstance(span, dict):
        return None, "not_found"
    quote = span.get("quote")
    if not isinstance(quote, str) or not quote.strip():
        return None, "empty"
    block = next((b for b in blocks if b.get("id") == span.get("block_id")), None)
    if not block:
        return None, "not_found"
    start = _quote_in_block(quote, block["text"])
    if start is None:
        return None, "quote_mismatch"
    if field in NAME_FIELDS and block.get("metadata_key") in {"uploader", "channel", "platform"}:
        return None, "wrong_role"
    if field == "publisher" and block.get("metadata_key") == "platform":
        return None, "wrong_role"
    if field == "title" and block.get("metadata_key") in {"filename", "source_path"}:
        return None, "wrong_role"
    if field == "accessed" and block.get("metadata_key") != "accessed":
        return None, "wrong_role"
    metadata_key = str(block.get("metadata_key", "")).casefold()
    if field == "issued" and ("modified" in metadata_key or metadata_key in {"event-date", "event_date"}):
        return None, "wrong_role"
    # start already resolved tolerantly; an exact re-index here raised on
    # OCR line-break drift.
    locator = {k: block[k] for k in ("physical_page_index", "printed_page_label", "section_index", "segment_id", "start_seconds", "end_seconds", "metadata_key", "snapshot_artifact", "source_digest") if k in block}
    locator.update(block_id=block["id"], char_start=start, char_end=start + len(quote))
    if not valid_source_locator(locator):
        return None, "not_found"
    return {"block_id": block["id"], "quote": quote, "locator": locator}, None


def _resolve_joined_span(field: str, quote: str, span: dict, blocks: list[dict]) -> tuple[dict | None, str | None]:
    """Resolve a quote assembled from two adjacent same-page blocks.

    The join must be exactly one separator (a space or a line break) and both
    halves must resolve in their own block; a join across pages is rejected.
    """
    ids = [block.get("id") for block in blocks]
    try:
        index = ids.index(span.get("block_id"))
    except ValueError:
        return None, "not_found"
    if index + 1 >= len(blocks):
        return None, "quote_mismatch"
    neighbour = blocks[index + 1]
    if not isinstance(neighbour.get("text"), str):
        return None, "quote_mismatch"
    for position, char in enumerate(quote):
        if char not in {" ", "\n"}:
            continue
        if position == 0 or position + 1 >= len(quote):
            continue
        if quote[position - 1].isspace() or quote[position + 1].isspace():
            continue
        head, tail = quote[:position], quote[position + 1:]
        left, left_reason = _resolve_span(field, {"block_id": span.get("block_id"), "quote": head}, blocks)
        right, _ = _resolve_span(field, {"block_id": neighbour.get("id"), "quote": tail}, blocks)
        if left and right and _same_span_page(left, right):
            return {"spans": [left, right], "quote": quote,
                    "locator": left["locator"]}, None
        if left_reason == "wrong_role":
            return None, "wrong_role"
    return None, "quote_mismatch"


def evaluate_block_evidence(field: str, value: Any, evidence: Any, blocks: list[dict]) -> tuple[dict | None, str | None]:
    """Resolve source spans, returning ``(evidence, drop_reason)``.

    Reasons come from :data:`DROP_REASONS`; ``None`` means the field is
    supported.
    """
    if not valid_host_value(field, value):
        return None, "empty" if _empty_value(value) else "wrong_role"
    spans = evidence.get("spans") if isinstance(evidence, dict) and "spans" in evidence else evidence
    if spans is None:
        return None, "not_found"
    if isinstance(spans, list):
        if not 1 <= len(spans) <= 8:
            return None, "not_found"
        resolved: list[dict] = []
        for span in spans:
            item, reason = _resolve_span(field, span, blocks)
            if item is None:
                return None, reason
            resolved.append(item)
        for left, right in zip(resolved, resolved[1:], strict=False):
            if not _same_span_page(left, right) or not _adjacent_blocks(
                    left["block_id"], right["block_id"], blocks):
                return None, "quote_mismatch"
        quote = " ".join(span["quote"] for span in resolved)
        if not _supports_quote(field, value, quote):
            return None, "quote_mismatch"
        return {"spans": resolved, "quote": quote, "locator": resolved[0]["locator"]}, None
    single, reason = _resolve_span(field, spans, blocks)
    if single is None:
        quote = spans.get("quote") if isinstance(spans, dict) else None
        if reason == "quote_mismatch" and isinstance(quote, str):
            joined, join_reason = _resolve_joined_span(field, quote, spans, blocks)
            if joined is not None and _supports_quote(field, value, joined["quote"]):
                return joined, None
            return None, join_reason or reason
        return None, reason
    if not _supports_quote(field, value, single["quote"]):
        return None, "quote_mismatch"
    return single, None


def validate_block_evidence(field: str, value: Any, evidence: Any, blocks: list[dict]) -> dict | None:
    """Resolve exact source spans. This establishes support, not host attribution."""
    return evaluate_block_evidence(field, value, evidence, blocks)[0]


def valid_source_locator(locator: Any) -> bool:
    """Validate block coordinates without conflating PDF pages with media time."""
    if not isinstance(locator, dict):
        return False
    if "segment_id" in locator:
        start: Any = locator.get("start_seconds")
        end: Any = locator.get("end_seconds")
        return (isinstance(locator["segment_id"], str) and bool(locator["segment_id"])
                and type(start) in (int, float) and type(end) in (int, float)
                and math.isfinite(start) and math.isfinite(end) and 0 <= start < end)
    if "metadata_key" in locator:
        return (isinstance(locator["metadata_key"], str) and bool(locator["metadata_key"])
                and bool(locator.get("snapshot_artifact")) and bool(locator.get("source_digest")))
    if "section_index" in locator:
        return type(locator["section_index"]) is int and locator["section_index"] >= 1
    return type(locator.get("physical_page_index")) is int and locator["physical_page_index"] >= 0


def _locator(data: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
    """Keep stable source coordinates when an extractor provides them."""
    result = dict(fallback)
    for key in _LOCATOR_KEYS:
        if data.get(key) is not None:
            result[key] = data[key]
    if data.get("id") is not None and "node_id" not in result:
        result["node_id"] = data["id"]
    return result


def _text_items(
    document_json: dict[str, Any] | None, resource_type: str, *, raw_spans: bool = False,
) -> Iterable[dict[str, Any]]:
    document = document_json or {}
    seen: set[tuple[str, str]] = set()
    paragraph_nodes: set[str] = set()

    def emit(text: Any, locator: dict[str, Any]) -> Iterable[dict[str, Any]]:
        if not isinstance(text, str) or not text.strip():
            return []
        item: dict[str, Any] = {"quote": text if raw_spans else text.strip(), "locator": locator}
        key = (item["quote"], json.dumps(locator, sort_keys=True, default=str))
        if key in seen:
            return []
        seen.add(key)
        return [item]

    for physical_page_index, page in enumerate(document.get("structure", {}).get("pages", [])):
        if not isinstance(page, dict):
            continue
        for paragraph_index, paragraph in enumerate(page.get("paragraphs", []), start=1):
            data = paragraph if isinstance(paragraph, dict) else {"text": str(paragraph)}
            locator: dict[str, Any] = {"paragraph_id": data.get("paragraph_id", f"p{physical_page_index + 1}_{paragraph_index}")}
            if resource_type == "url_article":
                locator["section_index"] = physical_page_index + 1
                if document.get("metadata", {}).get("url"):
                    locator["section_url"] = document["metadata"]["url"]
            else:
                locator["physical_page_index"] = page.get("physical_page_index", page.get("page_idx", physical_page_index))
                if page.get("printed_page_label") is not None:
                    locator["printed_page_label"] = page["printed_page_label"]
                    locator["page_label_source"] = page.get("page_label_source")
                elif not raw_spans and page.get("page_number") is not None:
                    locator["printed_page_label"] = str(page["page_number"])
            if data.get("node_id"):
                paragraph_nodes.add(data["node_id"])
                locator.setdefault("char_start", 0)
                locator.setdefault("char_end", len(data.get("text", "")))
            yield from emit(data.get("text"), _locator(data, locator))

    def walk(value: Any, inherited: dict[str, Any]) -> Iterable[dict[str, Any]]:
        if isinstance(value, list):
            for child in value:
                yield from walk(child, inherited)
            return
        if not isinstance(value, dict):
            return
        locator = _locator(value, inherited)
        for key in ("physical_page_index", "printed_page_label"):
            if value.get(key) is not None:
                locator[key] = str(value[key]) if key == "printed_page_label" else value[key]
        if "node_id" in locator and "physical_page_index" not in locator and isinstance(value.get("page"), int):
            locator["physical_page_index"] = max(value["page"] - 1, 0)
            locator["snapshot_artifact"] = "document.json"
        if "node_id" in locator and isinstance(value.get("text"), str):
            locator.setdefault("char_start", 0)
            locator.setdefault("char_end", len(value["text"]))
        if not raw_spans or locator.get("node_id") not in paragraph_nodes:
            yield from emit(value.get("text"), locator)
        for key, child in value.items():
            if key not in {"text", "bbox", "char_start", "char_end", "node_id", "id"}:
                yield from walk(child, locator)

    yield from walk(document.get("nodes", []), {})


def _evidence(
    document_json: dict[str, Any] | None, resource_type: str, extra: dict[str, Any], *, raw_spans: bool = False,
) -> list[dict[str, Any]]:
    items = list(_text_items(document_json, resource_type, raw_spans=raw_spans))
    for block in extra.get("source_blocks", []):
        if isinstance(block.get("text"), str):
            locator = {k: v for k, v in block.items() if k not in {"text", "role", "id"}}
            locator.update(block_id=block["id"], char_start=0, char_end=len(block["text"]))
            if not raw_spans:
                locator.setdefault("node_id", block["id"])
            if valid_source_locator(locator):
                items.append({"quote": block["text"], "locator": locator})
    snapshot_path = extra.get("source_snapshot_path")
    if resource_type == "url_article" and isinstance(snapshot_path, str) and os.path.isfile(snapshot_path):
        with open(snapshot_path, "rb") as source:
            raw = source.read()
        digest = hashlib.sha256(raw).hexdigest()
        html = raw.decode("utf-8", "replace")
        for item in items:
            item["locator"].setdefault("snapshot_artifact", "source.html")
            item["locator"].setdefault("source_digest", digest)
            start = html.casefold().find(item["quote"].casefold())
            if start >= 0:
                item["locator"].update(snapshot_char_start=start, snapshot_char_end=start + len(item["quote"]))
    elif not raw_spans:
        digest = _source_digest(document_json, resource_type, extra, items)
        for item in items:
            item["locator"].setdefault("snapshot_artifact", "document.json")
            item["locator"].setdefault("source_digest", digest)
    return items


def _valid_locator(locator: Any) -> bool:
    if not isinstance(locator, dict) or not locator.get("node_id"):
        return False
    if not isinstance(locator.get("char_start"), int) or not isinstance(locator.get("char_end"), int):
        return False
    return valid_source_locator(locator)


def _evidence_rank(item: dict[str, Any]) -> int:
    text = item.get("quote", "").casefold()
    if any(marker in text for marker in ("cite this", "citation", "引用格式", "若要引用")):
        return 100
    page = item.get("locator", {}).get("physical_page_index")
    return 90 if isinstance(page, int) and page <= 1 else 70


def _value_strings(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        if "literal" in value:
            return _value_strings(value["literal"])
        if "date-parts" in value:
            parts = next((group for group in value.get("date-parts", []) if isinstance(group, list)), [])
            if not parts:
                return []
            raw = [str(part) for part in parts]
            padded = [raw[0]] + [part.zfill(2) for part in raw[1:]]
            candidates = {"-".join(raw), "-".join(padded), "/".join(raw), "/".join(padded), " ".join(raw), "".join(padded)}
            if len(parts) >= 2 and 1 <= parts[1] <= 12:
                month = calendar.month_name[parts[1]]
                candidates.add(f"{parts[0]}年{parts[1]}月" + (f"{parts[2]}日" if len(parts) == 3 else ""))
                candidates.add(
                    f"{parts[0]}年{str(parts[1]).zfill(2)}月"
                    + (f"{str(parts[2]).zfill(2)}日" if len(parts) == 3 else "")
                )
                if len(parts) == 3:
                    for name in (month, calendar.month_abbr[parts[1]]):
                        candidates.update((f"{name} {parts[2]}, {parts[0]}", f"{parts[2]} {name} {parts[0]}"))
                else:
                    candidates.add(f"{month} {parts[0]}")
            return sorted(candidates, key=len, reverse=True)
        name = " ".join(str(value.get(key, "")).strip() for key in ("given", "family")).strip()
        return [name] if name else []
    if isinstance(value, list):
        return [part for item in value for part in _value_strings(item)]
    if isinstance(value, (int, float)):
        return [str(value)]
    return []


def _roman_to_int(numeral: str) -> int:
    """'mdccccxlix' → 1949 (numeral already case-folded)."""
    values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    total = sum(values[c] for c in numeral)
    for a, b in zip(numeral, numeral[1:], strict=False):
        if values[a] < values[b]:
            total -= 2 * values[a]
    return total


def _find_evidence(value: Any, evidence: Iterable[dict[str, Any]]) -> dict[str, Any] | None:
    values = [item.casefold().strip() for item in _value_strings(value) if item.strip()]
    if not values:
        return None
    # Scholarly imprints print year as a Roman numeral ('PARISIIS MDCCCCXLIX'
    # = 1949): the Arabic year is genuine support even though it is not the
    # literal glyph run. Narrow to standalone numerals so embedded ones
    # ('saeculi IX') cannot vouch for a year.
    roman_year = None
    if isinstance(value, dict) and "date-parts" in value:
        parts = next((group for group in value.get("date-parts", []) if isinstance(group, list)), [])
        if parts and type(parts[0]) is int and 1400 <= parts[0] <= 2030:
            roman_year = parts[0]
    for item in evidence:
        quote = item["quote"].casefold()
        if isinstance(value, dict) and "date-parts" in value:
            matched = any(candidate in quote for candidate in values)
            if not matched and roman_year is not None:
                matched = any(_roman_to_int(m.group()) == roman_year
                              for m in re.finditer(r"\b[mdclxvi]{6,18}\b", quote))
        else:
            matched = all(candidate in quote for candidate in values)
        if matched:
            return item
    return None


def _supported_evidence(field: str, value: Any, evidence: Iterable[dict[str, Any]], blocks: list[dict]) -> dict[str, Any] | None:
    """Find evidence and apply metadata-role rules when it comes from a block."""
    source = _find_evidence(value, evidence)
    if not source:
        return None
    locator = source.get("locator", {})
    block_id = locator.get("block_id")
    if block_id is None:
        return source
    return source if validate_block_evidence(field, value, {"block_id": block_id, "quote": source["quote"]}, blocks) else None


def _same_value(field: str, left: Any, right: Any) -> bool:
    if field == "DOI":
        return normalize_doi(left) == normalize_doi(right)
    if field in {"title", "publisher", "publisher-place", "container-title", "URL", "page"}:
        return " ".join(str(left or "").replace("–", "-").split()).casefold().rstrip("/") == " ".join(str(right or "").replace("–", "-").split()).casefold().rstrip("/")
    return left == right


def _source_digest(document_json: dict[str, Any] | None, resource_type: str, extra: dict[str, Any], evidence: list[dict[str, Any]]) -> str:
    path = extra.get("source_snapshot_path") or extra.get("source_path") or (document_json or {}).get("metadata", {}).get("source_path")
    if isinstance(path, str) and os.path.isfile(path):
        with open(path, "rb") as source:
            return hashlib.sha256(source.read()).hexdigest()
    return hashlib.sha256("\n".join(item["quote"] for item in evidence).encode()).hexdigest()


def _model_review(field: str, draft_value: Any, registry_value: Any, evidence: list[dict[str, Any]], model_name: str) -> dict[str, Any] | None:
    """Ask the configured model about one disputed field, never the whole record."""
    import dspy

    from ..llm import get_llm_model

    lm = get_llm_model(model_name, temperature=0.0)
    class CitationReview(dspy.Signature):
        """Resolve one disputed CSL field using only supplied source evidence."""
        field = dspy.InputField()
        draft_value = dspy.InputField()
        registry_value = dspy.InputField()
        source_evidence = dspy.InputField()
        verdict = dspy.OutputField(desc="accept, reject, or needs_review")
        selected_value_json = dspy.OutputField(desc="JSON value for this field")
        quote = dspy.OutputField(desc="exact source quote")
        locator_json = dspy.OutputField(desc="JSON stable locator")
        confidence = dspy.OutputField(desc="number from 0 to 1")
        rationale = dspy.OutputField()
    with dspy.context(lm=lm):
        response = dspy.Predict(CitationReview)(
            field=field,
            draft_value=json.dumps(draft_value, ensure_ascii=False),
            registry_value=json.dumps(registry_value, ensure_ascii=False),
            source_evidence=json.dumps(evidence, ensure_ascii=False),
        )
    try:
        decision = {
            "verdict": response.verdict,
            "field": field,
            "selected_value": json.loads(response.selected_value_json),
            "quote": response.quote,
            "locator": json.loads(response.locator_json),
            "confidence": float(response.confidence),
            "rationale": response.rationale,
        }
    except (TypeError, ValueError):
        return None
    required = {"verdict", "field", "selected_value", "quote", "locator", "confidence", "rationale"}
    if not isinstance(decision, dict) or not required.issubset(decision):
        return None
    if decision["verdict"] not in {"accept", "reject", "needs_review"} or decision["field"] != field:
        return None
    if not isinstance(decision["quote"], str) or not isinstance(decision["locator"], dict):
        return None
    if not isinstance(decision["confidence"], (int, float)) or not 0 <= decision["confidence"] <= 1:
        return None
    return decision if isinstance(decision["rationale"], str) else None


def _valid_model_decision(decision: dict[str, Any] | None, field: str, draft_value: Any, registry_value: Any, evidence: list[dict[str, Any]]) -> TypeGuard[dict[str, Any]]:
    required = {"verdict", "field", "selected_value", "quote", "locator", "confidence", "rationale"}
    if not decision or not required.issubset(decision) or decision["verdict"] != "accept" or decision["field"] != field:
        return False
    if not decision.get("rationale") or not isinstance(decision.get("confidence"), (int, float)):
        return False
    if not 0 <= decision["confidence"] <= 1:
        return False
    selected = decision["selected_value"]
    if not (_same_value(field, selected, draft_value) or _same_value(field, selected, registry_value)):
        return False
    if not _valid_locator(decision["locator"]):
        return False
    return any(item["quote"] == decision["quote"] and item["locator"] == decision["locator"] and _find_evidence(selected, [item]) for item in evidence)


def verify_citation_metadata(
    csl_json: dict[str, Any], document_json: dict[str, Any] | None, resource_type: str,
    extra: dict[str, Any], config: Any, *, registry_client: Any = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Verify registry metadata; one unresolved conflict prevents all CSL changes."""
    original, proposed = dict(csl_json), dict(csl_json)
    evidence = _evidence(document_json, resource_type, extra)
    blocks = [block for block in extra.get("source_blocks", []) if isinstance(block, dict)]
    # A DOI in the bibliography belongs to a cited work unless host identity
    # has already established it. Do not borrow the first DOI in body text.
    doi = normalize_doi(original.get("DOI"))
    registry = (registry_client.lookup("crossref", doi) if registry_client is not None else
                lookup_crossref_doi(doi, crossref_enabled=config.crossref_enabled, offline_verification=config.offline_verification, contact_email=config.registry_contact_email))
    if registry.get("status") != "found":
        # Books rarely carry DOIs Crossref resolves; try the ISBN at OpenLibrary.
        isbn = normalize_isbn(original.get("ISBN"))
        if isbn:
            registry = (registry_client.lookup("openlibrary", isbn) if registry_client is not None else
                        lookup_openlibrary_isbn(isbn, openlibrary_enabled=getattr(config, "openlibrary_enabled", True), offline_verification=config.offline_verification))
    corrections: list[dict[str, Any]] = []
    needs_review: list[dict[str, Any]] = []
    candidate = registry.get("candidate") or {}
    for field in _RECONCILABLE_FIELDS:
        value = candidate.get(field)
        if value is None or _same_value(field, original.get(field), value):
            continue
        source = _supported_evidence(field, value, evidence, blocks)
        existing_source = _supported_evidence(field, original.get(field), evidence, blocks)
        if source and existing_source and field != "DOI" and _evidence_rank(existing_source) >= _evidence_rank(source):
            needs_review.append({"field": field, "draft_value": original.get(field), "registry_value": value, "source_value": original.get(field), "provenance": registry["provenance"]})
        elif source and _valid_locator(source["locator"]):
            proposed[field] = value
            corrections.append({"field": field, "value": value, "quote": source["quote"], "locator": source["locator"], "confidence": 1.0, "provenance": registry["provenance"]})
        else:
            needs_review.append({"field": field, "draft_value": original.get(field), "registry_value": value, "provenance": registry["provenance"]})

    model_review = "not_requested"
    if needs_review and config.citation_verifier_model and not config.offline_verification:
        model_review, unresolved = "accepted", []
        for conflict in needs_review:
            field = conflict["field"]
            try:
                field_evidence = [item for item in evidence if _find_evidence(original.get(field), [item]) or _find_evidence(conflict["registry_value"], [item])]
                if not field_evidence:
                    unresolved.append(conflict)
                    model_review = "rejected"
                    continue
                decision = _model_review(field, original.get(field), conflict["registry_value"], field_evidence, config.citation_verifier_model)
            except Exception:
                decision, model_review = None, "unavailable"
            if not _valid_model_decision(decision, field, original.get(field), conflict["registry_value"], evidence):
                unresolved.append(conflict)
                if model_review != "unavailable":
                    model_review = "rejected"
            else:
                proposed[field] = decision["selected_value"]
                corrections.append({"field": field, "value": decision["selected_value"], "quote": decision["quote"], "locator": decision["locator"], "confidence": decision["confidence"], "rationale": decision["rationale"], "provenance": {"provider": "model", "model": config.citation_verifier_model}})
        needs_review = unresolved

    field_states = {}
    for field in HOST_FIELDS:
        if original.get(field) is None:
            field_states[field] = "missing"
        elif _supported_evidence(field, original[field], evidence, blocks):
            field_states[field] = "source-supported"
        else:
            field_states[field] = "unverified"
    for correction in corrections:
        field_states[correction["field"]] = "source-supported"
    for conflict in needs_review:
        field_states[conflict["field"]] = "conflicting"

    registry_status = registry.get("status")
    registry_fields = [field for field in _RECONCILABLE_FIELDS if candidate.get(field) is not None]
    source_supported = all(field_states.get(field) == "source-supported" for field in registry_fields)
    if registry_status == "found":
        status = "verified" if not needs_review and source_supported else "needs_review"
    elif registry_status == "not_found":
        status = "needs_review"
    else:
        status = registry_status or "needs_review"
    applied = corrections if not needs_review else []
    digest = _source_digest(document_json, resource_type, extra, evidence)
    report = {
        "status": status,
        "source_digest": digest,
        "evidence": evidence,
        "registry": registry,
        "corrections": corrections,
        "proposed_corrections": corrections,
        "applied_corrections": applied,
        "verified": status == "verified",
        "corrected": applied,
        "needs-review": needs_review,
        "needs_review": needs_review,
        "model_review": model_review,
        "field_states": field_states,
    }
    return (proposed if not needs_review else original), report


def _quotation_evidence(evidence: Iterable[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    anchors: list[dict[str, Any]] = []
    pdf_aliases: dict[tuple[int, str], list[dict[str, Any]]] = {}
    for item in evidence:
        locator = dict(item["locator"])
        if locator.get("metadata_key") or not valid_source_locator(locator):
            continue
        if "physical_page_index" in locator and not locator.get("page_label_source"):
            locator.pop("printed_page_label", None)
        anchor = {"quote": item["quote"], "locator": locator}
        anchors.append(anchor)
        if "physical_page_index" in locator:
            pdf_aliases.setdefault((locator["physical_page_index"], item["quote"]), []).append(anchor)
    duplicates = set()
    for group in pdf_aliases.values():
        if len(group) != 2:
            continue
        first, second = group
        if ("block_id" in first["locator"]) == ("block_id" in second["locator"]):
            continue
        if "block_id" in first["locator"]:
            first, second = second, first
        first["locator"] = {**first["locator"], **second["locator"]}
        duplicates.add(id(second))
    yield from (anchor for anchor in anchors if id(anchor) not in duplicates)


def quotation_entry(
    source_id: str, source_sha256: str | None, quote: str, locator: dict[str, Any],
) -> dict[str, Any]:
    """Describe an extracted span without asserting original-source confirmation."""
    from .deterministic import hash_payload
    from .markdown_export import _format_timestamp

    entry: dict[str, Any] = {
        "schema_version": "1.0",
        "source_id": source_id,
        "source_sha256": source_sha256,
        "span_id": hash_payload({"source_id": source_id, "source_sha256": source_sha256, "locator": locator}),
        "quote": quote,
        "locator": locator,
        "citation_item": {"id": source_id},
    }
    if locator.get("printed_page_label"):
        label, display = "page", locator["printed_page_label"]
    elif locator.get("segment_id"):
        entry["segment_id"] = locator["segment_id"]
        start = _format_timestamp(locator["start_seconds"])
        end = _format_timestamp(locator["end_seconds"])
        label, display = "timestamp", f"{start}–{end}"
    elif locator.get("section_index"):
        label, display = "section", str(locator["section_index"])
    else:
        return entry
    entry["citation_item"].update(label=label, locator=display)
    return entry


def build_quotation_locators(
    source_id: str, source_sha256: str | None, document: dict[str, Any] | None,
    resource_type: str, blocks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the portable quotation sidecar using the same anchors as replay."""
    evidence = _evidence(document, resource_type, {"source_blocks": blocks}, raw_spans=True)
    entries = {}
    for item in _quotation_evidence(evidence):
        locator = {**item["locator"], "char_start": 0, "char_end": len(item["quote"])}
        entry = quotation_entry(source_id, source_sha256, item["quote"], locator)
        entries[entry["span_id"]] = entry
    return list(entries.values())


def replay_citation_evidence(
    csl: dict[str, Any],
    document: dict[str, Any] | None,
    resource_type: str,
    blocks: list[dict[str, Any]],
    fields: list[str] | None = None,
    quotation: str | None = None,
    locator: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Compare the stored citation and exact quotation with extracted evidence."""
    fields = fields if fields is not None else [field for field in HOST_FIELDS if csl.get(field) is not None]
    if any(field not in HOST_FIELDS for field in fields):
        raise ValueError("fields must contain CSL fields for the ingested work")
    evidence = _evidence(document, resource_type, {"source_blocks": blocks}, raw_spans=True)
    field_states, field_evidence = {}, {}
    for field in fields:
        value = csl.get(field)
        bound = (csl.get("_field_evidence") or {}).get(field)
        match = validate_block_evidence(field, value, bound, blocks) if bound and value is not None else None
        if match is None and value is not None:
            match = next((item for item in evidence if _supported_evidence(field, value, [item], blocks)), None)
        field_states[field] = "source-supported" if match else "missing" if value is None else "unverified"
        if match:
            field_evidence[field] = match
    result: dict[str, Any] = {"field_states": field_states, "field_evidence": field_evidence}
    if quotation is None and locator is None:
        return result
    locator = locator or {}
    if not quotation or not quotation.strip():
        raise ValueError("Supply a nonempty quotation")
    if "char_start" in locator and (type(locator["char_start"]) is not int or locator["char_start"] < 0):
        raise ValueError("char_start must be a nonnegative integer")
    if "char_end" in locator and (type(locator["char_end"]) is not int or locator["char_end"] < 0):
        raise ValueError("char_end must be a nonnegative integer")
    matches: dict[str, Any] = {}
    for item in _quotation_evidence(evidence):
        coordinates = item["locator"]
        if any(coordinates.get(key) != value for key, value in locator.items() if key not in {"char_start", "char_end"}):
            continue
        text = item["quote"]
        start = locator.get("char_start", text.find(quotation))
        while start >= 0:
            end = start + len(quotation)
            if locator.get("char_end", end) == end and text[start:end] == quotation:
                resolved = {**coordinates, "char_start": start, "char_end": end}
                matches[json.dumps(resolved, sort_keys=True)] = {
                    "locator": resolved,
                    "context": text[max(0, start - 45):min(len(text), end + 45)],
                }
            if "char_start" in locator:
                break
            start = text.find(quotation, start + 1)
    options = list(matches.values())
    if len(options) == 1:
        result["quotation"] = {"status": "extraction-match", "quote": quotation, "locator": options[0]["locator"]}
    elif options:
        result["quotation"] = {
            "status": "needs-review", "reason": "ambiguous_span", "quote": quotation,
            "candidate_count": len(options), "candidates": options[:20],
        }
    else:
        result["quotation"] = {"status": "needs-review", "reason": "span_not_found"}
    return result
