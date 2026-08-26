"""Evidence-first citation metadata verification."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, Iterable

from .metadata_registry import extract_doi, lookup_crossref_doi, normalize_doi


_RECONCILABLE_FIELDS = (
    "author", "title", "issued", "publisher", "publisher-place",
    "container-title", "DOI", "URL", "page",
)
_LOCATOR_KEYS = ("node_id", "char_start", "char_end", "bbox")


def _locator(data: Dict[str, Any], fallback: Dict[str, Any]) -> Dict[str, Any]:
    """Keep stable source coordinates when an extractor provides them."""
    result = dict(fallback)
    for key in _LOCATOR_KEYS:
        if data.get(key) is not None:
            result[key] = data[key]
    if data.get("id") is not None and "node_id" not in result:
        result["node_id"] = data["id"]
    return result


def _text_items(document_json: Dict[str, Any] | None, resource_type: str) -> Iterable[Dict[str, Any]]:
    document = document_json or {}
    seen: set[tuple[str, str]] = set()

    def emit(text: Any, locator: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
        if not isinstance(text, str) or not text.strip():
            return []
        item = {"quote": text.strip(), "locator": locator}
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
            locator: Dict[str, Any] = {"paragraph_id": data.get("paragraph_id", f"p{physical_page_index + 1}_{paragraph_index}")}
            if resource_type == "url_article":
                locator["section_index"] = physical_page_index + 1
                if document.get("metadata", {}).get("url"):
                    locator["section_url"] = document["metadata"]["url"]
            else:
                locator["physical_page_index"] = page.get("physical_page_index", page.get("page_idx", physical_page_index))
                if page.get("page_number") is not None:
                    locator["printed_page_label"] = str(page["page_number"])
            if data.get("node_id"):
                locator.setdefault("char_start", 0)
                locator.setdefault("char_end", len(data.get("text", "")))
            yield from emit(data.get("text"), _locator(data, locator))

    def walk(value: Any, inherited: Dict[str, Any]) -> Iterable[Dict[str, Any]]:
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
        yield from emit(value.get("text"), locator)
        for key, child in value.items():
            if key not in {"text", "bbox", "char_start", "char_end", "node_id", "id"}:
                yield from walk(child, locator)

    yield from walk(document.get("nodes", []), {})


def _evidence(document_json: Dict[str, Any] | None, resource_type: str, extra: Dict[str, Any]) -> list[Dict[str, Any]]:
    items = list(_text_items(document_json, resource_type))
    snapshot_path = extra.get("source_snapshot_path")
    if resource_type == "url_article" and isinstance(snapshot_path, str) and os.path.isfile(snapshot_path):
        with open(snapshot_path, "rb") as source:
            raw = source.read()
        digest = hashlib.sha256(raw).hexdigest()
        html = raw.decode("utf-8", "replace")
        for item in items:
            item["locator"].update(snapshot_artifact="source.html", source_digest=digest)
            start = html.casefold().find(item["quote"].casefold())
            if start >= 0:
                item["locator"].update(snapshot_char_start=start, snapshot_char_end=start + len(item["quote"]))
    else:
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
    return locator.get("physical_page_index") is not None or locator.get("section_index") is not None


def _evidence_rank(item: Dict[str, Any]) -> int:
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
            return [str(part) for group in value.get("date-parts", []) for part in (group if isinstance(group, list) else [])]
        name = " ".join(str(value.get(key, "")).strip() for key in ("given", "family")).strip()
        return [name] if name else []
    if isinstance(value, list):
        return [part for item in value for part in _value_strings(item)]
    if isinstance(value, (int, float)):
        return [str(value)]
    return []


def _find_evidence(value: Any, evidence: Iterable[Dict[str, Any]]) -> Dict[str, Any] | None:
    values = [item.casefold().strip() for item in _value_strings(value) if item.strip()]
    if not values:
        return None
    for item in evidence:
        if all(value in item["quote"].casefold() for value in values):
            return item
    return None


def _same_value(field: str, left: Any, right: Any) -> bool:
    if field == "DOI":
        return normalize_doi(left) == normalize_doi(right)
    if field in {"title", "publisher", "publisher-place", "container-title", "URL", "page"}:
        return " ".join(str(left or "").replace("–", "-").split()).casefold().rstrip("/") == " ".join(str(right or "").replace("–", "-").split()).casefold().rstrip("/")
    return left == right


def _source_digest(document_json: Dict[str, Any] | None, resource_type: str, extra: Dict[str, Any], evidence: list[Dict[str, Any]]) -> str:
    path = extra.get("source_snapshot_path") if resource_type == "url_article" else (document_json or {}).get("metadata", {}).get("source_path")
    if isinstance(path, str) and os.path.isfile(path):
        with open(path, "rb") as source:
            return hashlib.sha256(source.read()).hexdigest()
    return hashlib.sha256("\n".join(item["quote"] for item in evidence).encode()).hexdigest()


def _model_review(field: str, draft_value: Any, registry_value: Any, evidence: list[Dict[str, Any]], model_name: str) -> Dict[str, Any] | None:
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


def _valid_model_decision(decision: Dict[str, Any] | None, field: str, draft_value: Any, registry_value: Any, evidence: list[Dict[str, Any]]) -> bool:
    required = {"verdict", "field", "selected_value", "quote", "locator", "confidence", "rationale"}
    if not decision or not required.issubset(decision) or decision["verdict"] != "accept" or decision["field"] != field:
        return False
    if not decision.get("rationale") or not isinstance(decision.get("confidence"), (int, float)):
        return False
    if not 0 <= decision["confidence"] <= 1:
        return False
    if not _valid_locator(decision["locator"]):
        return False
    return any(item["quote"] == decision["quote"] and item["locator"] == decision["locator"] and _find_evidence(decision["selected_value"], [item]) for item in evidence)


def verify_citation_metadata(
    csl_json: Dict[str, Any], document_json: Dict[str, Any] | None, resource_type: str,
    extra: Dict[str, Any], config: Any,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Verify registry metadata; one unresolved conflict prevents all CSL changes."""
    original, proposed = dict(csl_json), dict(csl_json)
    evidence = _evidence(document_json, resource_type, extra)
    doi = normalize_doi(original.get("DOI")) or next((found for item in evidence if (found := extract_doi(item["quote"]))), None)
    registry = lookup_crossref_doi(doi, crossref_enabled=config.crossref_enabled, offline_verification=config.offline_verification, contact_email=config.registry_contact_email)
    corrections: list[Dict[str, Any]] = []
    needs_review: list[Dict[str, Any]] = []
    candidate = registry.get("candidate") or {}
    for field in _RECONCILABLE_FIELDS:
        value = candidate.get(field)
        if value is None or _same_value(field, original.get(field), value):
            continue
        source = _find_evidence(value, evidence)
        existing_source = _find_evidence(original.get(field), evidence)
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

    status = "verified" if not needs_review else "needs_review"
    if registry["status"] not in {"found", "not_found"}:
        status = "needs_review" if config.citation_verifier_model else registry["status"]
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
    }
    return (proposed if not needs_review else original), report
