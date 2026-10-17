"""GeoNames place search (free community server).

Uses ``http://api.geonames.org`` because the free server's https endpoint
presents a certificate that does not match the ``api.`` subdomain
(probed 2026-10-17). Requires ``GEONAMES_USERNAME`` in the environment;
the username is an accounting identity, not a credential, and it is
redacted from provenance URLs. Error payloads (``{"status": ...}``, e.g.
quota exhaustion) are not result sets and surface as
``malformed_response``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

GEONAMES_SEARCH_URL = "http://api.geonames.org/searchJSON"


def geonames_search_params(name: str) -> dict[str, str]:
    params: dict[str, str] = {"q": name, "maxRows": "5"}
    if username := os.environ.get("GEONAMES_USERNAME", "").strip():
        params["username"] = username
    return params


def normalize_geonames_place(record: Mapping[str, Any]) -> dict[str, Any]:
    """Map one GeoNames result into an authority place candidate."""
    geoname_id = str(record.get("geonameId") or "").strip()
    if not geoname_id:
        return {}
    candidate: dict[str, Any] = {
        "type": "place",
        "authority": {"system": "geonames", "id": geoname_id, "uri": f"https://www.geonames.org/{geoname_id}/"},
    }
    label = str(record.get("name") or "").strip()
    if label:
        candidate["label"] = label
    for source_key, csl_key in (("countryName", "country"), ("adminName1", "admin1"), ("fcode", "feature-code")):
        value = str(record.get(source_key) or "").strip()
        if value:
            candidate[csl_key] = value
    lat = str(record.get("lat") or "").strip()
    lng = str(record.get("lng") or "").strip()
    if lat and lng:
        candidate["coordinates"] = {"lat": lat, "lng": lng}
    return candidate
