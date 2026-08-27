"""Layout analysis for PDF pages: column detection, footnote isolation,
page number extraction from headers/footers, reading order.

Two implementations:
  - analyze_document_layout_pymupdf4llm(): Uses PyMuPDF4LLM's GNN to classify
    blocks into DocLayNet labels (footnote, page-header, page-footer, etc.).
    Hybrid: GNN for headers/footers (85-100% AP), heuristic for footnotes
    (better recall than GNN's ~0.72 F1). Preferred when pymupdf4llm is installed.
  - analyze_document_layout(): Uses heuristic position/font-size analysis.
    Fallback when pymupdf4llm is not available.
"""

import logging
import re
import statistics
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from .common import extract_page_number_candidates

import fitz

logger = logging.getLogger(__name__)


def extract_text_blocks(page: fitz.Page) -> List[Dict[str, Any]]:
    """Extract text blocks with positional coordinates and font info from a page.

    Returns list of dicts with keys:
      page_number, block_id, text, bbox (x0,y0,x1,y1), font_size, font_name, lines
    """
    page_dict = page.get_text("dict")
    page_number = page.number + 1
    blocks: List[Dict[str, Any]] = []

    for block_idx, block in enumerate(page_dict.get("blocks", [])):
        if "lines" not in block:
            continue

        lines: List[Dict[str, Any]] = []
        font_sizes: List[float] = []
        font_names: List[str] = []

        for line in block["lines"]:
            spans = line.get("spans", [])
            line_text = "".join(span.get("text", "") for span in spans)
            if not line_text.strip():
                continue

            for span in spans:
                size = span.get("size", 0.0)
                name = span.get("font", "")
                char_count = len(span.get("text", ""))
                font_sizes.extend([size] * char_count)
                font_names.extend([name] * char_count)

            lines.append({
                "text": line_text,
                "bbox": list(line["bbox"]),
            })

        if not lines:
            continue

        full_text = "\n".join(ln["text"] for ln in lines)
        size_counter = Counter(font_sizes)
        name_counter = Counter(font_names)
        dominant_size = size_counter.most_common(1)[0][0] if size_counter else 0.0
        dominant_name = name_counter.most_common(1)[0][0] if name_counter else ""

        blocks.append({
            "page_number": page_number,
            "block_id": block_idx,
            "text": full_text,
            "bbox": list(block["bbox"]),
            "font_size": dominant_size,
            "font_name": dominant_name,
            "lines": lines,
        })

    return blocks


def detect_columns(
    blocks: List[Dict[str, Any]], page_width: float
) -> List[List[Dict[str, Any]]]:
    """Detect column boundaries using x-coordinate clustering.

    Groups blocks into columns by clustering their x0 coordinates.
    Returns list of columns, each column is a list of blocks, ordered left-to-right.
    """
    if not blocks:
        return []

    sorted_blocks = sorted(blocks, key=lambda b: b["bbox"][0])
    gap_threshold = page_width * 0.10

    columns: List[List[Dict[str, Any]]] = [[sorted_blocks[0]]]
    for block in sorted_blocks[1:]:
        prev_x1_values = [b["bbox"][2] for b in columns[-1]]
        prev_x1_max = max(prev_x1_values)
        cur_x0 = block["bbox"][0]

        if cur_x0 - prev_x1_max > gap_threshold:
            columns.append([block])
        else:
            columns[-1].append(block)

    return columns


def _find_footnote_separator(page: fitz.Page) -> Optional[float]:
    """Find a footnote separator line (footnote rule) on the page.

    Looks for thin horizontal vector drawings typical of LaTeX's
    ``\\footnoterule`` or similar separators. Returns the Y coordinate
    of the separator, or None if not found.

    Research shows this is the most reliable single signal for
    identifying the footnote zone (GROBID, pdfalto).
    """
    try:
        drawings = page.get_drawings()
    except Exception:
        return None

    for d in drawings:
        for item in d.get("items", []):
            # 're' = rectangle (common for footnote rules in many PDFs)
            if item[0] == "re":
                rect = item[1]
                height = rect[3] - rect[1]
                width = rect[2] - rect[0]
                # Thin rectangle (< 2pt tall, > 50pt wide) = horizontal rule.
                # Must be in the bottom half of the page (footnote rules are never
                # at the top — top rules are decorative or section separators).
                if height < 2 and width > 50 and rect[1] > page.rect.height * 0.5:
                    return rect[1]
            # 'l' = line
            elif item[0] == "l":
                p1, p2 = item[1], item[2]
                if abs(p1.y - p2.y) < 1 and abs(p2.x - p1.x) > 50:
                    if p1.y > page.rect.height * 0.5:
                        return p1.y

    return None


def _compute_font_size_clusters(
    blocks: List[Dict[str, Any]], n_clusters: int = 3
) -> Dict[float, str]:
    """Classify font sizes into clusters: small (footnotes), medium (body), large (headings).

    Uses median font size as the body reference (robust against footnote-heavy
    pages where small-font text may have more total characters).

    Research (PDFBoT, TETer) shows font-size clustering outperforms fixed
    ratio thresholds because it adapts to different document styles
    (e.g., 8pt+7pt vs 12pt+9pt).
    """
    if not blocks:
        return {}

    font_sizes = [b["font_size"] for b in blocks if b.get("font_size", 0) > 0]
    if not font_sizes:
        return {}

    # Body font = median (robust against footnote-heavy pages)
    body_size = statistics.median(font_sizes)

    result: Dict[float, str] = {}
    for sz in set(font_sizes):
        ratio = sz / body_size if body_size > 0 else 1.0
        if ratio < 0.93:
            result[sz] = "small"
        elif ratio > 1.15:
            result[sz] = "large"
        else:
            result[sz] = "medium"

    return result


def detect_footnotes(
    blocks: List[Dict[str, Any]],
    page_height: float,
    separator_y: Optional[float] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Identify footnotes using a composite of heuristics.

    Detection strategy (in priority order, based on PDF parsing research):

    1. **Separator line** (most reliable): If a footnote rule (thin horizontal
       line) is found in the bottom half of the page, blocks below it with
       small font or numeric marker are treated as footnotes.
    2. **Font size clustering** (adaptive): Classifies font sizes relative
       to the body text median, avoiding fixed percentage thresholds that
       break on different document styles (8pt/7pt vs 12pt/9pt).
    3. **Position + marker** (fallback): If no separator is found, uses
       position (bottom 25%) combined with either smaller font or a
       numeric marker at the start of the text.
    4. **Minimum length**: Blocks shorter than 5 words are excluded to
       avoid false positives from page numbers and running headers/footers.

    Returns (body_blocks, footnote_blocks)
    """
    if not blocks:
        return [], []

    font_sizes = [b["font_size"] for b in blocks if b["font_size"] > 0]
    if not font_sizes:
        return list(blocks), []

    font_clusters = _compute_font_size_clusters(blocks)

    # Minimum word count — page numbers and short labels are not footnotes.
    # Research (GROBID's repetitivePattern) shows that short, recurring
    # text at fixed positions is typically running headers/footers,
    # not content footnotes.
    MIN_FOOTNOTE_WORDS = 5

    def _is_footnote_text(text: str) -> bool:
        """True if text is long enough to be a meaningful footnote."""
        return len(text.split()) >= MIN_FOOTNOTE_WORDS

    body: List[Dict[str, Any]] = []
    footnotes: List[Dict[str, Any]] = []

    for block in blocks:
        y0 = block["bbox"][1]
        text = block["text"].lstrip()
        block_size = block["font_size"]
        cluster = font_clusters.get(block_size, "medium")

        # Skip short blocks — page numbers, running headers/footers
        if not _is_footnote_text(text):
            body.append(block)
            continue

        # Heuristic 1: Separator line defines the footnote zone
        if separator_y is not None and y0 >= separator_y:
            # Below separator — likely footnote. Verify with font size
            # or marker to avoid catching body text that extends below.
            is_small = cluster == "small"
            has_marker = (
                len(text) > 1
                and text[0].isdigit()
                and text[1] in (" ", ".", "\t")
            )
            if is_small or has_marker:
                footnotes.append(block)
                continue

        # Heuristic 2: Position + font cluster + marker (fallback)
        # Bottom 25% AND (small font cluster OR numeric marker)
        in_bottom = y0 >= page_height * 0.75
        is_small = cluster == "small"
        has_marker = (
            len(text) > 1
            and text[0].isdigit()
            and text[1] in (" ", ".", "\t")
        )

        if in_bottom and (is_small or has_marker):
            footnotes.append(block)
        else:
            body.append(block)

    return body, footnotes


# ---------------------------------------------------------------------------
# Page number detection from headers/footers
# ---------------------------------------------------------------------------

# Top/bottom fraction of page considered header/footer zone.
# Conservative: only the outer 12% — avoids body text that spills into margins.
_HEADER_ZONE = 0.12
_FOOTER_ZONE = 0.88

# Maximum word count for a block to be considered a header/footer.
# Running headers like "TOOLS 49" are 2 words; standalone "25" is 1 word.
_MAX_HEADER_FOOTER_WORDS = 8


def detect_page_numbers(
    blocks: List[Dict[str, Any]],
    page_height: float,
    header_zone: float = _HEADER_ZONE,
    footer_zone: float = _FOOTER_ZONE,
    max_words: int = _MAX_HEADER_FOOTER_WORDS,
) -> Tuple[List[Dict[str, Any]], List[int]]:
    """Identify page numbers in header/footer blocks.

    Mirrors ``detect_footnotes``: scans raw layout blocks, identifies header/
    footer blocks by position, extracts numeric page-number candidates.

    Detection strategy:

    1. **Position**: Blocks in the top 12% or bottom 12% of the page
       are candidate headers/footers.
    2. **Length**: Only short blocks (≤ 8 words) qualify — longer text
       at page edges is likely marginalia, not a simple header.
    3. **Font size** (for block classification only): Blocks with body-size
       font in the header/footer zone are likely footnotes or body text
       that spills into the margin — they are NOT classified as header/
       footer blocks (and thus NOT removed from body paragraphs).  However,
       their numeric candidates are still extracted because body-font
       headers like ``"TOOLS 49"`` use the same font as body text.
    4. **Extraction**: Numeric values are extracted via regex patterns
       covering common formats (bare, decorated, prefixed).

    Returns
    -------
    (header_footer_blocks, candidate_page_numbers)
        header_footer_blocks: blocks to remove from body (small-font or
            bare-number headers/footers only).
        candidate_page_numbers: all numeric candidates from the header/
            footer zone, regardless of font size.
    """
    if not blocks:
        return [], []

    font_clusters = _compute_font_size_clusters(blocks)

    header_footer: List[Dict[str, Any]] = []
    candidates: List[int] = []

    for block in blocks:
        y0 = block["bbox"][1]
        text = block["text"].strip()
        block_size = block["font_size"]

        in_header = y0 < page_height * header_zone
        in_footer = y0 >= page_height * footer_zone

        if not (in_header or in_footer):
            continue

        # Only short blocks are headers/footers
        if len(text.split()) > max_words:
            continue

        # Extract page number candidates from ALL short blocks in the
        # header/footer zone, regardless of font size.  Many academic PDFs
        # use body-size font for running headers (e.g., "TOOLS 49" in 11pt
        # when body is also 11pt).
        candidates.extend(extract_page_number_candidates(text))

        # But only classify as "header/footer block to remove" if the font
        # is smaller than body OR the text is a bare number.  Body-font
        # blocks in the header zone (like "Grammar (Winona Lake, ...)")
        # are NOT headers — they're body text that starts high on the page.
        cluster = font_clusters.get(block_size, "medium")
        is_small_or_large = cluster in ("small", "large")
        is_bare_number = bool(re.match(r"^\s*\d{1,4}\s*$", text))

        if is_small_or_large or is_bare_number:
            header_footer.append(block)

    return header_footer, candidates


def resolve_reading_order(
    columns: List[List[Dict[str, Any]]], footnotes: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Construct deterministic reading order across columns.

    Algorithm from YAML:
    1. Sort columns left to right
    2. Sort blocks top to bottom within each column
    3. Append footnotes after body

    Returns ordered list of all blocks.
    """
    ordered: List[Dict[str, Any]] = []

    cols_sorted = sorted(columns, key=lambda col: min(b["bbox"][0] for b in col))
    for col in cols_sorted:
        for block in sorted(col, key=lambda b: b["bbox"][1]):
            ordered.append(block)

    for fn in sorted(footnotes, key=lambda b: b["bbox"][1]):
        ordered.append(fn)

    return ordered


def analyze_page_layout(page: fitz.Page, page_number: int) -> Dict[str, Any]:
    """Main entry point: analyze a single page's layout.

    Returns structured dict:
    {
        "page_number": int,
        "columns": [...],
        "footnotes": [...],
        "headers_footers": [
            {"text": str, "bbox": [...]}
        ],
        "page_number_candidates": [int, ...],
        "ordered_text": str
    }
    """
    blocks = extract_text_blocks(page)

    # Detect footnote separator line (most reliable signal)
    separator_y = _find_footnote_separator(page)

    # Detect page numbers and header/footer blocks FIRST
    # (same pattern as footnotes: find from raw layout before filtering)
    header_footer_blocks, page_num_candidates = detect_page_numbers(
        blocks, page.rect.height
    )

    body_blocks, footnote_blocks = detect_footnotes(
        blocks, page.rect.height, separator_y=separator_y
    )

    # Remove header/footer blocks from body (they are not content)
    hf_block_ids = {id(b) for b in header_footer_blocks}
    body_blocks = [b for b in body_blocks if id(b) not in hf_block_ids]

    columns = detect_columns(body_blocks, page.rect.width)
    ordered = resolve_reading_order(columns, footnote_blocks)

    column_dicts: List[Dict[str, Any]] = []
    for col_idx, col_blocks in enumerate(columns):
        col_sorted = sorted(col_blocks, key=lambda b: b["bbox"][1])
        paragraphs: List[Dict[str, Any]] = []
        for para_idx, block in enumerate(col_sorted, start=1):
            paragraphs.append({
                "paragraph_id": f"p{page_number}_c{col_idx}_para{para_idx}",
                "text": block["text"],
                "lines": block["lines"],
                "bbox": block["bbox"],
            })
        column_dicts.append({
            "column_id": col_idx,
            "paragraphs": paragraphs,
        })

    fn_dicts: List[Dict[str, Any]] = []
    for fn_idx, fn_block in enumerate(
        sorted(footnote_blocks, key=lambda b: b["bbox"][1]), start=1
    ):
        fn_dicts.append({
            "footnote_id": f"p{page_number}_fn{fn_idx}",
            "text": fn_block["text"],
            "bbox": fn_block["bbox"],
        })

    # Build header/footer dicts
    hf_dicts: List[Dict[str, Any]] = []
    for hf_block in sorted(header_footer_blocks, key=lambda b: b["bbox"][1]):
        hf_dicts.append({
            "text": hf_block["text"],
            "bbox": hf_block["bbox"],
        })

    ordered_text = "\n\n".join(block["text"] for block in ordered)

    return {
        "page_number": page_number,
        "columns": column_dicts,
        "footnotes": fn_dicts,
        "headers_footers": hf_dicts,
        "page_number_candidates": page_num_candidates,
        "ordered_text": ordered_text,
    }


def analyze_document_layout(pdf_path: str) -> List[Dict[str, Any]]:
    """Analyze layout for all pages in a PDF document.

    Returns list of page layout dicts from analyze_page_layout().
    """
    doc = fitz.open(pdf_path)
    results: List[Dict[str, Any]] = []
    for page_idx in range(doc.page_count):
        page = doc[page_idx]
        page_number = page_idx + 1
        try:
            layout = analyze_page_layout(page, page_number)
        except Exception:
            logger.exception("Layout analysis failed for page %d", page_number)
            layout = {
                "page_number": page_number,
                "columns": [],
                "footnotes": [],
                "headers_footers": [],
                "page_number_candidates": [],
                "ordered_text": "",
            }
        results.append(layout)
    doc.close()
    return results


def _gnn_classified_blocks_to_layout(
    pdoc,
    pdf_path: str,
) -> List[Dict[str, Any]]:
    """Convert PyMuPDF4LLM ParsedDocument to our layout dict shape.

    Uses GNN classification for headers/footers (excellent accuracy)
    but falls back to our heuristic detect_footnotes() for footnote
    detection (better recall — GNN F1 ~0.72 misses footnotes that
    lack clear spatial separation from body text).

    Returns list of page layout dicts with the same keys as
    analyze_page_layout().
    """
    # Open the PDF again for footnote-separator detection
    # (which needs fitz.Page drawing access not available through ParsedDocument)
    sep_doc = fitz.open(pdf_path)
    separator_cache: Dict[int, Optional[float]] = {}

    results: List[Dict[str, Any]] = []

    for page in pdoc.pages:
        page_number = page.page_number  # 1-based

        # ── Collect GNN-classified blocks per category ──
        gnn_footnote_blocks: List[Dict[str, Any]] = []
        gnn_header_blocks: List[Dict[str, Any]] = []
        gnn_footer_blocks: List[Dict[str, Any]] = []
        gnn_body_blocks: List[Dict[str, Any]] = []

        for box in page.boxes:
            btype = box.boxclass
            text = ""
            lines_data: List[Dict[str, Any]] = []
            font_sizes: List[float] = []

            if box.textlines:
                for tl in box.textlines:
                    line_text = "".join(s.get("text", "") for s in tl.get("spans", []))
                    lines_data.append({
                        "text": line_text,
                        "bbox": list(tl.get("bbox", [])) if isinstance(tl.get("bbox", []), (list, tuple)) else [],
                    })
                    for s in tl.get("spans", []):
                        char_count = len(s.get("text", ""))
                        font_sizes.extend([s.get("size", 0.0)] * char_count)
                text = "\n".join(ln["text"] for ln in lines_data)

            # Dominant font size from spans
            dom_size = Counter(font_sizes).most_common(1)[0][0] if font_sizes else 0.0

            block_dict = {
                "page_number": page_number,
                "block_id": 0,
                "text": text,
                "bbox": [box.x0, box.y0, box.x1, box.y1],
                "font_size": dom_size,
                "font_name": "",
                "lines": lines_data,
                "_gnn_class": btype,  # preserve for hybrid footnote detection
            }

            if btype == "footnote":
                gnn_footnote_blocks.append(block_dict)
            elif btype == "page-header":
                gnn_header_blocks.append(block_dict)
            elif btype == "page-footer":
                gnn_footer_blocks.append(block_dict)
            else:
                gnn_body_blocks.append(block_dict)

        # ── Hybrid footnote detection ──
        # Run our heuristic on body blocks to catch footnotes the GNN missed.
        # However, only reclassify blocks as footnotes if:
        #   - The GNN classified them as "text" (not section-header, title, etc.)
        #   - Our heuristic has high confidence (separator line OR small font + marker)
        # This prevents false positives on OCR'd pages where body text is
        # positioned low and looks footnotelike to a position-only heuristic.
        separator_y = separator_cache.get(page_number)
        if separator_y is None and 0 <= page_number - 1 < len(sep_doc):
            separator_y = _find_footnote_separator(sep_doc[page_number - 1])
            separator_cache[page_number] = separator_y

        # Only run heuristic on blocks the GNN classified as "text"
        # (skip section-header, title, list-item — these are clearly not footnotes)
        heuristic_candidate_blocks = [
            b for b in gnn_body_blocks
            if b.get("_gnn_class") == "text"
        ]
        heuristic_body, heuristic_footnotes = detect_footnotes(
            heuristic_candidate_blocks, page.height, separator_y=separator_y,
        )

        # GNN-classified non-text body blocks (section-header, title, etc.)
        # are always body — never reclassify as footnotes
        gnn_non_text_body = [
            b for b in gnn_body_blocks
            if b.get("_gnn_class") != "text"
        ]

        # Merge: heuristic footnotes + GNN footnotes (dedup by signature)
        all_footnote_blocks = list(heuristic_footnotes)
        existing_fn_sigs = {" ".join(b["text"][:200].split()) for b in all_footnote_blocks}
        for fn_block in gnn_footnote_blocks:
            sig = " ".join(fn_block["text"][:200].split())
            if sig not in existing_fn_sigs:
                all_footnote_blocks.append(fn_block)
                existing_fn_sigs.add(sig)

        # Body blocks = heuristic body (text-only, footnotes removed)
        # + non-text GNN body blocks (section-header, title, etc.)
        final_body_blocks = gnn_non_text_body + heuristic_body

        # Remove internal _gnn_class before building output
        for b in final_body_blocks:
            b.pop("_gnn_class", None)

        # ── Header/footer dicts ──
        hf_dicts: List[Dict[str, Any]] = []
        for hf_block in sorted(gnn_header_blocks + gnn_footer_blocks, key=lambda b: b["bbox"][1]):
            hf_dicts.append({
                "text": hf_block["text"],
                "bbox": hf_block["bbox"],
            })

        # ── Page number candidates from headers/footers ──
        page_num_candidates: List[int] = []
        for hf_block in gnn_header_blocks + gnn_footer_blocks:
            page_num_candidates.extend(
                extract_page_number_candidates(hf_block["text"])
            )

        # ── Column detection on body blocks ──
        columns = detect_columns(final_body_blocks, page.width)

        # ── Build output dicts (same shape as analyze_page_layout) ──
        column_dicts: List[Dict[str, Any]] = []
        for col_idx, col_blocks in enumerate(columns):
            col_sorted = sorted(col_blocks, key=lambda b: b["bbox"][1])
            paragraphs: List[Dict[str, Any]] = []
            for para_idx, block in enumerate(col_sorted, start=1):
                paragraphs.append({
                    "paragraph_id": f"p{page_number}_c{col_idx}_para{para_idx}",
                    "text": block["text"],
                    "lines": block["lines"],
                    "bbox": block["bbox"],
                })
            column_dicts.append({
                "column_id": col_idx,
                "paragraphs": paragraphs,
            })

        fn_dicts: List[Dict[str, Any]] = []
        for fn_idx, fn_block in enumerate(
            sorted(all_footnote_blocks, key=lambda b: b["bbox"][1]), start=1
        ):
            fn_dicts.append({
                "footnote_id": f"p{page_number}_fn{fn_idx}",
                "text": fn_block["text"],
                "bbox": fn_block["bbox"],
            })

        # Ordered text: body + footnotes
        ordered_blocks = sorted(final_body_blocks, key=lambda b: (b["bbox"][0], b["bbox"][1]))
        ordered_blocks.extend(sorted(all_footnote_blocks, key=lambda b: b["bbox"][1]))
        ordered_text = "\n\n".join(b["text"] for b in ordered_blocks)

        results.append({
            "page_number": page_number,
            "columns": column_dicts,
            "footnotes": fn_dicts,
            "headers_footers": hf_dicts,
            "page_number_candidates": page_num_candidates,
            "ordered_text": ordered_text,
        })

    sep_doc.close()
    return results


def analyze_document_layout_pymupdf4llm(pdf_path: str) -> List[Dict[str, Any]]:
    """Analyze layout using PyMuPDF4LLM's GNN classification.

    Hybrid approach:
      - **GNN for headers/footers**: PyMuPDF4LLM's GNN (page-header,
        page-footer labels at 85-100% AP) reliably separates running
        headers/footers from body text.
      - **Heuristics for footnotes**: Our ``detect_footnotes()`` has
        better recall than the GNN (which F1 ~0.72 — misses footnotes
        that lack clear spatial separation from body text).

    Returns the same dict shape as ``analyze_document_layout()`` so all
    downstream consumers work unchanged.

    Falls back to ``analyze_document_layout()`` if pymupdf4llm is not
    installed or if ``parse_document()`` fails.
    """
    try:
        from pymupdf4llm.helpers.document_layout import parse_document
    except ImportError:
        logger.warning("pymupdf4llm not installed, falling back to heuristic layout")
        return analyze_document_layout(pdf_path)

    import pymupdf

    doc = pymupdf.open(pdf_path)
    try:
        pdoc = parse_document(doc, show_progress=False)
        return _gnn_classified_blocks_to_layout(pdoc, pdf_path)
    except Exception:
        logger.warning("pymupdf4llm parse_document() failed, falling back", exc_info=True)
        doc.close()
        return analyze_document_layout(pdf_path)
    finally:
        if not doc.is_closed:
            doc.close()


def build_page_number_map(
    page_layouts: List[Dict[str, Any]],
) -> Dict[int, int]:
    """Build physical→printed page number map from layout analysis results.

    Uses ``_select_continuous_sequence`` (from dspy_extract) to find the
    best offset that explains the most candidate page numbers as a
    continuous sequence.

    Parameters
    ----------
    page_layouts : list of dict
        Per-page layout dicts from ``analyze_document_layout()``.

    Returns
    -------
    dict
        Mapping ``{0-based physical page index → printed page number}``.
        Empty dict if no consistent page numbering is found.
    """
    from .dspy_extract import _select_continuous_sequence

    candidates: Dict[int, List[int]] = {}
    for layout in page_layouts:
        # page_number in layout is 1-based physical index
        physical_1based = layout.get("page_number")
        if not isinstance(physical_1based, int):
            continue
        idx_0based = physical_1based - 1

        page_candidates = layout.get("page_number_candidates", [])
        if page_candidates:
            candidates[idx_0based] = page_candidates

    return _select_continuous_sequence(candidates)
