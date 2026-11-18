"""Per-field extraction confidence and the review queue (plan Phase 6).

A dropped field and a confidently wrong field used to look identical: the CSL
record said nothing about either. This module scores every accepted field and
builds one report, ``_review_queue``, that ranks the fields a human should look
at first. Nothing is repaired or re-extracted from the score; the queue is
audit data that rides inside the CSL document under an ``_`` key.

The ladder is deliberately coarse (Azure and Adobe publish per-field scores;
this is the cheap printed-evidence version):

===========  ============================================================
0.95         exact substring quote in a zone that owns the field
             (title/cover/statement-of-responsibility for ``title``;
             imprint/copyright/CIP for ``publisher``)
0.90         quote matched only after normalisation (NFKC, dashes, curly
             quotes, collapsed spaces); deterministic seeds also start here
             when their host block sits in an owning zone or carries a
             strong printed marker (CIP, ISBN, copyright, imprint)
0.85         quote assembled from two adjacent same-page blocks
0.80         exact quote in a non-owning zone
0.60         accepted only as a degenerate-zone fallback
             (``running_head`` / ``contents``)
0.50         deterministic seed whose host block lacks a strong marker
===========  ============================================================

A field that was evaluated and dropped gets no confidence entry at all: the
``_field_drops`` record from Phase 2 already says why it is absent, and the
queue lists it with confidence ``0.0``.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from typing import Any

from .zones import is_degenerate_zone

DEFAULT_REVIEW_THRESHOLD = 0.80

#: Exact substring quote in a zone that owns the field.
CONFIDENCE_OWNING_EXACT = 0.95
#: Quote matched only after normalisation, and the floor for a strong seed.
CONFIDENCE_NORMALISED = 0.90
#: Quote assembled from two adjacent same-page blocks.
CONFIDENCE_JOINED = 0.85
#: Exact quote in a zone that does not own the field.
CONFIDENCE_NON_OWNING_EXACT = 0.80
#: Accepted only as a ``running_head`` / ``contents`` fallback.
CONFIDENCE_DEGENERATE_FALLBACK = 0.60
#: Deterministic seed whose host block carries no strong marker.
CONFIDENCE_WEAK_SEED = 0.50
#: Deterministic seeds start here (same value as the normalised rung).
CONFIDENCE_SEED = CONFIDENCE_NORMALISED

MATCH_EXACT = "exact"
MATCH_NORMALISED = "normalised"
MATCH_JOINED = "joined"
MATCH_DEGENERATE = "degenerate_zone"
MATCH_SEED = "seed"
MATCH_WEAK_SEED = "weak_marker"

# Tokens callers may have used for the same rung.
_REASON_ALIASES = {
    "degenerate": MATCH_DEGENERATE,
    "weak_seed": MATCH_WEAK_SEED,
    "assembled": MATCH_JOINED,
    "normalization": MATCH_NORMALISED,
}

#: Zones that own a field. Fields absent from the map accept any zone that is
#: not degenerate (an unlabelled block also counts as owning).
OWNING_ZONES: dict[str, frozenset[str]] = {
    "title": frozenset({"title", "cover", "statement_of_responsibility"}),
    "publisher": frozenset({"imprint", "copyright", "cip"}),
}

# A host block with one of these printed markers can carry a seed even when
# the zone labeller did not put it in an owning zone.
_STRONG_SEED_MARKER_RE = re.compile(
    r"图书在版编目|在版编目|\bCIP\b|Library\s+of\s+Congress|ISBN|©|\(c\)\s*\d"
    r"|copyright|版权所有|出版|发行"
    r"|\bpress\b|\bpublications?\b|\bpublishers?\b|\bpublishing\b|\bverlag\b"
    r"|\bbuchhandlung\b|书店|書店",
    re.IGNORECASE,
)


def validate_review_threshold(value: Any) -> float:
    """Return *value* as a float in ``[0, 1]`` or raise a clear ``ValueError``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(
            f"review_threshold must be a real number between 0 and 1, got {value!r}")
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError(
            f"review_threshold must be a real number between 0 and 1, got {value!r}")
    return number


def review_threshold(config: Any = None) -> float:
    """Validated ``config.review_threshold``, defaulting to 0.80.

    The attribute is read with ``getattr`` so a config object that predates
    Phase 6 (or a stub in a test) still yields the documented default.
    """
    return validate_review_threshold(
        getattr(config, "review_threshold", DEFAULT_REVIEW_THRESHOLD))


def _owns_zone(field: str | None, zone: str | None) -> bool:
    """Whether *zone* may own *field* without costing a confidence rung."""
    owning = OWNING_ZONES.get(field or "")
    if owning is None:
        # Only title and publisher restrict their zones; any other field is
        # owned by every zone that is not page furniture. An unlabelled block
        # has shown no signal against it either.
        return not is_degenerate_zone(zone)
    return zone in owning


def field_confidence(
    reason: str,
    evidence: Any,
    zone: str | None,
    *,
    field: str | None = None,
) -> float:
    """Score one accepted field from its match *reason*, *evidence* and *zone*.

    Parameters
    ----------
    reason:
        Ladder token: ``"exact"``, ``"normalised"``, ``"joined"``,
        ``"degenerate_zone"``, ``"seed"`` or ``"weak_marker"``.
    evidence:
        The resolved span record that supports the value. A confidence is never
        assigned without evidence, so ``None`` is a programming error.
    zone:
        The :mod:`zones` label of the block that supplied the quote.
    field:
        Keyword-only; selects the owning-zone set. Without it every
        non-degenerate zone counts as owning.

    Raises ``ValueError`` for an unknown reason or missing evidence.
    """
    reason = _REASON_ALIASES.get(reason, reason)
    if not reason:
        raise ValueError("field confidence needs a match reason")
    if evidence is None:
        raise ValueError(f"cannot assign {reason!r} confidence without evidence")
    if reason == MATCH_DEGENERATE or is_degenerate_zone(zone):
        return CONFIDENCE_DEGENERATE_FALLBACK
    if reason == MATCH_WEAK_SEED:
        return CONFIDENCE_WEAK_SEED
    if reason == MATCH_SEED:
        return CONFIDENCE_SEED
    if reason == MATCH_EXACT:
        if _owns_zone(field, zone):
            return CONFIDENCE_OWNING_EXACT
        return CONFIDENCE_NON_OWNING_EXACT
    if reason == MATCH_NORMALISED:
        return CONFIDENCE_NORMALISED
    if reason == MATCH_JOINED:
        return CONFIDENCE_JOINED
    raise ValueError(f"unknown confidence reason {reason!r}")


def seed_confidence(
    field: str,
    text: str | None,
    zone: str | None,
) -> tuple[float, str]:
    """Score a deterministic seed: 0.90, or 0.50 when its block is unmarked.

    A seed starts at 0.90 when the host block sits in a zone that owns the
    field or prints a strong marker (CIP/ISBN/copyright/imprint). It drops to
    0.50 only for the weak-marker case; a degenerate-zone fallback still scores
    0.60 through :func:`field_confidence`.
    """
    strong = _owns_zone(field, zone) or bool(_STRONG_SEED_MARKER_RE.search(text or ""))
    reason = MATCH_SEED if strong else MATCH_WEAK_SEED
    confidence = field_confidence(reason, {"quote": text or ""}, zone, field=field)
    effective = MATCH_DEGENERATE if is_degenerate_zone(zone) else reason
    return confidence, effective


def spans_of(evidence: Any) -> list[Mapping[str, Any]]:
    """Resolved spans held by an evidence record, whether single or joined."""
    if isinstance(evidence, Mapping):
        spans: Any = evidence.get("spans") if "spans" in evidence else evidence
    else:
        spans = evidence
    if isinstance(spans, Mapping):
        return [spans]
    if isinstance(spans, Sequence) and not isinstance(spans, (str, bytes)):
        return [span for span in spans if isinstance(span, Mapping)]
    return []


def evidence_quote(evidence: Any) -> str | None:
    """The quote(s) of a resolved evidence record, joined for multi-span."""
    quotes = [span.get("quote") for span in spans_of(evidence)
              if isinstance(span.get("quote"), str)]
    return " ".join(quotes) if quotes else None


def confidence_for_evidence(
    field: str,
    evidence: Any,
    blocks: list[dict[str, Any]] | None,
    zone_by_id: Mapping[str, str] | None = None,
) -> tuple[float, str]:
    """Score an accepted model value from the evidence that passed the gate.

    ``reason`` is the ladder rung that decided the score (``"exact"``,
    ``"normalised"``, ``"joined"`` or ``"degenerate_zone"``). Raises
    ``ValueError`` when the record carries no span, because a value that passed
    the evidence gate always carries one.
    """
    spans = spans_of(evidence)
    if not spans:
        raise ValueError(f"accepted {field!r} has no resolved evidence span")
    by_id = {block.get("id"): block for block in blocks or [] if isinstance(block, Mapping)}
    first = spans[0]
    block_id = first.get("block_id")
    if len(spans) > 1:
        reason = MATCH_JOINED
    else:
        block = by_id.get(block_id) or {}
        quote = first.get("quote")
        text = block.get("text")
        reason = MATCH_EXACT if isinstance(quote, str) and isinstance(text, str) \
            and quote in text else MATCH_NORMALISED
    zone = (zone_by_id or {}).get(block_id)
    if zone is None:
        zone = by_id.get(block_id, {}).get("zone") if block_id in by_id else None
    confidence = field_confidence(reason, evidence, zone, field=field)
    return confidence, MATCH_DEGENERATE if is_degenerate_zone(zone) else reason


def build_review_queue(
    confidences: Mapping[str, float],
    drops: Mapping[str, Mapping[str, Any]] | None,
    threshold: float = DEFAULT_REVIEW_THRESHOLD,
    *,
    reasons: Mapping[str, str] | None = None,
    evidence: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Rank the fields a human should review, lowest confidence first.

    Membership is every accepted field scoring below *threshold* plus every
    field recorded in ``_field_drops`` (which has no accepted value, so it
    ranks at ``0.0``). Ties break on the field name, keeping the report stable.
    """
    threshold = validate_review_threshold(threshold)
    reasons = reasons or {}
    evidence = evidence or {}
    entries: dict[str, dict[str, Any]] = {}
    for field, confidence in confidences.items():
        if confidence >= threshold:
            continue
        entries[field] = {
            "field": field,
            "reason": reasons.get(field) or "low_confidence",
            "confidence": float(confidence),
            "quote": evidence_quote(evidence.get(field)),
        }
    for field, drop in (drops or {}).items():
        record = drop if isinstance(drop, Mapping) else {}
        entries[field] = {
            "field": field,
            "reason": record.get("reason") or "quote_mismatch",
            "confidence": 0.0,
            "quote": record.get("quote"),
        }
    return sorted(entries.values(), key=lambda entry: (entry["confidence"], entry["field"]))
