"""Identifier-first registry fill: discovery, fill rules, and off-by-default wiring.

No test here touches the network: every registry answer comes from a stub client.
"""

import json
import sys

import pytest

from citeindex import cli
from citeindex.ingestion import master
from citeindex.ingestion.models import IngestionConfig, PipelineResult
from citeindex.ingestion.registry_fill import (
    fill_from_registry,
    find_identifiers,
    isbn10_valid,
    isbn13_valid,
)


class StubClient:
    """Records calls and answers with a fixed candidate record per identifier."""

    def __init__(self, *, isbn_record=None, doi_record=None, error=None):
        self.isbn_record = isbn_record
        self.doi_record = doi_record
        self.error = error
        self.calls = []

    def lookup_isbn(self, isbn):
        self.calls.append(("isbn", isbn))
        if self.error is not None:
            raise self.error
        return self.isbn_record

    def lookup_doi(self, doi):
        self.calls.append(("doi", doi))
        if self.error is not None:
            raise self.error
        return self.doi_record


def as_bytes(record):
    return json.dumps(record, sort_keys=True, ensure_ascii=False)


# ── Identifier discovery ──────────────────────────────────────────────
def test_isbn13_check_digit_is_validated():
    assert isbn13_valid("9780306406157") is True
    assert isbn13_valid("9780306406158") is False


def test_isbn10_accepts_x_check_digit():
    assert isbn10_valid("0306406152") is True
    assert isbn10_valid("043942089X") is True
    assert isbn10_valid("0439420899") is False


def test_hyphenated_isbn_in_block_text_is_found():
    blocks = [{"text": "Imprint: ISBN 978-0-306-40615-7"}, {"text": "Later 978-0-13-468599-1"}]
    assert find_identifiers(blocks) == {"isbn": "9780306406157"}


def test_invalid_check_digit_is_ignored():
    assert find_identifiers([{"text": "ISBN 978-0-306-40615-8"}]) == {}


def test_doi_in_block_text_is_found():
    blocks = [{"text": "See https://doi.org/10.1000/Example.DOI-42."}]
    assert find_identifiers(blocks) == {"doi": "10.1000/example.doi-42"}


def test_isbn10_x_is_reported_as_printed():
    assert find_identifiers([{"text": "ISBN 0-439-42089-X (pbk.)"}]) == {"isbn": "043942089X"}


# ── Fill rules ────────────────────────────────────────────────────────
def test_only_empty_fields_are_filled():
    csl = {
        "type": "book",
        "title": "Printed Title",
        "publisher": None,
        "publisher-place": "",
        "_field_status": {"title": "source-supported", "publisher": "missing"},
    }
    client = StubClient(
        isbn_record={
            "title": "Registry Title",
            "publisher": "Registry Press",
            "publisher-place": "Oxford",
            "issued": {"date-parts": [[1999]]},
        }
    )
    filled, provenance = fill_from_registry(csl, {"isbn": "9780306406157"}, client=client, doc_type="book")

    assert filled["title"] == "Printed Title"
    assert filled["publisher"] == "Registry Press"
    assert filled["publisher-place"] == "Oxford"
    assert filled["issued"] == {"date-parts": [[1999]]}
    assert "title" not in provenance
    assert set(provenance) == {"publisher", "publisher-place", "issued"}


def test_source_supported_value_is_never_overwritten():
    csl = {
        "type": "book",
        "title": "Printed Title",
        "publisher": "Printed Press",
        "_field_status": {"title": "source-supported", "publisher": "source-supported"},
    }
    client = StubClient(isbn_record={"title": "Registry Title", "publisher": "Registry Press"})
    filled, provenance = fill_from_registry(csl, {"isbn": "9780306406157"}, client=client, doc_type="book")

    assert filled["title"] == "Printed Title"
    assert filled["publisher"] == "Printed Press"
    assert provenance == {}


def test_disagreement_is_recorded_as_printed_conflict():
    csl = {
        "type": "book",
        "title": "Printed Title",
        "publisher": "Printed Press",
        "_field_status": {"title": "source-supported", "publisher": "source-supported"},
    }
    client = StubClient(isbn_record={"title": "Printed Title", "publisher": "Registry Press"})
    filled, _ = fill_from_registry(csl, {"isbn": "9780306406157"}, client=client, doc_type="book")

    assert filled["_field_conflicts"] == {
        "publisher": {"printed": "Printed Press", "registry": "Registry Press", "chosen": "printed"}
    }
    assert "title" not in filled["_field_conflicts"]


def test_agreement_is_not_a_conflict():
    csl = {"type": "book", "publisher": "Printed Press", "_field_status": {"publisher": "source-supported"}}
    client = StubClient(isbn_record={"publisher": "  printed   press. "})
    filled, _ = fill_from_registry(csl, {"isbn": "9780306406157"}, client=client, doc_type="book")

    assert "_field_conflicts" not in filled


def test_provenance_records_source_and_identifier():
    csl = {"type": "article-journal", "title": "A Paper"}
    client = StubClient(
        doi_record={
            "title": "A Paper",
            "publisher": "Journal Press",
            "author": [{"family": "Doe", "given": "Jane"}],
        }
    )
    filled, provenance = fill_from_registry(csl, {"doi": "10.1000/example"}, client=client, doc_type="article-journal")

    assert provenance == {
        "publisher": {"source": "crossref", "identifier": "10.1000/example"},
        "author": {"source": "crossref", "identifier": "10.1000/example"},
    }
    assert filled["_field_provenance"] == provenance
    assert filled["author"] == [{"family": "Doe", "given": "Jane"}]


def test_identifier_lookup_order_follows_document_type():
    book_client = StubClient(isbn_record={"publisher": "Book Press"}, doi_record={"publisher": "Doi Press"})
    fill_from_registry({"type": "book"}, {"isbn": "9780306406157", "doi": "10.1000/x"}, client=book_client, doc_type="book")
    assert book_client.calls[0] == ("isbn", "9780306406157")

    article_client = StubClient(isbn_record={"publisher": "Book Press"}, doi_record={"publisher": "Doi Press"})
    fill_from_registry(
        {"type": "article-journal"}, {"isbn": "9780306406157", "doi": "10.1000/x"},
        client=article_client, doc_type="article-journal",
    )
    assert article_client.calls[0] == ("doi", "10.1000/x")


# ── Failure behaviour ─────────────────────────────────────────────────
def test_missing_client_leaves_record_untouched():
    csl = {"type": "book", "title": "Printed Title"}
    before = as_bytes(csl)
    filled, provenance = fill_from_registry(csl, {"isbn": "9780306406157"}, client=None, doc_type="book")

    assert filled is csl
    assert as_bytes(filled) == before
    assert provenance == {}


def test_empty_identifiers_leave_record_untouched():
    csl = {"type": "book", "title": "Printed Title"}
    before = as_bytes(csl)
    filled, provenance = fill_from_registry(csl, {}, client=StubClient(isbn_record={"title": "X"}), doc_type="book")

    assert filled is csl
    assert as_bytes(filled) == before
    assert provenance == {}


def test_client_exception_is_swallowed():
    csl = {"type": "book", "title": "Printed Title", "_field_status": {"title": "source-supported"}}
    before = as_bytes(csl)
    client = StubClient(error=RuntimeError("registry down"))
    filled, provenance = fill_from_registry(csl, {"isbn": "9780306406157"}, client=client, doc_type="book")

    assert as_bytes(filled) == before
    assert provenance == {}


def test_input_record_is_not_mutated():
    csl = {"type": "book", "publisher": None}
    before = as_bytes(csl)
    fill_from_registry(csl, {"isbn": "9780306406157"}, client=StubClient(isbn_record={"publisher": "P"}), doc_type="book")

    assert as_bytes(csl) == before


# ── Wiring ────────────────────────────────────────────────────────────
def test_cli_registry_fill_defaults_off(monkeypatch):
    captured = {}

    class Orchestrator:
        def __init__(self, **kwargs):
            captured["orchestrator"] = kwargs

        def ingest(self, input_ref, *, config):
            captured["config"] = config
            return {"status": "ok"}

    monkeypatch.setattr(cli, "CiteIndexIngestionOrchestrator", Orchestrator)
    monkeypatch.setattr(sys, "argv", ["citeindex", "source.pdf"])

    with pytest.raises(SystemExit, match="0"):
        cli.main()

    assert captured["config"].registry_fill is False


def test_config_flag_defaults_off():
    assert IngestionConfig().registry_fill is False
    assert IngestionConfig(registry_fill=True).registry_fill is True
    with pytest.raises(ValueError, match="registry_fill"):
        IngestionConfig(registry_fill="yes")


def _pipeline_result_with_isbn():
    return PipelineResult(
        status="ok",
        source_id="source",
        resource_type="digital_pdf",
        csl_json={"type": "book"},
        extra={"source_blocks": [{"text": "ISBN 978-0-306-40615-7"}]},
    )


def test_master_does_not_build_adapter_when_flag_is_off(monkeypatch, tmp_path):
    def fail(*args, **kwargs):
        raise AssertionError("adapter must not be constructed when registry_fill is off")

    monkeypatch.setattr(master, "_RegistryFillClient", fail)
    orchestrator = master.CiteIndexIngestionOrchestrator(corpus_root=str(tmp_path))
    csl = {"type": "book", "publisher": None}

    filled = orchestrator._apply_registry_fill(csl, _pipeline_result_with_isbn(), IngestionConfig(), "digital_pdf")

    assert filled is csl
    assert filled == {"type": "book", "publisher": None}


def test_master_uses_adapter_when_flag_is_on(monkeypatch, tmp_path):
    constructed = []

    class Adapter:
        def __init__(self, config, *, cache_dir=None, timeout=5.0):
            constructed.append(cache_dir)

        def lookup_isbn(self, isbn):
            return {"publisher": "Registry Press", "issued": {"date-parts": [[1994]]}}

        def lookup_doi(self, doi):
            return None

    monkeypatch.setattr(master, "_RegistryFillClient", Adapter)
    orchestrator = master.CiteIndexIngestionOrchestrator(corpus_root=str(tmp_path))
    csl = {"type": "book", "publisher": None, "_field_status": {"publisher": "missing"}}

    filled = orchestrator._apply_registry_fill(
        csl, _pipeline_result_with_isbn(), IngestionConfig(registry_fill=True), "digital_pdf"
    )

    assert constructed == [str(tmp_path / ".registry-cache")]
    assert filled["publisher"] == "Registry Press"
    assert filled["issued"] == {"date-parts": [[1994]]}
    assert filled["_field_provenance"]["publisher"] == {
        "source": "openlibrary",
        "identifier": "9780306406157",
    }
