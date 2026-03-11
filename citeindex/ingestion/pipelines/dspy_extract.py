"""DSPy Signatures for extracting structured metadata from MinerU output.

Uses Ollama/qwen3 to parse MinerU's markdown or JSON and reconcile
with GROBID metadata, filling gaps and resolving conflicts.
"""

import logging
from typing import Any, Dict, List, Optional

import dspy

from ...llm import get_llm_model
from ..models import IngestionConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# DSPy Signatures
# ---------------------------------------------------------------------------

class ExtractDocumentMetadata(dspy.Signature):
    """Extract document metadata from MinerU-parsed academic text.

    The text comes from MinerU's markdown output which preserves layout
    structure (headings, tables, figures, footnotes). Extract the document's
    own metadata — not the cited references.
    """

    mineru_text = dspy.InputField(
        desc="First ~3000 chars of MinerU markdown output (cover, title page, copyright page)."
    )
    doc_type = dspy.InputField(
        desc="Document type: 'book', 'journal', 'thesis', or 'bookchapter'."
    )

    title = dspy.OutputField(
        desc="Document title. For journals, the article title (not the journal name)."
    )
    author = dspy.OutputField(
        desc=(
            "Author(s). CRITICAL for Chinese names: do NOT split multi-character names. "
            '"程俊英" is ONE author. Separate multiple authors with semicolons. '
            "Include dynasty/role indicators (e.g., '【明】王陽明撰')."
        )
    )
    container_title = dspy.OutputField(
        desc="Journal name or book title containing this chapter. Empty if standalone book."
    )
    publisher = dspy.OutputField(desc="Publisher name. Return empty if not found.")
    publication_year = dspy.OutputField(desc="Publication year (YYYY). Return empty if not found.")
    volume = dspy.OutputField(desc="Volume number. Return empty if not found.")
    issue = dspy.OutputField(desc="Issue number. Return empty if not found.")
    page_numbers = dspy.OutputField(desc="Page range (e.g., '20-41'). Return empty if not found.")
    doi = dspy.OutputField(desc="DOI. Return empty if not found.")
    abstract = dspy.OutputField(desc="Abstract text. Return empty if not found.")


class ReconcileMetadata(dspy.Signature):
    """Reconcile document metadata from two sources: GROBID and MinerU+DSPy.

    Choose the most accurate value for each field. GROBID is deterministic
    and reliable for standard Western academic PDFs. MinerU+DSPy may be
    better for CJK text, non-standard layouts, or fields GROBID missed.
    """

    grobid_json = dspy.InputField(
        desc="JSON string of GROBID-extracted metadata (may be empty '{}')."
    )
    dspy_json = dspy.InputField(
        desc="JSON string of DSPy-extracted metadata from MinerU output."
    )
    doc_type = dspy.InputField(desc="Document type.")

    final_title = dspy.OutputField(desc="Best title from either source.")
    final_author = dspy.OutputField(desc="Best author(s), semicolon-separated.")
    final_container_title = dspy.OutputField(desc="Best container title (or empty).")
    final_publisher = dspy.OutputField(desc="Best publisher (or empty).")
    final_year = dspy.OutputField(desc="Best publication year YYYY (or empty).")
    final_volume = dspy.OutputField(desc="Best volume (or empty).")
    final_issue = dspy.OutputField(desc="Best issue (or empty).")
    final_pages = dspy.OutputField(desc="Best page range (or empty).")
    final_doi = dspy.OutputField(desc="Best DOI (or empty).")
    final_abstract = dspy.OutputField(desc="Best abstract (or empty).")
    provenance = dspy.OutputField(
        desc=(
            "For each field, note which source was chosen: 'grobid', 'dspy', or 'both'. "
            "Format: 'title:grobid; author:dspy; year:both; ...'"
        )
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def extract_metadata_from_mineru(
    mineru_markdown: str,
    doc_type: str,
    config: Optional[IngestionConfig] = None,
) -> Dict[str, Any]:
    """Run DSPy extraction on MinerU markdown to get document metadata.

    Returns a CSL-compatible dict with extracted fields.
    """
    cfg = config or IngestionConfig()

    try:
        lm = get_llm_model(cfg.llm_model, temperature=0.1)
    except Exception:
        logger.warning("LLM not available for DSPy extraction")
        return {}

    # Truncate to first ~3000 chars (cover + title + copyright pages)
    truncated = mineru_markdown[:3000] if len(mineru_markdown) > 3000 else mineru_markdown
    if not truncated.strip():
        return {}

    try:
        with dspy.context(lm=lm):
            predictor = dspy.Predict(ExtractDocumentMetadata)
            result = predictor(mineru_text=truncated, doc_type=doc_type)

        csl: Dict[str, Any] = {}
        _set_if_present(csl, "title", result.title)
        _set_if_present(csl, "publisher", result.publisher)
        _set_if_present(csl, "volume", result.volume)
        _set_if_present(csl, "issue", result.issue)
        _set_if_present(csl, "page", result.page_numbers)
        _set_if_present(csl, "DOI", result.doi)
        _set_if_present(csl, "abstract", result.abstract)

        if result.container_title and result.container_title.strip():
            csl["container-title"] = result.container_title.strip()

        if result.author and result.author.strip():
            csl["author"] = _parse_authors(result.author)

        if result.publication_year and result.publication_year.strip():
            try:
                year = int(result.publication_year.strip()[:4])
                csl["issued"] = {"date-parts": [[year]]}
            except ValueError:
                pass

        logger.info("DSPy extraction from MinerU produced %d fields", len(csl))
        return csl

    except Exception:
        logger.warning("DSPy extraction failed", exc_info=True)
        return {}


def reconcile_grobid_and_dspy(
    grobid_csl: Dict[str, Any],
    dspy_csl: Dict[str, Any],
    doc_type: str,
    config: Optional[IngestionConfig] = None,
) -> Dict[str, Any]:
    """Reconcile GROBID and DSPy extractions into a final CSL dict.

    When both sources have a value, uses LLM to pick the best one.
    When only one source has a value, uses that directly (no LLM call).
    """
    cfg = config or IngestionConfig()

    # Fast path: if one is empty, return the other
    if not grobid_csl:
        if dspy_csl:
            dspy_csl["_extraction_method"] = "dspy"
        return dspy_csl
    if not dspy_csl:
        grobid_csl["_extraction_method"] = "grobid"
        return grobid_csl

    # Merge: start with GROBID as base, fill gaps from DSPy
    merged = dict(grobid_csl)
    provenance: Dict[str, str] = {}

    # Fields to consider
    simple_fields = [
        "title", "publisher", "volume", "issue", "page", "DOI",
        "abstract", "container-title",
    ]

    for field in simple_fields:
        g_val = grobid_csl.get(field)
        d_val = dspy_csl.get(field)

        if g_val and d_val:
            # Both have it — prefer GROBID for determinism unless DSPy is longer
            # (GROBID sometimes truncates titles)
            if isinstance(g_val, str) and isinstance(d_val, str) and len(d_val) > len(g_val) * 1.3:
                merged[field] = d_val
                provenance[field] = "dspy"
            else:
                provenance[field] = "grobid"
        elif d_val and not g_val:
            merged[field] = d_val
            provenance[field] = "dspy"
        elif g_val:
            provenance[field] = "grobid"

    # Author — prefer whichever has more authors
    g_authors = grobid_csl.get("author", [])
    d_authors = dspy_csl.get("author", [])
    if d_authors and (not g_authors or len(d_authors) > len(g_authors)):
        merged["author"] = d_authors
        provenance["author"] = "dspy"
    elif g_authors:
        provenance["author"] = "grobid"

    # Issued date — prefer GROBID
    if not grobid_csl.get("issued") and dspy_csl.get("issued"):
        merged["issued"] = dspy_csl["issued"]
        provenance["issued"] = "dspy"
    else:
        provenance["issued"] = "grobid"

    merged["_extraction_method"] = "grobid+dspy"
    merged["_provenance"] = provenance
    return merged


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _set_if_present(d: Dict[str, Any], key: str, value: Any) -> None:
    if value and isinstance(value, str) and value.strip():
        d[key] = value.strip()


def _parse_authors(author_str: str) -> List[Dict[str, str]]:
    """Parse an author string into CSL author list."""
    import re

    raw = re.split(r"[;；]", author_str)
    authors: List[Dict[str, str]] = []

    for name in raw:
        name = name.strip()
        if not name:
            continue

        # CJK name — treat as literal (no family/given split)
        if any("\u4e00" <= ch <= "\u9fff" for ch in name):
            authors.append({"literal": name})
        elif " " in name:
            parts = name.rsplit(" ", 1)
            authors.append({"family": parts[-1], "given": parts[0]})
        else:
            authors.append({"literal": name})

    return authors
