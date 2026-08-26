"""Small, provenance-preserving metadata registry clients.

Phase one intentionally supports exact DOI lookups against Crossref only.
Registry output is a candidate; reconciliation decides whether it can change
the source-derived CSL record.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any, Mapping
from urllib.parse import quote

import requests


_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_DOI_PREFIX_RE = re.compile(r"^(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)", re.IGNORECASE)
_TRAILING_DOI_PUNCTUATION = ".,;:!?"
_CROSSREF_WORKS_URL = "https://api.crossref.org/works/"
_TYPE_MAP = {
    "journal-article": "article-journal",
    "journal": "article-journal",
    "book": "book",
    "book-chapter": "chapter",
    "proceedings-article": "paper-conference",
    "report": "report",
    "dissertation": "thesis",
    "posted-content": "article",
}


def normalize_doi(value: str | None) -> str | None:
    """Return a canonical DOI string, or ``None`` when one is not present."""
    if not isinstance(value, str):
        return None
    candidate = _DOI_PREFIX_RE.sub("", value.strip()).strip()
    match = _DOI_RE.search(candidate)
    if not match:
        return None
    doi = match.group(0).rstrip(_TRAILING_DOI_PUNCTUATION)
    # Parentheses may be valid DOI characters, so only remove an unmatched
    # closing delimiter introduced by surrounding prose.
    while doi.endswith(")") and doi.count(")") > doi.count("("):
        doi = doi[:-1]
    while doi.endswith("]") and doi.count("]") > doi.count("["):
        doi = doi[:-1]
    return doi.lower() or None


def extract_doi(text: str | None) -> str | None:
    """Extract and normalize the first DOI in arbitrary source text."""
    return normalize_doi(text)


def _first_string(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        return next((item.strip() for item in value if isinstance(item, str) and item.strip()), None)
    return None


def _date_parts(record: Mapping[str, Any]) -> dict[str, list[list[int]]] | None:
    for key in ("published-print", "issued", "published-online", "published"):
        date = record.get(key)
        if not isinstance(date, Mapping):
            continue
        parts = date.get("date-parts")
        if (
            isinstance(parts, list)
            and parts
            and isinstance(parts[0], list)
            and parts[0]
            and all(isinstance(part, int) for part in parts[0])
        ):
            return {"date-parts": [parts[0]]}
    return None


def _authors(record: Mapping[str, Any]) -> list[dict[str, str]] | None:
    people = record.get("author")
    if not isinstance(people, list):
        return None
    authors: list[dict[str, str]] = []
    for person in people:
        if not isinstance(person, Mapping):
            continue
        family = _first_string(person.get("family"))
        given = _first_string(person.get("given"))
        literal = _first_string(person.get("name"))
        if family:
            author = {"family": family}
            if given:
                author["given"] = given
            authors.append(author)
        elif literal:
            authors.append({"literal": literal})
    return authors or None


def normalize_crossref_work(record: Mapping[str, Any]) -> dict[str, Any]:
    """Map a Crossref work message into the CSL fields used by CiteIndex."""
    candidate: dict[str, Any] = {}
    work_type = _first_string(record.get("type"))
    if work_type:
        candidate["type"] = _TYPE_MAP.get(work_type, "article")
    for source_key, csl_key in (
        ("title", "title"),
        ("container-title", "container-title"),
        ("publisher", "publisher"),
        ("publisher-location", "publisher-place"),
        ("page", "page"),
        ("volume", "volume"),
        ("issue", "issue"),
        ("URL", "URL"),
    ):
        value = _first_string(record.get(source_key))
        if value:
            candidate[csl_key] = value

    doi = normalize_doi(_first_string(record.get("DOI")))
    if doi:
        candidate["DOI"] = doi
    authors = _authors(record)
    if authors:
        candidate["author"] = authors
    issued = _date_parts(record)
    if issued:
        candidate["issued"] = issued
    return candidate


def _result(
    status: str,
    doi: str | None,
    *,
    request_url: str | None = None,
    http_status: int | None = None,
    response_digest: str | None = None,
    candidate: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    provenance = {
        "provider": "crossref",
        "request_identifier": doi,
        "request_url": request_url,
        "http_status": http_status,
        "response_digest": response_digest,
    }
    result: dict[str, Any] = {"status": status, "candidate": candidate, "provenance": provenance}
    if error:
        result["error"] = error
    return result


def lookup_crossref_doi(
    doi: str | None,
    *,
    crossref_enabled: bool = True,
    offline_verification: bool = False,
    timeout: float = 10.0,
    contact_email: str | None = None,
    session: Any = requests,
) -> dict[str, Any]:
    """Look up one DOI at Crossref, retrying one safe request failure once.

    The returned digest is of the response bytes only.  Neither those bytes nor
    the optional contact email are returned, so callers cannot persist them by
    accident as part of the lookup result.
    """
    normalized_doi = normalize_doi(doi)
    if not normalized_doi:
        return _result("not_requested", None, error="no_valid_doi")
    if offline_verification:
        return _result("skipped", normalized_doi, error="offline_verification")
    if not crossref_enabled:
        return _result("skipped", normalized_doi, error="crossref_disabled")

    request_url = _CROSSREF_WORKS_URL + quote(normalized_doi, safe="/")
    headers = {"User-Agent": "CiteIndex/0.12"}
    if contact_email:
        headers["User-Agent"] += " (mailto:" + contact_email + ")"

    response = None
    try:
        for attempt in range(2):
            try:
                response = session.get(request_url, headers=headers, timeout=timeout)
                break
            except requests.RequestException:
                if attempt:
                    raise
        if response is None:  # Defensive; the loop either returns a response or raises.
            return _result("error", normalized_doi, request_url=request_url, error="request_failed")
    except requests.RequestException as exc:
        return _result("error", normalized_doi, request_url=request_url, error=type(exc).__name__)

    raw_response = response.content
    digest = hashlib.sha256(raw_response).hexdigest()
    http_status = response.status_code
    if http_status == 404:
        return _result("not_found", normalized_doi, request_url=request_url, http_status=http_status, response_digest=digest)
    if not 200 <= http_status < 300:
        return _result("error", normalized_doi, request_url=request_url, http_status=http_status, response_digest=digest)

    try:
        payload = response.json()
        work = payload["message"]
        if not isinstance(work, Mapping):
            raise ValueError("Crossref message is not an object")
    except (ValueError, KeyError, TypeError):
        return _result(
            "error", normalized_doi, request_url=request_url, http_status=http_status,
            response_digest=digest, error="malformed_response",
        )

    candidate = normalize_crossref_work(work)
    if candidate.get("DOI") != normalized_doi:
        return _result(
            "error", normalized_doi, request_url=request_url, http_status=http_status,
            response_digest=digest, error="doi_mismatch",
        )
    return _result(
        "found", normalized_doi, request_url=request_url, http_status=http_status,
        response_digest=digest, candidate=candidate,
    )
