"""Small, provenance-preserving metadata registry clients.

Registry lookups resolve one authority identifier: DOIs at Crossref, ISBNs
at OpenLibrary. Registry output is a candidate; reconciliation decides
whether it can change the source-derived CSL record.
"""

from __future__ import annotations

import hashlib
import copy
import json
import os
import re
import time
import unicodedata
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote, urlencode, urlsplit, urlunsplit, parse_qsl

import requests
from urllib3.exceptions import HTTPError, ReadTimeoutError

from .pipelines.frontmatter import validate_isbn13


_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_DOI_PREFIX_RE = re.compile(r"^(?:doi\s*:\s*|https?://(?:dx\.)?doi\.org/)", re.IGNORECASE)
_TRAILING_DOI_PUNCTUATION = ".,;:!?"
_CROSSREF_WORKS_URL = "https://api.crossref.org/works/"
_OPENLIBRARY_ISBN_URL = "https://openlibrary.org/isbn/"
_ISBN_PREFIX_RE = re.compile(r"^\s*(?:isbn(?:-13)?)?\s*[:：]?\s*", re.IGNORECASE)
_ISBN_SEPARATORS_RE = re.compile(r"[\s\-–]")
_YEAR_RE = re.compile(r"\d{4}")
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


def normalize_isbn(value: str | None) -> str | None:
    """Return a canonical ISBN-13 string, or ``None`` when one is not present."""
    if not isinstance(value, str):
        return None
    candidate = _ISBN_SEPARATORS_RE.sub("", _ISBN_PREFIX_RE.sub("", value))
    return candidate if len(candidate) == 13 and candidate.isdigit() and validate_isbn13(candidate) else None


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
    for key in ("ISBN", "ISSN", "language", "edition"):
        value = _first_string(record.get(key))
        if value:
            candidate[key] = value
    for role in ("editor", "translator"):
        people = _authors({"author": record.get(role)})
        if people:
            candidate[role] = people
    return candidate


def normalize_openlibrary_edition(record: Mapping[str, Any], isbn: str) -> dict[str, Any]:
    """Map an OpenLibrary edition record into the CSL fields used by CiteIndex."""
    candidate: dict[str, Any] = {"type": "book", "ISBN": isbn}
    title = _first_string(record.get("title"))
    if title:
        candidate["title"] = title
    subtitle = _first_string(record.get("subtitle"))
    if subtitle:
        candidate["subtitle"] = subtitle
    for source_key, csl_key in (("publishers", "publisher"), ("publish_places", "publisher-place")):
        publishers = record.get(source_key)
        joined = (
            " and ".join(item.strip() for item in publishers if isinstance(item, str) and item.strip())
            if isinstance(publishers, list) and publishers else _first_string(publishers)
        )
        if joined:
            candidate[csl_key] = joined
    year = _YEAR_RE.search(str(record.get("publish_date") or ""))
    if year:
        candidate["issued"] = {"date-parts": [[int(year.group(0))]]}
    for source_key, csl_key in (("edition_name", "edition"), ("number_of_pages", "number-of-pages")):
        value = record.get(source_key)
        if isinstance(value, (str, int)) and str(value).strip():
            candidate[csl_key] = str(value).strip()
    languages = record.get("languages")
    if isinstance(languages, list) and languages and isinstance(languages[0], Mapping):
        language = str(languages[0].get("key", "")).removeprefix("/languages/")
        if language:
            candidate["language"] = language
    people = record.get("authors")
    if isinstance(people, list):
        names = [{"literal": name} for person in people if isinstance(person, Mapping)
                 if (name := _first_string(person.get("name")))]
        if names:
            candidate["author"] = names
    return candidate


def _result(
    status: str,
    identifier: str | None,
    *,
    provider: str = "crossref",
    request_url: str | None = None,
    http_status: int | None = None,
    response_digest: str | None = None,
    candidate: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    provenance = {
        "provider": provider,
        "request_identifier": identifier,
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


def lookup_openlibrary_isbn(
    isbn: str | None,
    *,
    openlibrary_enabled: bool = True,
    offline_verification: bool = False,
    timeout: float = 10.0,
    session: Any = requests,
) -> dict[str, Any]:
    """Look up one ISBN-13 at OpenLibrary, retrying one safe request failure once.

    Mirrors :func:`lookup_crossref_doi`: the digest covers the response bytes
    only, and neither those bytes nor any other response content are returned.
    """
    normalized_isbn = normalize_isbn(isbn)
    if not normalized_isbn:
        return _result("not_requested", None, provider="openlibrary", error="invalid_isbn")
    if offline_verification:
        return _result("skipped", normalized_isbn, provider="openlibrary", error="offline_verification")
    if not openlibrary_enabled:
        return _result("skipped", normalized_isbn, provider="openlibrary", error="openlibrary_disabled")

    request_url = _OPENLIBRARY_ISBN_URL + quote(normalized_isbn) + ".json"
    headers = {"User-Agent": "CiteIndex/0.12"}

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
            return _result("error", normalized_isbn, provider="openlibrary", request_url=request_url, error="request_failed")
    except requests.RequestException as exc:
        return _result("error", normalized_isbn, provider="openlibrary", request_url=request_url, error=type(exc).__name__)

    raw_response = response.content
    digest = hashlib.sha256(raw_response).hexdigest()
    http_status = response.status_code
    if http_status == 404:
        return _result("not_found", normalized_isbn, provider="openlibrary", request_url=request_url, http_status=http_status, response_digest=digest)
    if not 200 <= http_status < 300:
        return _result("error", normalized_isbn, provider="openlibrary", request_url=request_url, http_status=http_status, response_digest=digest)

    try:
        edition = response.json()
        if not isinstance(edition, Mapping) or "title" not in edition:
            raise ValueError("OpenLibrary edition is not an object")
    except (ValueError, KeyError, TypeError):
        return _result(
            "error", normalized_isbn, provider="openlibrary", request_url=request_url, http_status=http_status,
            response_digest=digest, error="malformed_response",
        )

    candidate = normalize_openlibrary_edition(edition, normalized_isbn)
    return _result(
        "found", normalized_isbn, provider="openlibrary", request_url=request_url, http_status=http_status,
        response_digest=digest, candidate=candidate,
    )


NORMALIZATION_VERSION = 1
_PROVIDER_URLS = {
    "crossref": "https://api.crossref.org/works",
    "datacite": "https://api.datacite.org/dois",
    "openalex": "https://api.openalex.org/works",
    "openlibrary": "https://openlibrary.org",
}
_PROVIDER_INTERVALS = {"crossref": 0.1, "datacite": 0.6, "openalex": 0.1, "openlibrary": 1.0}
_MAX_RESPONSE_BYTES = 2 * 1024 * 1024


def bounded_response_bytes(response: Any, deadline: float, clock: Any = time.monotonic,
                           max_bytes: int = _MAX_RESPONSE_BYTES) -> bytes:
    """Read available bytes while shrinking socket timeouts to the stage deadline.

    Requests must use ``Accept-Encoding: identity``. Fixture response objects may
    implement ``iter_content``; real requests must expose their streaming socket.
    """
    headers = getattr(response, "headers", {})
    headers = headers if isinstance(headers, Mapping) else {}
    declared = headers.get("Content-Length", headers.get("content-length", "0"))
    if int(declared) > max_bytes:
        raise ValueError("response_too_large")
    encoding = headers.get("Content-Encoding", headers.get("content-encoding", "identity"))
    if str(encoding).casefold() not in {"", "identity"}:
        raise ValueError("unsupported_content_encoding")
    raw = getattr(response, "raw", None)
    socket = None
    if isinstance(response, requests.Response):
        socket = getattr(getattr(raw, "_connection", None), "sock", None)
        if socket is None:
            socket = getattr(getattr(getattr(getattr(raw, "_fp", None), "fp", None), "raw", None), "_sock", None)
        if not callable(getattr(raw, "read1", None)) or socket is None:
            if getattr(raw, "length_remaining", None) == 0 and clock() < deadline:
                return b""
            raise ValueError("unsupported_bounded_stream")
        chunks = None
    else:
        chunks = iter(response.iter_content(chunk_size=64 * 1024))
    received: list[bytes] = []
    size = 0
    while True:
        remaining = deadline - clock()
        if remaining <= 0:
            raise ValueError("budget_exhausted")
        try:
            if chunks is None:
                assert socket is not None and raw is not None
                socket.settimeout(min(5.0, remaining))
                chunk = raw.read1(64 * 1024, decode_content=False)
            else:
                chunk = next(chunks, b"")
        except (ReadTimeoutError, TimeoutError) as exc:
            raise requests.Timeout() from exc
        except (HTTPError, OSError) as exc:
            raise requests.ConnectionError() from exc
        if clock() >= deadline:
            raise ValueError("budget_exhausted")
        if not chunk:
            return b"".join(received)
        size += len(chunk)
        if size > max_bytes:
            raise ValueError("response_too_large")
        received.append(chunk)


def normalize_datacite_work(record: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a DataCite JSON:API record without storing provider internals."""
    attrs = record.get("attributes", record)
    if not isinstance(attrs, Mapping):
        return {}
    titles = attrs.get("titles", [])
    people = attrs.get("creators", [])
    types = attrs.get("types", {})
    general_type = types.get("resourceTypeGeneral", "") if isinstance(types, Mapping) else ""
    candidate = normalize_crossref_work({
        "DOI": attrs.get("doi"), "title": [item.get("title") for item in titles if isinstance(item, Mapping)],
        "publisher": attrs.get("publisher"), "language": attrs.get("language"),
        "author": [{"family": person.get("familyName"), "given": person.get("givenName"),
                    "name": person.get("name")} for person in people if isinstance(person, Mapping)],
    })
    candidate["type"] = {"Dissertation": "thesis", "Report": "report", "Dataset": "dataset",
                         "Book": "book", "BookChapter": "chapter", "JournalArticle": "article-journal"}.get(general_type, "article")
    if general_type in {"Text", "Other", ""} and isinstance(types, Mapping):
        specific_type = str(types.get("resourceType") or "").strip().casefold()
        if specific_type in {"thesis", "dissertation"}:
            candidate["type"] = "thesis"
        elif specific_type == "report":
            candidate["type"] = "report"
    year = str(attrs.get("publicationYear") or "")
    if year.isdigit() and 1000 <= int(year) <= 9999:
        candidate["issued"] = {"date-parts": [[int(year)]]}
    contributors = attrs.get("contributors", [])
    for role, source_role in (("editor", "Editor"), ("translator", "Translator")):
        names = _authors({"author": [{"family": person.get("familyName"), "given": person.get("givenName"),
                                    "name": person.get("name")} for person in contributors
                                   if isinstance(person, Mapping) and person.get("contributorType") == source_role]})
        if names:
            candidate[role] = names
    return candidate


def normalize_openalex_work(record: Mapping[str, Any]) -> dict[str, Any]:
    """Map OpenAlex work fields; authors retain their supplied spelling."""
    authorships = record.get("authorships", [])
    location = record.get("primary_location") or {}
    source = location.get("source") or {} if isinstance(location, Mapping) else {}
    biblio = record.get("biblio") or {}
    candidate = normalize_crossref_work({
        "DOI": record.get("doi"), "title": record.get("title") or record.get("display_name"),
        "type": {"article": "journal-article", "dissertation": "dissertation", "book-chapter": "book-chapter"}.get(str(record.get("type") or ""), record.get("type")),
        "author": [{"name": person["author"].get("display_name")} for person in authorships
                   if isinstance(person, Mapping) and isinstance(person.get("author"), Mapping)],
        "container-title": source.get("display_name") if isinstance(source, Mapping) else None,
        "publisher": source.get("host_organization_name") if isinstance(source, Mapping) else None,
        "ISSN": source.get("issn") if isinstance(source, Mapping) else None,
        "volume": biblio.get("volume") if isinstance(biblio, Mapping) else None,
        "issue": biblio.get("issue") if isinstance(biblio, Mapping) else None,
        "language": record.get("language"),
    })
    if isinstance(biblio, Mapping) and biblio.get("first_page"):
        candidate["page"] = str(biblio["first_page"])
        if biblio.get("last_page") and biblio["last_page"] != biblio["first_page"]:
            candidate["page"] += "-" + str(biblio["last_page"])
    year = record.get("publication_year")
    if isinstance(year, int) and 1000 <= year <= 9999:
        candidate["issued"] = {"date-parts": [[year]]}
    return candidate


def _sanitized_url(url: str) -> str:
    parts = urlsplit(url)
    query = [(key, value) for key, value in parse_qsl(parts.query)
             if key.lower() not in {"mailto", "email", "contact_email", "api_key", "apikey", "key", "token", "access_token", "authorization"}]
    return urlunsplit((parts.scheme, parts.hostname or "", parts.path, urlencode(query), ""))


class RegistryClient:
    """One bounded request context shared by enrichment and registry verification.

    Cache files contain normalized candidates and redacted provenance only.
    The legacy lookup functions remain available with their original signatures.
    """

    def __init__(self, config: Any, cache_dir: str | Path | None = None, *, session: Any = requests,
                 clock: Any = time.monotonic, sleep: Any = time.sleep):
        self.config, self.session, self.clock, self.sleep = config, session, clock, sleep
        self.deadline = clock() + float(self._config("online_enrich_timeout", 20.0))
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.request_count = self.cache_hits = 0
        self.attempts: list[dict[str, Any]] = []
        self._cache: dict[str, dict[str, Any]] = {}
        # ponytail: pacing is per process/context; coordinate externally for multi-process batches.
        self._next_request: dict[str, float] = {}
        self._intervals = dict(_PROVIDER_INTERVALS)

    def _config(self, key: str, default: Any = None) -> Any:
        return self.config.get(key, default) if isinstance(self.config, Mapping) else getattr(self.config, key, default)

    def remaining(self) -> float:
        return max(0.0, self.deadline - self.clock())

    def reserve_request(self, timeout: float = 5.0) -> float:
        """Reserve an external AI/location attempt in the same stage budget."""
        if timeout <= 0:
            raise ValueError("invalid_timeout")
        if self.request_count >= 12 or not self.remaining():
            raise ValueError("budget_exhausted")
        self.request_count += 1
        return min(5.0, float(timeout), self.remaining())

    def _enabled(self, provider: str) -> str | None:
        if provider not in _PROVIDER_URLS:
            return "unsupported_provider"
        if self._config("offline_verification", False):
            return "offline_verification"
        if not self._config(provider + "_enabled", True):
            return provider + "_disabled"
        selected = self._config("online_enrich_providers", tuple(_PROVIDER_URLS))
        if isinstance(selected, str):
            selected = [name.strip() for name in selected.split(",")]
        return None if provider in selected else provider + "_disabled"

    def _failure(self, provider: str, identifier: str | None, error: str, status: str = "error") -> dict[str, Any]:
        result = _result(status, identifier, provider=provider, error=error)
        self.attempts.append(copy.deepcopy(result))
        return result

    def _wait(self, seconds: float) -> bool:
        if self.remaining() <= 0 or seconds >= self.remaining():
            return False
        if seconds > 0:
            self.sleep(seconds)
        return self.remaining() > 0

    def _retry_delay(self, headers: Mapping[str, Any]) -> float:
        value = headers.get("Retry-After") or headers.get("retry-after")
        if value is not None:
            try:
                return max(0.0, float(value))
            except (ValueError, TypeError):
                try:
                    return max(0.0, parsedate_to_datetime(str(value)).timestamp() - time.time())
                except (ValueError, TypeError, OverflowError):
                    pass
        return 0.5

    def _rate_headers(self, provider: str, headers: Mapping[str, Any]) -> None:
        headers = {str(key).lower(): value for key, value in headers.items()}
        try:
            limit = float(headers.get("x-rate-limit-limit", headers.get("ratelimit-limit", 0)))
            interval = float(str(headers.get("x-rate-limit-interval", "1s")).rstrip("s"))
            if limit > 0:
                self._intervals[provider] = max(self._intervals[provider], interval / limit)
                self._next_request[provider] = max(self._next_request.get(provider, 0), self.clock() + self._intervals[provider])
            remaining = headers.get("x-ratelimit-remaining", headers.get("x-rate-limit-remaining", headers.get("ratelimit-remaining")))
            reset = headers.get("x-ratelimit-reset", headers.get("x-rate-limit-reset", headers.get("ratelimit-reset")))
            if remaining is not None and float(remaining) <= 0 and reset is not None:
                wait = float(reset)
                if wait > 1_000_000_000:
                    wait = max(0, wait - time.time())
                self._next_request[provider] = self.clock() + max(0, wait)
        except (ValueError, TypeError):
            pass

    def _request(self, provider: str, url: str, identifier: str | None,
                 params: Mapping[str, Any] | None = None) -> tuple[Any, dict[str, Any]]:
        params = dict(params or {})
        headers = {"User-Agent": "CiteIndex/0.12", "Accept-Encoding": "identity"}
        contact = self._config("registry_contact_email")
        if contact:
            headers["User-Agent"] += " (mailto:" + str(contact) + ")"
        if provider == "openalex" and os.environ.get("OPENALEX_API_KEY"):
            params["api_key"] = os.environ["OPENALEX_API_KEY"]
        request_url = _sanitized_url(url + ("?" + urlencode(params) if params else ""))
        for attempt in range(2):
            if self.request_count >= 12 or not self._wait(max(0, self._next_request.get(provider, 0) - self.clock())):
                return None, self._failure(provider, identifier, "budget_exhausted", "budget_exhausted")
            self.request_count += 1
            self._next_request[provider] = self.clock() + self._intervals[provider]
            response = None
            try:
                response = self.session.get(url, params=params, headers=headers, timeout=min(5.0, self.remaining()),
                                            stream=True, allow_redirects=False)
                if not self.remaining():
                    return None, self._failure(provider, identifier, "budget_exhausted", "budget_exhausted")
                self._next_request[provider] = self.clock() + self._intervals[provider]
                response_headers = response.headers if isinstance(response.headers, Mapping) else {}
                self._rate_headers(provider, response_headers)
                status = response.status_code
                try:
                    raw = bounded_response_bytes(response, self.deadline, self.clock)
                except ValueError as exc:
                    reason = str(exc)
                    if reason not in {"budget_exhausted", "response_too_large", "unsupported_bounded_stream", "unsupported_content_encoding"}:
                        reason = "malformed_response"
                    return None, self._failure(provider, identifier, reason,
                                               "budget_exhausted" if reason == "budget_exhausted" else "error")
                result = _result("found" if 200 <= status < 300 else "not_found" if status == 404 else "error",
                                 identifier, provider=provider, request_url=request_url, http_status=status,
                                 response_digest=hashlib.sha256(raw).hexdigest())
                result["provenance"]["retrieved_at"] = datetime.now(timezone.utc).isoformat()
                self.attempts.append(result)
                if status == 429 or 500 <= status < 600:
                    if not attempt:
                        if not self._wait(self._retry_delay(response_headers)):
                            return None, self._failure(provider, identifier, "budget_exhausted", "budget_exhausted")
                        continue
                if result["status"] != "found":
                    return None, result
                try:
                    payload = json.loads(raw)
                    if not isinstance(payload, Mapping):
                        raise ValueError("expected object")
                    return payload, result
                except (ValueError, TypeError, UnicodeError):
                    result.update(status="error", error="malformed_response")
                    return None, result
            except requests.RequestException as exc:
                self.attempts.append(_result("error", identifier, provider=provider, request_url=request_url, error=type(exc).__name__))
                if attempt:
                    return None, _result("error", identifier, provider=provider, request_url=request_url, error=type(exc).__name__)
                if not self._wait(0.5):
                    return None, self._failure(provider, identifier, "budget_exhausted", "budget_exhausted")
            except (ValueError, TypeError, AttributeError):
                return None, self._failure(provider, identifier, "malformed_response")
            finally:
                if response is not None:
                    response.close()
        return None, self._failure(provider, identifier, "request_failed")

    def _cache_key(self, provider: str, operation: str, query: Any) -> str:
        encoded = json.dumps([NORMALIZATION_VERSION, provider, operation, query], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(encoded.encode()).hexdigest()

    def _cached(self, key: str) -> Any:
        entry = self._cache.get(key)
        if entry is None and self.cache_dir is not None:
            try:
                entry = json.loads((self.cache_dir / (key + ".json")).read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                return None
        try:
            if entry is None or entry["version"] != NORMALIZATION_VERSION or float(entry["expires_at"]) <= time.time():
                return None
            result = copy.deepcopy(entry["result"])
            results = result if isinstance(result, list) else [result]
            if any(not isinstance(item, dict) or item.get("status") not in {"found", "not_found"}
                   or not isinstance(item.get("provenance"), dict) for item in results):
                return None
            for item in results:
                item["provenance"]["cache_hit"] = True
            self.cache_hits += 1
            self.attempts.extend(copy.deepcopy(results))
            return result
        except (KeyError, ValueError, TypeError):
            return None

    def _store(self, key: str, result: Any) -> None:
        results = result if isinstance(result, list) else [result]
        if not results or any(item.get("status") not in {"found", "not_found"} for item in results):
            return
        if any(evidence.get("status") != "found" for item in results
               for evidence in item.get("provenance", {}).get("hydration", [])):
            return
        positive = any(item["status"] == "found" for item in results)
        ttl = float(self._config("online_enrich_cache_ttl", 7 * 86400)) if positive else 3600
        entry = {"version": NORMALIZATION_VERSION, "expires_at": time.time() + ttl, "result": copy.deepcopy(result)}
        self._cache[key] = entry
        if self.cache_dir is not None:
            try:
                from .storage import write_json
                write_json(str(self.cache_dir / (key + ".json")), entry)
            except (OSError, ValueError, TypeError):
                pass

    def lookup(self, provider: str, identifier: str, fresh: bool = False) -> dict[str, Any]:
        """Resolve a fixed authority endpoint and verify its returned identifier."""
        if reason := self._enabled(provider):
            return self._failure(provider, None, reason, "skipped")
        normalized = normalize_isbn(identifier) if provider == "openlibrary" else normalize_doi(identifier)
        if not normalized:
            return self._failure(provider, None, "invalid_identifier", "not_requested")
        key = self._cache_key(provider, "lookup", normalized)
        if not fresh and (cached := self._cached(key)) is not None:
            return cached
        endpoint = _PROVIDER_URLS[provider]
        if provider == "openlibrary":
            url = endpoint + "/isbn/" + normalized + ".json"
        elif provider == "openalex":
            url = endpoint + "/" + quote("https://doi.org/" + normalized, safe="")
        else:
            url = endpoint + "/" + quote(normalized, safe="/")
        payload, result = self._request(provider, url, normalized)
        if payload is not None:
            try:
                if provider == "openlibrary":
                    return copy.deepcopy(self._edition_result(payload, result, normalized, key))
                work = payload["message"] if provider == "crossref" else payload["data"] if provider == "datacite" else payload
                if not isinstance(work, Mapping):
                    raise ValueError("expected work")
                normalizer = {"crossref": normalize_crossref_work, "datacite": normalize_datacite_work,
                              "openalex": normalize_openalex_work}[provider]
                candidate = normalizer(work)
                if candidate.get("URL"):
                    candidate["URL"] = _sanitized_url(candidate["URL"])
                if candidate.get("DOI") != normalized:
                    result.update(status="error", error="doi_mismatch")
                else:
                    result["candidate"] = candidate
                    result["provenance"]["source_id"] = str(work.get("id") or candidate["DOI"])
            except (ValueError, KeyError, TypeError, AttributeError):
                result.update(status="error", error="malformed_response", candidate=None)
        self._store(key, result)
        return copy.deepcopy(result)

    def _edition_result(self, edition: Mapping[str, Any], result: dict[str, Any],
                        isbn: str | None = None, cache_key: str | None = None) -> dict[str, Any]:
        edition_key = edition.get("key")
        if not isinstance(edition_key, str) or not re.fullmatch(r"/books/OL\d+M", edition_key) or not _first_string(edition.get("title")):
            result.update(status="error", error="malformed_edition", candidate=None)
            return result
        identifiers = edition.get("isbn_13", [])
        returned = [value for item in identifiers if (value := normalize_isbn(item))] if isinstance(identifiers, list) else []
        if isbn and isbn not in returned:
            result.update(status="error", error="isbn_mismatch", candidate=None)
            return result
        candidate = normalize_openlibrary_edition(edition, isbn or (returned[0] if returned else ""))
        if not candidate.get("ISBN"):
            candidate.pop("ISBN", None)
        result["provenance"]["source_id"] = edition_key
        evidence: list[dict[str, Any]] = []
        authors: list[dict[str, str]] = []
        people = edition.get("authors") or []
        if not isinstance(people, list):
            result.update(status="error", error="malformed_edition", candidate=None)
            return result
        incomplete_authors = False
        for person in people:
            if not isinstance(person, Mapping):
                incomplete_authors = True
                continue
            name = _first_string(person.get("name"))
            if name:
                authors.append({"literal": name})
                continue
            author_key = person.get("key")
            if not isinstance(author_key, str) or not re.fullmatch(r"/authors/OL\d+A", author_key):
                incomplete_authors = True
                continue
            if len(evidence) >= 3:
                incomplete_authors = True
                continue
            payload, fetched = self._request("openlibrary", _PROVIDER_URLS["openlibrary"] + author_key + ".json", author_key)
            evidence.append(fetched)
            if payload is not None and payload.get("key") == author_key and (name := _first_string(payload.get("name"))):
                authors.append({"literal": name})
            else:
                incomplete_authors = True
        if authors and not incomplete_authors:
            candidate["author"] = authors
        elif incomplete_authors:
            candidate.pop("author", None)
            result["provenance"]["author_hydration_incomplete"] = True
        # Missing hydration is an incomplete candidate, so the matcher must abstain if it needs authors.
        if evidence:
            result["provenance"]["hydration"] = evidence
        result["candidate"] = candidate
        if cache_key and (not evidence or all(item["status"] == "found" for item in evidence)):
            self._store(cache_key, result)
        return result

    def search(self, provider: str, title: str, author: str | None = None, year: int | None = None) -> list[dict[str, Any]]:
        """Search at most five records; OpenLibrary work hits become edition records."""
        if reason := self._enabled(provider):
            return [self._failure(provider, None, reason, "skipped")]
        if not isinstance(title, str) or not title.strip():
            return [self._failure(provider, None, "missing_title", "not_requested")]
        title = unicodedata.normalize("NFKC", " ".join(title.split()))
        key = self._cache_key(provider, "search", [title, author, year])
        if (cached := self._cached(key)) is not None:
            return cached
        query = title + (" " + author if author else "")
        if provider == "crossref":
            url, params = _PROVIDER_URLS[provider], {"query.bibliographic": query, "rows": 5}
        elif provider == "openalex":
            url, params = _PROVIDER_URLS[provider], {"search": query, "per-page": 5}
        elif provider == "datacite":
            url, params = _PROVIDER_URLS[provider], {"query": query, "page[size]": 5}
        else:
            url, params = _PROVIDER_URLS[provider] + "/search.json", {"title": title, "limit": 5, "fields": "key,title,author_name,edition_key,editions"}
            if author:
                params["author"] = author
        payload, fetched = self._request(provider, url, title, params)
        if payload is None:
            return [fetched]
        try:
            records = payload["message"]["items"] if provider == "crossref" else payload["results"] if provider == "openalex" else payload["data"] if provider == "datacite" else payload["docs"]
            if not isinstance(records, list) or any(not isinstance(item, Mapping) for item in records[:5]):
                raise ValueError("expected records")
            results: list[dict[str, Any]] = []
            seen: set[str] = set()
            for record in records[:5]:
                if provider == "openlibrary":
                    editions = record.get("editions", {})
                    edition_docs = editions.get("docs", []) if isinstance(editions, Mapping) else []
                    edition_keys = [item.get("key") for item in edition_docs if isinstance(item, Mapping)]
                    if not edition_keys:
                        edition_keys = record.get("edition_key", [])
                    if not isinstance(edition_keys, list):
                        continue
                    for edition_key in edition_keys[:5]:
                        edition_key = str(edition_key)
                        if not edition_key.startswith("/books/"):
                            edition_key = "/books/" + edition_key
                        if not re.fullmatch(r"/books/OL\d+M", edition_key) or edition_key in seen or len(results) >= 5:
                            continue
                        seen.add(edition_key)
                        edition, result = self._request(provider, _PROVIDER_URLS[provider] + edition_key + ".json", edition_key)
                        if edition is not None:
                            if edition.get("key") != edition_key:
                                result.update(status="error", error="edition_mismatch")
                            else:
                                result = self._edition_result(edition, result)
                        result["provenance"]["search"] = copy.deepcopy(fetched["provenance"])
                        results.append(result)
                        if result["status"] == "budget_exhausted":
                            break
                else:
                    normalizer = {"crossref": normalize_crossref_work, "datacite": normalize_datacite_work, "openalex": normalize_openalex_work}[provider]
                    candidate = normalizer(record)
                    if candidate.get("URL"):
                        candidate["URL"] = _sanitized_url(candidate["URL"])
                    if not candidate.get("title"):
                        raise ValueError("missing title")
                    identity = str(candidate.get("DOI") or record.get("id") or candidate["title"])
                    if identity in seen:
                        continue
                    seen.add(identity)
                    result = copy.deepcopy(fetched)
                    result["candidate"] = candidate
                    result["provenance"]["source_id"] = str(record.get("id") or identity)
                    results.append(result)
                if len(results) >= 5 or not self.remaining() or self.request_count >= 12:
                    break
            if not results:
                fetched.update(status="not_found", candidate=None)
                results = [fetched]
            self._store(key, results)
            return results
        except (ValueError, KeyError, TypeError, AttributeError):
            fetched.update(status="error", error="malformed_response", candidate=None)
            return [fetched]
