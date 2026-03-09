import logging
import os
from typing import Any, Dict, List, Optional, Tuple

import fitz

from ..models import IngestionConfig, PipelineResult
from ..deterministic import build_hierarchical_merkle_tree
from .common import (
    build_document_structure,
    build_layout_document_structure,
    build_merkle_for_nodes,
    build_nodes,
    build_nodes_with_granularity,
    build_retrieval_index,
    enrich_csl_with_llm,
    make_basic_csl,
    make_source_id,
    split_paragraphs,
)
from .layout import analyze_document_layout

logger = logging.getLogger(__name__)


def _extract_page_paragraphs(pdf_path: str) -> List[Tuple[int, List[str]]]:
    doc = fitz.open(pdf_path)
    page_paragraphs: List[Tuple[int, List[str]]] = []
    for page_idx in range(doc.page_count):
        text = page_paragraphs_from_page(doc[page_idx])
        page_paragraphs.append((page_idx + 1, text))
    doc.close()
    return page_paragraphs


def page_paragraphs_from_page(page: fitz.Page) -> List[str]:
    text = page.get_text("text")
    paragraphs = split_paragraphs(text)
    if paragraphs:
        return paragraphs

    blocks = page.get_text("blocks")
    fallback: List[str] = []
    for block in sorted(blocks, key=lambda b: (b[1], b[0])):
        content = (block[4] or "").strip()
        if content:
            fallback.extend(split_paragraphs(content))
    return fallback


def _extract_paragraphs_from_layouts(
    page_layouts: List[Dict[str, Any]],
) -> List[Tuple[int, List[str]]]:
    """Convert layout analysis results into the (page_number, [paragraphs]) format."""
    page_paragraphs: List[Tuple[int, List[str]]] = []
    for pl in page_layouts:
        paragraphs: List[str] = []
        for col in pl.get("columns", []):
            for para in col.get("paragraphs", []):
                text = para.get("text", "").strip()
                if text:
                    paragraphs.append(text)
        for fn in pl.get("footnotes", []):
            text = fn.get("text", "").strip()
            if text:
                paragraphs.append(text)
        page_paragraphs.append((pl["page_number"], paragraphs))
    return page_paragraphs


def _collect_ordered_text(page_layouts: List[Dict[str, Any]]) -> str:
    """Gather all ordered text from layout analysis for LLM extraction."""
    parts: List[str] = []
    for pl in page_layouts:
        ordered = pl.get("ordered_text", "")
        if ordered.strip():
            parts.append(ordered)
    return "\n\n".join(parts)


def run(
    pdf_path: str,
    source_type: str = "digital_pdf",
    config: Optional[IngestionConfig] = None,
) -> PipelineResult:
    cfg = config or IngestionConfig()
    source_id = make_source_id(pdf_path)

    # Phase 2: Use layout analysis when enabled
    if cfg.use_layout_analysis:
        logger.info("Running layout-aware extraction for %s", pdf_path)
        page_layouts = analyze_document_layout(pdf_path)
        page_paragraphs = _extract_paragraphs_from_layouts(page_layouts)
        document_structure = build_layout_document_structure(page_layouts)
        ordered_text = _collect_ordered_text(page_layouts)
    else:
        page_paragraphs = _extract_page_paragraphs(pdf_path)
        document_structure = build_document_structure(page_paragraphs)
        ordered_text = "\n\n".join(
            "\n".join(paras) for _, paras in page_paragraphs
        )

    nodes = build_nodes_with_granularity(source_id, page_paragraphs, is_primary=cfg.is_primary)
    merkle_tree = build_merkle_for_nodes(nodes)

    # Build hierarchical Merkle tree from document structure when layout is available
    if cfg.use_layout_analysis and document_structure.get("pages"):
        hierarchical_merkle = build_hierarchical_merkle_tree(document_structure)
        merkle_tree["hierarchical_root"] = hierarchical_merkle["root"]
        merkle_tree["proof_tree"] = hierarchical_merkle.get("proof_tree")

    retrieval_index = build_retrieval_index(nodes)

    doc = fitz.open(pdf_path)
    title = doc.metadata.get("title") or os.path.basename(pdf_path)
    num_pages = doc.page_count
    doc.close()

    base_csl = make_basic_csl(
        source_id=source_id,
        title=title,
        csl_type="book",
        extra={"genre": source_type},
    )

    # Phase 1: Enrich CSL with LLM-based citation extraction
    csl = enrich_csl_with_llm(
        base_csl=base_csl,
        ordered_text=ordered_text,
        pdf_path=pdf_path,
        num_pages=num_pages,
        config=cfg,
    )

    document_json: Dict[str, Any] = {
        "source_id": source_id,
        "source_type": source_type,
        "metadata": {
            "title": title,
            "page_count": len(page_paragraphs),
            "source_path": os.path.abspath(pdf_path),
        },
        "structure": document_structure,
        "nodes": nodes,
    }

    return PipelineResult(
        status="ok",
        source_id=source_id,
        resource_type=source_type,
        csl_json=csl,
        document_json=document_json,
        merkle_tree=merkle_tree,
        retrieval_index=retrieval_index,
    )
