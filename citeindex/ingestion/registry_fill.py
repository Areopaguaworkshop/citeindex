"""Identifier-first registry fill for metadata the source text never printed.

A scanned book can lose its imprint line to OCR while still showing an ISBN on
the title-page verso.  When the front matter prints an ISBN or DOI, the matching
authority record is the only route to a correct publisher, place, or date.

This module is deliberately network-free: the HTTP client is injected and must
expose ``lookup_isbn(isbn) -> dict | None`` and ``lookup_doi(doi) -> dict | None``.
Registry values only ever fill fields the source left empty; a printed value is
never overwritten.  Disagreements with a source-supported field are recorded for
audit under ``_field_conflicts``, and every fill carries ``_field_provenance``.
"""

from __future__ import annotations

import copy
import logging
import re
from collections.abc import Iterable, Mapping
from typing import Any

logger = logging.getLogger(__name__)

# Fields the authority may supply.  Everything else stays as printed.
FILLABLE_FIELDS: tuple[str, ...] = (
    "title",
    "author",
    "editor",
    "translator",
    "publisher",
    "publisher-place",
    "issued",
    "ISBN",
)

# Doc types where the ISBN (OpenLibrary edition) is the primary identifier.
_BOOK_TYPES = frozenset({"book", "chapter", "entry-encyclopedia", "thesis", "report", "monograph", "edited-book"})

_ISBN_RE = re.compile(
    r"(?<![0-9Xx])(?<![0-9Xx][-\s])"
    r"(?:97[89](?:[-\s]?[0-9]){10}|[0-9](?:[-\s]?[0-9]){8}[-\s]?[0-9Xx])"
    r"(?![0-9Xx])",
    re.IGNORECASE,
)
_ISBN_SEPARATORS_RE = re.compile(r"[-\s\u2010-\u2015]+")
_DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
_DOI_TRAILING_PUNCTUATION = ".,;:!?"
_YEAR_RE = re.compile(r"\d{4}")
_TEXT_KEYS = ("text", "content", "markdown", "raw_text")


# ----------------------------------------------------------------------
# Identifier discovery
# ----------------------------------------------------------------------
def isbn10_valid(value: str) -> bool:
    """Return True when *value* is a valid ISBN-10 (check digit may be X)."""
    if len(value) != 10 or not value[:9].isdigit():
        return False
    check = value[9].upper()
    if not (check.isdigit() or check == "X"):
        return False
    total = sum((10 - index) * (10 if char.upper() == "X" else int(char)) for index, char in enumerate(value))
    return total % 11 == 0


def isbn13_valid(value: str) -> bool:
    """Return True when *value* is a valid ISBN-13."""
    if len(value) != 13 or not value.isdigit():
        return False
    total = sum(int(char) * (1 if index % 2 == 0 else 3) for index, char in enumerate(value))
    return total % 10 == 0


def isbn13_from_isbn10(value: str) -> str:
    """Convert a valid ISBN-10 to its ISBN-13 equivalent."""
    core = "978" + value[:9]
    total = sum(int(char) * (1 if index % 2 == 0 else 3) for index, char in enumerate(core))
    return core + str((10 - total % 10) % 10)


def canonical_isbn(value: str) -> str | None:
    """Return a compact, valid-looking ISBN; ISBN-10 is upgraded to ISBN-13."""
    compact = _ISBN_SEPARATORS_RE.sub("", value)
    if isbn10_valid(compact):
        return isbn13_from_isbn10(compact)
    if isbn13_valid(compact):
        return compact
    return None


def _block_texts(blocks: Iterable[Any] | Any) -> Iterable[str]:
    items = blocks if isinstance(blocks, (list, tuple)) else [blocks]
    for item in items:
        if isinstance(item, str):
            yield item
            continue
        if not isinstance(item, Mapping):
            continue
        for key in _TEXT_KEYS:
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                yield value
                break


def _clean_doi(value: str) -> str:
    doi = value.rstrip(_DOI_TRAILING_PUNCTUATION)
    while doi.endswith(")") and doi.count(")") > doi.count("("):
        doi = doi[:-1]
    while doi.endswith("]") and doi.count("]") > doi.count("["):
        doi = doi[:-1]
    return doi.lower()


def find_identifiers(blocks: Any) -> dict[str, str]:
    """Scan block text for the first valid ISBN and DOI.

    Only the first valid hit of each kind is returned, so a book's ISBN is never
    replaced by a later ISBN from a bibliography page.
    """
    found: dict[str, str] = {}
    for text in _block_texts(blocks):
        if "isbn" not in found:
            for match in _ISBN_RE.finditer(text):
                compact = _ISBN_SEPARATORS_RE.sub("", match.group(0))
                if isbn13_valid(compact) or isbn10_valid(compact):
                    found["isbn"] = compact
                    break
        if "doi" not in found:
            match = _DOI_RE.search(text)
            if match:
                doi = _clean_doi(match.group(0))
                if "/" in doi and len(doi) > 7:
                    found["doi"] = doi
        if len(found) == 2:
            break
    return found


# ----------------------------------------------------------------------
# Fill rules
# ----------------------------------------------------------------------
def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return True


def _status(record: Mapping[str, Any], field: str) -> Any:
    states = record.get("_field_status")
    return states.get(field) if isinstance(states, Mapping) else None


def _is_source_supported(record: Mapping[str, Any], field: str) -> bool:
    return _status(record, field) == "source-supported"


def _is_fillable(record: Mapping[str, Any], field: str) -> bool:
    if _is_source_supported(record, field):
        return False
    if _status(record, field) == "missing":
        return True
    return not _has_value(record.get(field))


def _issued_year(value: Any) -> int | None:
    if isinstance(value, Mapping):
        parts = value.get("date-parts")
        if isinstance(parts, list) and parts and isinstance(parts[0], list) and parts[0]:
            first = parts[0][0]
            if isinstance(first, int) and not isinstance(first, bool):
                return first
            if isinstance(first, str) and first.strip().isdigit():
                return int(first.strip())
        return None
    if isinstance(value, str):
        match = _YEAR_RE.search(value)
        if match:
            return int(match.group(0))
    return None


def _name_keys(value: Any) -> set[str]:
    keys: set[str] = set()
    if not isinstance(value, list):
        return keys
    for person in value:
        if not isinstance(person, Mapping):
            continue
        parts = [person.get("family"), person.get("given"), person.get("literal")]
        name = " ".join(str(part).strip() for part in parts if isinstance(part, str) and part.strip())
        if name:
            keys.add(_text_key(name))
    return keys


def _text_key(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip().strip(".,;:").casefold()


def _disagrees(field: str, printed: Any, registry: Any) -> bool:
    if not _has_value(printed) or not _has_value(registry):
        return False
    if field == "issued":
        printed_year, registry_year = _issued_year(printed), _issued_year(registry)
        if printed_year is not None and registry_year is not None:
            return printed_year != registry_year
        return _text_key(str(printed)) != _text_key(str(registry))
    if field in {"author", "editor", "translator"}:
        printed_keys, registry_keys = _name_keys(printed), _name_keys(registry)
        if printed_keys and registry_keys:
            return printed_keys != registry_keys
        return _text_key(str(printed)) != _text_key(str(registry))
    if field == "ISBN":
        return canonical_isbn(str(printed)) != canonical_isbn(str(registry))
    return _text_key(printed) != _text_key(registry)


def _lookup_order(identifiers: Mapping[str, Any], doc_type: Any) -> list[tuple[str, str, str]]:
    """Order lookups by document kind; each identifier still gets one attempt."""
    pairs: list[tuple[str, str, str]] = []
    isbn = identifiers.get("isbn")
    doi = identifiers.get("doi")
    if isinstance(isbn, str) and isbn.strip():
        pairs.append(("openlibrary", isbn.strip(), "lookup_isbn"))
    if isinstance(doi, str) and doi.strip():
        pairs.append(("crossref", doi.strip().lower(), "lookup_doi"))
    if doc_type not in _BOOK_TYPES:
        pairs.reverse()
    return pairs


def fill_from_registry(
    csl: dict[str, Any],
    identifiers: Mapping[str, Any] | None,
    *,
    client: Any,
    doc_type: Any = None,
) -> tuple[dict[str, Any], dict[str, dict[str, str]]]:
    """Fill source-empty CSL fields from the authority record for an identifier.

    Returns the (possibly unchanged) CSL record and a provenance map of
    ``field -> {"source": ..., "identifier": ...}``.  The input record is never
    mutated, and a record with nothing to fill is returned as-is.
    """
    if client is None or not identifiers:
        return csl, {}

    pending: dict[str, tuple[str | None, str, Any]] = {}
    provenance: dict[str, dict[str, str]] = {}
    conflicts: dict[str, dict[str, Any]] = {}

    for source, identifier, method_name in _lookup_order(identifiers, doc_type):
        lookup = getattr(client, method_name, None)
        if not callable(lookup):
            continue
        try:
            candidate = lookup(identifier)
        except Exception:  # a registry failure must never break ingestion
            logger.warning("Registry fill lookup failed for %s %s", source, identifier, exc_info=True)
            continue
        if not isinstance(candidate, Mapping):
            continue
        for field in FILLABLE_FIELDS:
            if field in pending or field in provenance:
                continue
            value = candidate.get(field)
            if not _has_value(value):
                continue
            if _is_source_supported(csl, field):
                if field not in conflicts and _disagrees(field, csl.get(field), value):
                    conflicts[field] = {
                        "printed": copy.deepcopy(csl.get(field)),
                        "registry": copy.deepcopy(value),
                        "chosen": "printed",
                    }
                continue
            if not _is_fillable(csl, field):
                continue
            pending[field] = (source, identifier, copy.deepcopy(value))

    if not pending and not conflicts:
        return csl, {}

    record = copy.deepcopy(dict(csl))
    for field, (source, identifier, value) in pending.items():
        record[field] = value
        provenance[field] = {"source": source, "identifier": identifier}
    if provenance:
        existing = record.get("_field_provenance")
        record["_field_provenance"] = {**(existing if isinstance(existing, Mapping) else {}), **provenance}
    if conflicts:
        existing_conflicts = record.get("_field_conflicts")
        record["_field_conflicts"] = {
            **(existing_conflicts if isinstance(existing_conflicts, Mapping) else {}),
            **conflicts,
        }
    return record, provenance
