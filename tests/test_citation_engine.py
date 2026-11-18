from citeindex.ingestion.models import IngestionConfig
from citeindex.ingestion.pipelines import common, dspy_extract


def test_evidence_selection_reaches_imprint_beyond_initial_window():
    text = "front matter " * 800 + "Published by Evidence Press. ISBN 978-1-234."

    selected = dspy_extract.select_source_evidence(text, budget=8000)

    assert "Evidence Press" in selected


def test_dspy_is_the_digital_metadata_engine(monkeypatch):
    called = []

    def extract(text, doc_type, config, **kwargs):
        called.append((text, doc_type))
        return {"title": "DSPy title"}

    monkeypatch.setattr(dspy_extract, "_run_dspy_extraction", extract)

    result = common.enrich_csl_with_citation_cascade(
        {"id": "source", "title": "fallback"}, "source evidence", None, 10, IngestionConfig(),
    )

    assert result["title"] == "DSPy title"
    assert result["_extraction_method"] == "dspy"
    assert called == [("source evidence", "journal")]
