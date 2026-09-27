"""Offline checks for bounded registry transport and authority identity."""
import hashlib
import json
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path

import pytest
import requests

from citeindex.ingestion.metadata_registry import RegistryClient, bounded_response_bytes


FIXTURES = json.loads((Path(__file__).parent / "fixtures/online_enrichment/registry_responses.json").read_text())
ISBN = "9780199535569"


class Clock:
    def __init__(self):
        self.now = 0.0
        self.waits = []

    def __call__(self):
        return self.now

    def sleep(self, delay):
        self.waits.append(delay)
        self.now += delay


class Response:
    def __init__(self, payload=None, status=200, headers=None, chunks=None, tick=None):
        self.status_code = status
        self.headers = headers or {}
        self.raw = json.dumps(payload).encode()
        self.chunks = chunks if chunks is not None else [self.raw]
        self.tick = tick
        self.closed = False

    def iter_content(self, chunk_size):
        for chunk in self.chunks:
            if self.tick:
                self.tick()
            yield chunk

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


def client(*responses, config=None, cache_dir=None):
    clock = Clock()
    session = Session(*responses)
    instance = RegistryClient(config or {}, cache_dir, session=session, clock=clock, sleep=clock.sleep)
    return instance, session, clock


def crossref(doi="10.1000/one"):
    return {"message": {"DOI": doi, "title": ["Registry Identity"]}}


@pytest.mark.parametrize("provider", list(FIXTURES))
@pytest.mark.parametrize("kind,status", [("positive", "found"), ("empty", "not_found"), ("malformed", "error")])
def test_all_catalog_search_contracts(provider, kind, status):
    responses = [Response(FIXTURES[provider][kind])]
    if provider == "openlibrary" and kind == "positive":
        responses += [Response(FIXTURES[provider]["edition"]), Response(FIXTURES[provider]["author"])]
    instance, session, _ = client(*responses)
    results = instance.search(provider, "Registry Identity", "Ada Smith", 2024)
    assert len(results) <= 5
    assert results[0]["status"] == status
    assert all(call[1]["stream"] and not call[1]["allow_redirects"] for call in session.calls)
    if status == "found":
        assert results[0]["candidate"]["title"] == "Registry Identity"
        assert results[0]["candidate"]["issued"]["date-parts"][0][0] == 2024
        assert results[0]["candidate"]["author"]
        assert results[0]["provenance"]["source_id"]
        if provider == "openlibrary":
            assert results[0]["candidate"]["ISBN"] == ISBN
            assert "search" in results[0]["provenance"]
            assert "hydration" in results[0]["provenance"]


@pytest.mark.parametrize("provider", ["crossref", "datacite", "openalex"])
def test_doi_roundtrip_and_mismatch(provider):
    record = FIXTURES[provider]["positive"]
    payload = {"message": record["message"]["items"][0]} if provider == "crossref" else {"data": record["data"][0]} if provider == "datacite" else record["results"][0]
    instance, _, _ = client(Response(payload), Response(payload))
    assert instance.lookup(provider, "https://doi.org/10.1000/ONE")["status"] == "found"
    wrong = instance.lookup(provider, "10.1000/two")
    assert wrong["error"] == "doi_mismatch"
    assert wrong["candidate"] is None


def test_datacite_text_thesis_specific_resource_type():
    instance, _, _ = client(Response(FIXTURES["datacite"]["thesis"]))
    result = instance.lookup("datacite", "10.1000/thesis")
    assert result["status"] == "found" and result["candidate"]["type"] == "thesis"


def test_strict_isbn_membership_and_author_hydration():
    edition = FIXTURES["openlibrary"]["edition"]
    instance, _, clock = client(Response(edition), Response(FIXTURES["openlibrary"]["author"]), Response({**edition, "isbn_13": []}))
    found = instance.lookup("openlibrary", ISBN)
    assert found["candidate"]["author"] == [{"literal": "Ada Smith"}]
    assert found["provenance"]["source_id"] == "/books/OL1M"
    assert clock.waits == [1.0]
    mismatch = instance.lookup("openlibrary", ISBN, fresh=True)
    assert mismatch["error"] == "isbn_mismatch"
    assert mismatch["candidate"] is None


def test_work_search_without_edition_cannot_inject_work_isbn():
    instance, session, _ = client(Response({"docs": [{"title": "Wrong work", "isbn": [ISBN], "first_publish_year": 1900}]}))
    assert instance.search("openlibrary", "Title")[0]["status"] == "not_found"
    assert len(session.calls) == 1


def test_offline_and_provider_disabled_do_not_fetch_or_read_cache(tmp_path):
    instance, session, _ = client(config={"offline_verification": True}, cache_dir=tmp_path)
    assert instance.lookup("crossref", "10.1000/one")["status"] == "skipped"
    assert instance.search("openlibrary", "Title")[0]["status"] == "skipped"
    instance.config = {"crossref_enabled": False, "online_enrich_providers": ["openlibrary"]}
    assert instance.lookup("crossref", "10.1000/one")["status"] == "skipped"
    assert instance.search("datacite", "Title")[0]["status"] == "skipped"
    assert not session.calls


def test_cache_digest_redaction_fresh_and_corruption(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENALEX_API_KEY", "secret-key")
    payload = FIXTURES["openalex"]["positive"]["results"][0]
    response = Response(payload)
    instance, session, _ = client(response, Response(payload), config={"registry_contact_email": "secret@example.com"}, cache_dir=tmp_path)
    first = instance.lookup("openalex", "10.1000/one")
    assert first["provenance"]["response_digest"] == hashlib.sha256(response.raw).hexdigest()
    assert instance.lookup("openalex", "10.1000/one")["provenance"]["cache_hit"]
    assert instance.cache_hits == 1 and len(session.calls) == 1
    assert session.calls[0][1]["params"]["api_key"] == "secret-key"
    persisted = "".join(path.read_text() for path in tmp_path.iterdir())
    assert "secret-key" not in persisted and "secret@example.com" not in persisted
    assert "authorships" not in persisted  # No provider body snapshot.
    assert instance.lookup("openalex", "10.1000/one", fresh=True)["status"] == "found"
    other, other_session, _ = client(cache_dir=tmp_path)
    assert other.lookup("openalex", "10.1000/one")["provenance"]["cache_hit"]
    assert not other_session.calls
    for path in tmp_path.iterdir():
        path.write_text("broken json")
    broken, broken_session, _ = client(Response(payload), cache_dir=tmp_path)
    assert broken.lookup("openalex", "10.1000/one")["status"] == "found"
    assert len(broken_session.calls) == 1


def test_not_found_ttl_error_no_cache_and_unwritable_cache(tmp_path):
    instance, session, _ = client(Response(status=404), Response(status=400), Response(crossref()), cache_dir=tmp_path)
    assert instance.lookup("crossref", "10.1000/missing")["status"] == "not_found"
    files = list(tmp_path.iterdir())
    entry = json.loads(files[0].read_text())
    import time
    assert 3500 < entry["expires_at"] - time.time() <= 3600
    assert instance.lookup("crossref", "10.1000/error")["status"] == "error"
    assert len(list(tmp_path.iterdir())) == 1
    instance.cache_dir = files[0]  # A file cannot be a cache directory.
    assert instance.lookup("crossref", "10.1000/one")["status"] == "found"


def test_retry_once_transient_only_and_retry_after_budget():
    instance, session, clock = client(Response(status=429, headers={"Retry-After": "2"}), Response(crossref()))
    assert instance.lookup("crossref", "10.1000/one")["status"] == "found"
    assert clock.waits == [2.0] and instance.request_count == 2
    assert all(call[1]["timeout"] <= 5 for call in session.calls)
    exhausted, rejected_session, _ = client(Response(status=503, headers={"Retry-After": "30"}))
    assert exhausted.lookup("crossref", "10.1000/one")["status"] == "budget_exhausted"
    assert len(rejected_session.calls) == 1
    no_retry, no_retry_session, _ = client(Response(status=401))
    assert no_retry.lookup("crossref", "10.1000/one")["status"] == "error"
    assert len(no_retry_session.calls) == 1


def test_http_date_and_provider_rate_limit_headers():
    retry_date = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=30), usegmt=True)
    instance, _, _ = client(Response(status=429, headers={"Retry-After": retry_date}))
    assert instance.lookup("crossref", "10.1000/one")["status"] == "budget_exhausted"
    paced, _, clock = client(Response(crossref(), headers={"X-Rate-Limit-Limit": "1", "X-Rate-Limit-Interval": "3s"}), Response(crossref("10.1000/two")))
    assert paced.lookup("crossref", "10.1000/one")["status"] == "found"
    assert paced.lookup("crossref", "10.1000/two")["status"] == "found"
    assert clock.waits == [3.0]


def test_transport_retry_and_no_exception_text_leak():
    instance, session, _ = client(requests.Timeout("api_key=secret"), requests.Timeout("api_key=secret"))
    result = instance.lookup("crossref", "10.1000/one")
    assert result["status"] == "error" and result["error"] == "Timeout"
    assert len(session.calls) == 2
    assert "secret" not in json.dumps(instance.attempts)


def test_size_stream_deadline_and_attempt_budget():
    too_large, _, _ = client(Response(chunks=[b"x" * (2 * 1024 * 1024 + 1)]))
    assert too_large.lookup("crossref", "10.1000/one")["error"] == "response_too_large"
    declared, _, _ = client(Response(headers={"Content-Length": str(2 * 1024 * 1024 + 1)}))
    assert declared.lookup("crossref", "10.1000/one")["error"] == "response_too_large"
    timed, _, clock = client()
    response = Response(crossref(), tick=lambda: clock.sleep(21))
    timed.session.responses.append(response)
    assert timed.lookup("crossref", "10.1000/one")["status"] == "budget_exhausted"
    assert response.closed
    attempts, session, _ = client(*[Response(status=404) for _ in range(12)])
    for n in range(12):
        assert attempts.lookup("crossref", f"10.1000/missing{n}")["status"] == "not_found"
    assert attempts.lookup("crossref", "10.1000/last")["status"] == "budget_exhausted"
    assert len(session.calls) == 12
    with pytest.raises(ValueError, match="budget_exhausted"):
        attempts.reserve_request()


def test_external_request_reservation_and_remaining_timeout():
    instance, _, clock = client(config={"online_enrich_timeout": 2})
    assert instance.reserve_request() == 2
    clock.sleep(1.5)
    assert instance.reserve_request() == 0.5
    assert instance.request_count == 2


def test_search_five_distinct_identity_limit_and_author_hydration_bound():
    records = [{"DOI": f"10.1000/{n}", "title": ["Title"]} for n in range(9)]
    instance, _, _ = client(Response({"message": {"items": records}}))
    assert len(instance.search("crossref", "Title")) == 5
    edition = {**FIXTURES["openlibrary"]["edition"], "authors": [{"key": f"/authors/OL{n}A"} for n in range(6)]}
    bounded, session, _ = client(Response(edition), *[Response({"key": f"/authors/OL{n}A", "name": str(n)}) for n in range(3)])
    result = bounded.lookup("openlibrary", ISBN)
    assert "author" not in result["candidate"] and len(session.calls) == 4
    assert result["provenance"]["author_hydration_incomplete"]


def test_bounded_read1_shrinks_timeout_and_checks_each_available_read():
    from types import SimpleNamespace

    clock = Clock()
    timeouts = []
    response = requests.Response()
    response.status_code = 200
    calls = []

    def read1(size, decode_content):
        calls.append((size, decode_content))
        clock.sleep(0.6)
        return b"x"

    response.raw = SimpleNamespace(_connection=SimpleNamespace(sock=SimpleNamespace(settimeout=timeouts.append)), read1=read1)
    with pytest.raises(ValueError, match="budget_exhausted"):
        bounded_response_bytes(response, 1.0, clock)
    assert timeouts == [1.0, 0.4]
    assert len(calls) == 2 and calls[0][1] is False
    response.raw = SimpleNamespace(read1=read1)
    with pytest.raises(ValueError, match="unsupported_bounded_stream"):
        bounded_response_bytes(response, 2.0, clock)


def test_real_requests_slow_drip_stops_at_deadline():
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "100")
            self.end_headers()
            for _ in range(100):
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
                time.sleep(0.04)

        def log_message(self, *args):
            pass

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    except PermissionError:
        pytest.skip("Sandbox does not permit loopback listeners")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with requests.get(f"http://127.0.0.1:{server.server_port}", stream=True,
                          headers={"Accept-Encoding": "identity"}, timeout=1) as response:
            start = time.monotonic()
            with pytest.raises((ValueError, requests.Timeout)):
                bounded_response_bytes(response, start + 0.2)
            assert time.monotonic() - start < 0.5
    finally:
        server.shutdown()
        server.server_close()
