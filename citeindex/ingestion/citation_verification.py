"""Evidence-first citation metadata verification."""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any, Dict, Iterable

from .metadata_registry import extract_doi, lookup_crossref_doi, normalize_doi


_RECONCILABLE_FIELDS = ("DOI", "title", "container-title", "publisher", "page", "volume", "issue")


def _text_items(document_json: Dict[str, Any] | None, resource_type: str) -> Iterable[Dict[str, Any]]:
    for physical_page_index, page in enumerate((document_json or {}).get("structure", {}).get("pages", [])):
        if not isinstance(page, dict):
            continue
        for paragraph_index, paragraph in enumerate(page.get("paragraphs", []), start=1):
            text = paragraph.get("text") if isinstance(paragraph, dict) else str(paragraph)
            if not isinstance(text, str) or not text.strip():
                continue
            locator: Dict[str, Any] = {
                "paragraph_id": paragraph.get("paragraph_id", f"p{physical_page_index + 1}_{paragraph_index}") if isinstance(paragraph, dict) else f"p{physical_page_index + 1}_{paragraph_index}",
            }
            if resource_type == "url_article":
                locator["section_index"] = physical_page_index + 1
            else:
                locator["physical_page_index"] = physical_page_index + 1
                if page.get("page_number") is not None:
                    locator["printed_page_label"] = str(page["page_number"])
            yield {"quote": text.strip(), "locator": locator}


def _evidence(document_json: Dict[str, Any] | None, resource_type: str, extra: Dict[str, Any]) -> list[Dict[str, Any]]:
    items = list(_text_items(document_json, resource_type))
    snapshot_path = extra.get("source_snapshot_path")
    if resource_type == "url_article" and isinstance(snapshot_path, str) and os.path.isfile(snapshot_path):
        with open(snapshot_path, "rb") as source:
            digest = hashlib.sha256(source.read()).hexdigest()
        for item in items:
            item["locator"]["snapshot_artifact"] = "source.html"
            item["locator"]["source_digest"] = digest
    return items


def _find_evidence(value: Any, evidence: Iterable[Dict[str, Any]]) -> Dict[str, Any] | None:
    if not isinstance(value, str) or not value.strip():
        return None
    needle = value.casefold().strip()
    for item in evidence:
        if needle in item["quote"].casefold():
            return item
    return None


def _model_review(draft: Dict[str, Any], candidate: Dict[str, Any], evidence: list[Dict[str, Any]], model_name: str) -> Dict[str, Any] | None:
    """Ask an explicitly configured model for one evidence-bound correction."""
    import dspy
    from ..llm import get_llm_model

    lm = get_llm_model(model_name, temperature=0.0)
    signature = dspy.Signature(
        "draft, registry_candidate, source_evidence -> decision_json",
        "Return JSON only: {\"field\": string, \"value\": string, \"quote\": exact source quote}. "
        "Choose only DOI, title, container-title, publisher, page, volume, or issue; return {} when uncertain.",
    )
    with dspy.context(lm=lm):
        response = dspy.Predict(signature)(
            draft=json.dumps(draft, ensure_ascii=False),
            registry_candidate=json.dumps(candidate, ensure_ascii=False),
            source_evidence=json.dumps(evidence, ensure_ascii=False),
        )
    try:
        decision = json.loads(response.decision_json)
    except (TypeError, ValueError):
        return None
    if not isinstance(decision, dict) or decision.get("field") not in _RECONCILABLE_FIELDS:
        return None
    return decision


def verify_citation_metadata(
    csl_json: Dict[str, Any],
    document_json: Dict[str, Any] | None,
    resource_type: str,
    extra: Dict[str, Any],
    config: Any,
) -> tuple[Dict[str, Any], Dict[str, Any]]:
    """Return corrected CSL only where a registry value has source evidence."""
    draft = dict(csl_json)
    evidence = _evidence(document_json, resource_type, extra)
    doi = normalize_doi(draft.get("DOI")) or next((extract_doi(item["quote"]) for item in evidence if extract_doi(item["quote"])), None)
    registry = lookup_crossref_doi(
        doi,
        crossref_enabled=config.crossref_enabled,
        offline_verification=config.offline_verification,
        contact_email=config.registry_contact_email,
    )
    corrections: list[Dict[str, Any]] = []
    needs_review: list[Dict[str, Any]] = []
    candidate = registry.get("candidate") or {}
    for field in _RECONCILABLE_FIELDS:
        value = candidate.get(field)
        if value is None or draft.get(field) == value:
            continue
        source = _find_evidence(value, evidence)
        if source:
            draft[field] = value
            corrections.append({"field": field, "value": value, "quote": source["quote"], "locator": source["locator"], "provenance": registry["provenance"]})
        else:
            needs_review.append({"field": field, "draft_value": csl_json.get(field), "registry_value": value, "provenance": registry["provenance"]})

    model_review = "not_requested"
    if needs_review and config.citation_verifier_model and not config.offline_verification:
        try:
            decision = _model_review(draft, candidate, evidence, config.citation_verifier_model)
            source = _find_evidence(decision.get("value"), evidence) if decision else None
            pending_fields = {item["field"] for item in needs_review}
            if source and decision.get("field") in pending_fields and decision.get("quote") == source["quote"]:
                field = decision["field"]
                draft[field] = decision["value"]
                corrections.append({"field": field, "value": decision["value"], "quote": source["quote"], "locator": source["locator"], "provenance": {"provider": "model", "model": config.citation_verifier_model}})
                needs_review = [item for item in needs_review if item["field"] != field]
                model_review = "accepted"
            else:
                model_review = "rejected"
        except Exception:
            model_review = "unavailable"

    status = "verified" if not needs_review else "needs_review"
    if registry["status"] not in {"found", "not_found"}:
        status = "needs_review" if config.citation_verifier_model else registry["status"]
    report = {
        "status": status,
        "source_digest": hashlib.sha256("\n".join(item["quote"] for item in evidence).encode()).hexdigest(),
        "registry": registry,
        "corrections": corrections,
        "needs_review": needs_review,
        "model_review": model_review,
    }
    return draft, report
