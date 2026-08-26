import json

from citeindex.ingestion.master import CiteIndexIngestionOrchestrator
from citeindex.ingestion.models import IngestionConfig, PipelineResult
import citeindex.ingestion.master as master


def test_verification_is_persisted_before_csl_identity_and_markdown(tmp_path, monkeypatch):
    result = PipelineResult(
        status="ok",
        source_id="source",
        resource_type="digital_pdf",
        csl_json={"title": "Draft", "author": [{"family": "Doe"}], "type": "book"},
        document_json={"structure": {"pages": []}},
        merkle_tree={"root": "root"},
    )
    report = {"status": "verified", "corrections": [{"field": "title"}], "needs_review": []}
    monkeypatch.setattr(master.CiteIndexIngestionOrchestrator, "detect_resource_type", lambda *_args, **_kwargs: ("digital_pdf", "source.pdf"))
    monkeypatch.setattr(master.CiteIndexIngestionOrchestrator, "route_to_pipeline", lambda *_args, **_kwargs: result)
    monkeypatch.setattr(master, "verify_citation_metadata", lambda csl, *_args: ({**csl, "title": "Correct"}, report))

    output = CiteIndexIngestionOrchestrator(str(tmp_path / "corpus")).ingest(
        "source.pdf", IngestionConfig(verify_citations=True),
    )

    document_path = tmp_path / "corpus" / "doe_correct"
    persisted_csl = json.loads((document_path / "csl.json").read_text())
    persisted_report = json.loads((document_path / "citation_verification.json").read_text())
    persisted_output = json.loads((document_path / "ingestion_output.json").read_text())
    markdown = (tmp_path / "library" / "doe_correct.md").read_text()
    assert output["standardized_csl_json"]["title"] == persisted_csl["title"] == "Correct"
    assert persisted_report == persisted_output["citation_verification"] == report
    assert "citation_verification: verified" in markdown
