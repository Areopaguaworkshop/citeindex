import hashlib
from unittest.mock import Mock

import requests

from citeindex.ingestion.metadata_registry import (
    extract_doi,
    lookup_crossref_doi,
    lookup_openlibrary_isbn,
    normalize_doi,
    normalize_isbn,
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


# 978-0-19-953556-9 has a valid mod-10 checksum (unlike the corpus fixture).
_VALID_ISBN = "978-0-19-953556-9"
_NORMALIZED_ISBN = "9780199535569"


def test_normalize_isbn_strips_prefixes_and_separators_and_validates():
    assert normalize_isbn("ISBN: " + _VALID_ISBN) == _NORMALIZED_ISBN
    assert normalize_isbn("isbn-13 9780425278437") == "9780425278437"
    assert normalize_isbn("978-0-19-953566-9") is None  # bad checksum
    assert normalize_isbn("978-1-234") is None  # too short
    assert normalize_isbn(None) is None


def test_openlibrary_isbn_returns_normalized_candidate_and_provenance():
    raw = b'{"recorded only for digest"}'
    response = _response(
        payload={
            "title": "The Book",
            "subtitle": "A Study",
            "publishers": ["Press One", "Press Two"],
            "publish_places": ["London", "Oxford"],
            "publish_date": "2015.",
            "key": "/books/OL1M",
        },
        content=raw,
    )
    session = Mock()
    session.get.return_value = response

    result = lookup_openlibrary_isbn("ISBN " + _VALID_ISBN, session=session)

    assert result["status"] == "found"
    assert result["candidate"] == {
        "type": "book", "ISBN": _NORMALIZED_ISBN, "title": "The Book", "subtitle": "A Study",
        "publisher": "Press One and Press Two", "publisher-place": "London and Oxford",
        "issued": {"date-parts": [[2015]]},
    }
    assert result["provenance"] == {
        "provider": "openlibrary", "request_identifier": _NORMALIZED_ISBN,
        "request_url": f"https://openlibrary.org/isbn/{_NORMALIZED_ISBN}.json", "http_status": 200,
        "response_digest": hashlib.sha256(raw).hexdigest(),
    }
    assert session.get.call_args.kwargs["headers"] == {"User-Agent": "CiteIndex/0.12"}


def test_openlibrary_isbn_not_found_and_invalid():
    session = Mock()
    session.get.return_value = _response(status_code=404, content=b"")
    assert lookup_openlibrary_isbn(_VALID_ISBN, session=session)["status"] == "not_found"

    invalid = lookup_openlibrary_isbn("978-0-19-953566-9", session=session)
    assert invalid["status"] == "not_requested"
    assert invalid["error"] == "invalid_isbn"
    assert invalid["provenance"]["provider"] == "openlibrary"
    session.get.assert_called_once()  # only the 404 lookup hit the network


def test_openlibrary_offline_and_disabled_paths_make_no_http_request():
    session = Mock()
    offline = lookup_openlibrary_isbn(_VALID_ISBN, offline_verification=True, session=session)
    disabled = lookup_openlibrary_isbn(_VALID_ISBN, openlibrary_enabled=False, session=session)

    assert offline["status"] == disabled["status"] == "skipped"
    assert offline["error"] == "offline_verification"
    assert disabled["error"] == "openlibrary_disabled"
    session.get.assert_not_called()


def test_openlibrary_malformed_json_is_an_error():
    response = Mock(status_code=200, content=b"not json")
    response.json.side_effect = ValueError("no json")
    session = Mock()
    session.get.return_value = response

    result = lookup_openlibrary_isbn(_VALID_ISBN, session=session)

    assert result["status"] == "error"
    assert result["error"] == "malformed_response"


def test_openlibrary_request_failure_is_retried_once_then_succeeds():
    raw = b'{"title": "The Book"}'
    session = Mock()
    session.get.side_effect = [requests.Timeout("slow"), _response(payload={"title": "The Book"}, content=raw)]

    result = lookup_openlibrary_isbn(_VALID_ISBN, session=session, timeout=0.1)

    assert result["status"] == "found"
    assert result["candidate"]["title"] == "The Book"
    assert session.get.call_count == 2
