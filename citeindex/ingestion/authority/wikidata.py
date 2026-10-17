"""Wikidata entity search via the wbsearchentities Action API.

wbsearchentities is preferred over SPARQL for name search: it is a plain
public JSON endpoint without the SPARQL query-timeout policy. Language is
fixed to ``en`` for phase 1; the phase 2 matcher can add language or
instance-type filters.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

WIKIDATA_SEARCH_URL = "https://www.wikidata.org/w/api.php"


def wikidata_search_params(name: str) -> dict[str, str]:
    return {"action": "wbsearchentities", "search": name, "language": "en", "limit": "5", "format": "json"}


def normalize_wikidata_entity(record: Mapping[str, Any]) -> dict[str, Any]:
    """Map one wbsearchentities result into an authority entity candidate."""
    qid = str(record.get("id") or "").strip()
    if not qid:
        return {}
    uri = str(record.get("concepturi") or "").strip() or "https://www.wikidata.org/wiki/" + qid
    if uri.startswith("http://"):
        uri = "https://" + uri[len("http://") :]
    candidate: dict[str, Any] = {
        "type": "entity",
        "authority": {"system": "wikidata", "id": qid, "uri": uri},
    }
    label = str(record.get("label") or "").strip()
    if label:
        candidate["label"] = label
    description = str(record.get("description") or "").strip()
    if description:
        candidate["description"] = description
    return candidate
