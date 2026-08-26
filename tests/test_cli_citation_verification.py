import argparse
import sys

import pytest

from citeindex import cli


def test_verification_options_are_wired_to_ingestion_config(monkeypatch):
    captured = {}

    class Orchestrator:
        def __init__(self, **kwargs):
            captured["orchestrator"] = kwargs

        def ingest(self, input_ref, *, config):
            captured["input"] = input_ref
            captured["config"] = config
            return {"status": "ok"}

    monkeypatch.setattr(cli, "CiteIndexIngestionOrchestrator", Orchestrator)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "citeindex",
            "source.pdf",
            "--verify-citations",
            "--citation-verifier-model",
            "openai/gpt-5",
            "--no-crossref",
            "--offline-verification",
            "--registry-contact-email",
            "registry@example.org",
        ],
    )

    with pytest.raises(SystemExit, match="0"):
        cli.main()

    config = captured["config"]
    assert config.verify_citations is True
    assert config.citation_verifier_model == "openai/gpt-5"
    assert config.crossref_enabled is False
    assert config.offline_verification is True
    assert config.registry_contact_email == "registry@example.org"


@pytest.mark.parametrize("model", ["gpt-5", "/gpt-5", "openai/"])
def test_citation_verifier_model_must_be_provider_qualified(model):
    with pytest.raises(argparse.ArgumentTypeError, match="provider-qualified"):
        cli._provider_qualified_model(model)


@pytest.mark.parametrize("email", ["not-an-email", "name@example"])
def test_registry_contact_email_must_be_valid(email):
    with pytest.raises(argparse.ArgumentTypeError, match="valid email"):
        cli._contact_email(email)
