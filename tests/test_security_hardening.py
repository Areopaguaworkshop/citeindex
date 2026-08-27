from types import SimpleNamespace

import pytest

from citeindex.ingestion.citation_verification import _find_evidence, verify_citation_metadata
from citeindex.ingestion.master import CiteIndexIngestionOrchestrator
from citeindex.ingestion.pipelines.common import attach_evidence_locators
from citeindex.ingestion.pipelines.media import run as run_media
from citeindex.ingestion.storage import store_corpus_artifacts
from citeindex.ingestion.url_security import UnsafeUrlError, fetch_text, validate_public_url


def _config(**changes):
    values = {
        "crossref_enabled": True,
        "offline_verification": False,
        "registry_contact_email": None,
        "citation_verifier_model": None,
    }
    values.update(changes)
    return SimpleNamespace(**values)


def test_private_url_is_rejected_before_routing(tmp_path):
    orchestrator = CiteIndexIngestionOrchestrator(str(tmp_path / "corpus"))
    assert orchestrator.detect_resource_type("http://127.0.0.1/private")[0] == "unsupported"
    with pytest.raises(UnsafeUrlError):
        validate_public_url("http://169.254.169.254/latest/meta-data/")
    with pytest.raises(UnsafeUrlError):
        run_media("http://127.0.0.1/internal.mp3")


def test_artifact_folder_name_cannot_escape_corpus(tmp_path):
    with pytest.raises(ValueError):
        store_corpus_artifacts(str(tmp_path), "../outside", {})


def test_fetch_text_revalidates_redirects_and_bounds_body():
    class Response:
        def __init__(self, status, headers, chunks):
            self.status_code = status
            self.headers = headers
            self._chunks = chunks
            self.encoding = "utf-8"

        def close(self):
            pass

        def raise_for_status(self):
            if self.status_code >= 400:
                raise ValueError("HTTP error")

        def iter_content(self, chunk_size):
            return iter(self._chunks)

    class Session:
        def get(self, url, **kwargs):
            assert kwargs["allow_redirects"] is False
            return Response(302, {"Location": "http://127.0.0.1/private"}, [])

    with pytest.raises(UnsafeUrlError):
        fetch_text("http://8.8.8.8/article", session=Session())


def test_not_found_registry_is_not_verified(monkeypatch):
    import citeindex.ingestion.citation_verification as verification

    monkeypatch.setattr(
        verification,
        "lookup_crossref_doi",
        lambda *args, **kwargs: {"status": "not_found", "candidate": None, "provenance": {}},
    )
    _, report = verify_citation_metadata(
        {"DOI": "10.1000/missing"}, {"structure": {"pages": []}}, "digital_pdf", {}, _config(),
    )
    assert report["status"] == "needs_review"
    assert report["verified"] is False


def test_structured_date_requires_a_complete_date_shape():
    evidence = [{"quote": "Published 2019 in volume 11", "locator": {}}]
    assert _find_evidence({"date-parts": [[2019, 1, 1]]}, evidence) is None
    assert _find_evidence({"date-parts": [[2019]]}, evidence) == evidence[0]


def test_duplicate_paragraphs_receive_distinct_nodes():
    document = {"pages": [{"page_number": 1, "paragraphs": [{"text": "same"}, {"text": "same"}]}]}
    nodes = [{"node_id": "n1", "page": 1, "text": "same"}, {"node_id": "n2", "page": 1, "text": "same"}]
    attach_evidence_locators(document, nodes)
    assert [paragraph["node_id"] for paragraph in document["pages"][0]["paragraphs"]] == ["n1", "n2"]
