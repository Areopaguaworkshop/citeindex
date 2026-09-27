"""Fixture contracts for native search; live tests require an explicit opt-in."""

import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import requests

from citeindex.ingestion import ai_discovery as ai


FIXTURES = Path(__file__).parent / "fixtures" / "online_enrichment" / "ai"
CSL = {"title": "The structure of DNA", "author": [{"family": "Watson"}, {"family": "Crick"}]}
DOI = "10.1038/171737a0"
SOURCE = "https://www.nature.com/articles/171737a0"


def config(provider="openai", **kwargs):
    return SimpleNamespace(online_enrich_ai_fallback=True, online_enrich_ai_provider=provider,
                           online_enrich_ai_model=kwargs.pop("model", None),
                           online_enrich_ai_domains=kwargs.pop("domains", ("nature.com", "doi.org")),
                           offline_verification=False, **kwargs)


class Response:
    def __init__(self, payload, status=200, raw=None):
        self.raw_bytes = raw if raw is not None else json.dumps(payload).encode()
        self.status_code = status
        self.closed = False

    def iter_content(self, chunk_size):
        for offset in range(0, len(self.raw_bytes), chunk_size):
            yield self.raw_bytes[offset:offset + chunk_size]

    def close(self):
        self.closed = True


class Session:
    def __init__(self, payload=None, status=200, error=None, raw=None):
        self.response = Response(payload, status, raw)
        self.calls = []
        self.error = error

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return self.response


@pytest.fixture(autouse=True)
def keys(monkeypatch):
    for name in ai._KEYS.values():
        monkeypatch.setenv(name, "fixture-secret-key")


def payload(provider, case="valid"):
    return json.loads((FIXTURES / f"{provider}_{case}.json").read_text())


@pytest.mark.parametrize("provider", ai.DEFAULT_MODELS)
def test_search_backed_identifier_and_native_request(provider):
    session = Session(payload(provider))
    original = copy.deepcopy(CSL)
    result = ai.discover_identifier(CSL, config(provider), session=session)
    assert result["status"] == "found"
    assert CSL == original
    assert [(i["kind"], i["value"]) for i in result["identifiers"]] == [("DOI", DOI)]
    assert result["identifiers"][0]["evidence"]["source_url"] == SOURCE
    assert DOI in result["identifiers"][0]["evidence"]["supporting_span"]
    assert result["provenance"]["response_digest"] == hashlib.sha256(session.response.raw_bytes).hexdigest()
    assert result["provenance"]["search_count"] == 1
    assert "fixture-secret-key" not in json.dumps(result)
    assert session.response.closed
    assert len(session.calls) == 1
    url, options = session.calls[0]
    assert options["timeout"] == 5
    assert options["allow_redirects"] is False
    assert options["stream"] is True
    assert "?" not in url
    body = options["json"]
    if provider == "openai":
        assert body["tools"] == [{"type": "web_search", "external_web_access": True,
                                  "filters": {"allowed_domains": ["nature.com", "doi.org"]}}]
        assert body["tool_choice"] == "required"
        assert body["include"] == ["web_search_call.action.sources"]
        assert body["store"] is False
    elif provider == "claude":
        assert body["tools"][0]["type"] == "web_search_20250305"
        assert body["tools"][0]["max_uses"] == 3
        assert body["tools"][0]["allowed_domains"] == ["nature.com", "doi.org"]
        assert body["tool_choice"] == {"type": "tool", "name": "web_search"}
    else:
        assert body["tools"] == [{"google_search": {}}]
        assert "generateContent" in url
        assert "allowed_domains" not in json.dumps(body["tools"])


@pytest.mark.parametrize("provider", ai.DEFAULT_MODELS)
@pytest.mark.parametrize("case", ["no_search", "fabricated", "off_domain", "uncited", "incomplete", "malformed", "tool_error"])
def test_untrusted_search_evidence_abstains(provider, case):
    result = ai.discover_identifier(CSL, config(provider), session=Session(payload(provider, case)))
    assert result["status"] in {"error", "unapplied"}
    assert result["identifiers"] == []


@pytest.mark.parametrize("provider", ai.DEFAULT_MODELS)
def test_null_is_not_found_only_after_search(provider):
    result = ai.discover_identifier(CSL, config(provider), session=Session(payload(provider, "null")))
    assert result["status"] == "not_found"
    assert result["provenance"]["search_count"] == 1


def test_openai_cache_only_is_rejected():
    result = ai.discover_identifier(CSL, config(), session=Session(payload("openai", "cache_only")))
    assert result["error"] == "live_search_required"


@pytest.mark.parametrize("provider", ai.DEFAULT_MODELS)
@pytest.mark.parametrize("failure", ["timeout", "quota", "transport", "oversize", "invalid_json"])
def test_bounded_request_failures_are_sanitized(provider, failure):
    session = Session(payload(provider))
    expected = failure
    if failure == "timeout":
        session.error = requests.Timeout("fixture-secret-key in https://api.test/?api_key=bad")
    elif failure == "transport":
        session.error = requests.ConnectionError("fixture-secret-key")
        expected = "request_failed"
    elif failure == "quota":
        session.response.status_code = 429
        expected = "quota_exceeded"
    elif failure == "oversize":
        session.response.raw_bytes = b"x" * (ai._MAX_BYTES + 1)
        expected = "response_too_large"
    else:
        session.response.raw_bytes = b"not JSON fixture-secret-key"
        expected = "malformed_response"
    result = ai.discover_identifier(CSL, config(provider), session=session)
    assert result["status"] == "error"
    assert result["error"] == expected
    assert "fixture-secret-key" not in json.dumps(result)
    assert len(session.calls) == 1


def test_config_rejects_unpinned_models_and_unsafe_domains():
    for cfg in [config("other"), config(model="https://evil.test/model"), config("gemini", model="gemini-1.5-pro"),
                config(model=""), config(model=False),
                config(domains=()), config(domains=("https://nature.com",)), config(domains=("localhost",)),
                config(domains=("127.0.0.1",)), config(domains=("nature.com", "nature.com"))]:
        with pytest.raises(ValueError):
            ai.validate_ai_config(cfg)
    for provider, models in ai.SUPPORTED_MODELS.items():
        for model in models:
            ai.validate_ai_config(config(provider, model=model))


def test_disabled_offline_missing_credentials_and_title_do_not_request(monkeypatch):
    for provider in ai.DEFAULT_MODELS:
        session = Session(payload(provider))
        cfg = config(provider)
        cfg.online_enrich_ai_fallback = False
        assert ai.discover_identifier(CSL, cfg, session=session)["error"] == "ai_discovery_disabled"
        cfg.online_enrich_ai_fallback = True
        cfg.offline_verification = True
        assert ai.discover_identifier(CSL, cfg, session=session)["error"] == "offline_verification"
        cfg.offline_verification = False
        assert ai.discover_identifier({}, cfg, session=session)["error"] == "missing_title"
        monkeypatch.delenv(ai._KEYS[provider])
        assert ai.discover_identifier(CSL, cfg, session=session)["error"] == "missing_api_key"
        assert session.calls == []
        ai.validate_ai_config(cfg)  # Environment values do not affect config validity.


def test_gemini_redirect_requires_budgeted_resolver_and_valid_destination():
    data = payload("gemini")
    proxy = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/opaque"
    data["candidates"][0]["groundingMetadata"]["groundingChunks"][0]["web"]["uri"] = proxy
    assert ai.discover_identifier(CSL, config("gemini"), session=Session(data))["status"] == "unapplied"
    calls = []

    def resolve(url, timeout):
        calls.append((url, timeout))
        return SOURCE

    result = ai.discover_identifier(CSL, config("gemini"), session=Session(data), resolve_source_url=resolve)
    assert result["status"] == "found"
    assert calls[0][0] == proxy and 0 < calls[0][1] <= 5
    assert result["identifiers"][0]["evidence"]["grounding_url"] == proxy
    result = ai.discover_identifier(CSL, config("gemini"), session=Session(data),
                                    resolve_source_url=lambda *_: "http://127.0.0.1/private")
    assert result["status"] == "unapplied"


def test_gemini_grounding_offsets_use_utf8_bytes_and_validate_segment():
    data = payload("gemini")
    candidate = data["candidates"][0]
    text = "文献 DOI " + DOI
    candidate["content"]["parts"][0]["text"] = text
    segment = candidate["groundingMetadata"]["groundingSupports"][0]["segment"]
    segment.update(text=text, startIndex=0, endIndex=len(text.encode()))
    assert ai.discover_identifier(CSL, config("gemini"), session=Session(data))["status"] == "found"
    segment["text"] = "unrelated " + DOI
    assert ai.discover_identifier(CSL, config("gemini"), session=Session(data))["status"] == "unapplied"


def test_only_identifiers_inside_cited_output_are_returned():
    data = payload("openai")
    block = data["output"][1]["content"][0]
    block["text"] += " Uncited ISBN 9780306406157"
    result = ai.discover_identifier(CSL, config(), session=Session(data))
    assert [(i["kind"], i["value"]) for i in result["identifiers"]] == [("DOI", DOI)]


def test_isbn_checksum_and_evidence_urls():
    assert ai._identifiers("ISBN 978-0-306-40615-7") == [("ISBN", "9780306406157")]
    assert ai._identifiers("ISBN 978-0-306-40615-8") == []
    for url in ["https://nature.com.evil.test/a", "https://nature.com@evil.test/a", "http://nature.com/a",
                "https://nature.com:8080/a", "https://127.0.0.1/a", "https://nature.com/a\n"]:
        assert ai._safe_url(url, ("nature.com",)) is None
    assert ai._safe_url(SOURCE + "?api_key=secret#token", ("nature.com",)) == SOURCE
    assert ai._safe_url(SOURCE + "/fixture-secret-key", ("nature.com",)) is None


def test_gemini_pro_keeps_required_thinking():
    session = Session(payload("gemini"))
    ai.discover_identifier(CSL, config("gemini", model="gemini-2.5-pro"), session=session)
    assert "thinkingConfig" not in session.calls[0][1]["json"]["generationConfig"]


def test_deadline_is_checked_while_reading(monkeypatch):
    ticks = iter([0.0, 6.0])
    monkeypatch.setattr(ai.time, "monotonic", lambda: next(ticks))
    session = Session(payload("openai"))
    result = ai.discover_identifier(CSL, config(), session=session, timeout=5)
    assert result["error"] == "timeout"
    assert session.response.closed


def test_queries_and_source_quotes_redact_secrets_and_contact_data():
    data = payload("claude")
    query = "DOI " + DOI + " fixture-secret-key contact@example.org"
    data["content"][0]["input"]["query"] = query
    data["content"][-1]["citations"][0]["cited_text"] = query
    result = ai.discover_identifier(CSL, config("claude"), session=Session(data))
    assert result["status"] == "found"
    assert "fixture-secret-key" not in json.dumps(result)
    assert "contact@example.org" not in json.dumps(result)


def test_opt_in_required_for_live_smoke(monkeypatch):
    monkeypatch.delenv("CITEINDEX_AI_LIVE_SMOKE", raising=False)
    with pytest.raises(ValueError, match="CITEINDEX_AI_LIVE_SMOKE"):
        ai.smoke_discovery("openai")


@pytest.mark.skipif(os.environ.get("CITEINDEX_AI_LIVE_SMOKE") != "1", reason="explicit live smoke opt-in required")
@pytest.mark.parametrize("provider", ai.DEFAULT_MODELS)
def test_live_search_smoke(provider, monkeypatch):
    # The synthetic-key fixture must not turn an opted-in test into a fake request.
    monkeypatch.undo()
    if not os.environ.get(ai._KEYS[provider]):
        pytest.skip("provider credential is unavailable")
    result = ai.smoke_discovery(provider)
    assert result["status"] == "found", result
    assert result["provenance"]["search_count"] > 0
    assert any(i["kind"] == "DOI" and i["value"] == DOI for i in result["identifiers"])
