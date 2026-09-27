"""Identity, policy and finalization regressions; no network or model calls."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

from citeindex.ingestion import online_enrichment as enrichment
from citeindex.ingestion.models import IngestionConfig, PipelineResult
from citeindex.ingestion.master import CiteIndexIngestionOrchestrator
from citeindex.ingestion.storage import store_corpus_artifacts

TITLE = "Specific studies in manuscript transmission"
DOI = "10.1000/host"
ISBN = "9780199535569"


def source_csl(**updates):
    csl = {"type": "article-journal", "title": TITLE, "author": [{"family": "Jones"}],
           "issued": {"date-parts": [[2020]]}, "DOI": DOI}
    csl.update(updates)
    csl["_field_evidence"] = {}
    blocks = []
    for field in ("title", "author", "issued", "DOI", "ISBN", "edition", "language", "publisher"):
        if csl.get(field) is None:
            continue
        value = csl[field]
        text = value[0].get("family", value[0].get("literal")) if field == "author" else str(value["date-parts"][0][0]) if field == "issued" else str(value)
        block = {"id": "source:" + field, "text": text, "role": "front_matter", "physical_page_index": 0}
        blocks.append(block)
        csl["_field_evidence"][field] = {"block_id": block["id"], "quote": text}
    return csl, {"source_blocks": blocks}


def registry(candidate=None, provider="crossref", **changes):
    candidate = candidate or {"type": "article-journal", "title": TITLE, "DOI": DOI,
                              "author": [{"family": "Jones"}], "issued": {"date-parts": [[2019]]},
                              "publisher": "Registry Press", "publisher-place": "London"}
    result = {"status": "found", "candidate": candidate, "provenance": {
        "provider": provider, "request_identifier": candidate.get("DOI", candidate.get("ISBN")),
        "request_url": "https://api.crossref.org/works/" + DOI if provider == "crossref" else "https://openlibrary.org/isbn/" + ISBN + ".json",
        "response_digest": hashlib.sha256(json.dumps(candidate, sort_keys=True).encode()).hexdigest(),
        "retrieved_at": "2026-09-27T00:00:00Z", "source_id": candidate.get("DOI", candidate.get("ISBN")),
    }}
    result.update(changes)
    return result


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


def refreshed(csl=None, extra=None):
    if csl is None:
        csl, extra = source_csl()
    return enrichment.refresh_field_states(csl, {}, "digital_pdf", extra or {})


def pdf(tmp_path):
    path = tmp_path / "source.pdf"
    path.write_bytes(b"source fixture")
    return str(path)


def test_refresh_does_not_trust_stale_states_filename_authors_or_reference_quotes():
    csl, extra = source_csl()
    csl["author"] = [{"family": "Filename"}]
    csl["_field_status"] = {"author": "source-supported", "publisher": "missing"}
    csl["publisher"] = "Unverified Press"
    result = refreshed(csl, extra)
    assert result["_field_status"]["author"] == "unverified"
    assert result["_field_status"]["publisher"] == "unverified"
    assert result["_field_status"]["publisher-place"] == "missing"
    assert "author" not in result["_field_evidence"]
    extra["source_blocks"][0]["role"] = "bibliography"
    assert refreshed(csl, extra)["_field_status"]["title"] == "unverified"


@pytest.mark.parametrize("updates,reason", [
    ({"title": "An unrelated work"}, "title_mismatch"),
    ({"type": "book"}, "work_type_mismatch"),
    ({"edition": "2"}, "edition_mismatch"),
    ({"language": "fr"}, "language_mismatch"),
    ({"author": [{"family": "Other"}]}, "author_mismatch"),
    ({"issued": {"date-parts": [[1930]]}}, "issued_mismatch"),
])
def test_discovered_identity_contradictions_abstain(updates, reason):
    csl, extra = source_csl(edition="1", language="en")
    candidate = {**registry()["candidate"], **updates}
    match = enrichment.match_candidate(refreshed(csl, extra), candidate)
    assert match["accepted"] is False
    assert match["reason"] == reason


def test_t1_source_identifier_can_correct_author_and_print_year_without_confirmation():
    csl = refreshed()
    candidate = {**registry()["candidate"], "author": [{"family": "RegistryAuthor"}], "issued": {"date-parts": [[1990]]}}
    match = enrichment.match_candidate(csl, candidate, existing_identifier=True)
    assert match["accepted"]
    original = deepcopy(csl)
    result, decisions = enrichment.merge_registry(csl, registry(candidate), "T1", "source_identifier", match)
    assert result["author"] == candidate["author"] and result["issued"] == candidate["issued"]
    assert result["_field_status"]["issued"] == "registry-corrected"
    assert "issued" not in result["_field_evidence"]
    assert any(d["field"] == "issued" and d["source_evidence"] for d in decisions)
    assert csl == original


def test_t2_requires_title_author_agreement_and_checks_edition():
    csl, extra = source_csl(type="book", DOI=None, ISBN=ISBN)
    candidate = {"type": "book", "title": TITLE, "ISBN": ISBN, "author": [{"family": "Jones"}], "publisher": "Catalog"}
    assert enrichment.match_candidate(refreshed(csl, extra), candidate, isbn=True)["accepted"]
    candidate["author"] = [{"family": "Other"}]
    assert not enrichment.match_candidate(refreshed(csl, extra), candidate, isbn=True)["accepted"]
    del candidate["author"]
    assert not enrichment.match_candidate(refreshed(csl, extra), candidate, isbn=True)["accepted"]


def test_missing_author_can_match_using_source_year_but_generic_title_cannot():
    csl, extra = source_csl(author=None)
    assert enrichment.match_candidate(refreshed(csl, extra), registry()["candidate"])["accepted"]
    csl, extra = source_csl(title="History", author=None)
    candidate = {**registry()["candidate"], "title": "History"}
    assert not enrichment.match_candidate(refreshed(csl, extra), candidate)["accepted"]


def test_cjk_exact_normalized_title_still_needs_identity_anchor():
    csl, extra = source_csl(title="中国古代文献传承研究", author=[{"literal": "张三"}])
    candidate = {**registry()["candidate"], "title": "中国古代文献传承研究", "author": [{"literal": "张三"}]}
    assert enrichment.match_candidate(refreshed(csl, extra), candidate)["accepted"]
    candidate["title"] += "续编"
    assert not enrichment.match_candidate(refreshed(csl, extra), candidate)["accepted"]


def test_duplicate_doi_not_tie_but_distinct_equal_title_is_ambiguous():
    csl = refreshed()
    result = registry()
    winner, _ = enrichment.select_candidate(csl, [result, deepcopy(result)])
    assert winner
    other = registry({**result["candidate"], "DOI": "10.1000/other"})
    assert enrichment.select_candidate(csl, [result, other])[0] is None


def test_t4_fills_only_and_missing_provenance_blocks_all_writes():
    csl = refreshed()
    match = enrichment.match_candidate(csl, registry()["candidate"])
    result, decisions = enrichment.merge_registry(csl, registry(), "T4", "ai_search", match)
    assert result["issued"] == csl["issued"] and result["publisher"] == "Registry Press"
    assert all(d["action"] == "fill" for d in decisions)
    no_provenance = registry(); no_provenance["provenance"].pop("response_digest")
    assert enrichment.merge_registry(csl, no_provenance, "T1", "source_identifier", match) == (csl, [])


@pytest.mark.parametrize("config,modality,reason", [
    (IngestionConfig(), "digital_pdf", "disabled"),
    (IngestionConfig(online_enrich=True, offline_verification=True), "scanned_pdf", "offline_verification"),
    (IngestionConfig(online_enrich=True), "url_article", "pdf_only"),
    (IngestionConfig(online_enrich=True), "media", "pdf_only"),
])
def test_disabled_offline_and_non_pdf_gates_make_zero_registry_calls(tmp_path, config, modality, reason):
    csl, extra = source_csl(); client = Client(registry())
    result, report = enrichment.enrich_metadata(csl, {}, modality, extra, config, pdf(tmp_path), client=client)
    assert report["reason"] == reason and result == csl and not client.calls


def test_unvalidated_title_gate_and_ordinary_complete_record_skip(tmp_path):
    path = pdf(tmp_path); client = Client(registry())
    csl, extra = source_csl(); csl["_field_evidence"].pop("title")
    result, report = enrichment.enrich_metadata(csl, {}, "digital_pdf", extra, IngestionConfig(online_enrich=True), path, client=client)
    assert report["reason"] == "no_source_supported_title" and not client.calls
    csl, extra = source_csl()
    for field in enrichment.target_fields(csl):
        csl.setdefault(field, "present")
    _, report = enrichment.enrich_metadata(csl, {}, "digital_pdf", extra, IngestionConfig(online_enrich=True), path, client=client)
    assert report["reason"] == "no_missing_target_fields" and not client.calls


def proposal(csl, path, **entry_updates):
    entry = {"kind": "registry_correction", "field": "issued", "old_value": csl.get("issued"),
             "proposed_value": {"date-parts": [[2019]]}, "provider": "crossref", "identifier": DOI,
             "evidence_reference": {"response_digest": registry()["provenance"]["response_digest"]}}
    entry.update(entry_updates)
    return {"schema_version": "1.0", "source_sha256": enrichment.source_digest(path),
            "base_candidate_digest": enrichment.candidate_digest(csl), "proposals": [entry]}


def test_proposal_fresh_lookup_and_atomic_validation(tmp_path):
    csl = refreshed(); path = pdf(tmp_path); cfg = IngestionConfig()
    client = Client(registry()); payload = proposal(csl, path)
    result, decisions = enrichment.apply_enrichment_proposal(csl, payload, enrichment.source_digest(path), client, cfg)
    assert result["issued"] == {"date-parts": [[2019]]} and decisions[0]["tier"] == "T1"
    assert client.calls[0][-1] is True
    payload["proposals"].append({**payload["proposals"][0], "field": "publisher", "old_value": None, "proposed_value": "Forged", "kind": "registry_fill"})
    before = deepcopy(csl)
    with pytest.raises(ValueError, match="freshly fetched"):
        enrichment.apply_enrichment_proposal(csl, payload, enrichment.source_digest(path), client, cfg)
    assert csl == before


@pytest.mark.parametrize("tamper", ["source_sha256", "base_candidate_digest", "old_value", "duplicate", "value", "kind", "provider"])
def test_stale_tampered_proposals_reject(tmp_path, tamper):
    csl = refreshed(); path = pdf(tmp_path); payload = proposal(csl, path)
    if tamper in {"source_sha256", "base_candidate_digest"}:
        payload[tamper] = "stale"
    elif tamper == "duplicate":
        payload["proposals"].append(deepcopy(payload["proposals"][0]))
    else:
        payload["proposals"][0]["proposed_value" if tamper == "value" else tamper] = "invalid"
    with pytest.raises(ValueError):
        enrichment.apply_enrichment_proposal(csl, payload, enrichment.source_digest(path), Client(registry()), IngestionConfig())


@pytest.mark.parametrize("kind", ["digital_pdf", "scanned_pdf"])
def test_orchestrator_finalizes_all_copies_and_stable_identity(tmp_path, monkeypatch, kind):
    path = pdf(tmp_path)
    csl, extra = source_csl()
    extra["quotation_locators"] = [{"citation_item": {"id": "old"}}]
    orchestrator = CiteIndexIngestionOrchestrator(str(tmp_path / "corpus"))
    monkeypatch.setattr(orchestrator, "detect_resource_type", lambda *a, **k: (kind, path))
    monkeypatch.setattr(orchestrator, "route_to_pipeline", lambda *a: PipelineResult(
        status="ok", source_id="source", resource_type=kind, csl_json=deepcopy(csl),
        document_json={"metadata": {"title": TITLE}, "structure": {"pages": []}}, merkle_tree={"root": "source-root"}, extra=deepcopy(extra)))
    result = registry()
    def client(*a, **k):
        return Client(result)
    from citeindex.ingestion import metadata_registry
    monkeypatch.setattr(metadata_registry, "RegistryClient", client)
    cfg = IngestionConfig(online_enrich=True)
    first = orchestrator.ingest(path, cfg)
    assert first["status"] == "ok", first
    result["provenance"]["retrieved_at"] = "2099-01-01T00:00:00Z"
    second = orchestrator.ingest(path, cfg)
    assert first["standardized_csl_json"]["id"] == second["standardized_csl_json"]["id"]
    directory = Path(first["document_path"])
    stored = json.loads((directory / "csl.json").read_text())
    assert stored["issued"] == {"date-parts": [[2019]]}
    assert stored["merkle_root"] == "source-root"
    exported = json.loads((directory / "csl-export.json").read_text())[0]
    assert exported["id"] == stored["id"]
    assert json.loads((directory / "online_enrichment.json").read_text())["status"] == "enriched"
    quotations = json.loads((directory / "quotation_locators.json").read_text())
    assert quotations[0]["citation_item"]["id"] == stored["id"]
    assert json.loads((directory / "document.json").read_text())["metadata"]["title"] == stored["title"]
    markdown = Path(first["library_md_path"]).read_text()
    assert "2019" in markdown and "Registry Press" in markdown


def test_source_repair_then_registry_overwrite_keeps_both_events(tmp_path, monkeypatch):
    path = pdf(tmp_path); csl, extra = source_csl()
    extra["source_blocks"].append({"id": "repair-year", "text": "2021", "physical_page_index": 0})
    evidence = enrichment.validate_block_evidence("issued", {"date-parts": [[2021]]}, {"block_id": "repair-year", "quote": "2021"}, extra["source_blocks"])
    repair = {"source_sha256": enrichment.source_digest(path), "proposals": [{"status": "proposed", "field": "issued",
              "old_value": csl["issued"], "proposed_value": {"date-parts": [[2021]]}, "quote": "2021", "locator": evidence["locator"], "reason": "Printed year"}]}
    repair_path = tmp_path / "repair.json"; repair_path.write_text(json.dumps(repair))
    orch = CiteIndexIngestionOrchestrator(str(tmp_path / "corpus"))
    monkeypatch.setattr(orch, "detect_resource_type", lambda *a, **k: ("digital_pdf", path))
    monkeypatch.setattr(orch, "route_to_pipeline", lambda *a: PipelineResult("ok", "source", "digital_pdf", deepcopy(csl), merkle_tree={"root": "root"}, extra=deepcopy(extra)))
    from citeindex.ingestion import metadata_registry
    monkeypatch.setattr(metadata_registry, "RegistryClient", lambda *a, **k: Client(registry()))
    output = orch.ingest(path, IngestionConfig(online_enrich=True, repair_proposal=str(repair_path)))
    assert output["status"] == "ok", output
    assert output["standardized_csl_json"]["issued"] == {"date-parts": [[2019]]}
    applied = output["sub_pipeline_outputs"]["citation_repair"]["applied"][0]
    assert applied["proposed_value"] == {"date-parts": [[2021]]}
    assert applied["superseded_by_enrichment"]["new_value"] == {"date-parts": [[2019]]}


def test_enrichment_sidecar_removed_when_artifact_set_no_longer_has_it(tmp_path):
    csl = {"id": "one", "title": TITLE, "type": "book"}
    corpus = tmp_path / "corpus"; corpus.mkdir()
    directory = Path(store_corpus_artifacts(str(corpus), "one", {"csl_json": csl, "online_enrichment": {"status": "enriched"}}))
    assert (directory / "online_enrichment.json").is_file()
    store_corpus_artifacts(str(corpus), "one", {"csl_json": csl})
    assert not (directory / "online_enrichment.json").exists()


@pytest.mark.parametrize("config", [{"online_enrich_min_score": float("nan")}, {"online_enrich_timeout": 0},
                                   {"online_enrich_providers": ("unknown",)}, {"online_enrich_providers": ("crossref", "crossref")},
                                   {"enrich_proposal": "proposal.json", "offline_verification": True}])
def test_invalid_config_rejected(config):
    with pytest.raises(ValueError):
        IngestionConfig(**config)


def test_unverified_identifier_cannot_override_conflicting_source_author(tmp_path):
    csl, extra = source_csl()
    csl["_field_evidence"].pop("DOI")
    candidate = {**registry()["candidate"], "author": [{"family": "Smith"}]}
    current, report = enrichment.enrich_metadata(csl, {}, "digital_pdf", extra,
        IngestionConfig(online_enrich=True), pdf(tmp_path), client=Client(registry(candidate)))
    assert current["author"] == csl["author"] and not report["decisions"]


def test_arbitrary_proposal_identifier_requires_source_anchor_agreement(tmp_path):
    csl = refreshed(); path = pdf(tmp_path)
    candidate = {**registry()["candidate"], "DOI": "10.1000/namesake", "author": [{"family": "Smith"}]}
    payload = proposal(csl, path, identifier=candidate["DOI"])
    with pytest.raises(ValueError, match="host identity"):
        enrichment.apply_enrichment_proposal(csl, payload, enrichment.source_digest(path),
            Client(registry(candidate)), IngestionConfig())


def test_multiple_matching_ai_identifiers_abstain(tmp_path, monkeypatch):
    from citeindex.ingestion import ai_discovery
    csl, extra = source_csl(DOI=None)
    monkeypatch.setattr(ai_discovery, "discover_identifier", lambda *a, **k: {
        "status": "found", "identifiers": [{"kind": "DOI", "value": "10.1000/first"},
                                            {"kind": "DOI", "value": "10.1000/second"}]})
    class MultipleClient(Client):
        def lookup(self, provider, identifier, fresh=False):
            return registry({**registry()["candidate"], "DOI": identifier,
                             "issued": {"date-parts": [[2020]]}})
    current, report = enrichment.enrich_metadata(csl, {}, "digital_pdf", extra,
        IngestionConfig(online_enrich=True, online_enrich_ai_fallback=True, online_enrich_ai_provider="openai"),
        pdf(tmp_path), client=MultipleClient())
    assert current.get("DOI") is None and not report["decisions"]
    assert report["rejected_candidates"][-1]["reason"] == "ambiguous_ai_identifiers"


def test_validated_proposal_identity_survives_automatic_discovery(tmp_path):
    csl, extra = source_csl(); path = pdf(tmp_path)
    new_doi = "10.1000/accepted"
    payload = proposal(refreshed(csl, extra), path, field="DOI", old_value=DOI,
                       proposed_value=new_doi, identifier=new_doi)
    proposal_path = tmp_path / "enrichment.json"
    proposal_path.write_text(json.dumps(payload))
    class IdentityClient(Client):
        def lookup(self, provider, identifier, fresh=False):
            return registry({**registry()["candidate"], "DOI": identifier,
                             "issued": {"date-parts": [[2020]]}})
    current, report = enrichment.enrich_metadata(csl, {}, "digital_pdf", extra,
        IngestionConfig(online_enrich=True, enrich_proposal=str(proposal_path)), path, client=IdentityClient())
    assert current["DOI"] == new_doi
    assert all(d["discovery_origin"] == "registry_proposal" for d in report["decisions"])
    assert current.get("publisher") is None


@pytest.mark.parametrize("field", ["author", "editor"])
def test_shared_surname_cannot_hide_conflicting_given_names(field):
    csl = {"type": "book", "title": TITLE, field: [{"family": "Smith", "given": "John"}],
           "_field_status": {field: "source-supported"}}
    candidate = {**csl, field: [{"family": "Smith", "given": "Jane"}]}
    assert not enrichment.match_candidate(csl, candidate)["accepted"]
    candidate[field][0]["given"] = "J."
    assert enrichment.match_candidate(csl, candidate)["accepted"]


@pytest.mark.parametrize("left,right", [("volume 1", "volume 2"), ("volume i", "volume ii"),
                                       ("1914", "1918")])
def test_title_similarity_cannot_hide_distinct_numbers(left, right):
    csl = {"type": "book", "title": TITLE + " " + left, "author": [{"family": "Smith"}],
           "_field_status": {"author": "source-supported"}}
    candidate = {**csl, "title": TITLE + " " + right}
    assert not enrichment.match_candidate(csl, candidate, isbn=True)["accepted"]


def test_empty_values_are_missing_but_unverified_present_values_are_preserved():
    csl, extra = source_csl()
    csl.update(publisher="   ", editor=[], issued={}, language="unknown")
    current = refreshed(csl, extra)
    for field in ("publisher", "editor", "issued"):
        assert current[field] is None and current["_field_status"][field] == "missing"
    assert current["language"] == "unknown" and current["_field_status"]["language"] == "unverified"
