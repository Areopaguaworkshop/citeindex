"""Registry-backed PDF metadata enrichment, before artifact finalization."""
from __future__ import annotations

from copy import deepcopy
from difflib import SequenceMatcher
import hashlib
import json
import re
import time
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlsplit

import requests

from .citation_verification import (
    _find_evidence, _same_value, _text_items, validate_block_evidence,
)
from .csl import HOST_FIELDS, valid_host_value
from .deterministic import hash_payload
from .metadata_registry import normalize_doi, normalize_isbn

SCHEMA_VERSION = "1.0"
POLICY_VERSION = "online-enrichment-v1"
_BASE_FIELDS = {"title", "subtitle", "author", "issued", "publisher", "publisher-place", "DOI", "URL", "language"}


def target_fields(csl: dict) -> set[str]:
    fields = set(_BASE_FIELDS)
    kind = csl.get("type")
    if kind in {"book", "chapter", "entry-encyclopedia"}:
        fields |= {"ISBN", "edition", "editor", "translator", "collection-title"}
    if kind in {"article-journal", "article", "chapter", "paper-conference", "entry-encyclopedia"}:
        fields |= {"container-title", "volume", "issue", "page", "ISSN"}
    return fields


def candidate_digest(csl: dict) -> str:
    """Digest the replay stage, excluding finalized identity and volatile output data."""
    return hash_payload({k: v for k, v in csl.items() if k not in {
        "id", "content_hash", "merkle_root", "source_type", "ingestion_timestamp",
    }})


def source_digest(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def refresh_field_states(csl: dict, document: dict | None, resource_type: str, extra: dict) -> dict:
    refreshed = deepcopy(csl)
    old_states = csl.get("_field_status") or {}
    evidence = csl.get("_field_evidence") or {}
    blocks = [b for b in extra.get("source_blocks", []) if isinstance(b, dict)]
    text_items = list(_text_items(document, resource_type))
    supported, states = {}, {}
    for field in set(HOST_FIELDS) | target_fields(csl):
        value, span = csl.get(field), evidence.get(field)
        if (isinstance(value, str) and not value.strip()) or value == [] or value == {}:
            value = refreshed[field] = None
        resolved = None
        if valid_host_value(field, value) and isinstance(span, dict):
            block_id = span.get("block_id") or span.get("locator", {}).get("block_id")
            if block_id or "spans" in span:
                span = {**span, "block_id": block_id}
                used = span.get("spans") or [span]
                ids = {item.get("block_id") for item in used if isinstance(item, dict)}
                # An extraction citation must identify the host, never a reference-list entry.
                if not any(b.get("id") in ids and b.get("role") in {"bibliography", "references"} for b in blocks):
                    resolved = validate_block_evidence(field, value, span, blocks)
            elif span.get("locator"):
                resolved = next((item for item in text_items if item["quote"] == span.get("quote")
                                 and item["locator"] == span["locator"] and _find_evidence(value, [item])), None)
        if resolved:
            supported[field] = resolved
            states[field] = "source-supported"
        elif value is None:
            states[field] = "missing"
        elif field == "title" and old_states.get(field) == "provisional-filename":
            states[field] = "provisional-filename"
        elif old_states.get(field) in {"registry-sourced", "registry-corrected"}:
            states[field] = old_states[field]
        else:
            states[field] = "unverified"
    refreshed["_field_evidence"] = supported
    refreshed["_field_status"] = states
    refreshed["_citation_status"] = "source-supported" if (
        states.get("title") == "source-supported"
        and any(states.get(role) == "source-supported" for role in ("author", "editor"))
        and states.get("issued") == "source-supported"
    ) else "incomplete"
    return refreshed


def _normalized(value: Any) -> str:
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", str(value or "")).casefold()))


def _title(csl: dict) -> str:
    title = str(csl.get("title") or "")
    subtitle = csl.get("subtitle")
    if subtitle and _normalized(subtitle) not in _normalized(title):
        title += ": " + str(subtitle)
    return _normalized(title)


def _year(csl: dict) -> int | None:
    try:
        year = csl["issued"]["date-parts"][0][0]
        return year if type(year) is int else None
    except (KeyError, TypeError, IndexError):
        return None


def _names(csl: dict, field: str = "author") -> set[str]:
    names = set()
    for person in csl.get(field) or []:
        if not isinstance(person, dict):
            continue
        name = _normalized(person.get("family") or person.get("literal"))
        if name:
            names.add(name)
            # Latin literal names may contain given names; CJK names stay intact.
            if " " in name and not re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", name):
                names.add(name.split()[-1])
    return names


def _name_agreement(csl: dict, candidate: dict, field: str) -> bool:
    matched = False
    for source in csl.get(field) or []:
        for target in candidate.get(field) or []:
            if not isinstance(source, dict) or not isinstance(target, dict):
                continue
            if not (_names({field: [source]}, field) & _names({field: [target]}, field)):
                continue
            matched = True
            given_names = []
            for person in (source, target):
                given = person.get("given")
                literal = person.get("literal")
                if not given and literal and not re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", literal):
                    given = literal.split(",", 1)[1] if "," in literal else " ".join(literal.split()[:-1])
                given_names.append(_normalized(given).split())
            for left, right in zip(*given_names):
                if left != right and not (left[0] == right[0] and min(len(left), len(right)) == 1):
                    return False
    return matched


def match_candidate(csl: dict, candidate: dict, *, min_score: float = .90,
                    existing_identifier: bool = False, isbn: bool = False) -> dict:
    """Establish identity before allowing tier-based field corrections."""
    left, right = _title(csl), _title(candidate)
    cjk = bool(re.search(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]", left + right))
    score = float(left == right) if cjk else SequenceMatcher(None, left, right, autojunk=False).ratio()
    result = {"accepted": False, "score": score, "anchors": [], "reason": "title_mismatch"}
    if not left or not right or score < min_score:
        return result
    numeral_pattern = r"\d+|\b(?:volume|vol|part|tome|book)\s+([ivxlcdm]+)\b"
    left_numerals = [m.group(1) or m.group(0) for m in re.finditer(numeral_pattern, left)]
    right_numerals = [m.group(1) or m.group(0) for m in re.finditer(numeral_pattern, right)]
    if left_numerals != right_numerals:
        result["reason"] = "title_numeral_mismatch"
        return result
    kind, candidate_kind = csl.get("type"), candidate.get("type")
    if kind and candidate_kind and kind != candidate_kind:
        if not (kind == "article" and candidate_kind in {"article-journal", "paper-conference"}):
            result["reason"] = "work_type_mismatch"
            return result
    for field in ("edition", "language"):
        if csl.get(field) and candidate.get(field) and _normalized(csl[field]) != _normalized(candidate[field]):
            result["reason"] = field + "_mismatch"
            return result
    states = csl.get("_field_status") or {}
    for field in ("author", "editor"):
        if states.get(field) != "source-supported" or not candidate.get(field):
            continue
        if _name_agreement(csl, candidate, field):
            result["anchors"].append(field)
        elif not existing_identifier or isbn:
            result["reason"] = field + "_mismatch"
            return result
    if isbn and "author" not in result["anchors"]:
        result["reason"] = "isbn_requires_title_author_agreement"
        return result
    for field in ("issued", "publisher", "container-title"):
        if states.get(field) != "source-supported" or not candidate.get(field):
            continue
        source_year, registry_year = _year(csl), _year(candidate)
        agreement = (source_year is not None and registry_year is not None
                     and abs(source_year - registry_year) <= 1) if field == "issued" else _normalized(csl[field]) == _normalized(candidate[field])
        if agreement:
            result["anchors"].append(field)
        elif not existing_identifier:
            result["reason"] = field + "_mismatch"
            return result
    short = len(left.replace(" ", "")) < 12 or (not cjk and len(left.split()) < 3)
    required = 0 if existing_identifier and not isbn else 2 if short else 1
    if len(result["anchors"]) < required:
        result["reason"] = "insufficient_source_anchors"
        return result
    result.update(accepted=True, reason="host_identity_agreement")
    return result


def _identity(result: dict) -> str:
    candidate = result.get("candidate") or {}
    if candidate.get("DOI"):
        return "doi:" + str(normalize_doi(candidate["DOI"]))
    if candidate.get("ISBN"):
        return "isbn:" + str(normalize_isbn(candidate["ISBN"]))
    return str(result.get("provenance", {}).get("source_id") or _title(candidate))


def select_candidate(csl: dict, results: list[dict], min_score: float = .9) -> tuple[dict | None, list[dict]]:
    accepted, rejected = {}, []
    for result in results:
        candidate = result.get("candidate")
        if result.get("status") != "found" or not isinstance(candidate, dict):
            continue
        match = match_candidate(csl, candidate, min_score=min_score, isbn=bool(candidate.get("ISBN") and not candidate.get("DOI")))
        if not match["accepted"]:
            rejected.append({"identity": _identity(result), "match": match, "provenance": result.get("provenance")})
            continue
        row = {**result, "match": match}
        identity = _identity(row)
        if identity not in accepted or row["match"]["score"] > accepted[identity]["match"]["score"]:
            accepted[identity] = row
    ranked = sorted(accepted.values(), key=lambda row: row["match"]["score"], reverse=True)
    if len(ranked) > 1 and ranked[0]["match"]["score"] - ranked[1]["match"]["score"] < .05:
        rejected.extend({"identity": _identity(row), "match": {**row["match"], "reason": "ambiguous_identity"}} for row in ranked)
        return None, rejected
    return (ranked[0] if ranked else None), rejected


def merge_registry(csl: dict, result: dict, tier: str, origin: str, match: dict) -> tuple[dict, list[dict]]:
    merged, decisions = deepcopy(csl), []
    provenance = result.get("provenance") or {}
    if (result.get("status") != "found" or not provenance.get("response_digest")
            or not provenance.get("request_url") or not provenance.get("provider") or tier not in {"T1", "T2", "T4"}):
        return merged, decisions
    evidence = merged.setdefault("_field_evidence", {})
    states = merged.setdefault("_field_status", {})
    for field in sorted(target_fields(csl)):
        value = (result.get("candidate") or {}).get(field)
        old = csl.get(field)
        if not valid_host_value(field, value) or _same_value(field, old, value):
            continue
        if tier == "T4" and old is not None:
            continue
        decision = {"field": field, "old_value": deepcopy(old), "new_value": deepcopy(value),
                    "action": "fill" if old is None else "overwrite", "tier": tier,
                    "discovery_origin": origin,
                    "verification_basis": "isbn_edition_round_trip" if result["provenance"]["provider"] == "openlibrary" else "doi_round_trip",
                    "provenance": deepcopy(provenance), "match": deepcopy(match)}
        if field in evidence:
            decision["source_evidence"] = evidence.pop(field)
        merged[field] = deepcopy(value)
        states[field] = "registry-sourced" if old is None else "registry-corrected"
        decisions.append(decision)
    if decisions:
        merged["_citation_status"] = "source-supported" if all(
            states.get(field) == "source-supported" for field in ("title", "author", "issued")
        ) else "incomplete"
    return merged, decisions


def resolve_citation_url(url: str, timeout: float, client: Any, domains: tuple[str, ...],
                         *, session: Any = requests) -> str | None:
    """Resolve only Google grounding redirects within the shared request budget."""
    from .url_security import validate_public_url, UnsafeUrlError
    deadline = time.monotonic() + timeout
    for _ in range(3):
        response = None
        try:
            parts = urlsplit(url)
            host = parts.hostname or ""
            if parts.scheme != "https" or parts.username or parts.password or parts.port not in (None, 443):
                return None
            validate_public_url(url)
            if any(host == domain or host.endswith("." + domain) for domain in domains):
                return url
            if host != "vertexaisearch.cloud.google.com" or not parts.path.startswith("/grounding-api-redirect/"):
                return None
            remaining = min(client.remaining(), deadline - time.monotonic())
            if remaining <= 0:
                return None
            request_timeout = client.reserve_request(timeout=remaining)
            response = session.get(url, timeout=request_timeout, stream=True, allow_redirects=False)
            if response.status_code not in {301, 302, 303, 307, 308} or not response.headers.get("Location"):
                return None
            url = urljoin(url, response.headers["Location"])
        except (requests.RequestException, ValueError, UnsafeUrlError):
            return None
        finally:
            if response is not None:
                response.close()
    return None


def _resolve(client: Any, config: Any, candidate: dict, *, fresh: bool = False, provider: str | None = None) -> dict | None:
    doi = normalize_doi(candidate.get("DOI"))
    if doi:
        for name in ([provider] if provider else ["crossref", "datacite"]):
            if name not in config.online_enrich_providers or (name == "crossref" and not config.crossref_enabled):
                continue
            result = client.lookup(name, doi, fresh=fresh)
            if result.get("status") == "found":
                return result
    isbn = normalize_isbn(candidate.get("ISBN"))
    if isbn and "openlibrary" in config.online_enrich_providers and config.openlibrary_enabled:
        result = client.lookup("openlibrary", isbn, fresh=fresh)
        if result.get("status") == "found":
            return result
    return None


def apply_enrichment_proposal(csl: dict, payload: dict, source_sha256: str, client: Any, config: Any) -> tuple[dict, list[dict]]:
    """Freshly validate every entry before applying any entry."""
    if config.offline_verification:
        raise ValueError("enrichment proposal requires online verification")
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("unsupported enrichment proposal schema_version")
    if payload.get("source_sha256") != source_sha256 or payload.get("base_candidate_digest") != candidate_digest(csl):
        raise ValueError("enrichment proposal source or candidate digest mismatch")
    proposals = payload.get("proposals")
    if not isinstance(proposals, list) or not proposals or len(proposals) > len(target_fields(csl)):
        raise ValueError("enrichment proposal requires a bounded non-empty proposals array")
    seen, verified, lookups = set(), [], {}
    for entry in proposals:
        if not isinstance(entry, dict):
            raise ValueError("invalid enrichment proposal entry")
        field, provider, kind = entry.get("field"), entry.get("provider"), entry.get("kind")
        old, value = entry.get("old_value"), entry.get("proposed_value")
        if (field not in target_fields(csl) or field in seen or not valid_host_value(field, value)
                or "old_value" not in entry or old != csl.get(field)
                or provider not in {"crossref", "datacite", "openlibrary"}
                or kind not in {"registry_fill", "registry_correction"}
                or not isinstance(entry.get("evidence_reference"), dict) or not entry["evidence_reference"]):
            raise ValueError("invalid, duplicate, unsupported or stale registry proposal")
        if (kind == "registry_fill") != (old is None):
            raise ValueError("registry proposal kind does not match current value")
        seen.add(field)
        identifier = entry.get("identifier")
        normalized = normalize_isbn(identifier) if provider == "openlibrary" else normalize_doi(identifier)
        if not normalized:
            raise ValueError("invalid registry proposal identifier")
        key = (provider, normalized)
        if key not in lookups:
            probe = {"ISBN" if provider == "openlibrary" else "DOI": normalized}
            result = _resolve(client, config, probe, fresh=True, provider=provider)
            if not result:
                raise ValueError("registry proposal identifier could not be freshly verified")
            source_identifier = (normalize_isbn(csl.get("ISBN")) == normalized if provider == "openlibrary"
                                 else normalize_doi(csl.get("DOI")) == normalized)
            source_identifier = source_identifier and csl.get("_field_status", {}).get("ISBN" if provider == "openlibrary" else "DOI") == "source-supported"
            match = match_candidate(csl, result["candidate"], min_score=config.online_enrich_min_score,
                                    existing_identifier=source_identifier, isbn=provider == "openlibrary")
            if not match["accepted"]:
                raise ValueError("registry proposal does not match host identity")
            lookups[key] = result, match
        result, match = lookups[key]
        if not _same_value(field, result["candidate"].get(field), value):
            raise ValueError("registry proposal value does not match freshly fetched evidence")
        if not result.get("provenance", {}).get("response_digest"):
            raise ValueError("registry proposal lacks provenance")
        verified.append((field, result, match, "T2" if provider == "openlibrary" else "T1"))
    if len({_identity(row[1]) for row in verified}) > 1:
        raise ValueError("registry proposal mixes work or edition identities")
    merged, decisions = deepcopy(csl), []
    for field, result, match, tier in verified:
        single = {**result, "candidate": {field: result["candidate"][field]}}
        merged, applied = merge_registry(merged, single, tier, "registry_proposal", match)
        decisions.extend(applied)
    return merged, decisions


def enrich_metadata(csl: dict, document: dict | None, resource_type: str, extra: dict,
                    config: Any, input_ref: str, *, client: Any = None) -> tuple[dict, dict]:
    started = time.monotonic()
    original = deepcopy(csl)
    report: dict = {"schema_version": SCHEMA_VERSION, "policy_version": POLICY_VERSION,
                    "status": "skipped", "decisions": [], "attempts": [], "rejected_candidates": []}
    original_pdf = resource_type in {"digital_pdf", "scanned_pdf"} and Path(input_ref).suffix.lower() == ".pdf"
    if not original_pdf:
        if config.enrich_proposal:
            raise ValueError("enrichment proposals require an original PDF")
        report["reason"] = "pdf_only"
        return original, report
    if config.offline_verification or (not config.online_enrich and not config.enrich_proposal):
        report["reason"] = "offline_verification" if config.offline_verification else "disabled"
        return original, report
    current = refresh_field_states(csl, document, resource_type, extra)
    report.update(source_sha256=source_digest(input_ref), candidate_input_snapshot=deepcopy(current),
                  candidate_input_digest=candidate_digest(current),
                  source_repaired_fields=sorted({entry["field"] for entry in extra.get("citation_repair", {}).get("applied", [])}))
    if current["_field_status"].get("title") != "source-supported":
        if config.enrich_proposal:
            raise ValueError("enrichment proposal requires source-supported title")
        report["reason"] = "no_source_supported_title"
        return current, report
    if client is None:
        from .metadata_registry import RegistryClient
        client = RegistryClient(config)
    def finish() -> tuple[dict, dict]:
        report.update(attempts=deepcopy(client.attempts), request_count=client.request_count,
                      cache_hits=client.cache_hits, budget_seconds=config.online_enrich_timeout,
                      elapsed_seconds=round(time.monotonic() - started, 3))
        if report["decisions"]:
            report["status"] = "enriched"
        return current, report
    if config.enrich_proposal:
        path = Path(config.enrich_proposal)
        if path.stat().st_size > 2 * 1024 * 1024:
            raise ValueError("enrichment proposal exceeds size limit")
        payload = json.loads(path.read_text(encoding="utf-8"))
        current, decisions = apply_enrichment_proposal(current, payload, report["source_sha256"], client, config)
        report["decisions"].extend(decisions)
    if not config.online_enrich:
        return finish()
    missing = [field for field in target_fields(current) if current.get(field) is None]
    if not missing:
        report["reason"] = "no_missing_target_fields"
        return finish()
    # Match against the accepted source snapshot, never against our own registry writes.
    anchors = report["candidate_input_snapshot"]
    resolved = _resolve(client, config, anchors)
    proposal_identifiers = {d["provenance"]["provider"]: d["provenance"].get("request_identifier")
                            for d in report["decisions"]}
    accepted_identity = next((("isbn:" if p == "openlibrary" else "doi:") + identifier
                              for p, identifier in proposal_identifiers.items() if identifier), None)
    if resolved:
        isbn = resolved["provenance"]["provider"] == "openlibrary"
        source_identifier = anchors["_field_status"].get("ISBN" if isbn else "DOI") == "source-supported"
        match = match_candidate(anchors, resolved["candidate"], min_score=config.online_enrich_min_score,
                                existing_identifier=source_identifier, isbn=isbn)
        if match["accepted"] and (accepted_identity is None or accepted_identity == _identity(resolved)):
            tier = "T2" if isbn else "T1"
            protected = {d["field"] for d in report["decisions"] if int(d["tier"][1:]) < int(tier[1:])}
            resolved["candidate"] = {k: v for k, v in resolved["candidate"].items() if k not in protected}
            current, decisions = merge_registry(current, resolved, tier, "source_identifier", match)
            report["decisions"].extend(decisions)
            accepted_identity = _identity(resolved)
        else:
            report["rejected_candidates"].append({"identity": _identity(resolved), "match": match})
    order = ["openlibrary", "crossref", "datacite", "openalex"] if anchors.get("type") == "book" else ["datacite", "crossref", "openalex", "openlibrary"] if anchors.get("type") in {"thesis", "report", "dataset"} else ["crossref", "openalex", "datacite", "openlibrary"]
    results = []
    for provider in order:
        if provider not in config.online_enrich_providers or client.remaining() <= 0:
            continue
        if (provider == "crossref" and not config.crossref_enabled) or (provider == "openlibrary" and not config.openlibrary_enabled):
            continue
        if not any(current.get(field) is None for field in target_fields(current)):
            break
        results.extend(client.search(provider, anchors["title"],
                                     author=next(iter(sorted(_names(anchors))), None) if anchors["_field_status"].get("author") == "source-supported" else None,
                                     year=_year(anchors) if anchors["_field_status"].get("issued") == "source-supported" else None))
    winner, rejected = select_candidate(anchors, results, config.online_enrich_min_score)
    report["rejected_candidates"].extend(rejected)
    if winner:
        authority = _resolve(client, config, winner["candidate"])
        if authority:
            match = match_candidate(anchors, authority["candidate"], min_score=config.online_enrich_min_score,
                                    isbn=authority["provenance"]["provider"] == "openlibrary")
            if match["accepted"] and (accepted_identity is None or accepted_identity == _identity(authority)):
                authority["provenance"] = {**authority["provenance"], "search_evidence": winner["provenance"]}
                tier = "T2" if authority["provenance"]["provider"] == "openlibrary" else "T1"
                protected = {d["field"] for d in report["decisions"] if int(d["tier"][1:]) < int(tier[1:])}
                authority["candidate"] = {k: v for k, v in authority["candidate"].items() if k not in protected}
                current, decisions = merge_registry(current, authority, tier, "catalog_search", match)
                report["decisions"].extend(decisions)
                accepted_identity = _identity(authority)
        else:
            report["rejected_candidates"].append({"identity": _identity(winner), "reason": "identifier_free_writes_deferred"})
    if config.online_enrich_ai_fallback and accepted_identity is None and client.remaining() > 5 and client.request_count < 12:
        from .ai_discovery import discover_identifier
        try:
            timeout = client.reserve_request(timeout=client.remaining() - 5)
        except ValueError:
            report["reason"] = "budget_exhausted"
            return finish()
        discovery = discover_identifier(
            anchors, config, timeout=timeout,
            resolve_source_url=lambda url, remaining: resolve_citation_url(url, remaining, client, config.online_enrich_ai_domains),
        )
        report["ai_discovery"] = discovery
        verified = {}
        for identifier in discovery.get("identifiers", []) if discovery.get("status") == "found" else []:
            authority = _resolve(client, config, {identifier["kind"]: identifier["value"]})
            if not authority:
                continue
            match = match_candidate(anchors, authority["candidate"], min_score=config.online_enrich_min_score,
                                    isbn=identifier["kind"] == "ISBN")
            if match["accepted"]:
                authority["provenance"] = {**authority["provenance"], "discovery_evidence": identifier.get("evidence"), "ai_provenance": discovery.get("provenance")}
                verified[_identity(authority)] = authority, match
        if len(verified) == 1:
            authority, match = next(iter(verified.values()))
            current, decisions = merge_registry(current, authority, "T4", "ai_search", match)
            report["decisions"].extend(decisions)
        elif verified:
            report["rejected_candidates"].append({"reason": "ambiguous_ai_identifiers", "identities": sorted(verified)})
    report["status"] = "unresolved"
    report["reason"] = "budget_exhausted" if client.remaining() <= 0 or client.request_count >= 12 else "no_additional_verified_fields"
    return finish()
