"""Layout cues for a document's opening pages.

This stage only *records* layout statistics (block geometry, font size,
weight) so later stages can reason about them.  It never feeds data back
into extraction, so scores cannot change because of it.

Two sources of geometry are supported:

* digital pages are read from the PDF text layer (``page.get_text("dict")``);
* scanned pages reuse the OCR backend's ``content_list`` bounding boxes when
  the caller has one.

The module is deliberately dependency-light: the standard library plus
PyMuPDF, no network and no model calls.
"""

import logging
import statistics
from collections.abc import Iterable
from typing import Any

import pymupdf

logger = logging.getLogger(__name__)

#: Pages inspected by default.  Front matter is where layout cues matter.
DEFAULT_PAGES = (1, 2, 3, 4, 5, 6)

#: PyMuPDF span flag bit that marks bold text.
_BOLD_FLAG = 16


def _round1(value: float) -> float:
    return round(float(value), 1)


def _requested_pages(pages: Iterable[int] | None, page_count: int | None = None) -> list[int]:
    """Return unique 1-based page numbers in ascending order.

    ``None`` means every page of the document; the caller passes page_count.
    """
    if pages is None:
        return list(range(1, (page_count or 0) + 1))
    requested: list[int] = []
    for page in pages:
        try:
            number = int(page)
        except (TypeError, ValueError):
            continue
        if number >= 1 and number not in requested:
            requested.append(number)
    return sorted(requested)


def _numeric_bbox(value: Any) -> list[float] | None:
    """Return a four-number bbox, or ``None`` when the value is unusable."""
    if isinstance(value, str) or not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    numbers: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        numbers.append(float(item))
    return numbers


def _block_record(
    *,
    block_index: int,
    bbox: list[float],
    size: float,
    median_size: float,
    bold: bool,
    line_count: int,
    chars: int,
) -> dict[str, Any]:
    """Build one feature record, leaving normalised coordinates to the caller."""
    return {
        "block_index": block_index,
        "bbox": [round(value, 2) for value in bbox],
        "size": _round1(size),
        "median_size": _round1(median_size),
        "bold": bool(bold),
        "line_count": int(line_count),
        "chars": int(chars),
    }


def _digital_page_blocks(page: pymupdf.Page) -> list[dict[str, Any]]:
    """Convert one text-layer page into feature records."""
    page_dict = page.get_text("dict")
    blocks: list[dict[str, Any]] = []
    for block_index, block in enumerate(page_dict.get("blocks", [])):
        bbox = _numeric_bbox(block.get("bbox"))
        lines = block.get("lines")
        if bbox is None or not isinstance(lines, list):
            continue
        spans = [span for line in lines for span in line.get("spans", [])]
        if not spans:
            continue
        sizes = [float(span.get("size") or 0.0) for span in spans]
        chars = sum(len(span.get("text") or "") for span in spans)
        blocks.append(
            _block_record(
                block_index=block_index,
                bbox=bbox,
                size=max(sizes),
                median_size=statistics.median(sizes),
                bold=any(int(span.get("flags") or 0) & _BOLD_FLAG for span in spans),
                line_count=len(lines),
                chars=chars,
            )
        )
    return blocks


def _content_list_page_items(content_list: Iterable[Any]) -> dict[int, list[dict[str, Any]]]:
    """Group OCR content items by their 0-based ``page_idx``."""
    grouped: dict[int, list[dict[str, Any]]] = {}
    for item in content_list:
        if not isinstance(item, dict):
            continue
        try:
            page_idx = int(item.get("page_idx", 0))
        except (TypeError, ValueError):
            page_idx = 0
        grouped.setdefault(page_idx, []).append(item)
    return grouped


def _ocr_page_blocks(items: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Convert OCR content items into feature records.

    ``size`` is the bbox height because OCR backends do not report font sizes;
    ``median_size`` mirrors it so downstream code can treat both sources alike.
    """
    blocks: list[dict[str, Any]] = []
    for block_index, item in enumerate(items):
        text = item.get("text")
        bbox = _numeric_bbox(item.get("bbox"))
        # Items without text (figures, tables) or without a usable bbox are
        # not layout evidence we can compare against the text layer.
        if bbox is None or not isinstance(text, str):
            continue
        height = max(0.0, bbox[3] - bbox[1])
        line_count = len([line for line in text.splitlines() if line.strip()]) or 1
        blocks.append(
            _block_record(
                block_index=block_index,
                bbox=bbox,
                size=height,
                median_size=height,
                bold=False,
                line_count=line_count,
                chars=len(text),
            )
        )
    return blocks


def _page_dimensions(rect: pymupdf.Rect, blocks: list[dict[str, Any]]) -> tuple[float, float]:
    """Page size, widened when block boxes extend past the text-layer page box.

    OCR bboxes are sometimes reported in a different unit than the PDF page
    rectangle.  Widening keeps the normalised coordinates in ``0..1`` instead
    of silently clipping them.
    """
    width = float(rect.width) or 1.0
    height = float(rect.height) or 1.0
    for block in blocks:
        width = max(width, block["bbox"][2])
        height = max(height, block["bbox"][3])
    return width, height


def extract_layout_features(
    pdf_path: str,
    *,
    pages: Iterable[int] | None = None,
    content_list: Iterable[Any] | None = None,
) -> dict[str, Any]:
    """Capture layout cues for the requested pages of ``pdf_path``.

    Parameters
    ----------
    pdf_path : str
        PDF to inspect.
    pages : iterable of int, optional
        1-based page numbers; pages outside the document are ignored.
        ``None`` (the default) captures every page.
    content_list : list, optional
        OCR content items (MinerU shape: ``page_idx``, ``bbox``, ``text``).
        When given, pages covered by OCR geometry use those bounding boxes
        instead of the text layer.

    Returns
    -------
    dict
        ``{"source", "page_count", "pages": [{"page", "width", "height", "blocks"}]}``.
        Unreadable input yields ``{"source", "pages": [], "error"}`` and never
        raises.
    """
    source = "mineru" if content_list is not None else "pymupdf"
    ocr_pages = _content_list_page_items(content_list) if content_list is not None else {}

    try:
        document = pymupdf.open(pdf_path)
    except Exception:
        logger.warning("Layout feature capture could not open %s", pdf_path)
        return {"source": source, "pages": [], "error": f"cannot read PDF: {pdf_path}"}

    try:
        page_count = int(document.page_count)
        if page_count <= 0:
            return {"source": source, "pages": [], "error": "PDF has no pages"}

        requested = _requested_pages(pages, page_count)
        collected: list[dict[str, Any]] = []
        for page_number in requested:
            if page_number > page_count:
                continue
            try:
                page = document[page_number - 1]
                items = ocr_pages.get(page_number - 1)
                blocks = _ocr_page_blocks(items) if items else _digital_page_blocks(page)
                width, height = _page_dimensions(page.rect, blocks)
            except Exception:
                logger.warning("Layout feature capture failed on page %d", page_number, exc_info=True)
                return {
                    "source": source,
                    "pages": [],
                    "error": f"cannot read page {page_number}",
                }
            for block in blocks:
                x0, y0, x1, y1 = block["bbox"]
                block["x_center"] = round(min(1.0, max(0.0, ((x0 + x1) / 2) / width)), 4)
                block["y_top"] = round(min(1.0, max(0.0, y0 / height)), 4)
            collected.append(
                {
                    "page": page_number,
                    "width": round(width, 2),
                    "height": round(height, 2),
                    "blocks": blocks,
                }
            )
        return {"source": source, "page_count": page_count, "pages": collected}
    except Exception:
        logger.warning("Layout feature capture failed for %s", pdf_path, exc_info=True)
        return {"source": source, "pages": [], "error": f"cannot inspect PDF: {pdf_path}"}
    finally:
        document.close()
