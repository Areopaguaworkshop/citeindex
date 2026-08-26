from types import SimpleNamespace

from citeindex.ingestion import citation_verification


def _config(**changes):
    values = {
        "crossref_enabled": True,
        "offline_verification": False,
        "registry_contact_email": None,
        "citation_verifier_model": None,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_registry_value_changes_csl_only_when_source_quote_supports_it(monkeypatch):
    monkeypatch.setattr(
        citation_verification,
        "lookup_crossref_doi",
        lambda *args, **kwargs: {
            "status": "found",
            "candidate": {"DOI": "10.1000/example", "title": "Correct Title", "publisher": "Unseen Press"},
            "provenance": {"provider": "crossref", "request_identifier": "10.1000/example"},
        },
    )
    document = {"structure": {"pages": [{"page_number": "xii", "physical_page_index": 0, "paragraphs": [{"paragraph_id": "p1_1", "node_id": "n1", "char_start": 0, "char_end": 42, "text": "Correct Title. DOI: 10.1000/example."}]}]}}

    csl, report = citation_verification.verify_citation_metadata(
        {"title": "Draft Title"}, document, "digital_pdf", {}, _config(),
    )

    assert csl == {"title": "Draft Title"}
    assert "publisher" not in csl
    assert report["status"] == "needs_review"
    assert report["corrections"][0]["locator"]["physical_page_index"] == 0
    assert report["corrections"][0]["locator"]["printed_page_label"] == "xii"
    assert report["applied_corrections"] == []


def test_url_evidence_uses_snapshot_and_paragraph_locator(tmp_path, monkeypatch):
    snapshot = tmp_path / "source.html"
    snapshot.write_text("<html>Correct Title</html>", encoding="utf-8")
    monkeypatch.setattr(
        citation_verification,
        "lookup_crossref_doi",
        lambda *args, **kwargs: {"status": "found", "candidate": {"title": "Correct Title"}, "provenance": {"provider": "crossref"}},
    )
    document = {"metadata": {"url": "https://example.org"}, "structure": {"pages": [{"paragraphs": [{"paragraph_id": "p1_1", "node_id": "n1", "char_start": 0, "char_end": 13, "text": "Correct Title"}]}]}}

    _, report = citation_verification.verify_citation_metadata(
        {"title": "Draft"}, document, "url_article", {"source_snapshot_path": str(snapshot)}, _config(),
    )

    locator = report["corrections"][0]["locator"]
    assert locator["snapshot_artifact"] == "source.html"
    assert locator["paragraph_id"] == "p1_1"


def test_model_is_not_called_without_an_unresolved_conflict(monkeypatch):
    monkeypatch.setattr(
        citation_verification,
        "lookup_crossref_doi",
        lambda *args, **kwargs: {"status": "found", "candidate": {"title": "Correct Title"}, "provenance": {"provider": "crossref"}},
    )
    monkeypatch.setattr(citation_verification, "_model_review", lambda *_args: (_ for _ in ()).throw(AssertionError("must not run")))

    _, report = citation_verification.verify_citation_metadata(
        {"title": "Correct Title"}, {"structure": {"pages": []}}, "digital_pdf", {}, _config(citation_verifier_model="openai/gpt-5"),
    )

    assert report["model_review"] == "not_requested"


def test_all_declared_scalar_and_structured_fields_use_source_evidence(monkeypatch):
    candidate = {
        "DOI": "10.1000/example", "title": "Correct Title",
        "author": [{"given": "Ada", "family": "Lovelace"}],
        "issued": {"date-parts": [[2019]]}, "publisher": "Press",
        "publisher-place": "London", "container-title": "Journal",
        "URL": "https://example.org/work", "page": "10-20",
    }
    monkeypatch.setattr(citation_verification, "lookup_crossref_doi", lambda *args, **kwargs: {"status": "found", "candidate": candidate, "provenance": {"provider": "crossref"}})
    text = "Ada Lovelace. Correct Title. 2019. Press, London. Journal 10-20. DOI: 10.1000/example. https://example.org/work"
    document = {"metadata": {"source_path": "missing.pdf"}, "structure": {"pages": [{"page_number": 12, "physical_page_index": 0, "paragraphs": [{"node_id": "source:n", "char_start": 0, "char_end": len(text), "text": text}]}]}}
    draft = {"title": "Draft", "author": [{"family": "Other"}], "issued": {"date-parts": [[2020]]}}

    result, report = citation_verification.verify_citation_metadata(draft, document, "scanned_pdf", {}, _config())

    assert report["status"] == "verified"
    assert result["author"] == candidate["author"]
    assert result["issued"] == candidate["issued"]
    assert result["publisher-place"] == "London"
    assert result["URL"] == candidate["URL"]
    assert report["applied_corrections"]
    assert report["applied_corrections"][0]["locator"]["node_id"] == "source:n"


def test_model_receives_only_disputed_field_and_invalid_decision_is_rejected(monkeypatch):
    captured = {}
    monkeypatch.setattr(citation_verification, "lookup_crossref_doi", lambda *args, **kwargs: {"status": "found", "candidate": {"title": "Registry title"}, "provenance": {"provider": "crossref"}})
    document = {"structure": {"pages": [{"paragraphs": [{"node_id": "n1", "text": "Draft title"}]}]}}

    def model(field, draft, registry, evidence, model_name):
        captured.update(field=field, draft=draft, registry=registry, evidence=evidence)
        return {"verdict": "accept", "field": field, "selected_value": "Draft title", "quote": "Draft title", "locator": evidence[0]["locator"], "confidence": 0.9, "rationale": "exact source"}

    monkeypatch.setattr(citation_verification, "_model_review", model)
    result, report = citation_verification.verify_citation_metadata({"title": "Draft title"}, document, "digital_pdf", {}, _config(citation_verifier_model="openai/gpt-5"))

    assert captured["field"] == "title"
    assert captured["draft"] == "Draft title"
    assert captured["registry"] == "Registry title"
    assert captured["evidence"] and all("title" not in item for item in captured["evidence"])
    assert result["title"] == "Draft title"
    assert report["status"] == "verified"


def test_print_date_source_wins_over_conflicting_online_registry_date(monkeypatch):
    monkeypatch.setattr(citation_verification, "lookup_crossref_doi", lambda *args, **kwargs: {"status": "found", "candidate": {"issued": {"date-parts": [[2020]]}}, "provenance": {"provider": "crossref"}})
    text = "Printed edition 2019; online publication 2020."
    document = {"structure": {"pages": [{"physical_page_index": 0, "paragraphs": [{"node_id": "n-date", "text": text}]}]}}

    result, report = citation_verification.verify_citation_metadata({"issued": {"date-parts": [[2019]]}}, document, "digital_pdf", {}, _config())

    assert result["issued"] == {"date-parts": [[2019]]}
    assert report["status"] == "needs_review"
    assert report["needs_review"][0]["source_value"] == {"date-parts": [[2019]]}
