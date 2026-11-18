"""Closed zone taxonomy for the citation window.

Every block handed to the extractor gets exactly one label from :data:`ZONES`.
The pipeline uses it for three things:

* :func:`rank_zones` orders the prompt so identity-bearing zones
  (title/statement of responsibility/copyright/CIP/imprint) are read first and
  page furniture (running heads, tables of contents) last;
* :func:`has_zone_candidate` and :func:`evidence_zone` let the extractor refuse
  a ``running_head`` or ``contents`` block as the source of a ``title`` or
  ``publisher`` while any other zone could supply that field, recording
  ``fallback_zone`` when nothing else could;
* the seed rules read the same labels, so a deterministic candidate obeys the
  same arbitration as a model proposal.

The labeller is rule-based and deterministic: no model call, no network, no
randomness. Every input block is copied, so ``role`` and the caller's dicts are
never mutated.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

ZONES: tuple[str, ...] = (
    "cover",
    "title",
    "statement_of_responsibility",
    "copyright",
    "cip",
    "imprint",
    "contents",
    "running_head",
    "body",
    "other",
)

# Zones that may never supply a title or publisher while another zone can.
DEGENERATE_ZONES: tuple[str, ...] = ("running_head", "contents")

# Prompt ordering only: identity zones first, then the cover, then the rest;
# running heads and contents are read last because they repeat or list other
# people's titles.
_ZONE_ORDER: dict[str, int] = {
    "title": 0,
    "statement_of_responsibility": 1,
    "copyright": 2,
    "cip": 3,
    "imprint": 4,
    "cover": 5,
    "body": 6,
    "other": 7,
    "running_head": 8,
    "contents": 9,
}

# Which zones could supply a given field. Used by the degenerate-zone guard:
# a running-head title loses only when one of these zones exists.
_FIELD_ZONES: dict[str, tuple[str, ...]] = {
    "title": ("title", "cover", "statement_of_responsibility", "cip"),
    "publisher": ("imprint", "copyright", "cip"),
}

# --- Signals ---------------------------------------------------------------

_CIP_RE = re.compile(
    r"图书在版编目|在版编目|\bCIP\b|Library\s+of\s+Congress",
    re.IGNORECASE,
)
_COPYRIGHT_RE = re.compile(r"©|\(c\)\s*\d|版权所有|copyright|ISBN", re.IGNORECASE)
_CONTENTS_RE = re.compile(
    r"^(?:目\s*录|contents|table\s+of\s+contents|inhalt|sommaire)$",
    re.IGNORECASE,
)
_DOT_LEADER_RE = re.compile(r"(?:\.{4,}|…{2,}|·{4,}|_{4,})\s*[\d一二三四五六七八九十]+")

_CJK_IMPRINT_LINE_RE = re.compile(
    r"^(?:(?P<place>[\u4e00-\u9fff]{2,10})[:：,，]\s*)?"
    r"(?P<pub>[\u4e00-\u9fff·]{2,14}"
    r"(?:出版社|出版集团|出版公司|书局|書局|印书馆|印書館|书店|書店))$"
)
# Latin imprint line: a publisher suffix closing the name, optionally followed
# by a place ('CISTERCIAN PUBLICATIONS KALAMAZOO, MICHIGAN'). Matched
# case-insensitively for the suffix while requiring a capitalised name.
_LATIN_IMPRINT_LINE_RE = re.compile(
    r"^(?P<pub>(?-i:[A-Z])[\w&'’.\- ]{1,60}?\s"
    r"(?i:press|publications|publishers|publishing|verlag|books)\.?)"
    r"(?:\s*[,;]\s*|\s+)?"
    r"(?P<place>[A-Za-zÀ-ÿ][\w&'’.\-]*(?:[ ,]+[A-Za-zÀ-ÿ][\w&'’.\-]*){0,4})?\.?$"
)
_YEAR_RE = re.compile(r"\b(?:1[5-9]|20)\d{2}\b")

# A body sentence carries terminal or clause punctuation; a title-page line
# does not. CJK titles rarely end in 。 or ；.
_BODY_PUNCTUATION_RE = re.compile(r"[。；，]|\.\s*$|,\s*$|;\s*$")


def _page_number(block: dict[str, Any]) -> int | None:
    """1-based physical page for a block; printed folios are never trusted."""
    index = block.get("physical_page_index")
    if isinstance(index, int):
        return index + 1
    page = block.get("page")
    return page if isinstance(page, int) else None


def _text(block: dict[str, Any]) -> str:
    value = block.get("text")
    return value if isinstance(value, str) else ""


def _normalized(text: str) -> str:
    """Position- and punctuation-free key, the same shape as frontmatter's."""
    if len(text.strip()) >= 60:
        return ""
    return re.sub(r"[\W_]+", "", text).casefold()


def _is_display_line(text: str) -> bool:
    """A short, mostly upper-case line: a cover/half-title display line."""
    letters = [char for char in text if char.isalpha()]
    if not letters or len(text.split()) > 12:
        return False
    if sum(char.isupper() for char in letters) / len(letters) < 0.6:
        return False
    return not _BODY_PUNCTUATION_RE.search(text)


def _is_imprint_line(text: str) -> bool:
    """True for a whole-line publisher imprint ('北京：商务印书馆', 'Oxford University Press')."""
    line = text.strip()
    if not line or "\n" in line or len(line) > 100:
        return False
    if _CJK_IMPRINT_LINE_RE.match(line):
        return True
    match = _LATIN_IMPRINT_LINE_RE.match(line)
    if not match:
        return False
    publisher = (match.group("pub") or "").strip(" .,")
    place = (match.group("place") or "").strip(" .,")
    # A digit means the year landed in the name group ('Inc. 2004'); a 'by'
    # line is an attribution ('Typography by ... at Humble Hills Press').
    if re.search(r"\d", publisher) or re.search(r"\d", place):
        return False
    if re.search(r"\bby\b", line, re.IGNORECASE):
        return False
    if not re.search(r"[A-Z]", publisher):
        return False
    return len(publisher.split()) <= 6 or bool(place or _YEAR_RE.search(line))


def _is_contents(block_lines: list[str]) -> bool:
    """A contents heading, or a page of dot leaders with page numbers."""
    if any(_CONTENTS_RE.match(line) for line in block_lines):
        return True
    return sum(1 for line in block_lines if _DOT_LEADER_RE.search(line)) >= 2


def _is_title_page(texts: list[str]) -> bool:
    """Short lines, no running prose: a title page rather than body text."""
    if not texts:
        return False
    if any(len(text) > 160 for text in texts):
        return False
    if any(_BODY_PUNCTUATION_RE.search(text) for text in texts):
        return False
    if any(_CIP_RE.search(text) or _COPYRIGHT_RE.search(text) or _is_contents([text])
           for text in texts):
        return False
    words = sum(len(text.split()) for text in texts)
    chars = sum(len(text) for text in texts)
    return words <= 40 and chars <= 260


def _repeated_keys(blocks: list[dict[str, Any]]) -> set[str]:
    """Normalized short lines printed on three or more distinct pages."""
    pages_by_key: dict[str, set[int]] = {}
    for block in blocks:
        page = _page_number(block)
        if page is None:
            continue
        for line in _text(block).splitlines():
            key = _normalized(line)
            if key:
                pages_by_key.setdefault(key, set()).add(page)
    return {key for key, pages in pages_by_key.items() if len(pages) >= 3}


def label_zones(blocks: list[dict[str, Any]], *, page_count: int | None = None) -> list[dict[str, Any]]:
    """Return copies of *blocks* with ``zone`` set to one member of :data:`ZONES`.

    The signals are the printed shapes of a book's front matter: CIP and
    copyright markers, imprint lines, contents headings and dot leaders,
    display lines repeated on three or more pages (running heads), and
    title-page position (a short-line page within the first three pages).
    Everything else falls back to ``body``.
    """
    labelled = [{**block, "zone": "body",
                 "text": block.get("text") if isinstance(block.get("text"), str) else ""}
                for block in blocks]
    if not labelled:
        return labelled

    by_id = {block["id"]: block for block in labelled}
    pages: dict[int, list[dict[str, Any]]] = {}
    for block in labelled:
        page = _page_number(block)
        if page is not None:
            pages.setdefault(page, []).append(block)

    # 1. Explicit front-matter signals, strongest first.
    for block in labelled:
        text = _text(block)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if _CIP_RE.search(text):
            block["zone"] = "cip"
        elif _COPYRIGHT_RE.search(text):
            block["zone"] = "copyright"
        elif _is_imprint_line(text):
            block["zone"] = "imprint"
        elif _is_contents(lines):
            block["zone"] = "contents"

    # 1b. Page-level signal: the ISBN/catalog lines beside a CIP block belong
    # to the CIP page, and a page of dot leaders is a contents page even when
    # each entry is its own block.
    for page_blocks in pages.values():
        zones_on_page = {block["zone"] for block in page_blocks}
        leaders = [block for block in page_blocks
                   if any(_DOT_LEADER_RE.search(line) for line in _text(block).splitlines())]
        if "cip" in zones_on_page:
            fill = "cip"
        elif "contents" in zones_on_page or len(leaders) >= 2:
            fill = "contents"
        else:
            continue
        fillable = {"body", "other"} | ({"copyright"} if fill == "cip" else set())
        for block in page_blocks:
            if block["zone"] in fillable:
                block["zone"] = fill

    # 2. Title page: the first short-line page in the first three pages that
    # carries more than a display line; a lone display page ahead of it is the
    # cover. An imprint line printed at the foot of the page is allowed.
    limit = min(3, page_count) if page_count else 3
    candidate_pages = [
        page for page in sorted(pages)
        if 1 <= page <= limit
        and all(by_id[block["id"]]["zone"] in {"body", "imprint"} for block in pages[page])
        and _is_title_page([_text(block) for block in pages[page]])
    ]
    title_page = next(
        (page for page in candidate_pages if len(pages[page]) >= 2),
        candidate_pages[0] if candidate_pages else None,
    )
    if title_page is not None:
        body_positions = [position for position, block in enumerate(pages[title_page])
                          if block["zone"] == "body"]
        for position in body_positions[1:]:
            pages[title_page][position]["zone"] = "statement_of_responsibility"
        if body_positions:
            pages[title_page][body_positions[0]]["zone"] = "title"
            if title_page > 1:
                for page in sorted(pages):
                    if page >= title_page:
                        break
                    if page < 1 or len(pages[page]) != 1:
                        continue
                    block = pages[page][0]
                    if block["zone"] == "body" and _is_display_line(_text(block)):
                        block["zone"] = "cover"

    # 3. Running heads: a short line repeated on three or more pages. The
    # title page's own leading line is exempt, so a real title that recurs as
    # a running head keeps its title label.
    repeated = _repeated_keys(labelled)
    if repeated:
        for block in labelled:
            if block["zone"] in {"title", "statement_of_responsibility"}:
                continue
            key = _normalized(_text(block))
            if key and key in repeated and block["zone"] in {"body", "other"}:
                block["zone"] = "running_head"

    return labelled


def rank_zones(blocks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Order *blocks* for the prompt: identity zones first, furniture last.

    The sort is stable, so the caller's score order survives inside each zone.
    """
    return sorted(blocks, key=lambda block: _ZONE_ORDER.get(block.get("zone"), 7))


def is_degenerate_zone(zone: str | None) -> bool:
    """True for zones that repeat or list other works' identity."""
    return zone in DEGENERATE_ZONES


def has_zone_candidate(field: str, zone_by_id: Mapping[str, str]) -> bool:
    """True when any block sits in a zone that could supply *field*.

    ``title`` and ``publisher`` are the guarded fields; an unknown field falls
    back to every non-degenerate zone, matching the permissive default.
    """
    allowed = _FIELD_ZONES.get(field)
    if allowed is None:
        return any(not is_degenerate_zone(zone) for zone in zone_by_id.values())
    return any(zone in allowed for zone in zone_by_id.values())


def evidence_zone(evidence: Any, zone_by_id: Mapping[str, str]) -> str | None:
    """Degenerate zone of the supporting block(s), else ``None``.

    A multi-block quote counts as degenerate only when every supporting block
    sits in the same degenerate class.
    """
    if not isinstance(evidence, dict):
        return None
    spans = evidence.get("spans") if "spans" in evidence else [evidence]
    if not isinstance(spans, list):
        return None
    zones = [zone_by_id.get(span.get("block_id")) for span in spans if isinstance(span, dict)]
    zones = [zone for zone in zones if zone]
    if not zones or not all(is_degenerate_zone(zone) for zone in zones):
        return None
    return zones[0]
