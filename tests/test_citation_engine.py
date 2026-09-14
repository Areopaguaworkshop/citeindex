from citeindex.ingestion.models import IngestionConfig
from citeindex.ingestion.pipelines import common, dspy_extract


def test_evidence_selection_reaches_imprint_beyond_initial_window():
    text = "front matter " * 800 + "Published by Evidence Press. ISBN 978-1-234."

    selected = dspy_extract.select_source_evidence(text, budget=8000)

    assert "Evidence Press" in selected


def test_dspy_is_the_default_digital_metadata_engine(monkeypatch):
    called = []

    def extract(text, doc_type, config):
        called.append((text, doc_type, config.citation_engine))
        return {"title": "DSPy title"}

    monkeypatch.setattr(dspy_extract, "_run_dspy_extraction", extract)
    monkeypatch.setattr(common, "_extract_citation_grobid", lambda _: (_ for _ in ()).throw(AssertionError("GROBID must not run")))

    result = common.enrich_csl_with_citation_cascade(
        {"id": "source", "title": "fallback"}, "source evidence", None, 10, IngestionConfig(),
    )

    assert result["title"] == "DSPy title"
    assert result["_extraction_method"] == "dspy"
    assert called == [("source evidence", "journal", "dspy")]


def test_grobid_only_runs_when_explicitly_selected(monkeypatch):
    monkeypatch.setattr(common, "_extract_citation_grobid", lambda _: {"title": "GROBID title"})
    monkeypatch.setattr(dspy_extract, "_run_dspy_extraction", lambda *_: (_ for _ in ()).throw(AssertionError("DSPy must not run")))

    result = common.enrich_csl_with_citation_cascade(
        {"id": "source", "title": "fallback"}, "source evidence", "paper.pdf", 10,
        IngestionConfig(citation_engine="grobid"),
    )

    assert result["title"] == "GROBID title"
    assert result["_extraction_method"] == "grobid"
