"""Authority-file clients for entity normalisation (P2 phase 1, design D1).

One module per source owns the query shape, the record extraction path,
and the candidate normalisation. Transport — pacing, request budget,
bounded reads, caching, and provenance — stays in
``ingestion.metadata_registry.RegistryClient``; each source registers
there through ``_PROVIDER_URLS`` and a search dispatch branch.

Authority candidates are deliberately not work-shaped: they carry
``type`` (``person`` / ``place`` / ``entity``) and an ``authority`` block
(``system``/``id``/``uri``), never a CSL ``title``, so the work matcher
rejects them by construction and nothing can merge into a host record
until the phase 2 name/place policy exists.

VIAF was probed on 2026-10-17 and dropped: ``viaf.org/viaf/search``
307-redirects to the human interface even with
``httpAccept=application/json``, and non-browser clients receive
Cloudflare challenges (HTTP 403). Revisit only if VIAF publishes a
stable machine-readable API.
"""

from .geonames import GEONAMES_SEARCH_URL, geonames_search_params, normalize_geonames_place
from .matching import (
    AMBIGUITY_MARGIN,
    ENTITY_THRESHOLDS,
    fuzzy_similarity,
    get_embedder,
    match_entity,
    select_authority_candidate,
)
from .orcid import ORCID_HEADERS, ORCID_SEARCH_URL, normalize_orcid_person, orcid_search_params
from .wikidata import WIKIDATA_SEARCH_URL, normalize_wikidata_entity, wikidata_search_params

__all__ = [
    "AMBIGUITY_MARGIN",
    "ENTITY_THRESHOLDS",
    "GEONAMES_SEARCH_URL",
    "ORCID_HEADERS",
    "ORCID_SEARCH_URL",
    "WIKIDATA_SEARCH_URL",
    "fuzzy_similarity",
    "geonames_search_params",
    "get_embedder",
    "match_entity",
    "normalize_geonames_place",
    "normalize_orcid_person",
    "normalize_wikidata_entity",
    "orcid_search_params",
    "select_authority_candidate",
    "wikidata_search_params",
]
