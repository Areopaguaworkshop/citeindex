"""Plan the targeted second pass for fields the main extraction left empty.

Phase 7: the main pass sometimes returns nothing at all for a field even though
the window prints it (23 of 43 publisher misses, 20 of 31 date misses).  This
module only answers *which* fields deserve one focused question and *which*
blocks may be shown for it; the model call, the evidence gate and the record
update live in :mod:`dspy_extract`.

The module is deliberately pure: no DSPy import, no LM, no network, so the
selection rules are directly testable and the extraction pipeline stays the
only place that talks to a model.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: Fields re-asked, in priority order.  Only evaluated fields that are absent
#: from the accepted record reach this list.
TARGETED_FIELDS: tuple[str, ...] = ("title", "publisher", "issued", "author")

#: At most this many targeted model calls per record.
TARGETED_MAX_CALLS = 2

#: Character budget for one field's block excerpts.
TARGETED_BLOCK_BUDGET = 4000

#: Zones that own a field.  A targeted question is never asked about a field
#: whose zones are absent from the window: a body paragraph cannot vouch for an
#: imprint, so the call would only produce a guess.
FIELD_OWNING_ZONES: dict[str, tuple[str, ...]] = {
    "title": ("title", "cover", "statement_of_responsibility"),
    "publisher": ("imprint", "copyright", "cip"),
    "issued": ("imprint", "copyright", "cip"),
    "author": ("title", "statement_of_responsibility", "cover"),
}


def empty_fields(
    accepted: Mapping[str, Any],
    evaluated: set[str] | frozenset[str],
) -> list[str]:
    """Evaluated ``TARGETED_FIELDS`` with no accepted value, in priority order."""
    return [field for field in TARGETED_FIELDS
            if field in evaluated and field not in accepted]


def owning_blocks(
    field: str,
    blocks: list[dict[str, Any]],
    zone_by_id: Mapping[str, str],
    *,
    budget: int = TARGETED_BLOCK_BUDGET,
) -> list[dict[str, Any]]:
    """Excerpts of the blocks whose zone owns *field*, in zone priority order.

    The same zone may appear on several pages (one copyright notice per
    printing); every such block is a candidate.  Ordering follows
    :data:`FIELD_OWNING_ZONES`, so a title page is read before the cover.  Text
    is truncated to *budget* characters in total; an empty list means the field
    must not be asked at all.
    """
    zones = FIELD_OWNING_ZONES.get(field)
    if not zones:
        return []
    candidates = [block for block in blocks
                  if zone_by_id.get(block.get("id")) in zones
                  and isinstance(block.get("text"), str) and block["text"].strip()]
    candidates.sort(key=lambda block: zones.index(zone_by_id[block["id"]]))
    excerpts: list[dict[str, Any]] = []
    remaining = budget
    for block in candidates:
        if remaining <= 0:
            break
        text = block["text"][:remaining]
        if not text:
            continue
        excerpt = {"id": block["id"], "text": text, "zone": zone_by_id[block["id"]]}
        page = block.get("physical_page_index")
        if isinstance(page, int):
            excerpt["physical_page_index"] = page
        excerpts.append(excerpt)
        remaining -= len(text)
    return excerpts
