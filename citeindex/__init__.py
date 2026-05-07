"""
CiteIndex — ingest sources with proper citation.

Primary API:

    from citeindex import ingest
    result = ingest("paper.pdf")
    result = ingest("https://example.com/article", config=IngestionConfig(...))

CLI:

    citeindex paper.pdf
    citeindex https://example.com/article
"""

__version__ = "0.12.0"

from citeindex.ingestion import CiteIndexIngestionOrchestrator
from citeindex.ingestion.models import IngestionConfig, IngestionFailure, PipelineResult
from citeindex.ingestion.pdf_classifier import (
    PDFClassification,
    PageClassification,
    DocumentKind,
    PageKind,
    classify_pdf,
    pdf_kind,
)


def ingest(
    input_ref: str,
    corpus_root: str = "corpus",
    schema_version: str = "1.0.0",
    config: IngestionConfig | None = None,
) -> dict:
    """Ingest a file or URL and return structured citation data.

    Parameters
    ----------
    input_ref : str
        Path to a local file (PDF, Office, DJVU, media) or URL.
    corpus_root : str
        Root directory for storing ingested artifacts.
    schema_version : str
        Schema version tag.
    config : IngestionConfig, optional
        Ingestion configuration. Uses defaults if not provided.

    Returns
    -------
    dict
        Ingestion result with status, CSL JSON, Merkle tree, etc.
    """
    orchestrator = CiteIndexIngestionOrchestrator(
        corpus_root=corpus_root,
        schema_version=schema_version,
    )
    return orchestrator.ingest(input_ref, config=config)


__all__ = [
    "ingest",
    "CiteIndexIngestionOrchestrator",
    "IngestionConfig",
    "IngestionFailure",
    "PipelineResult",
]