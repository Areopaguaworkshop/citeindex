"""Digital PDF ingestion pipeline (v0.3).

Uses PyMuPDF as the primary tool — no MinerU dependency in this pipeline:
  1. PyMuPDF      — extract text per page + images to corpus/<slug>/images/
  2. PageIndex    — LLM-driven section tree building (optional, no MinerU needed)
  3. Citation     — GROBID (if available) or LLM on raw text
  4. Document     — page-paragraph structure augmented with PageIndex headings
  5. Merkle tree  — deterministic hash chain

Upstream PageIndex (VectifyAI/PageIndex) follows the same approach:
it opens PDFs with pymupdf (fitz) and reads page.get_text() for tree building.
"""

import logging
import os
import re
import shutil
import tempfile
from typing import Any, Dict, List, Optional, Tuple

import fitz

from ..models import IngestionConfig, PipelineResult
from ..deterministic import build_hierarchical_merkle_tree
from .common import (
    build_document_structure,
    build_merkle_for_nodes,
    build_nodes_with_granularity,
    determine_doc_type,
    make_basic_csl,
    make_source_id,
    split_paragraphs,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step 1: PyMuPDF text extraction
# ---------------------------------------------------------------------------

def _extract_pages(pdf_path: str) -> Tuple[fitz.Document, List[Dict[str, Any]]]:
    """Open PDF and return (doc, list of page texts for PageIndex)."""
    doc = fitz.open(pdf_path)
    pages: List[Dict[str, Any]] = []
    for idx in range(doc.page_count):
        page = doc[idx]
        text = page.get_text()
        pages.append({
            "page_number": idx + 1,
            "text": text,
            "blocks": page.get_text("blocks"),
        })
    return doc, pages


def _extract_page_paragraphs(pdf_path: str) -> List[Tuple[int, List[str]]]:
    """Return list of (page_num, [paragraph_texts])."""
    doc = fitz.open(pdf_path)
    result: List[Tuple[int, List[str]]] = []
    for page_idx in range(doc.page_count):
        page = doc[page_idx]
        text = page.get_text("text")
        paragraphs = split_paragraphs(text) or _fallback_paragraphs(page)
        result.append((page_idx + 1, paragraphs))
    doc.close()
    return result


def _fallback_paragraphs(page: fitz.Page) -> List[str]:
    """Split text from block bboxes as fallback."""
    blocks = page.get_text("blocks")
    fallback: List[str] = []
    for block in sorted(blocks, key=lambda b: (b[1], b[0])):
        content = (block[4] or "").strip()
        if content:
            fallback.extend(split_paragraphs(content))
    return fallback


# ---------------------------------------------------------------------------
# Step 2: Image extraction via PyMuPDF
# ---------------------------------------------------------------------------

def extract_pdf_images(
    pdf_path: str,
    output_dir: str,
    source_id: str,
    min_width: int = 100,
    min_height: int = 100,
) -> List[Dict[str, Any]]:
    """Extract images from a PDF and save them to output_dir/images/."""
    images_dir = os.path.join(output_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    doc = fitz.open(pdf_path)
    extracted: List[Dict[str, Any]] = []

    for page_idx in range(doc.page_count):
        page = doc[page_idx]
        image_list = page.get_images(full=True)

        for img_index, img_info in enumerate(image_list):
            xref = img_info[0]
            try:
                base_image = doc.extract_image(xref)
            except Exception:
                continue
            if not base_image:
                continue

            width = base_image.get("width", 0)
            height = base_image.get("height", 0)
            if width < min_width and height < min_height:
                continue

            ext = base_image.get("ext", "png")
            image_bytes = base_image.get("image")
            if not image_bytes:
                continue

            bbox = _get_image_bbox(page, xref)
            filename = f"{source_id}_p{page_idx + 1}_img{img_index + 1}.{ext}"
            filepath = os.path.join(images_dir, filename)
            with open(filepath, "wb") as f:
                f.write(image_bytes)

            rel_path = f"images/{filename}"
            extracted.append({
                "page_idx": page_idx,
                "bbox": list(bbox),
                "relative_path": rel_path,
                "width": width,
                "height": height,
                "filename": filename,
            })

    doc.close()
    logger.info("Extracted %d images from %s", len(extracted), pdf_path)
    return extracted


def _get_image_bbox(page: fitz.Page, xref: int) -> Tuple[float, ...]:
    """Find image bbox on a page by matching xref in text blocks."""
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") == 1 and block.get("image") == xref:
            return tuple(block.get("bbox", (0, 0, 0, 0)))
    return (0, 0, 0, 0)


def _embed_images_into_pages(
    document_structure: Dict[str, Any],
    images: List[Dict[str, Any]],
) -> None:
    """Add image_caption paragraphs to document_structure pages."""
    pages = document_structure.get("pages", [])
    for img in images:
        page_idx = img.get("page_idx", 0)
        if page_idx < len(pages):
            pages[page_idx].setdefault("paragraphs", []).append({
                "paragraph_id": f"p{page_idx + 1}_img_{len(pages[page_idx].get('paragraphs', []))}",
                "text": img.get("caption", "image"),
                "type": "image_caption",
                "image_path": img.get("relative_path"),
            })


def _page_range_bounds(page_range: Optional[str]) -> Tuple[Optional[int], Optional[int]]:
    """Parse a PageIndex page label like ``3`` or ``3-5`` into numeric bounds."""
    if not page_range:
        return None, None

    matches = re.findall(r"\d+", str(page_range))
    if not matches:
        return None, None

    start = int(matches[0])
    end = int(matches[-1]) if len(matches) > 1 else start
    return start, end


def _pageindex_sections_to_document_tree(
    nodes: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Convert CiteIndex PageIndex nodes into document_json section_tree nodes."""
    sections: List[Dict[str, Any]] = []
    for node in nodes:
        heading = node.get("heading") or node.get("title")
        if not heading:
            continue

        children = _pageindex_sections_to_document_tree(node.get("children", []))
        section: Dict[str, Any] = {
            "heading": heading,
            "page_range": node.get("page_range") or node.get("page_label"),
            "children": children,
        }
        if node.get("node_id"):
            section["node_id"] = node["node_id"]
        sections.append(section)
    return sections


def _collect_pageindex_headings(
    nodes: List[Dict[str, Any]],
    level: int = 2,
    spans: Optional[List[Dict[str, Any]]] = None,
) -> Tuple[Dict[int, List[Dict[str, Any]]], List[Dict[str, Any]]]:
    """Build page-start heading inserts and page spans from a section tree."""
    by_page: Dict[int, List[Dict[str, Any]]] = {}
    collected_spans = spans if spans is not None else []

    for node in nodes:
        heading = node.get("heading") or node.get("title")
        if not heading:
            continue

        start_page, end_page = _page_range_bounds(node.get("page_range") or node.get("page_label"))
        if start_page is not None:
            by_page.setdefault(start_page, []).append({
                "heading": heading,
                "level": level,
            })
            collected_spans.append({
                "heading": heading,
                "level": level,
                "start": start_page,
                "end": end_page or start_page,
            })

        child_by_page, _ = _collect_pageindex_headings(
            node.get("children", []),
            level=level + 1,
            spans=collected_spans,
        )
        for page_num, headings in child_by_page.items():
            by_page.setdefault(page_num, []).extend(headings)

    return by_page, collected_spans


def _annotate_document_with_pageindex(
    document_structure: Dict[str, Any],
    ci_tree: Dict[str, Any],
) -> None:
    """Inject PageIndex hierarchy into document structure for downstream export."""
    section_tree = _pageindex_sections_to_document_tree(ci_tree.get("level_1", []))
    if not section_tree:
        return

    document_structure["section_tree"] = section_tree
    headings_by_page, spans = _collect_pageindex_headings(section_tree)

    for page in document_structure.get("pages", []):
        page_num = page.get("page_number")
        if not isinstance(page_num, int):
            continue

        page_headings = headings_by_page.get(page_num, [])
        if page_headings:
            heading_paragraphs = [
                {
                    "paragraph_id": f"p{page_num}_heading_{idx + 1}",
                    "text": item["heading"],
                    "type": "heading",
                    "level": item["level"],
                }
                for idx, item in enumerate(page_headings)
            ]
            page["paragraphs"] = heading_paragraphs + page.get("paragraphs", [])

        active_sections = [
            span for span in spans
            if span["start"] <= page_num <= span["end"]
        ]
        if active_sections:
            active_sections.sort(key=lambda span: (span["level"], span["start"]))
            page["section_title"] = active_sections[-1]["heading"]


# ---------------------------------------------------------------------------
# Step 3: GROBID extraction
# ---------------------------------------------------------------------------

def _run_grobid(pdf_path: str) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Run GROBID on raw PDF. Returns (metadata_csl, references_dict)."""
    try:
        from .grobid import (
            extract_citations_grobid,
            extract_document_metadata_grobid,
            is_grobid_available,
        )
        if not is_grobid_available():
            return {}, {}
        metadata = extract_document_metadata_grobid(pdf_path)
        references = extract_citations_grobid(pdf_path)
        return metadata, references
    except Exception:
        logger.warning("GROBID extraction failed", exc_info=True)
        return {}, {}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def run(
    pdf_path: str,
    source_type: str = "digital_pdf",
    config: Optional[IngestionConfig] = None,
) -> PipelineResult:
    cfg = config or IngestionConfig()
    source_id = make_source_id(pdf_path)

    # Open PDF once for metadata
    doc_tmp = fitz.open(pdf_path)
    title = doc_tmp.metadata.get("title") or os.path.basename(pdf_path)
    num_pages = doc_tmp.page_count
    doc_tmp.close()

    doc_type = cfg.doc_type_override or determine_doc_type(pdf_path, num_pages)
    logger.info("Document type: %s (pages=%d)", doc_type, num_pages)

    # ── Step 1: PyMuPDF extraction ──────────────────────────────────
    doc, raw_pages = _extract_pages(pdf_path)
    page_paragraphs = _extract_page_paragraphs(pdf_path)
    ordered_text = "\n\n".join(p["text"] for p in raw_pages)

    # ── Step 2: Image extraction ──────────────────────────────────────
    pdf_images: List[Dict[str, Any]] = []
    _images_tmpdir: Optional[str] = None
    if cfg.use_layout_analysis:
        try:
            _images_tmpdir = tempfile.mkdtemp(prefix="citeindex_imgs_")
            pdf_images = extract_pdf_images(pdf_path, _images_tmpdir, source_id)
        except Exception:
            logger.warning("Image extraction failed", exc_info=True)

    # ── Step 3: Build flat document structure ───────────────────────
    document_structure = _build_flat_document_structure(page_paragraphs)

    # Embed images into pages if available
    if pdf_images:
        _embed_images_into_pages(document_structure, pdf_images)

    # ── Step 4: GROBID ──────────────────────────────────────────────
    grobid_metadata, grobid_references = _run_grobid(pdf_path)

    # ── Step 5: PageIndex tree (optional, uses PDF directly) ────────
    pageindex_tree_json = None
    page_number_map: Dict[int, int] = {i: i + 1 for i in range(num_pages)}
    if cfg.use_pageindex:
        try:
            from .pageindex_tree import run_pageindex_tree, pageindex_to_citeindex_tree
            pi_result = run_pageindex_tree(pdf_path, model=cfg.pageindex_model)
            if pi_result and pi_result.get("structure"):
                pageindex_tree_json = pi_result
                logger.info("PageIndex tree built: %d sections", len(pi_result["structure"]))
            else:
                logger.info("PageIndex tree empty, using flat structure")
        except Exception:
            logger.warning("PageIndex failed, using flat structure", exc_info=True)

    # ── Step 6: Citation extraction ─────────────────────────────────
    from .common import enrich_csl_with_citation_cascade

    base_csl = make_basic_csl(
        source_id=source_id, title=title, csl_type="book",
        extra={"genre": source_type},
    )
    enriched_csl = enrich_csl_with_citation_cascade(
        base_csl=base_csl, ordered_text=ordered_text,
        pdf_path=pdf_path, num_pages=num_pages, config=cfg,
    )

    csl = dict(base_csl)
    for key, value in enriched_csl.items():
        if key == "id":
            continue
        if value is not None:
            csl[key] = value

    if grobid_references.get("references"):
        csl["_cited_references"] = grobid_references["references"]

    # ── Step 7: Nodes + Merkle ─────────────────────────────────────
    nodes = build_nodes_with_granularity(source_id, page_paragraphs, is_primary=cfg.is_primary)
    merkle_tree = build_merkle_for_nodes(nodes)
    if document_structure.get("pages"):
        hierarchical = build_hierarchical_merkle_tree(document_structure)
        merkle_tree["hierarchical_root"] = hierarchical.get("root")
        merkle_tree["proof_tree"] = hierarchical.get("proof_tree")

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

    # ── Step 8: Extra (PageIndex tree + images) ─────────────────────
    extra: Dict[str, Any] = {}
    if pageindex_tree_json is not None:
        from .pageindex_tree import pageindex_to_citeindex_tree
        ci_tree = pageindex_to_citeindex_tree(
            pi_result=pageindex_tree_json,
            doc_id=source_id,
            csl_data=csl,
            page_number_map=page_number_map,
            merkle_root=merkle_tree.get("root"),
        )
        _annotate_document_with_pageindex(document_structure, ci_tree)
        extra["pageindex_tree"] = ci_tree
        logger.info("PageIndex → CiteIndex tree: %d sections", len(ci_tree.get("level_1", [])))

    if pdf_images and _images_tmpdir:
        extra["_images_tmpdir"] = _images_tmpdir
        extra["_images_list"] = pdf_images

    return PipelineResult(
        status="ok",
        source_id=source_id,
        resource_type=source_type,
        csl_json=csl,
        document_json=document_json,
        merkle_tree=merkle_tree,
        extra=extra,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_flat_document_structure(
    page_paragraphs: List[Tuple[int, List[str]]],
) -> Dict[str, Any]:
    """Build a minimal page-based document structure (no heading detection)."""
    pages: List[Dict[str, Any]] = []
    for page_num, paragraphs in page_paragraphs:
        page_dict: Dict[str, Any] = {
            "page_number": page_num,
            "paragraphs": [
                {
                    "paragraph_id": f"p{page_num}_{i + 1}",
                    "text": p,
                    "type": "text",
                }
                for i, p in enumerate(paragraphs)
                if p.strip()
            ],
            "footnotes": [],
        }
        pages.append(page_dict)

    return {"pages": pages, "section_tree": []}
