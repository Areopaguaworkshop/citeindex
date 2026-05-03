"""MinerU (magic-pdf) integration for layout analysis.

Invokes MinerU via CLI subprocess to produce:
  - middle JSON  (block-level layout with bboxes)
  - markdown     (reading-order text)
  - content_list (ordered content items)

Converts content_list.json into a section-hierarchical document structure
with page → paragraph layout and overlaid section tree.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import fitz  # PyMuPDF — for image extraction

from ..models import IngestionConfig, PipelineResult
from .common import make_source_id

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Patterns for classifying discarded items
# ---------------------------------------------------------------------------

# Footnote markers: ①②③ or LaTeX \textcircled
_FOOTNOTE_MARKER_RE = re.compile(
    r"^[\s]*(?:\$\\textcircled\{|[①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳])"
)

# Page numbers: · N ·, — N —, bare small numbers (1-4 digits alone on a line)
_PAGE_NUMBER_RE = re.compile(
    r"^\s*(?:·\s*\d+\s*·|—\s*\d+\s*—|\d{1,4})\s*$"
)

# Running header heuristic: very short text (≤ 40 chars) that looks like a
# repeated journal name or year+issue pattern (e.g. "考古 2023年第5期").
_RUNNING_HEADER_RE = re.compile(
    r"^\s*(?:[\u4e00-\u9fff]{2,8}\s*\d{4}\s*年\s*第?\s*\d+\s*期"
    r"|第?\s*\d+\s*期"
    r"|[\u4e00-\u9fff]{2,10})\s*$"
)

# Sub-section numbering patterns for inferring heading level
_LEVEL2_RE = re.compile(r"^\s*[\(（][一二三四五六七八九十\d]+[\)）]")
_LEVEL3_RE = re.compile(r"^\s*\d+\.\s")


# ---------------------------------------------------------------------------
# CLI invocation (kept unchanged)
# ---------------------------------------------------------------------------

def _resolve_mineru_cli() -> Optional[str]:
    """Return the available MinerU CLI executable name, if any."""
    for cli_name in ("magic-pdf", "mineru"):
        if shutil.which(cli_name):
            return cli_name
    return None


def is_mineru_available() -> bool:
    """Check whether a supported MinerU CLI is on PATH."""
    return _resolve_mineru_cli() is not None


def run_mineru(
    pdf_path: str,
    output_dir: Optional[str] = None,
    parse_method: str = "auto",
    backend: str = "pipeline",
) -> Dict[str, Any]:
    """Run MinerU on a PDF and return parsed outputs.

    Parameters
    ----------
    pdf_path : str
        Absolute path to the input PDF.
    output_dir : str, optional
        Directory for MinerU output.  A temp dir is created when *None*.
    parse_method : str
        ``"auto"`` (default), ``"ocr"``, or ``"txt"``.

    Returns
    -------
    dict with keys:
        ``middle_json``  – parsed middle JSON (list of page dicts)
        ``markdown``     – full markdown text
        ``content_list`` – parsed content_list.json (list of items)
        ``output_dir``   – path to the MinerU output directory
    """
    mineru_cli = _resolve_mineru_cli()
    if not mineru_cli:
        raise RuntimeError("MinerU CLI not found on PATH (expected 'magic-pdf' or 'mineru')")

    pdf_path = os.path.abspath(pdf_path)
    if not os.path.isfile(pdf_path):
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    cleanup_temp = False
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="mineru_")
        cleanup_temp = True

    try:
        cmd = [
            mineru_cli,
            "-p", pdf_path,
            "-o", output_dir,
            "-m", parse_method,
        ]
        if backend:
            cmd.extend(["-b", backend])
        logger.info("Running MinerU: %s", " ".join(cmd))

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )

        if result.returncode != 0:
            logger.error("MinerU failed (rc=%d): %s", result.returncode, result.stderr)
            raise RuntimeError(f"MinerU failed: {result.stderr[:500]}")

        return _collect_mineru_outputs(output_dir, pdf_path)

    except subprocess.TimeoutExpired:
        raise RuntimeError("MinerU timed out after 300s")
    except Exception:
        if cleanup_temp and os.path.isdir(output_dir):
            shutil.rmtree(output_dir, ignore_errors=True)
        raise


def _collect_mineru_outputs(
    output_dir: str,
    pdf_path: str,
) -> Dict[str, Any]:
    """Walk the MinerU output tree and load the key artifacts."""

    pdf_stem = Path(pdf_path).stem
    # MinerU writes to <output_dir>/<pdf_stem>/{auto,ocr,txt}/
    base = Path(output_dir)

    # Find the method subdirectory (auto / ocr / txt)
    candidates = list(base.glob(f"{pdf_stem}/*"))
    if not candidates:
        # Sometimes MinerU nests one level deeper
        candidates = list(base.glob(f"**/{pdf_stem}/*"))
    if not candidates:
        raise FileNotFoundError(
            f"No MinerU output found under {output_dir} for {pdf_stem}"
        )

    method_dir = candidates[0]

    middle_json = _load_json(method_dir / f"{pdf_stem}_middle.json")
    content_list = _load_json(method_dir / f"{pdf_stem}_content_list.json")
    markdown = _load_text(method_dir / f"{pdf_stem}.md")

    return {
        "middle_json": middle_json,
        "markdown": markdown,
        "content_list": content_list,
        "output_dir": str(method_dir),
    }


# ---------------------------------------------------------------------------
# content_list → section-hierarchical document structure
# ---------------------------------------------------------------------------

def _infer_heading_level(text: str) -> int:
    """Infer a sub-section level from heading text patterns.

    Returns 1 for top-level headings, 2 for (一)/(二) style, 3 for 1. 2. style.
    """
    if _LEVEL3_RE.match(text):
        return 3
    if _LEVEL2_RE.match(text):
        return 2
    return 1


def _content_item_heading_level(item: Dict[str, Any], text: str) -> int:
    explicit_level = item.get("heading_level")
    if isinstance(explicit_level, int) and explicit_level > 0:
        return max(1, min(explicit_level, 6))

    text_level = item.get("text_level")
    if isinstance(text_level, int) and text_level > 0:
        return max(1, min(text_level, 6))

    return _infer_heading_level(text)


def _classify_discarded(text: str) -> str:
    """Classify a discarded item as 'footnote', 'page_number', or 'header'.

    Returns one of: ``"footnote"``, ``"skip_page_number"``, ``"skip_header"``.
    """
    stripped = text.strip()
    if not stripped:
        return "skip_page_number"

    if _FOOTNOTE_MARKER_RE.match(stripped):
        return "footnote"

    if _PAGE_NUMBER_RE.match(stripped):
        return "skip_page_number"

    if _RUNNING_HEADER_RE.match(stripped) and len(stripped) <= 40:
        return "skip_header"

    # Default: treat as footnote
    return "footnote"


def _extract_footnote_marker(text: str) -> str:
    """Try to extract a footnote marker (①, ②, etc.) from the text."""
    m = re.match(r"^\s*([①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳])", text)
    if m:
        return m.group(1)
    m = re.match(r"^\s*\$\\textcircled\{(\d+)\}", text)
    if m:
        circled_digits = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳"
        idx = int(m.group(1)) - 1
        if 0 <= idx < len(circled_digits):
            return circled_digits[idx]
        return f"({m.group(1)})"
    return ""


def content_list_to_document_structure(
    content_list: Any,
    page_number_map: Dict[int, int],
    images: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build a section-hierarchical document structure from content_list.json.

    Parameters
    ----------
    content_list : list
        Parsed ``content_list.json`` from MinerU — a list of items each with
        ``type``, optionally ``text_level``, ``page_idx``, ``bbox``, ``text``.
    page_number_map : dict
        Mapping from 0-based ``page_idx`` to actual journal page numbers.

    Returns
    -------
    dict with ``"pages"`` (list of page dicts) and ``"section_tree"`` (nested
    section hierarchy).
    """
    if not content_list or not isinstance(content_list, list):
        return {"pages": [], "section_tree": []}

    # ── Pass 1: organise items by page and track sections ──────────

    # page_idx → page data accumulator
    pages_acc: Dict[int, Dict[str, Any]] = {}
    # section tracking
    current_section: Optional[str] = None
    # section tree builder: list of (level, node_dict) for stack-based nesting
    section_stack: List[Tuple[int, Dict[str, Any]]] = []
    section_roots: List[Dict[str, Any]] = []

    for item in content_list:
        item_type = item.get("type", "")
        text = item.get("text", "").strip()
        page_idx = item.get("page_idx", 0)
        bbox = item.get("bbox", [])
        text_level = item.get("text_level")

        actual_page = page_number_map.get(page_idx, page_idx + 1)

        # Ensure page accumulator exists
        if page_idx not in pages_acc:
            pages_acc[page_idx] = {
                "page_number": actual_page,
                "page_idx": page_idx,
                "sections": [],
                "paragraphs": [],
                "footnotes": [],
                "_section_set": set(),
            }
        page = pages_acc[page_idx]

        # ── Heading ────────────────────────────────────────────────
        if text_level is not None and text:
            level = _content_item_heading_level(item, text)
            current_section = text

            # Record section on this page
            if text not in page["_section_set"]:
                page["sections"].append(text)
                page["_section_set"].add(text)

            # Add as heading paragraph
            para_n = len(page["paragraphs"]) + 1
            page["paragraphs"].append({
                "paragraph_id": f"p{actual_page}_para{para_n}",
                "text": text,
                "type": "heading",
                "section": current_section,
                "bbox": bbox,
            })

            # Build section tree
            node: Dict[str, Any] = {
                "title": text,
                "level": level,
                "page_number": actual_page,
                "children": [],
            }

            # Pop stack until we find a parent with lower level
            while section_stack and section_stack[-1][0] >= level:
                section_stack.pop()

            if section_stack:
                section_stack[-1][1]["children"].append(node)
            else:
                section_roots.append(node)

            section_stack.append((level, node))
            continue

        # ── Regular text paragraph ─────────────────────────────────
        if item_type == "text" and text:
            # Track current section on this page
            if current_section and current_section not in page["_section_set"]:
                page["sections"].append(current_section)
                page["_section_set"].add(current_section)

            para_n = len(page["paragraphs"]) + 1
            page["paragraphs"].append({
                "paragraph_id": f"p{actual_page}_para{para_n}",
                "text": text,
                "type": "text",
                "section": current_section,
                "bbox": bbox,
            })
            continue

        # ── Image ──────────────────────────────────────────────────
        if item_type == "image":
            if current_section and current_section not in page["_section_set"]:
                page["sections"].append(current_section)
                page["_section_set"].add(current_section)

            para_n = len(page["paragraphs"]) + 1
            caption = text if text else ""
            
            # Look for a matching extracted image
            image_path = None
            if images:
                img_bbox = item.get("bbox", [])
                img_page_idx = page_idx
                for img in images:
                    if img.get("page_idx") == img_page_idx:
                        # Match by bbox overlap if available
                        ibbox = img.get("bbox", [])
                        if ibbox and img_bbox:
                            # Check if bounding boxes overlap significantly
                            overlap_x = max(0, min(img_bbox[2], ibbox[2]) - max(img_bbox[0], ibbox[0]))
                            overlap_y = max(0, min(img_bbox[3], ibbox[3]) - max(img_bbox[1], ibbox[1]))
                            area_item = (img_bbox[2] - img_bbox[0]) * (img_bbox[3] - img_bbox[1])
                            if area_item > 0 and (overlap_x * overlap_y) / area_item > 0.5:
                                image_path = img.get("relative_path")
                                break
                        elif not image_path:
                            # Fallback: assign first unmatched image on this page
                            image_path = img.get("relative_path")
            
            para_data = {
                "paragraph_id": f"p{actual_page}_para{para_n}",
                "text": caption,
                "type": "image_caption",
                "section": current_section,
                "bbox": bbox,
            }
            if image_path:
                para_data["image_path"] = image_path
            page["paragraphs"].append(para_data)
            continue

        # ── Discarded items ────────────────────────────────────────
        if item_type == "discarded" and text:
            classification = _classify_discarded(text)

            if classification == "footnote":
                fn_n = len(page["footnotes"]) + 1
                marker = _extract_footnote_marker(text)
                page["footnotes"].append({
                    "footnote_id": f"p{actual_page}_fn{fn_n}",
                    "text": text,
                    "marker": marker,
                    "bbox": bbox,
                })
            # skip_page_number and skip_header → silently drop
            continue

    # ── Pass 2: assemble final pages list (sorted by page_idx) ─────

    pages_list: List[Dict[str, Any]] = []
    for page_idx in sorted(pages_acc):
        page = pages_acc[page_idx]
        # Remove internal tracking set
        page.pop("_section_set", None)
        pages_list.append(page)

    return {
        "pages": pages_list,
        "section_tree": section_roots,
    }


# ---------------------------------------------------------------------------
# content_list → flat paragraphs for nodes/merkle system
# ---------------------------------------------------------------------------

def content_list_to_paragraphs(
    content_list: Any,
    page_number_map: Dict[int, int],
) -> List[Tuple[int, List[str]]]:
    """Convert content_list to ``(actual_page_number, [paragraph_texts])``.

    Only includes ``type: "text"`` items (excludes discarded, images, and
    headings marked with ``text_level``).
    """
    if not content_list or not isinstance(content_list, list):
        return []

    # page_idx → list of paragraph texts
    page_texts: Dict[int, List[str]] = {}

    for item in content_list:
        if item.get("type") != "text":
            continue
        # Skip headings (they have text_level)
        if item.get("text_level") is not None:
            continue

        text = item.get("text", "").strip()
        if not text:
            continue

        page_idx = item.get("page_idx", 0)
        if page_idx not in page_texts:
            page_texts[page_idx] = []
        page_texts[page_idx].append(text)

    result: List[Tuple[int, List[str]]] = []
    for page_idx in sorted(page_texts):
        actual_page = page_number_map.get(page_idx, page_idx + 1)
        result.append((actual_page, page_texts[page_idx]))

    return result


# ---------------------------------------------------------------------------
# PDF image extraction via PyMuPDF
# ---------------------------------------------------------------------------

def extract_pdf_images(
    pdf_path: str,
    output_dir: str,
    source_id: str,
    min_width: int = 100,
    min_height: int = 100,
) -> List[Dict[str, Any]]:
    """Extract images from a PDF and save them to output_dir/images/.

    Parameters
    ----------
    pdf_path : str
        Path to the input PDF.
    output_dir : str
        Corpus artifact directory for this document (e.g. corpus/<slug>/).
    source_id : str
        Source identifier used for image filenames.
    min_width, min_height : int
        Minimum pixel dimensions to keep (filters out tiny icons/logos).

    Returns
    -------
    list of dicts with keys: page_idx, bbox, relative_path, width, height.
    """
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
                logger.debug("Failed to extract image xref=%s on page %d", xref, page_idx)
                continue

            if not base_image:
                continue

            width = base_image.get("width", 0)
            height = base_image.get("height", 0)

            # Filter out tiny images (decorative lines, icons, etc.)
            if width < min_width and height < min_height:
                continue

            ext = base_image.get("ext", "png")
            image_bytes = base_image.get("image")
            if not image_bytes:
                continue

            # Get the image bbox on the page for matching with content_list
            bbox = (0, 0, 0, 0)
            for item in page.get_text("dict")["blocks"]:
                if item.get("type") == 1 and item.get("image") == xref:
                    bbox = tuple(item.get("bbox", (0, 0, 0, 0)))
                    break

            filename = f"{source_id}_p{page_idx + 1}_img{img_index + 1}.{ext}"
            filepath = os.path.join(images_dir, filename)

            with open(filepath, "wb") as f:
                f.write(image_bytes)

            relative_path = f"images/{filename}"
            extracted.append({
                "page_idx": page_idx,
                "bbox": list(bbox),
                "relative_path": relative_path,
                "width": width,
                "height": height,
                "filename": filename,
            })

    doc.close()
    logger.info("Extracted %d images from %s into %s", len(extracted), pdf_path, images_dir)
    return extracted


# ---------------------------------------------------------------------------
# File I/O helpers (kept unchanged)
# ---------------------------------------------------------------------------

def _load_json(path: Path) -> Any:
    """Load JSON file, return empty dict on failure."""
    try:
        if path.is_file():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        logger.warning("Failed to load JSON: %s", path)
    return {}


def _load_text(path: Path) -> str:
    """Load text file, return empty string on failure."""
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    except Exception:
        logger.warning("Failed to load text: %s", path)
    return ""


def run(
    pdf_path: str,
    source_type: str = "scanned_pdf",
    config: Optional[IngestionConfig] = None,
) -> PipelineResult:
    from .scanned_common import (
        attach_image_paths_to_content_list,
        build_page_number_map_from_content_list,
        build_scanned_pipeline_result,
        make_images_tmpdir,
    )

    cfg = config or IngestionConfig()
    source_id = make_source_id(pdf_path)
    mineru_tmpdir = tempfile.mkdtemp(prefix="citeindex_mineru_raw_")
    images_tmpdir: Optional[str] = None

    try:
        mineru_output = run_mineru(
            pdf_path,
            output_dir=mineru_tmpdir,
            parse_method="ocr",
            backend=cfg.mineru_backend,
        )
        content_list = mineru_output.get("content_list")
        if not isinstance(content_list, list) or not content_list:
            raise RuntimeError("MinerU did not return a usable content_list")

        images_list: List[Dict[str, Any]] = []
        images_tmpdir = make_images_tmpdir(prefix="citeindex_mineru_imgs_")
        try:
            images_list = extract_pdf_images(pdf_path, images_tmpdir, source_id)
        except Exception:
            logger.warning("MinerU image export fallback failed", exc_info=True)

        if not images_list and images_tmpdir:
            shutil.rmtree(images_tmpdir, ignore_errors=True)
            images_tmpdir = None

        attach_image_paths_to_content_list(content_list, images_list)

        page_number_map = build_page_number_map_from_content_list(content_list)
        document_structure = content_list_to_document_structure(
            content_list,
            page_number_map,
            images=images_list,
        )
        page_paragraphs = content_list_to_paragraphs(content_list, page_number_map)

        result = build_scanned_pipeline_result(
            pdf_path=pdf_path,
            backend_name="mineru",
            source_type=source_type,
            config=cfg,
            content_list=content_list,
            document_structure=document_structure,
            page_paragraphs=page_paragraphs,
            images_tmpdir=images_tmpdir,
            images_list=images_list,
        )
        if result.document_json is not None:
            result.document_json["metadata"]["original_pdf_path"] = os.path.abspath(pdf_path)
        return result
    except Exception:
        if images_tmpdir:
            shutil.rmtree(images_tmpdir, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(mineru_tmpdir, ignore_errors=True)
