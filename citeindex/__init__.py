"""
Citation extraction and ingestion package.
"""

__version__ = "0.10.0"
__all__ = [
    "CitationLLM",
    "format_bibliography",
    "CiteIndexIngestionOrchestrator",
]


def __getattr__(name):
    if name == "CitationLLM":
        from .model import CitationLLM

        return CitationLLM
    if name == "format_bibliography":
        from .citation_style import format_bibliography

        return format_bibliography
    if name == "CiteIndexIngestionOrchestrator":
        from .ingestion import CiteIndexIngestionOrchestrator

        return CiteIndexIngestionOrchestrator
    raise AttributeError(f"module 'citeindex' has no attribute {name!r}")
