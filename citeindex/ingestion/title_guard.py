"""Reject publishing an input filename as if it were the record's title.

Benchmark corpora name every source ``<sha256>.pdf``.  When page-level title
location fails, the pipeline falls back to the input filename and marks the
field ``provisional-filename``.  Persisting that value publishes a hash as the
bibliographic title, which no strict evaluation can accept.  This guard removes
such a title, records why it was dropped, and downgrades the record's citation
status rather than letting a filename masquerade as source evidence.
"""

from __future__ import annotations

import os
import re
from typing import Any
from urllib.parse import urlparse

_HEX_PDF_TITLE = re.compile(r"^[0-9a-f]{32,64}\.pdf$")


def _source_basename(source_path: str | None) -> str:
    """Return the filename component of *source_path*, tolerating URLs and junk."""
    if not isinstance(source_path, str) or not source_path.strip():
        return ""
    candidate = source_path.strip()
    parsed = urlparse(candidate)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        candidate = parsed.path
    # ``basename`` never raises for a URL path or a bare string, and an empty
    # path (e.g. a URL ending in "/") simply yields "".
    return os.path.basename(candidate)


def looks_like_filename(value: Any, source_path: str | None) -> bool:
    """True when *value* is the source filename (or a hash-named PDF) rather than a title."""
    if not isinstance(value, str):
        return False
    candidate = value.strip().casefold()
    if not candidate:
        return False
    if _HEX_PDF_TITLE.match(candidate):
        return True
    basename = _source_basename(source_path).casefold()
    if not basename:
        return False
    if candidate == basename:
        return True
    stem = os.path.splitext(basename)[0]
    return bool(stem) and candidate == stem


def _has_unsupported_citation_status(csl: dict[str, Any]) -> bool:
    """Whether the record claims citation completeness that a filename title cannot support.

    Every pipeline status formula (digital/scanned PDF, URL, media) requires a
    ``source-supported`` title before it can yield ``source-supported``.  Once
    that title is removed the claim is unreachable, so the status is recomputed
    to ``incomplete`` exactly as those formulas would.
    """
    return csl.get("_citation_status") == "source-supported"


def reject_filename_title(csl: dict[str, Any], source_path: str | None) -> bool:
    """Remove a filename-derived title from *csl* in place.

    Returns ``True`` when the title was rejected, ``False`` when the record was
    left untouched.  The operation is idempotent: once the title is gone the
    second call finds nothing to reject.
    """
    if not looks_like_filename(csl.get("title"), source_path):
        return False

    recompute_status = _has_unsupported_citation_status(csl)

    csl.pop("title", None)
    csl.pop("subtitle", None)

    field_status = csl.get("_field_status")
    if not isinstance(field_status, dict):
        field_status = {}
    field_status["title"] = "missing"
    csl["_field_status"] = field_status

    field_drops = csl.get("_field_drops")
    if not isinstance(field_drops, dict):
        field_drops = {}
    field_drops["title"] = {"reason": "filename_fallback"}
    csl["_field_drops"] = field_drops

    if recompute_status:
        csl["_citation_status"] = "incomplete"
    return True
