"""Deterministic document-type judge that runs before extraction.

The judge answers one question — is this work a book, a paper or a thesis —
from printed markers and page count alone. No model call, no network, no
randomness: the same blocks always produce the same verdict, so a measurement
can attribute a change to the stage that made it.

Stage 2 contract: :func:`judge_document_type` returns its verdict for the
record, and nothing reads it back yet. ``doc_type``, prompts, deterministic
seeds and the page locator keep their existing behaviour until a later stage
wires the verdict into policy.

Signals are evaluated in priority order:

1. an explicit ``override``;
2. thesis markers;
3. book markers (including a publisher-suffix line);
4. paper markers;
5. page count (``>= 60`` without a DOI; ``<= 12`` with a DOI or abstract);
6. the ``book`` default.

Every evidence entry quotes the matched stretch of the block text verbatim, so
an auditor can locate the claim in the source. Non-textual signals (override,
page count, default) carry an empty quote because no block prints them.
"""

from __future__ import annotations

import re
from typing import Any

DOCUMENT_TYPES: tuple[str, ...] = ("book", "paper", "thesis")

# Documented confidence ladder (see the stage plan).
CONFIDENCE_OVERRIDE = 1.0
CONFIDENCE_MULTI_SIGNAL = 0.9
CONFIDENCE_SINGLE_SIGNAL = 0.7
CONFIDENCE_PAGE_COUNT = 0.6
CONFIDENCE_DEFAULT = 0.3

# Two or more distinct signals for the winner raise confidence to 0.9.
_MULTI_SIGNAL_MINIMUM = 2

# --- Markers ---------------------------------------------------------------
# Each entry is (signal name, pattern). The signal name is what lands in the
# evidence entry; a marker that matches twice in the same document is still one
# signal, because a repeated phrase is not independent corroboration.

_THESIS_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        ("thesis", r"\bthesis\b"),
        ("dissertation", r"\bdissertation\b"),
        ("thesis_submitted", r"\ba\s+thesis\s+submitted\b"),
        ("partial_fulfillment", r"\bin\s+partial\s+fulfillment\b"),
        ("degree_of", r"\bdegree\s+of\b"),
        ("cn_degree_thesis", r"学位论文"),
        ("cn_master", r"硕士"),
        ("cn_doctor", r"博士"),
    )
)

_BOOK_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        ("cip_cataloguing", r"图书在版编目"),
        ("cip", r"\bcip\b"),
        ("isbn", r"\bisbn\b"),
        ("copyright_notice", r"copyright\s*©"),
        ("all_rights_reserved", r"all\s+rights\s+reserved"),
        ("published_by", r"published\s+by"),
        ("cn_publisher", r"出版社"),
        ("cn_publishing", r"出版发行"),
    )
)

_PAPER_MARKERS: tuple[tuple[str, re.Pattern[str]], ...] = tuple(
    (name, re.compile(pattern, re.IGNORECASE))
    for name, pattern in (
        ("doi", r"\bdoi\b"),
        ("abstract", r"\babstract\b"),
        ("keywords", r"\bkeywords?\b"),
        ("received", r"received\s*[:：]"),
        ("accepted", r"accepted\s*[:：]"),
        ("corresponding_author", r"corresponding\s+author"),
        ("volume", r"\bvolume\b"),
        ("issue", r"\bissue\b"),
        ("pages", r"\bpp\."),
    )
)

# A publisher-suffix line: the name opens with a capital and closes on the
# publisher word, optionally followed by a full stop. Keeping it line-shaped
# stops a body sentence that merely ends in 'books' from seeding a book.
_PUBLISHER_SUFFIX_LINE_RE = re.compile(
    r"^[A-Z][\w&'’.,\- ]{0,59}?(?i:press|publications|verlag|books)\.?$"
)

# A bare DOI string (10.1234/abcd) is length-bearing paper evidence even when
# the word 'DOI' is never printed beside it; the page-count rule reads this,
# the marker scan above stays literal.
_BARE_DOI_RE = re.compile(r"\b10\.\d{4,9}/\S+")


def _block_items(blocks: Any) -> list[tuple[str, str]]:
    """(block_id, text) pairs for every non-empty block, in source order."""

    def identity(block: Any, index: int) -> str:
        if isinstance(block, dict) and block.get("id") is not None:
            return str(block["id"])
        return f"block_{index}"

    def text_of(block: Any) -> str:
        if isinstance(block, dict):
            value = block.get("text")
            return value if isinstance(value, str) else ""
        return block if isinstance(block, str) else ""

    items: list[tuple[str, str]] = []
    for index, block in enumerate(blocks or []):
        text = text_of(block)
        if text.strip():
            items.append((identity(block, index), text))
    return items


def _publisher_suffix_line(text: str) -> str | None:
    """The first publisher-suffix line in *text*, verbatim, else None."""
    for line in text.splitlines():
        candidate = line.strip()
        if candidate and _PUBLISHER_SUFFIX_LINE_RE.match(candidate):
            return candidate
    return None


def _collect_signals(
    items: list[tuple[str, str]],
) -> dict[str, list[dict[str, str]]]:
    """One evidence entry per distinct signal, in block order."""
    found: dict[str, list[dict[str, str]]] = {name: [] for name in DOCUMENT_TYPES}
    seen: dict[str, set[str]] = {name: set() for name in DOCUMENT_TYPES}
    families = (
        ("thesis", _THESIS_MARKERS),
        ("book", _BOOK_MARKERS),
        ("paper", _PAPER_MARKERS),
    )
    for block_id, text in items:
        for document_type, markers in families:
            for signal, pattern in markers:
                if signal in seen[document_type]:
                    continue
                match = pattern.search(text)
                if match:
                    seen[document_type].add(signal)
                    found[document_type].append(
                        {"block_id": block_id, "quote": match.group(0), "signal": signal})
        if "publisher_suffix" not in seen["book"]:
            line = _publisher_suffix_line(text)
            if line:
                seen["book"].add("publisher_suffix")
                found["book"].append(
                    {"block_id": block_id, "quote": line, "signal": "publisher_suffix"})
    return found


def _normalise_override(override: Any) -> str | None:
    """One of the three documented types, or None when the value is not one."""
    if not isinstance(override, str):
        return None
    value = override.strip().casefold()
    return value if value in DOCUMENT_TYPES else None


def _resolve_book_paper_tie(
    book_evidence: list[dict[str, str]],
    paper_evidence: list[dict[str, str]],
) -> str:
    """Book wins a book/paper contest only while it holds a book signal."""
    return "book" if book_evidence else "paper"


def _verdict(document_type: str, evidence: list[dict[str, str]]) -> dict[str, Any]:
    """Verdict scored on the documented signal ladder."""
    confidence = (CONFIDENCE_MULTI_SIGNAL if len(evidence) >= _MULTI_SIGNAL_MINIMUM
                  else CONFIDENCE_SINGLE_SIGNAL)
    return {"type": document_type, "confidence": confidence, "evidence": list(evidence)}


def judge_document_type(
    blocks: Any,
    *,
    page_count: int | None = None,
    override: str | None = None,
) -> dict[str, Any]:
    """Judge whether the supplied blocks describe a book, paper or thesis.

    Parameters
    ----------
    blocks
        Source blocks (dicts with ``id``/``text`` or bare strings). Empty or
        unusable input never raises: the ``book`` default is returned.
    page_count
        Physical page count when the caller knows it; enables the length rule.
    override
        An explicit ``book``/``paper``/``thesis`` choice. Unknown values are
        ignored so a misconfigured override cannot invent a type.

    Returns
    -------
    dict
        ``{"type", "confidence", "evidence"}`` where each evidence entry is
        ``{"block_id", "quote", "signal"}`` and ``quote`` is a verbatim
        stretch of that block's text (empty for override/page count/default).
    """
    items = _block_items(blocks)
    override_type = _normalise_override(override)
    if override_type is not None:
        return {
            "type": override_type,
            "confidence": CONFIDENCE_OVERRIDE,
            "evidence": [{"block_id": "", "quote": str(override).strip(), "signal": "override"}],
        }

    found = _collect_signals(items)
    thesis_evidence = found["thesis"]
    book_evidence = found["book"]
    paper_evidence = found["paper"]

    # Priority: a thesis marker outranks both book and paper, then book
    # outranks paper. A book/paper contest is only a tie when both sides hold
    # signals, and it goes to book exactly then.
    if thesis_evidence:
        return _verdict("thesis", thesis_evidence)
    if book_evidence or paper_evidence:
        winner = (_resolve_book_paper_tie(book_evidence, paper_evidence)
                  if book_evidence and paper_evidence
                  else "book" if book_evidence else "paper")
        return _verdict(winner, found[winner])

    # Page count is the last evidence before the default. A marker rung wins
    # first, so these branches serve documents whose printed markers are
    # absent — a long body with no identity page, or a short offprint whose
    # only identifier is a bare DOI string. The 'with a DOI or abstract' half
    # of the rule is normally spent on the paper rung above; only the bare DOI
    # form (no printed 'DOI' word) can still reach this point.
    has_doi = any(_BARE_DOI_RE.search(text) for _, text in items)
    if isinstance(page_count, int) and page_count > 0:
        if page_count >= 60 and not has_doi:
            return {
                "type": "book",
                "confidence": CONFIDENCE_PAGE_COUNT,
                "evidence": [{"block_id": "", "quote": "", "signal": "page_count"}],
            }
        if page_count <= 12 and has_doi:
            return {
                "type": "paper",
                "confidence": CONFIDENCE_PAGE_COUNT,
                "evidence": [{"block_id": "", "quote": "", "signal": "page_count"}],
            }

    return {
        "type": "book",
        "confidence": CONFIDENCE_DEFAULT,
        "evidence": [{"block_id": "", "quote": "", "signal": "default"}],
    }
