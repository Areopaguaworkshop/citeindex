import logging
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..deterministic import build_merkle_tree, build_hierarchical_merkle_tree, canonicalize_text, hash_payload
from ..models import IngestionConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared page-number regex patterns
# ---------------------------------------------------------------------------

# Individual patterns for extracting a page number from a block of text.
# Used by both layout.py (digital PDF pipeline) and dspy_extract.py
# (scanned/mineru pipeline) to avoid duplication.
PAGE_NUMBER_PATTERNS: List[re.Pattern] = [
    re.compile(r"[·•∙・]\s*(\d{1,4})\s*[·•∙・]"),      # · 74 ·
    re.compile(r"[-–—]\s*(\d{1,4})\s*[-–—]"),         # — 74 —
    re.compile(r"第\s*(\d{1,4})\s*[页頁]"),               # 第74页
    re.compile(r"[Pp]age\s+(\d{1,4})"),                   # Page 74
    re.compile(r"[Ss]\.\s*(\d{1,4})"),                   # S. 74 (German)
    re.compile(r"[Pp]\.\s*(\d{1,4})"),                   # p. 74 (French)
    re.compile(r"^\s*(\d{1,4})\s*$"),                     # bare number
]

# Combined single-regex version (for use with finditer on short text
# that may contain an embedded number, e.g. "TOOLS 49").
# Catches all the individual patterns plus a catch-all for numbers
# embedded in short header text.
PAGE_NUMBER_RE = re.compile(
    r"(?:"
    r"[·•∙・]\s*(\d{1,4})\s*[·•∙・]"    # · 74 ·
    r"|[-–—]\s*(\d{1,4})\s*[-–—]"      # — 74 —
    r"|第\s*(\d{1,4})\s*[页頁]"         # 第74页
    r"|[Pp]age\s+(\d{1,4})"             # Page 74
    r"|[Ss]\.\s*(\d{1,4})"              # S. 74
    r"|[Pp]\.\s*(\d{1,4})"              # p. 74
    r"|(?:.*\s)?(\d{1,4})(?:\s.*)?"     # any number in short text
    r")"
)


def extract_page_number_candidates(text: str) -> List[int]:
    """Extract candidate page numbers from a short text block.

    Returns a list of ints (possibly empty).  Uses ``PAGE_NUMBER_RE``
    which handles decorated, prefixed, bare, and embedded numbers.
    """
    candidates: List[int] = []
    for m in PAGE_NUMBER_RE.finditer(text):
        for g in m.groups():
            if g is not None:
                try:
                    val = int(g)
                    if 1 <= val <= 9999:
                        candidates.append(val)
                except ValueError:
                    continue
                break  # only take first matching group per match
    return candidates


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify(value: str) -> str:
    value = value.strip().lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    return value.strip("_") or "source"


def make_source_id(input_ref: str) -> str:
    base = os.path.basename(input_ref) or input_ref
    base = os.path.splitext(base)[0]
    return slugify(base)


def split_paragraphs(text: str) -> List[str]:
    chunks = [canonicalize_text(p) for p in re.split(r"\n\s*\n", text)]
    return [p for p in chunks if p]


def build_nodes(source_id: str, page_paragraphs: List[Tuple[int, List[str]]]) -> List[Dict[str, Any]]:
    nodes: List[Dict[str, Any]] = []
    for page_number, paragraphs in page_paragraphs:
        for idx, paragraph in enumerate(paragraphs, start=1):
            text = canonicalize_text(paragraph)
            if not text:
                continue
            text_hash = hash_payload(text)
            section_slug = f"p{page_number}"
            unit_slug = f"para{idx}"
            node_id = f"{source_id}:{section_slug}:{unit_slug}:{text_hash[:8]}"
            nodes.append(
                {
                    "node_id": node_id,
                    "source_id": source_id,
                    "section_path": section_slug,
                    "text": text,
                    "sha256": text_hash,
                    "page": page_number,
                    "paragraph": idx,
                }
            )
    nodes.sort(key=lambda n: n["node_id"])
    return nodes


def build_nodes_with_granularity(
    source_id: str,
    page_paragraphs: List[Tuple[int, List[str]]],
    is_primary: bool = False,
) -> List[Dict[str, Any]]:
    """Build nodes with granularity based on source classification.

    Primary sources: line-level nodes (each line is a node)
    Secondary sources (default): paragraph-level nodes
    """
    if not is_primary:
        return build_nodes(source_id, page_paragraphs)

    nodes: List[Dict[str, Any]] = []
    for page_number, paragraphs in page_paragraphs:
        for pidx, paragraph in enumerate(paragraphs, start=1):
            text = canonicalize_text(paragraph)
            if not text:
                continue
            lines = [line for line in text.split("\n") if line.strip()]
            if not lines:
                continue
            section_slug = f"p{page_number}"
            for lidx, line in enumerate(lines, start=1):
                line_text = canonicalize_text(line)
                if not line_text:
                    continue
                line_hash = hash_payload(line_text)
                unit_slug = f"para{pidx}_line{lidx}"
                node_id = f"{source_id}:{section_slug}:{unit_slug}:{line_hash[:8]}"
                nodes.append(
                    {
                        "node_id": node_id,
                        "source_id": source_id,
                        "section_path": section_slug,
                        "text": line_text,
                        "sha256": line_hash,
                        "page": page_number,
                        "paragraph": pidx,
                        "line": lidx,
                    }
                )
    nodes.sort(key=lambda n: n["node_id"])
    return nodes


def attach_evidence_locators(
    document_structure: Dict[str, Any],
    nodes: List[Dict[str, Any]],
    page_layouts: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Attach persisted-node and stable source locators to paragraphs in place."""
    layouts_by_index = {
        layout.get("page_number", index + 1) - 1: layout
        for index, layout in enumerate(page_layouts or [])
        if isinstance(layout.get("page_number", index + 1), int)
    }
    for physical_index, page in enumerate(document_structure.get("pages", [])):
        if not isinstance(page, dict):
            continue
        page.setdefault("physical_page_index", page.get("page_idx", physical_index))
        if not isinstance(page["physical_page_index"], int):
            page["physical_page_index"] = physical_index
        page_number = page.get("page_number")
        layout_paragraphs = [
            paragraph
            for column in layouts_by_index.get(page["physical_page_index"], {}).get("columns", [])
            for paragraph in column.get("paragraphs", [])
        ]
        for paragraph in page.get("paragraphs", []):
            if not isinstance(paragraph, dict) or not isinstance(paragraph.get("text"), str):
                continue
            text = canonicalize_text(paragraph["text"])
            paragraph["char_start"] = 0
            paragraph["char_end"] = len(paragraph["text"])
            matching_nodes = [
                node for node in nodes
                if canonicalize_text(str(node.get("text", ""))) == text
                and (node.get("page") == page_number or node.get("page") == page["physical_page_index"] + 1)
            ]
            if matching_nodes:
                paragraph["node_id"] = matching_nodes[0]["node_id"]
            if not paragraph.get("bbox"):
                matching_layout = next(
                    (
                        item for item in layout_paragraphs
                        if canonicalize_text(str(item.get("text", ""))) == text and item.get("bbox")
                    ),
                    None,
                )
                if matching_layout:
                    paragraph["bbox"] = matching_layout["bbox"]


def build_document_structure(page_paragraphs: List[Tuple[int, List[str]]]) -> Dict[str, Any]:
    return {
        "pages": [
            {
                "page_number": page_number,
                "paragraphs": [
                    {
                        "paragraph_id": f"p{page_number}_{i+1}",
                        "text": p,
                        "lines": [line for line in p.split("\n") if line.strip()],
                    }
                    for i, p in enumerate(paragraphs)
                ],
            }
            for page_number, paragraphs in page_paragraphs
        ]
    }


def build_layout_document_structure(page_layouts: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Build document structure from layout analysis results (pages → columns → paragraphs)."""
    return {
        "pages": [
            {
                "page_number": pl["page_number"],
                "columns": pl.get("columns", []),
                "footnotes": pl.get("footnotes", []),
            }
            for pl in page_layouts
        ]
    }


def build_retrieval_index(nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "entries": [
            {
                "hash": n["sha256"],
                "page": n.get("page"),
                "paragraph": n.get("paragraph"),
                "text_reference": n["node_id"],
            }
            for n in nodes
        ]
    }


def build_merkle_for_nodes(nodes: List[Dict[str, Any]]) -> Dict[str, Any]:
    leaf_hashes = [n["sha256"] for n in nodes]
    return build_merkle_tree(leaf_hashes)


def make_basic_csl(source_id: str, title: str, csl_type: str, extra: Dict[str, Any] | None = None) -> Dict[str, Any]:
    csl: Dict[str, Any] = {
        "id": source_id,
        "type": csl_type,
        "title": title,
    }
    if extra:
        csl.update(extra)
    return csl


# ---------------------------------------------------------------------------
# Phase 1: LLM-based citation extraction (merged from legacy CitationExtractor)
# ---------------------------------------------------------------------------

def determine_doc_type(pdf_path: str, num_pages: int) -> str:
    """Determine document type using legacy type_judge rules."""
    try:
        from ...type_judge import determine_document_type
        return determine_document_type(pdf_path, num_pages)
    except Exception:
        logger.warning("type_judge unavailable, falling back to page-count heuristic")
        return "book" if num_pages >= 70 else "journal"


def extract_citation_with_llm(
    text: str,
    doc_type: str,
    config: Optional[IngestionConfig] = None,
) -> Dict[str, Any]:
    """Run LLM-based citation extraction and return rich CSL JSON.

    Merges the legacy CitationLLM path into the ingestion pipeline so that
    ``citeindex ingest`` produces both structural artifacts AND rich citation
    metadata in a single pass.

    Gracefully returns ``{}`` when dspy / LLM dependencies are unavailable so
    that the structural pipeline always completes.
    """
    cfg = config or IngestionConfig()

    try:
        from ...model import CitationLLM
        from ...utils import to_csl_json
    except Exception:
        logger.warning("LLM dependencies (dspy) not available; skipping citation extraction")
        return {}

    llm = CitationLLM(cfg.llm_model)

    # Truncate for LLM context window
    truncated = text[:8000] if len(text) > 8000 else text

    use_vertical = cfg.text_direction in ("vertical", "auto") and doc_type == "book"
    if use_vertical:
        try:
            from ...vertical_llm import VerticalCitationLLM
            vertical_llm = VerticalCitationLLM(cfg.llm_model)
            extracted = vertical_llm.extract_vertical_citation(truncated, doc_type)
        except Exception:
            logger.warning("Vertical LLM failed, falling back to standard LLM", exc_info=True)
            extracted = llm.extract_citation_from_text(truncated, doc_type)
    else:
        extracted = llm.extract_citation_from_text(truncated, doc_type)

    if not extracted:
        logger.warning("LLM citation extraction returned empty result")
        return {}

    csl = to_csl_json(extracted, doc_type)
    return csl


def _extract_citation_grobid(pdf_path: str) -> Dict[str, Any]:
    """Try grobid header extraction. Returns CSL dict or {}."""
    try:
        from .grobid import extract_document_metadata_grobid, is_grobid_available

        if not is_grobid_available():
            logger.info("GROBID not available, skipping")
            return {}

        csl = extract_document_metadata_grobid(pdf_path)
        if csl.get("title"):
            logger.info("GROBID metadata extraction succeeded")
            return csl
        logger.info("GROBID returned no title, treating as empty")
        return {}
    except Exception:
        logger.warning("GROBID extraction failed", exc_info=True)
        return {}


def enrich_csl_with_citation_cascade(
    base_csl: Dict[str, Any],
    ordered_text: str,
    pdf_path: Optional[str],
    num_pages: int,
    config: Optional[IngestionConfig] = None,
) -> Dict[str, Any]:
    """Enrich a basic CSL JSON dict using the citation extraction cascade.

    Cascade order (per migration plan Phase 1.3):
      1. GROBID (deterministic, primary)
      2. LLM extraction (probabilistic, fallback)
      3. PDF metadata only (last resort — base_csl as-is)

    Merges extracted fields into base_csl, preferring extracted values.
    """
    cfg = config or IngestionConfig()

    # Determine document type
    if cfg.doc_type_override:
        doc_type = cfg.doc_type_override
    elif pdf_path:
        doc_type = determine_doc_type(pdf_path, num_pages)
    else:
        doc_type = "book" if num_pages >= 70 else "journal"

    logger.info("Document type determined: %s (pages=%d)", doc_type, num_pages)

    # --- Cascade ---
    extracted_csl: Dict[str, Any] = {}
    extraction_method = "metadata-only"

    # 1. Try GROBID
    if pdf_path:
        extracted_csl = _extract_citation_grobid(pdf_path)
        if extracted_csl:
            extraction_method = "grobid"

    # 2. Fallback to LLM
    if not extracted_csl:
        extracted_csl = extract_citation_with_llm(ordered_text, doc_type, cfg)
        if extracted_csl:
            extraction_method = "llm"

    # 3. Last resort: base_csl as-is
    if not extracted_csl:
        logger.info("Citation cascade: using metadata-only (no enrichment)")
        return base_csl

    logger.info("Citation cascade: using %s extraction", extraction_method)

    # Merge: extracted values win for metadata fields; keep structural fields from base
    merged = dict(base_csl)
    for key, value in extracted_csl.items():
        if key in ("id",):
            continue  # keep deterministic id from base
        if value is not None:
            merged[key] = value
    merged["_extraction_method"] = extraction_method

    return merged


# Backward-compatible alias
enrich_csl_with_llm = enrich_csl_with_citation_cascade


# ---------------------------------------------------------------------------
# Author fallback: filename parsing → interactive prompt
# ---------------------------------------------------------------------------

import re as _re


def parse_author_from_filename(filename: str) -> Optional[Dict[str, Any]]:
    """Try to extract author from a structured PDF filename.

    Supports patterns like:
      - "Chatonnet-2023-Syriac-World-Search-…-1-24.pdf"  (Author-Year-Title)
      - "Smith-2024-Title-Here.pdf"
      - "张三-2023-书名.pdf"  (Chinese Author-Year-Title)

    Returns a CSL author dict (e.g. {"family": "Chatonnet"}) or None.
    """
    stem = os.path.splitext(os.path.basename(filename))[0]

    # Pattern: Author-Year-TitleWords  (e.g. "Chatonnet-2023-Syriac-World-...")
    # Author is the first segment before a 4-digit year
    m = _re.match(r"^(?:\d+-)?([A-Z][a-zA-Z]+)-(\d{4})-", stem)
    if m:
        author_name = m.group(1)
        return {"family": author_name, "literal": author_name}

    # Pattern: Chinese Author-Year (e.g. "张三-2023-书名")
    m = _re.match(r"^(?:\d+-)?([\u4e00-\u9fff]{1,6})-(\d{4})-", stem)
    if m:
        author_name = m.group(1)
        return {"family": author_name, "literal": author_name}

    # Pattern: Author_Year_Title or Author-Year-Title without 4-digit year
    m = _re.match(r"^(?:\d+-)?([A-Z][a-zA-Z]{1,30})[_-]", stem)
    if m:
        # Only accept if the first segment looks like a proper name (capitalized)
        author_name = m.group(1)
        return {"family": author_name, "literal": author_name}

    return None


def validate_authors(
    authors: Optional[List[Dict[str, Any]]],
    input_ref: str = "",
) -> Optional[List[Dict[str, Any]]]:
    """Validate and clean LLM-extracted author list.

    Heuristics to detect garbage author extraction:
      - Author names that are overly long (> 80 chars)
      - Author names containing common non-name words (e.g. "is a", "the", "that")
      - More than 5 authors (likely extraction artifacts)
      - Single-word "family" names that are common English words

    Returns cleaned author list, or None if all authors are suspicious.
    """
    if not authors:
        return None

    _GARBAGE_PATTERNS = _re.compile(
        r'(?:is a |are |that |which |the |from |into |with |this |can be '
        r'|known as |formed by|formed from|sense\)|dialect|language|culture|semitic'
        r'|adjectival|\(formed|\"|\))',
        _re.IGNORECASE,
    )
    _GARBAGE_FAMILY_PATTERNS = _re.compile(
        r'(?:sense\)|formed|adjectival|\)$|\(|^the |^a |^an |^is )',
        _re.IGNORECASE,
    )
    _COMMON_WORDS = {
        "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
        "have", "has", "had", "do", "does", "did", "will", "would", "could",
        "should", "may", "might", "shall", "can", "need", "dare", "ought",
        "used", "from", "into", "with", "this", "that", "which", "what",
    }

    cleaned: List[Dict[str, Any]] = []
    for author in authors:
        family = (author.get("family") or "").strip()
        given = (author.get("given") or "").strip()
        literal = (author.get("literal") or "").strip()

        # Full name for validation
        full_name = literal or f"{family} {given}".strip()

        # Skip if name is too long (likely a sentence fragment)
        if len(full_name) > 80:
            logger.warning("Skipping suspiciously long author name: %s...", full_name[:50])
            continue

        # Skip if name matches garbage patterns (sentence fragments)
        if _GARBAGE_PATTERNS.search(full_name):
            logger.warning("Skipping author name with non-name content: %s...", full_name[:50])
            continue

        # Also check the family name alone for garbage patterns
        if family and _GARBAGE_FAMILY_PATTERNS.search(family):
            logger.warning("Skipping author with garbage family name: %s", family[:50])
            continue

        # Skip if the family name alone is a very common English word
        if family.lower() in _COMMON_WORDS and not given and not literal:
            logger.warning("Skipping single common-word author: %s", family)
            continue

        cleaned.append(author)

    if not cleaned:
        return None

    # If more than 5 authors, likely extraction artifacts
    if len(cleaned) > 5:
        logger.warning("Too many authors (%d), keeping first 3", len(cleaned))
        cleaned = cleaned[:3]

    return cleaned


def prompt_author_interactively() -> Optional[Dict[str, Any]]:
    """Prompt the user on the terminal for author info when extraction fails.

    Returns a CSL author dict or None if the user skips.
    """
    import sys

    print("\n⚠️  Could not determine the author(s) of this document.")
    print("   You can provide author info now, or press Enter to skip.")
    try:
        raw = input("   Author (e.g. 'Chatonnet, Françoise Briquel' or '张三'): ").strip()
    except (EOFError, KeyboardInterrupt):
        return None

    if not raw:
        return None

    authors: List[Dict[str, Any]] = []
    for part in raw.split(";"):
        part = part.strip()
        if not part:
            continue
        # "Family, Given" format
        if "," in part:
            family, given = part.split(",", 1)
            authors.append({"family": family.strip(), "given": given.strip()})
        else:
            authors.append({"literal": part.strip()})

    return authors if authors else None
