"""
Citation extraction and ingestion package.
"""

__version__ = "0.10.0"
__all__ = [
    "CitationExtractor",
    "CitationLLM",
    "get_input_type",
    "format_bibliography",
]


def __getattr__(name):
    if name == "CitationExtractor":
        from .main import CitationExtractor

        return CitationExtractor
    if name == "CitationLLM":
        from .model import CitationLLM

        return CitationLLM
    if name == "get_input_type":
        from .utils import get_input_type

        return get_input_type
    if name == "format_bibliography":
        from .citation_style import format_bibliography

        return format_bibliography
    raise AttributeError(f"module 'citeindex' has no attribute {name!r}")
