"""Digital PDF ingestion pipeline (v0.4).

Uses PyMuPDF4LLM (GNN layout classification) as the primary tool —
falls back to raw PyMuPDF + heuristic layout if pymupdf4llm is not installed:
  1. PyMuPDF4LLM  — GNN-classified text blocks (footnote, header, footer, etc.)
  2. PageIndex    — LLM-driven section tree building (optional)
  3. Citation     — GROBID (if available) or LLM on raw text
  4. Document     — page-paragraph structure augmented with PageIndex headings
  5. Merkle tree  — deterministic hash chain

Layout analysis is hybrid: GNN for header/footer classification (85-100% AP),
heuristic detect_footnotes() for footnote detection (better recall than GNN's
~0.72 F1). Falls back to heuristic-only when pymupdf4llm is not installed.
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
from .pdf_text_cleanup import clean_page_texts
from .common import (
    build_document_structure,
    build_merkle_for_nodes,
    build_nodes_with_granularity,
    determine_doc_type,
    make_basic_csl,
    make_source_id,
    split_paragraphs,
)
from .layout import analyze_document_layout, analyze_document_layout_pymupdf4llm, build_page_number_map

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Step 1: PyMuPDF text extraction
# ---------------------------------------------------------------------------

def _extract_pages(pdf_path: str) -> List[Dict[str, Any]]:
    """Open PDF and return cleaned page texts for downstream processing."""
    doc = fitz.open(pdf_path)
    pages: List[Dict[str, Any]] = []
    for idx in range(doc.page_count):
        page = doc[idx]
        pages.append({
            "page_number": idx + 1,
            "text": page.get_text(),
            "blocks": page.get_text("blocks"),
        })
    doc.close()

    cleaned_texts = clean_page_texts([page["text"] for page in pages])
    for page, cleaned_text in zip(pages, cleaned_texts):
        page["text"] = cleaned_text
    return pages


def _extract_page_paragraphs(raw_pages: List[Dict[str, Any]]) -> List[Tuple[int, List[str]]]:
    """Return list of (page_num, [paragraph_texts]) from cleaned page text."""
    result: List[Tuple[int, List[str]]] = []
    for page in raw_pages:
        text = page.get("text", "")
        paragraphs = split_paragraphs(text)
        if not paragraphs and text.strip():
            paragraphs = [text.strip()]
        result.append((page["page_number"], paragraphs))
    return result


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


def _attach_layout_footnotes(
    document_structure: Dict[str, Any],
    page_layouts: List[Dict[str, Any]],
) -> None:
    """Copy layout-detected footnotes onto the matching document pages.

    Also removes footnote text from page body paragraphs to avoid
    duplication (PyMuPDF extracts footnotes as body text, but we now
    know they should be footnotes only).
    """
    pages = document_structure.get("pages", [])
    for layout in page_layouts:
        page_num = layout.get("page_number")
        if not isinstance(page_num, int):
            continue

        page_idx = page_num - 1
        if page_idx < 0 or page_idx >= len(pages):
            continue

        footnotes = layout.get("footnotes") or []
        if footnotes:
            pages[page_idx]["footnotes"] = footnotes
            # Remove footnote text from body paragraphs to avoid duplication.
            # Footnote text blocks were misclassified as body paragraphs by
            # PyMuPDF, so we strip them now that layout analysis identifies them.
            # Whitespace may differ (trailing spaces, line break variants), so
            # we normalize before comparing.
            fn_signatures = set()
            for fn in footnotes:
                fn_text = fn.get("text", "")
                if fn_text:
                    # Normalize: collapse whitespace, strip edges
                    sig = " ".join(fn_text.split())
                    fn_signatures.add(sig[:200])  # Use first 200 chars as signature

            if fn_signatures:
                filtered = []
                for para in pages[page_idx].get("paragraphs", []):
                    para_text = para.get("text", "")
                    if para_text:
                        sig = " ".join(para_text.split())
                        if sig[:200] in fn_signatures:
                            logger.debug(
                                "Removing footnote text from body paragraph on page %d: %.50s...",
                                page_num, para_text.strip(),
                            )
                            continue
                    filtered.append(para)
                pages[page_idx]["paragraphs"] = filtered


def _remove_header_footer_paragraphs(
    document_structure: Dict[str, Any],
    page_layouts: List[Dict[str, Any]],
) -> None:
    """Remove running header/footer text from page body paragraphs.

    Layout analysis identifies header/footer blocks (short text in the
    top/bottom 12% of the page).  These blocks are not body content but
    were extracted as paragraphs by PyMuPDF.  Remove them, using the
    same normalised-signature matching as footnote removal.
    """
    pages = document_structure.get("pages", [])
    for layout in page_layouts:
        page_num = layout.get("page_number")
        if not isinstance(page_num, int):
            continue

        page_idx = page_num - 1
        if page_idx < 0 or page_idx >= len(pages):
            continue

        headers_footers = layout.get("headers_footers") or []
        if not headers_footers:
            continue

        hf_signatures = set()
        for hf in headers_footers:
            hf_text = hf.get("text", "")
            if hf_text:
                sig = " ".join(hf_text.split())
                hf_signatures.add(sig[:200])

        if hf_signatures:
            filtered = []
            for para in pages[page_idx].get("paragraphs", []):
                para_text = para.get("text", "")
                if para_text:
                    sig = " ".join(para_text.split())
                    if sig[:200] in hf_signatures:
                        logger.debug(
                            "Removing header/footer text from body paragraph on page %d: %.50s...",
                            page_num, para_text.strip(),
                        )
                        continue
                filtered.append(para)
            pages[page_idx]["paragraphs"] = filtered


def _apply_page_number_map(
    document_structure: Dict[str, Any],
    page_number_map: Dict[int, int],
) -> None:
    """Update document structure page_number values using the real printed page numbers.

    By default PyMuPDF gives 1-based physical indices.  ``page_number_map``
    maps 0-based physical indices → printed page numbers.  This function
    rewrites each page's ``page_number`` field accordingly.
    """
    pages = document_structure.get("pages", [])
    for page_idx, page in enumerate(pages):
        printed = page_number_map.get(page_idx)
        if printed is not None:
            page["page_number"] = printed


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
        start_page, end_page = _section_bounds(node)
        section: Dict[str, Any] = {
            "heading": heading,
            "page_range": _bounds_to_page_range(start_page, end_page),
            "children": children,
        }
        if node.get("node_id"):
            section["node_id"] = node["node_id"]
        sections.append(section)
    return sections


def _bounds_to_page_range(
    start_page: Optional[int],
    end_page: Optional[int],
) -> Optional[str]:
    if start_page is None or end_page is None:
        return None
    if start_page == end_page:
        return str(start_page)
    return f"{start_page}-{end_page}"


def _section_bounds(node: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    """Derive a node's page span from its own range plus descendant locators."""
    start_page, end_page = _page_range_bounds(node.get("page_range") or node.get("page_label"))

    for child in node.get("children", []):
        child_start, child_end = _section_bounds(child)
        if child_start is None or child_end is None:
            continue
        start_page = child_start if start_page is None else min(start_page, child_start)
        end_page = child_end if end_page is None else max(end_page, child_end)

    return start_page, end_page


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

        body_paragraphs = [
            para for para in page.get("paragraphs", [])
            if para.get("type") != "heading"
        ]
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
            page["paragraphs"] = heading_paragraphs + body_paragraphs
        else:
            page["paragraphs"] = body_paragraphs

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

    page_layouts: Optional[List[Dict[str, Any]]] = None

    # ── Step 1: Text extraction + Layout analysis ─────────────────
    # When layout analysis is enabled and pymupdf4llm is installed,
    # we use the GNN-classified blocks for both text extraction and
    # layout analysis in a single pass (no duplicate fitz.open()).
    # Falls back to raw PyMuPDF extraction + heuristic layout otherwise.
    if cfg.use_layout_analysis:
        try:
            page_layouts = analyze_document_layout_pymupdf4llm(pdf_path)
            # Build page_paragraphs from the layout results (body text only,
            # headers/footers already excluded by the GNN classification).
            raw_pages = []
            for layout in page_layouts:
                page_texts = []
                for col in layout.get("columns", []):
                    for para in col.get("paragraphs", []):
                        page_texts.append(para.get("text", ""))
                raw_pages.append({
                    "page_number": layout.get("page_number", 0),
                    "text": "\n\n".join(t for t in page_texts if t.strip()),
                    "blocks": [],
                })
            # Apply clean_page_texts for text segmentation (split_paragraphs
            # needs clean \n\n breaks; layout gives classified blocks but
            # clean_page_texts handles repeated-header stripping).
            cleaned_texts = clean_page_texts([p.get("text", "") for p in raw_pages])
            for page, cleaned_text in zip(raw_pages, cleaned_texts):
                page["text"] = cleaned_text
            page_paragraphs = _extract_page_paragraphs(raw_pages)
            ordered_text = "\n\n".join(layout.get("ordered_text", "") for layout in page_layouts)
        except Exception:
            logger.warning("pymupdf4llm layout failed, falling back to raw extraction", exc_info=True)
            page_layouts = None

    if page_layouts is None:
        # Fallback: raw PyMuPDF extraction (no layout classification)
        raw_pages = _extract_pages(pdf_path)
        page_paragraphs = _extract_page_paragraphs(raw_pages)
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

    # ── Step 3b: Layout post-processing (footnotes, page numbers, headers/footers)
    # When pymupdf4llm was used, footnotes are already extracted and headers/footers
    # are already excluded from columns. We still need to:
    #   - Remove any footnote text that leaked into body paragraphs
    #   - Remove any header/footer text that leaked (safety net)
    #   - Apply the page number map
    page_number_map: Dict[int, int] = {i: i + 1 for i in range(num_pages)}
    if page_layouts is not None:
        _attach_layout_footnotes(document_structure, page_layouts)
        _remove_header_footer_paragraphs(document_structure, page_layouts)

        # Build page number map from detected page numbers in headers/footers
        detected_map = build_page_number_map(page_layouts)
        if detected_map:
            page_number_map = detected_map
            logger.info(
                "Page number map from layout: offset=%d (covers %d/%d pages)",
                page_number_map.get(0, 1) - 1,
                len(page_number_map),
                num_pages,
            )
            # Update document structure page numbers to use real printed numbers
            _apply_page_number_map(document_structure, page_number_map)
    elif cfg.use_layout_analysis:
        try:
            page_layouts = analyze_document_layout(pdf_path)
            _attach_layout_footnotes(document_structure, page_layouts)
            _remove_header_footer_paragraphs(document_structure, page_layouts)

            detected_map = build_page_number_map(page_layouts)
            if detected_map:
                page_number_map = detected_map
                logger.info(
                    "Page number map from layout: offset=%d (covers %d/%d pages)",
                    page_number_map.get(0, 1) - 1,
                    len(page_number_map),
                    num_pages,
                )
                _apply_page_number_map(document_structure, page_number_map)
        except Exception:
            logger.warning("Layout analysis failed", exc_info=True)

    # ── Step 4: GROBID ──────────────────────────────────────────────
    grobid_metadata, grobid_references = _run_grobid(pdf_path)

    # ── Step 5: PageIndex tree (default, uses PDF directly) ─────────
    pageindex_tree_json = None
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
            page_layouts=page_layouts,
        )
        _annotate_document_with_pageindex(document_structure, ci_tree)
        extra["pageindex_tree"] = ci_tree
        logger.info("PageIndex → CiteIndex tree: %d sections", len(ci_tree.get("level_1", [])))

    # Locators are part of the persisted source representation, not a
    # verification-only sidecar.  Add them after PageIndex has finished
    # reshaping paragraphs.
    from .common import attach_evidence_locators
    attach_evidence_locators(document_structure, nodes, page_layouts)

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
            "physical_page_index": len(pages),
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
