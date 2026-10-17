"""Offline checks for authority-source registration (P2 phase 1, design D1).

VIAF is absent by decision: probed 2026-10-17, viaf.org search redirects to
the human UI even with httpAccept=application/json and Cloudflare
challenges non-browser clients. See ingestion/authority/__init__.py.
"""

import json
from pathlib import Path

import pytest

from citeindex.ingestion.authority import (
    ORCID_SEARCH_URL,
    WIKIDATA_SEARCH_URL,
    normalize_orcid_person,
    orcid_search_params,
)
from citeindex.ingestion.metadata_registry import _PROVIDER_URLS, RegistryClient
from citeindex.ingestion.models import IngestionConfig
from citeindex.ingestion.online_enrichment import match_candidate

FIXTURES = json.loads((Path(__file__).parent / "fixtures/online_enrichment/authority_responses.json").read_text())
_QUERIES = {"orcid": ("Wright", "Nathan"), "wikidata": ("Antioch", None), "geonames": ("Antioch", None)}


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, delay):
        self.now += delay


class Response:
    def __init__(self, payload=None, status=200, headers=None):
        self.status_code = status
        self.headers = headers or {}
        self.raw = json.dumps(payload).encode()
        self.closed = False

    def iter_content(self, chunk_size):
        yield self.raw

    def close(self):
        self.closed = True


class Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def client(*responses, config=None):
    clock = Clock()
    session = Session(*responses)
    instance = RegistryClient(config or {}, None, session=session, clock=clock, sleep=clock.sleep)
    return instance, session


@pytest.mark.parametrize("provider", ["orcid", "wikidata", "geonames"])
@pytest.mark.parametrize("kind,status", [("positive", "found"), ("empty", "not_found"), ("malformed", "error")])
def test_authority_search_contracts(provider, kind, status, monkeypatch):
    monkeypatch.setenv("GEONAMES_USERNAME", "tester")
    instance, session = client(Response(FIXTURES[provider][kind]))
    title, author = _QUERIES[provider]

    results = instance.search(provider, title, author=author)

    assert len(results) <= 5
    assert results[0]["status"] == status
    if status == "error":
        assert results[0]["error"] == "malformed_response"
    assert all(call[1]["stream"] and not call[1]["allow_redirects"] for call in session.calls)
    if status != "found":
        return
    top = results[0]["candidate"]
    assert top["authority"]["system"] == provider
    assert top["authority"]["id"]
    assert top["authority"]["uri"].startswith("https://")
    assert "title" not in top  # authority candidates are never work-shaped
    assert results[0]["provenance"]["source_id"]


def test_orcid_negotiates_json_and_builds_fielded_query(monkeypatch):
    instance, session = client(Response(FIXTURES["orcid"]["empty"]))

    instance.search("orcid", "Wright", author="Nathan")

    url, kwargs = session.calls[0]
    assert url == ORCID_SEARCH_URL
    assert kwargs["headers"]["Accept"] == "application/json"
    assert kwargs["params"] == {"q": 'family-name:"Wright" AND given-names:"Nathan"', "rows": "5"}


def test_wikidata_params_https_upgrade_and_identity_dedupe(monkeypatch):
    instance, session = client(Response(FIXTURES["wikidata"]["positive"]))

    results = instance.search("wikidata", "Antioch")

    url, kwargs = session.calls[0]
    assert url == WIKIDATA_SEARCH_URL
    assert kwargs["params"]["action"] == "wbsearchentities"
    assert kwargs["params"]["search"] == "Antioch"
    assert len(results) == 2  # the duplicated QID is deduplicated
    assert results[0]["candidate"]["authority"] == {
        "system": "wikidata", "id": "Q200441", "uri": "https://www.wikidata.org/entity/Q200441"}
    assert results[0]["candidate"]["description"] == "ancient Greek city in southern Turkey"
    assert results[0]["provenance"]["source_id"] == "Q200441"


def test_geonames_username_required_and_redacted(monkeypatch):
    monkeypatch.delenv("GEONAMES_USERNAME", raising=False)
    skipped, skipped_session = client()
    results = skipped.search("geonames", "Antioch")
    assert results[0]["status"] == "skipped"
    assert results[0]["error"] == "geonames_no_username"
    assert not skipped_session.calls

    monkeypatch.setenv("GEONAMES_USERNAME", "tester")
    instance, session = client(Response(FIXTURES["geonames"]["positive"]))
    results = instance.search("geonames", "Antioch")
    assert session.calls[0][1]["params"]["username"] == "tester"
    assert "username" not in results[0]["provenance"]["request_url"]
    assert results[0]["candidate"]["authority"]["uri"] == "https://www.geonames.org/325311/"
    assert results[0]["candidate"]["country"] == "Türkiye"


def test_normalize_orcid_person_maps_names_and_institutions():
    record = FIXTURES["orcid"]["positive"]["expanded-result"][0]
    assert normalize_orcid_person(record) == {
        "type": "person",
        "authority": {"system": "orcid", "id": "0000-0003-1535-4568",
                      "uri": "https://orcid.org/0000-0003-1535-4568"},
        "family": "Wright",
        "given": "Nathan",
        "other-names": ["N. P. Wright"],
        "institutions": ["Wright State University"],
    }


def test_orcid_query_building_sanitises_phrases():
    assert orcid_search_params("Wright") == {"q": '"Wright"', "rows": "5"}
    assert orcid_search_params('"Odd  Name "') == {"q": '"Odd Name"', "rows": "5"}
    assert orcid_search_params("Wright", "Nathan")["q"] == 'family-name:"Wright" AND given-names:"Nathan"'


def test_viaf_is_dropped_with_documentation():
    assert "viaf" not in _PROVIDER_URLS


def test_models_accept_authority_provider_names():
    config = IngestionConfig(online_enrich_providers=("crossref", "orcid", "wikidata", "geonames"))
    assert config.online_enrich_providers == ("crossref", "orcid", "wikidata", "geonames")
    with pytest.raises(ValueError):
        IngestionConfig(online_enrich_providers=("viaf",))


def test_authority_candidates_are_rejected_by_the_work_matcher():
    csl = {"title": "Antioch on the Orontes", "type": "book", "_field_status": {}}
    person = normalize_orcid_person(FIXTURES["orcid"]["positive"]["expanded-result"][0])
    assert not match_candidate(csl, person)["accepted"]


def test_search_cache_is_keyed_per_query_not_per_record_path(tmp_path, monkeypatch):
    """Regression: the record-path walk once clobbered the cache key variable,
    so every ORCID search overwrote one ``expanded-result.json`` cache file."""
    monkeypatch.setenv("GEONAMES_USERNAME", "tester")
    instance, session = client(
        Response(FIXTURES["wikidata"]["empty"]),
        Response(FIXTURES["wikidata"]["positive"]),
        Response(FIXTURES["wikidata"]["positive"]),
    )
    instance.cache_dir = tmp_path

    instance.search("wikidata", "Nowhere")
    instance.search("wikidata", "Antioch")
    cached_hit = instance.search("wikidata", "Antioch")

    names = sorted(path.name for path in tmp_path.iterdir())
    assert all(name != "search.json" for name in names)  # the record-path segment
    assert len(names) == 2
    assert instance.cache_hits == 1
    assert cached_hit[0]["provenance"]["cache_hit"] is True