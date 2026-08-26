import hashlib
from unittest.mock import Mock

import requests

from citeindex.ingestion.metadata_registry import (
    extract_doi,
    lookup_crossref_doi,
    normalize_doi,
)


def _response(status_code=200, payload=None, content=b'{"message":{}}'):
    response = Mock(status_code=status_code, content=content)
    response.json.return_value = payload if payload is not None else {"message": {}}
    return response


def test_normalize_and_extract_doi():
    assert normalize_doi("https://doi.org/10.1000/ABC.1.") == "10.1000/abc.1"
    assert extract_doi("See DOI: 10.5555/AbC_2 for details.") == "10.5555/abc_2"
    assert normalize_doi("not an identifier") is None


def test_crossref_exact_doi_returns_normalized_candidate_and_provenance():
    raw = b'{"message":"recorded only for digest"}'
    response = _response(
        payload={
            "message": {
                "DOI": "10.1000/ABC.1",
                "type": "journal-article",
                "title": ["An Article"],
                "author": [{"given": "Ada", "family": "Lovelace"}],
                "issued": {"date-parts": [[1843, 1, 1]]},
                "container-title": ["Journal"],
                "publisher": "Publisher",
                "page": "10-20",
            }
        },
        content=raw,
    )
    session = Mock()
    session.get.return_value = response

    result = lookup_crossref_doi("doi:10.1000/ABC.1", session=session)

    assert result["status"] == "found"
    assert result["candidate"] == {
        "type": "article-journal", "DOI": "10.1000/abc.1", "title": "An Article",
        "author": [{"given": "Ada", "family": "Lovelace"}],
        "issued": {"date-parts": [[1843, 1, 1]]}, "container-title": "Journal",
        "publisher": "Publisher", "page": "10-20",
    }
    assert result["provenance"] == {
        "provider": "crossref", "request_identifier": "10.1000/abc.1",
        "request_url": "https://api.crossref.org/works/10.1000/abc.1", "http_status": 200,
        "response_digest": hashlib.sha256(raw).hexdigest(),
    }
    assert "contact_email" not in result


def test_offline_and_disabled_paths_make_no_http_request():
    session = Mock()
    offline = lookup_crossref_doi("10.1000/example", offline_verification=True, session=session)
    disabled = lookup_crossref_doi("10.1000/example", crossref_enabled=False, session=session)

    assert offline["status"] == disabled["status"] == "skipped"
    session.get.assert_not_called()


def test_timeout_is_retried_once_then_reported_without_response():
    session = Mock()
    session.get.side_effect = requests.Timeout("slow")

    result = lookup_crossref_doi("10.1000/example", session=session, timeout=0.1)

    assert result["status"] == "error"
    assert result["error"] == "Timeout"
    assert result["provenance"]["response_digest"] is None
    assert session.get.call_count == 2


def test_malformed_and_doi_mismatch_responses_are_not_candidates():
    malformed = _response(payload={"message": []})
    mismatch = _response(payload={"message": {"DOI": "10.1000/other"}})
    session = Mock()
    session.get.side_effect = [malformed, mismatch]

    assert lookup_crossref_doi("10.1000/example", session=session)["error"] == "malformed_response"
    result = lookup_crossref_doi("10.1000/example", session=session)
    assert result["error"] == "doi_mismatch"
    assert result["candidate"] is None
