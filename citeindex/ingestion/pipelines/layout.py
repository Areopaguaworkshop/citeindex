"""Layout analysis for PDF pages: column detection, footnote isolation, reading order."""

import logging
import statistics
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

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
    median_size = statistics.median(font_sizes)

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
        "columns": [
            {
                "column_id": int,
                "paragraphs": [
                    {"paragraph_id": str, "text": str, "lines": [...], "bbox": [...]}
                ]
            }
        ],
        "footnotes": [
            {"footnote_id": str, "text": str, "bbox": [...]}
        ],
        "ordered_text": str
    }
    """
    blocks = extract_text_blocks(page)

    # Detect footnote separator line (most reliable signal)
    separator_y = _find_footnote_separator(page)

    body_blocks, footnote_blocks = detect_footnotes(
        blocks, page.rect.height, separator_y=separator_y
    )
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

    ordered_text = "\n\n".join(block["text"] for block in ordered)

    return {
        "page_number": page_number,
        "columns": column_dicts,
        "footnotes": fn_dicts,
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
                "ordered_text": "",
            }
        results.append(layout)
    doc.close()
    return results
