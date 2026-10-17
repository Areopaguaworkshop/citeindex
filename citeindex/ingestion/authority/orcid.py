"""ORCID public person search (expanded-search; read-only, keyless).

The API returns XML unless ``Accept: application/json`` is sent, so that
header is attached to every ORCID request. Rate limits are not
published; requests are paced by RegistryClient and cached per query.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

ORCID_SEARCH_URL = "https://pub.orcid.org/v3.0/expanded-search/"
ORCID_HEADERS = {"Accept": "application/json"}


def _clean(value: str) -> str:
    return " ".join(str(value).replace('"', "").split())


def orcid_search_params(family: str, given: str | None = None) -> dict[str, str]:
    """Build the expanded-search query: fielded when both names are known."""
    family = _clean(family)
    if not family:
        return {"q": "", "rows": "5"}
    if given and _clean(given):
        query = f'family-name:"{family}" AND given-names:"{_clean(given)}"'
    else:
        query = f'"{family}"'
    return {"q": query, "rows": "5"}


def _first_string(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, list):
        return next((item.strip() for item in value if isinstance(item, str) and item.strip()), None)
    return None


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [item.strip() for item in value if isinstance(item, str) and item.strip()]


def normalize_orcid_person(record: Mapping[str, Any]) -> dict[str, Any]:
    """Map one expanded-search record into an authority person candidate."""
    orcid = str(record.get("orcid-id") or "").strip()
    if not orcid:
        return {}
    candidate: dict[str, Any] = {
        "type": "person",
        "authority": {"system": "orcid", "id": orcid, "uri": "https://orcid.org/" + orcid},
    }
    family = _first_string(record.get("family-names"))
    given = _first_string(record.get("given-names"))
    credit = _first_string(record.get("credit-name"))
    if family:
        candidate["family"] = family
    if given:
        candidate["given"] = given
    if not family and credit:
        candidate["literal"] = credit
    other_names = _strings(record.get("other-name"))
    if other_names:
        candidate["other-names"] = other_names
    institutions = _strings(record.get("institution-name"))
    if institutions:
        candidate["institutions"] = institutions
    return candidate
