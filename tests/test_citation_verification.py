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
    document = {"structure": {"pages": [{"page_number": "xii", "paragraphs": [{"paragraph_id": "p1_1", "text": "Correct Title. DOI: 10.1000/example."}]}]}}

    csl, report = citation_verification.verify_citation_metadata(
        {"title": "Draft Title"}, document, "digital_pdf", {}, _config(),
    )

    assert csl["title"] == "Correct Title"
    assert csl["DOI"] == "10.1000/example"
    assert "publisher" not in csl
    assert report["status"] == "needs_review"
    assert report["corrections"][0]["locator"] == {"paragraph_id": "p1_1", "physical_page_index": 1, "printed_page_label": "xii"}


def test_url_evidence_uses_snapshot_and_paragraph_locator(tmp_path, monkeypatch):
    snapshot = tmp_path / "source.html"
    snapshot.write_text("<html>Correct Title</html>", encoding="utf-8")
    monkeypatch.setattr(
        citation_verification,
        "lookup_crossref_doi",
        lambda *args, **kwargs: {"status": "found", "candidate": {"title": "Correct Title"}, "provenance": {"provider": "crossref"}},
    )
    document = {"structure": {"pages": [{"paragraphs": [{"paragraph_id": "p1_1", "text": "Correct Title"}]}]}}

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
