"""Offline checks for entity matching and the no-match policy (P2 D2/D3).

No network, no torch: the embedding tier is exercised through a stub
embedder; the pure-fuzzy tiers are the defaults everywhere.
"""

import importlib.util
import json
from copy import deepcopy
from difflib import SequenceMatcher

import pytest

from citeindex.ingestion import online_enrichment as enrichment
from citeindex.ingestion.authority import matching
from citeindex.ingestion.authority.matching import (
    ENTITY_THRESHOLDS,
    fuzzy_similarity,
    get_embedder,
    match_entity,
    select_authority_candidate,
)
from citeindex.ingestion.markdown_export import generate_library_markdown
from citeindex.ingestion.models import IngestionConfig
from citeindex.ingestion.online_enrichment import build_authority_links

TITLE = "Specific studies in manuscript transmission"
DOI = "10.1000/host"


class StubEmbedder:
    def __init__(self, value=1.0):
        self.value = value

    def similarity(self, left, right):
        return self.value


def person_result(orcid="0000-0002-0000-0001", family="Jones", given="Indiana", **candidate_updates):
    candidate = {
        "type": "person",
        "authority": {"system": "orcid", "id": orcid, "uri": f"https://orcid.org/{orcid}"},
        "family": family,
        "given": given,
    }
    candidate.update(candidate_updates)
    return {
        "status": "found",
        "candidate": candidate,
        "provenance": {
            "provider": "orcid",
            "request_url": "https://pub.orcid.org/v3.0/expanded-search/",
            "response_digest": "d" * 64,
            "source_id": orcid,
        },
    }


def place_result(label="London", system="geonames", id_="2643743", **candidate_updates):
    uri = f"https://www.geonames.org/{id_}/" if system == "geonames" else f"https://www.wikidata.org/entity/{id_}"
    candidate = {"type": "place", "authority": {"system": system, "id": id_, "uri": uri}, "label": label}
    candidate.update(candidate_updates)
    return {
        "status": "found",
        "candidate": candidate,
        "provenance": {
            "provider": system,
            "request_url": "https://example.org",
            "response_digest": "d" * 64,
            "source_id": id_,
        },
    }


def test_fuzzy_similarity_tiers_and_normalization():
    assert fuzzy_similarity("  Antioch! ", "antioch") == 1.0
    assert fuzzy_similarity("anything", "") == 0.0
    # token_set_ratio is order-insensitive: "Wright, Nathan" == "Nathan Wright"
    assert fuzzy_similarity("nathan wright", "Wright, Nathan") > 0.9
    # forced stdlib tier is the benchmark baseline's engine
    assert fuzzy_similarity("abc", "abd", engine="stdlib") == SequenceMatcher(None, "abc", "abd").ratio()


def test_person_match_accepts_initials_with_anchor():
    person = {"family": "Wright", "given": "Nathan"}
    candidate = {"authority": {"system": "orcid", "id": "x", "uri": "u"}, "family": "Wright", "given": "N."}
    match = match_entity("person", person, candidate)
    assert match["accepted"]
    assert match["components"]["anchor"] == 1
    assert match["components"]["embedding"] is None  # pure-fuzzy tier by default


def test_person_rejects_conflicting_given_name():
    person = {"family": "Wright", "given": "Nathan"}
    candidate = {"authority": {"system": "orcid", "id": "x", "uri": "u"}, "family": "Wright", "given": "John"}
    match = match_entity("person", person, candidate)
    assert not match["accepted"]
    assert match["components"]["anchor"] == 0


def test_script_mismatch_flagged_and_embeddings_skipped():
    source = {"literal": "马相伯"}
    candidate = {"authority": {"system": "orcid", "id": "x", "uri": "u"}, "literal": "Ma Xiangbo"}
    match = match_entity("person", source, candidate, embedder=StubEmbedder(1.0))
    assert match["components"]["script_mismatch"] is True
    assert match["components"]["embedding"] is None
    assert not match["accepted"]


def test_place_match_uses_positive_evidence_anchor():
    with_anchor = match_entity("place", "Antioch, Turkey", {"label": "Antioch", "country": "Turkey"})
    assert with_anchor["accepted"] and with_anchor["components"]["anchor"] == 1
    no_anchor = match_entity("place", "Antioch", {"label": "Antioch", "country": "Türkiye"})
    assert no_anchor["accepted"] and no_anchor["components"]["anchor"] == 0


def test_place_rejects_transliteration_drift_honestly():
    match = match_entity("place", "Antakya", {"label": "Antioch", "country": "Türkiye"})
    assert not match["accepted"]  # documented limitation: transliteration is the gold set's job


def test_journal_policy_thresholds_documented():
    assert ENTITY_THRESHOLDS["journal"] == 0.90 and ENTITY_THRESHOLDS["work"] == 0.90
    assert match_entity("journal", "Journal of Theological Studies", {"label": "Journal of Theological Studies"})[
        "accepted"
    ]
    assert not match_entity("journal", "Church History", {"label": "Journal of Theological Studies"})["accepted"]
    with pytest.raises(ValueError):
        match_entity("song", "anything", {"label": "anything"})


def test_embedder_component_and_weights():
    person = {"family": "Wright", "given": "Nathan"}
    candidate = {"authority": {"system": "orcid", "id": "x", "uri": "u"}, "family": "Wright", "given": "Nathan"}
    plain = match_entity("person", person, candidate)
    embedded = match_entity("person", person, candidate, embedder=StubEmbedder(0.9))
    assert embedded["components"]["embedding"] == 0.9
    # (0.5, 0.3, 0.2) weights with the embedder, vs (0.7, 0.0, 0.3) without
    assert embedded["score"] == round(0.5 * plain["components"]["fuzzy"] + 0.3 * 0.9 + 0.2 * 1, 4)


def test_select_links_unique_candidate_with_provenance():
    decision = select_authority_candidate(
        "person", {"family": "Jones", "given": "Indiana"}, [person_result()], field="author"
    )
    assert decision["status"] == "linked"
    assert decision["field"] == "author"
    assert decision["authority"]["id"] == "0000-0002-0000-0001"
    assert decision["provenance"]["provider"] == "orcid"
    assert decision["source"] == {"family": "Jones", "given": "Indiana"}
    assert decision["alternatives"] == 0


def test_select_reports_ambiguous_homonyms():
    results = [person_result(orcid="0000-0002-0000-0001"), person_result(orcid="0000-0002-0000-0002")]
    decision = select_authority_candidate("person", {"family": "Jones", "given": "Indiana"}, results)
    assert decision["status"] == "no_match"
    assert decision["reason"] == "ambiguous_identity"
    assert {row["id"] for row in decision["candidates"]} == {"0000-0002-0000-0001", "0000-0002-0000-0002"}


def test_select_reports_no_candidates_and_budget_separately():
    not_found = [{"status": "not_found", "candidate": None}]
    decision = select_authority_candidate("place", "London", not_found)
    assert decision["status"] == "no_match" and decision["reason"] == "no_candidates"

    exhausted = [{"status": "budget_exhausted", "error": "budget_exhausted"}]
    decision = select_authority_candidate("place", "London", exhausted)
    assert decision["status"] == "skipped" and decision["reason"] == "budget_exhausted"


def test_select_ignores_work_shaped_results():
    work = {"status": "found", "candidate": {"title": "A Work"}, "provenance": {"provider": "crossref"}}
    decision = select_authority_candidate("person", {"family": "Jones"}, [work])
    assert decision["status"] == "no_match" and decision["reason"] == "no_candidates"


def test_select_uses_process_embedder_when_available(monkeypatch):
    monkeypatch.setattr(matching, "get_embedder", lambda: StubEmbedder(0.5))
    decision = select_authority_candidate("person", {"family": "Jones", "given": "Indiana"}, [person_result()])
    assert decision["components"]["embedding"] == 0.5


@pytest.mark.skipif(
    importlib.util.find_spec("sentence_transformers") is not None,
    reason="sentence-transformers installed in this environment",
)
def test_get_embedder_is_none_without_the_extra():
    assert get_embedder() is None


# --- enrichment wiring ------------------------------------------------------


class Client:
    def __init__(self, result=None, searches=None):
        self.result = result
        self.searches = searches or {}
        self.request_count = self.cache_hits = 0
        self.attempts = []
        self.calls = []

    def lookup(self, provider, identifier, fresh=False):
        self.calls.append(("lookup", provider, identifier, fresh))
        return deepcopy(self.result) if self.result else {"status": "not_found", "candidate": None}

    def search(self, provider, title, author=None, year=None):
        self.calls.append(("search", provider, title))
        return deepcopy(self.searches.get(provider, []))

    def remaining(self):
        return 20

    def reserve_request(self, timeout=5):
        self.request_count += 1
        return min(timeout, 5)


def source_csl(**updates):
    csl = {
        "type": "article-journal",
        "title": TITLE,
        "author": [{"family": "Jones", "given": "Indiana"}],
        "issued": {"date-parts": [[2020]]},
        "DOI": DOI,
        "publisher-place": "London",
    }
    csl.update(updates)
    csl["_field_evidence"] = {}
    blocks = []
    for field in ("title", "author", "issued", "DOI"):
        value = csl[field]
        text = (
            value[0].get("family")
            if field == "author"
            else str(value["date-parts"][0][0])
            if field == "issued"
            else str(value)
        )
        block = {"id": "source:" + field, "text": text, "role": "front_matter", "physical_page_index": 0}
        blocks.append(block)
        csl["_field_evidence"][field] = {"block_id": block["id"], "quote": text}
    return csl, {"source_blocks": blocks}


def pdf(tmp_path):
    path = tmp_path / "source.pdf"
    path.write_bytes(b"source fixture")
    return str(path)


def test_authority_decisions_reported_without_csl_mutation(tmp_path):
    csl, extra = source_csl()
    client = Client(
        searches={
            "orcid": [person_result()],
            "geonames": [place_result(label="London", country="United Kingdom")],
        }
    )
    cfg = IngestionConfig(online_enrich=True, online_enrich_providers=("crossref", "orcid", "geonames"))

    current, report = enrichment.enrich_metadata(csl, {}, "digital_pdf", extra, cfg, pdf(tmp_path), client=client)

    assert ("search", "orcid", "Jones") in client.calls
    decisions = report["authority_decisions"]
    person = next(d for d in decisions if d["entity_type"] == "person")
    place = next(d for d in decisions if d["entity_type"] == "place")
    assert person["status"] == "linked" and person["field"] == "author"
    assert person["authority"] == {
        "system": "orcid",
        "id": "0000-0002-0000-0001",
        "uri": "https://orcid.org/0000-0002-0000-0001",
    }
    assert place["status"] == "linked" and place["field"] == "publisher-place"
    assert place["authority"]["system"] == "geonames"
    # authority links never mutate the CSL record (sidecar persistence is deferred)
    assert current["author"] == [{"family": "Jones", "given": "Indiana"}]
    assert current["publisher-place"] == "London"
    assert "authority" not in json.dumps(current)


def test_authority_pass_is_opt_in(tmp_path):
    csl, extra = source_csl()
    client = Client(searches={"orcid": [person_result()]})
    cfg = IngestionConfig(online_enrich=True)  # default providers: work registries only

    current, report = enrichment.enrich_metadata(csl, {}, "digital_pdf", extra, cfg, pdf(tmp_path), client=client)

    assert report["authority_decisions"] == []
    assert not any(call[1] == "orcid" for call in client.calls)
    assert current["author"] == [{"family": "Jones", "given": "Indiana"}]


# --- D4: sidecar links ------------------------------------------------------


def test_authority_links_sidecar_and_markdown_line(tmp_path):
    csl, extra = source_csl()
    client = Client(
        searches={
            "orcid": [person_result()],
            "geonames": [place_result(label="London", country="United Kingdom")],
        }
    )
    cfg = IngestionConfig(online_enrich=True, online_enrich_providers=("crossref", "orcid", "geonames"))
    current, report = enrichment.enrich_metadata(csl, {}, "digital_pdf", extra, cfg, pdf(tmp_path), client=client)

    sidecar = build_authority_links(report)
    assert sidecar is not None and sidecar["schema_version"] == 1
    person_link = next(link for link in sidecar["links"] if link["entity_type"] == "person")
    assert person_link == {
        "entity_type": "person",
        "field": "author",
        "source": {"family": "Jones", "given": "Indiana"},
        "system": "orcid",
        "id": "0000-0002-0000-0001",
        "uri": "https://orcid.org/0000-0002-0000-0001",
        "score": person_link["score"],
        "provenance": {
            "provider": "orcid",
            "request_url": "https://pub.orcid.org/v3.0/expanded-search/",
            "response_digest": "d" * 64,
            "source_id": "0000-0002-0000-0001",
        },
    }

    markdown = generate_library_markdown(current, None, None, "digital_pdf", authority_links=sidecar)
    assert "**Authority links:**" in markdown
    assert "[orcid 0000-0002-0000-0001](https://orcid.org/0000-0002-0000-0001)" in markdown
    assert "London → [geonames" in markdown


def test_build_authority_links_is_none_without_linked_entities():
    assert build_authority_links({}) is None
    assert build_authority_links({"authority_decisions": []}) is None
    below = {"authority_decisions": [{"status": "no_match", "reason": "below_threshold"}]}
    assert build_authority_links(below) is None
