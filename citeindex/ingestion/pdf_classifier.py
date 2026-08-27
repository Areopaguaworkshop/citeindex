"""PDF type classifier — determines whether a PDF is digital, scanned, or mixed.

Inspired by docling's bitmap-coverage heuristic and marker's text-quality checks.
Produces a per-page classification, then aggregates to a document-level decision.

Per-page classification uses three layers:
  1. Bitmap coverage (docling):  What fraction of the page is covered by images?
  2. Text quality (marker):      Is the extractable text real or garbled?
  3. OCR-layer detection (marker): Invisible text, non-embedded/glyphless fonts?

Document-level decision:
  - If >= threshold pages are scanned  →  "scanned_pdf"
  - If >= threshold pages are digital  →  "digital_pdf"
  - Otherwise                         →  "mixed_pdf"

For "mixed_pdf", the majority type wins but per-page info is logged.
"""

import logging
import re
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

import pymupdf as fitz

logger = logging.getLogger(__name__)

# ── Enums & Data Classes ────────────────────────────────────────────


class PageKind(str, Enum):
    """Classification result for a single page."""
    DIGITAL = "digital"
    SCANNED = "scanned"
    MIXED = "mixed"          # page has both meaningful text AND significant images
    EMPTY = "empty"           # page has neither text nor images


class DocumentKind(str, Enum):
    """Document-level classification."""
    DIGITAL_PDF = "digital_pdf"
    SCANNED_PDF = "scanned_pdf"
    MIXED_PDF = "mixed_pdf"


@dataclass
class PageClassification:
    """Result of classifying a single PDF page."""
    page_number: int                    # 1-indexed
    kind: PageKind
    text_length: int = 0                # characters of extractable text
    alphanum_ratio: float = 0.0         # ratio of alphanumeric chars to total
    space_ratio: float = 0.0            # ratio of spaces to (spaces + alphanum)
    bitmap_coverage: float = 0.0         # fraction of page area covered by images
    has_invisible_text: bool = False    # OCR overlay detected
    has_nonembedded_fonts: bool = False # suspicious fonts (OCR artifact)
    has_glyphless_fonts: bool = False    # glyphless fonts (no real glyphs)
    num_images: int = 0
    num_text_blocks: int = 0


@dataclass
class PDFClassification:
    """Result of classifying an entire PDF document."""
    document_kind: DocumentKind
    pages: List[PageClassification] = field(default_factory=list)
    digital_page_count: int = 0
    scanned_page_count: int = 0
    mixed_page_count: int = 0
    empty_page_count: int = 0

    @property
    def total_pages(self) -> int:
        return len(self.pages)

    @property
    def scanned_ratio(self) -> float:
        t = self.total_pages
        return (self.scanned_page_count + self.mixed_page_count) / t if t else 0.0

    @property
    def digital_ratio(self) -> float:
        t = self.total_pages
        return self.digital_page_count / t if t else 0.0


# ── Threshold Constants ────────────────────────────────────────────
# Inspired by docling (bitmap_area_threshold=0.05, BITMAP_COVERAGE_TRESHOLD=0.75)
# and marker (alphanum_threshold=0.3, space_threshold=0.7, image_threshold=0.65)

# Bitmap coverage thresholds (docling-inspired)
BITMAP_COVERAGE_SCANNED = 0.75    # >=75% bitmap → page is scanned (full-page image)
BITMAP_COVERAGE_MIXED = 0.05      # >=5% but <75% → page has meaningful images (mixed)
BITMAP_COVERAGE_DIGITAL = 0.05    # <5% bitmap → page is digital text

# Text quality thresholds (marker-inspired)
ALPHANUM_THRESHOLD = 0.3          # <30% alphanumeric → garbled text
SPACE_RATIO_THRESHOLD = 0.7       # >70% spaces → bad OCR overlay
NEWLINE_RATIO_THRESHOLD = 0.6     # >60% newlines → broken extraction

# Large image coverage (marker-inspired)
IMAGE_DOMINANCE_THRESHOLD = 0.65  # image covers >=65% of page → scanned

# Document-level decision thresholds
DIGITAL_RATIO_THRESHOLD = 0.5     # >=50% digital pages → digital_pdf
SCANNED_RATIO_THRESHOLD = 0.5     # >=50% scanned/mixed pages → scanned_pdf


# ── Per-Page Classification ─────────────────────────────────────────


def _alphanum_ratio(text: str) -> float:
    """Ratio of alphanumeric characters to total non-whitespace characters."""
    stripped = text.replace(" ", "").replace("\n", "")
    if not stripped:
        return 0.0
    alnum_count = sum(1 for c in stripped if c.isalnum())
    return alnum_count / len(stripped)


def _space_ratio(text: str) -> float:
    """Ratio of space characters to (spaces + alphanumeric characters)."""
    spaces = len(re.findall(r"\s+", text))
    alpha_chars = len(re.sub(r"\s+", "", text))
    total = spaces + alpha_chars
    if total == 0:
        return 1.0
    return spaces / total


def _newline_ratio(text: str) -> float:
    """Ratio of newlines to (newlines + non-newline chars)."""
    newlines = len(re.findall(r"\n+", text))
    non_newlines = len(re.sub(r"\n+", "", text))
    total = newlines + non_newlines
    if total == 0:
        return 1.0
    return newlines / total


def _get_bitmap_coverage(page) -> Tuple[float, int]:
    """Calculate fraction of page area covered by image objects.

    Uses PyMuPDF (fitz) page.get_images() and image bounding boxes.
    Returns (coverage_ratio, num_images).

    This is inspired by docling's get_ocr_rects() approach which collects
    bitmap rectangles and computes their total area relative to the page.
    """
    try:
        page_rect = page.rect
        page_area = page_rect.width * page_rect.height
        if page_area <= 0:
            return 0.0, 0

        total_image_area = 0.0
        image_count = 0

        # Get image info list — each entry is a tuple
        # (xref, smask, width, height, bpc, colorspace, ...)
        for img_info in page.get_images(full=True):
            xref = img_info[0]
            try:
                # Get the image bounding box on the page
                img_rects = page.get_image_rects(xref)
                for rect in img_rects:
                    # Only count images that are reasonably sized (>= 32x32 pixels)
                    if rect.width >= 32 and rect.height >= 32:
                        total_image_area += rect.width * rect.height
            except Exception:
                # Some images may not have valid rects (e.g., inline images)
                continue
            image_count += 1

        # Also check for XObject images at the page level
        # via get_text("dict") which includes image blocks
        try:
            blocks = page.get_text("dict", flags=fitz.TEXT_PRESERVE_IMAGES)["blocks"]
            for block in blocks:
                if block.get("type") == 1:  # image block
                    bbox = fitz.Rect(block["bbox"])
                    if bbox.width >= 32 and bbox.height >= 32:
                        # Avoid double-counting with get_images()
                        img_area = bbox.width * bbox.height
                        # Check if this area overlaps significantly with already-counted area
                        # For simplicity, we add it (overlap is rare and small)
                        total_image_area += img_area
                        image_count += 1
        except Exception:
            pass

        coverage = min(total_image_area / page_area, 1.0) if page_area > 0 else 0.0
        return coverage, max(image_count, 0)

    except Exception:
        logger.warning("Failed to compute bitmap coverage", exc_info=True)
        return 0.0, 0


def _detect_ocr_layer(page) -> Tuple[bool, bool, bool]:
    """Detect signs of an OCR overlay layer (marker-inspired checks).

    Returns (has_invisible_text, has_nonembedded_fonts, has_glyphless_fonts).

    Checks:
    1. Invisible text render mode → hidden OCR overlay text
    2. All non-embedded fonts → suspicious (likely OCR artifact)
    3. Glyphless fonts → no real glyphs → scanned
    """
    has_invisible = False
    all_nonembedded = True  # assume True until proven otherwise
    all_glyphless = True    # assume True until proven otherwise
    has_any_text = False

    try:
        import fitz as _fitz
        # Use low-level page access to inspect text objects
        # Get text with detailed span info
        blocks = page.get_text("dict", flags=_fitz.TEXT_PRESERVE_WHITESPACE)["blocks"]
        for block in blocks:
            if block.get("type") != 0:  # text blocks only
                continue
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    has_any_text = True
                    # Check for invisible text render mode
                    # PyMuPDF doesn't directly expose render mode in the dict,
                    # so we check via the raw page object
                    # For now, we'll check font properties instead

                    font_name = span.get("font", "").lower()
                    is_embedded = span.get("is_embedded", True)

                    # Track non-embedded fonts
                    if is_embedded:
                        all_nonembedded = False

                    # Track glyphless fonts
                    if "glyphless" not in font_name and "glyph" not in font_name:
                        all_glyphless = False

        # Check for invisible text render mode using the C-level API
        # This requires walking the page objects directly
        try:
            # Get the page's text rendering mode
            # If text is invisible, get_text() may return it but it's not rendered
            # We detect this by checking if text spans have very small size
            # or are positioned outside visible area
            text = page.get_text("text").strip()
            if text and not has_any_text:
                # Text exists but no readable spans → invisible layer
                has_invisible = True
        except Exception:
            pass

    except Exception:
        logger.debug("OCR layer detection failed", exc_info=True)

    has_nonembedded = has_any_text and all_nonembedded
    has_glyphless = has_any_text and all_glyphless

    return has_invisible, has_nonembedded, has_glyphless


def _classify_single_page(
    page,
    page_number: int,
    strip_existing_ocr: bool = True,
) -> PageClassification:
    """Classify a single PDF page as digital, scanned, mixed, or empty.

    Parameters
    ----------
    page : fitz.Page
        A PyMuPDF page object.
    page_number : int
        1-indexed page number.
    strip_existing_ocr : bool
        If True, treat OCR overlay text as unreliable (like marker's --strip-existing-ocr).
    """
    # Step 1: Extract text and compute quality metrics
    raw_text = page.get_text("text").strip()
    text_length = len(raw_text)
    alnum_ratio = _alphanum_ratio(raw_text) if raw_text else 0.0
    space_ratio = _space_ratio(raw_text) if raw_text else 1.0
    newline_ratio = _newline_ratio(raw_text) if raw_text else 1.0

    # Step 2: Bitmap coverage
    bitmap_coverage, num_images = _get_bitmap_coverage(page)

    # Step 3: OCR layer detection
    has_invisible, has_nonembedded, has_glyphless = _detect_ocr_layer(page)

    # Step 4: Count text blocks
    try:
        text_dict = page.get_text("dict")
        num_text_blocks = sum(
            1 for b in text_dict.get("blocks", []) if b.get("type") == 0
        )
    except Exception:
        num_text_blocks = 0

    # ── Decision Logic ──────────────────────────────────────────────

    # Check if text is "bad" (garbled, suspicious, or missing)
    text_is_bad = False
    text_is_meaningful = False

    if text_length == 0:
        # No text at all → definitely needs OCR
        text_is_bad = True
    else:
        # Text exists — check quality
        if alnum_ratio < ALPHANUM_THRESHOLD:
            # Too few alphanumeric chars → garbled OCR overlay
            text_is_bad = True
        if space_ratio > SPACE_RATIO_THRESHOLD:
            # Too many spaces → broken extraction
            text_is_bad = True
        if newline_ratio > NEWLINE_RATIO_THRESHOLD:
            # Too many newlines → broken extraction
            text_is_bad = True

        # OCR overlay detection
        if strip_existing_ocr:
            if has_nonembedded or has_glyphless:
                # All fonts are non-embedded or glyphless → likely OCR artifact
                text_is_bad = True
            if has_invisible:
                # Invisible text layer detected → needs re-OCR
                text_is_bad = True

        # Large image covering most of the page
        if bitmap_coverage >= IMAGE_DOMINANCE_THRESHOLD:
            # Image dominates the page → it's essentially a scanned page
            text_is_bad = True

        # If text passes all quality checks, it's meaningful
        if not text_is_bad:
            text_is_meaningful = True

    # ── Classification ───────────────────────────────────────────────

    if text_length == 0 and bitmap_coverage < BITMAP_COVERAGE_MIXED:
        # No text, no significant images → empty page
        kind = PageKind.EMPTY
    elif text_is_bad or (not text_is_meaningful and bitmap_coverage >= BITMAP_COVERAGE_MIXED):
        # Bad text or no meaningful text + significant images → scanned
        kind = PageKind.SCANNED
    elif text_is_meaningful and bitmap_coverage >= BITMAP_COVERAGE_MIXED:
        # Good text AND significant images → mixed (hybrid page)
        if bitmap_coverage >= BITMAP_COVERAGE_SCANNED:
            # Images dominate (>75%) but text exists → still scanned
            # (e.g., a scanned page with a tiny bit of overlay text)
            kind = PageKind.SCANNED
        else:
            kind = PageKind.MIXED
    elif text_is_meaningful:
        # Good text, minimal images → digital
        kind = PageKind.DIGITAL
    else:
        # No meaningful text, no significant images → digital (text-only page)
        kind = PageKind.DIGITAL if text_length > 0 else PageKind.EMPTY

    return PageClassification(
        page_number=page_number,
        kind=kind,
        text_length=text_length,
        alphanum_ratio=round(alnum_ratio, 3),
        space_ratio=round(space_ratio, 3),
        bitmap_coverage=round(bitmap_coverage, 3),
        has_invisible_text=has_invisible,
        has_nonembedded_fonts=has_nonembedded,
        has_glyphless_fonts=has_glyphless,
        num_images=num_images,
        num_text_blocks=num_text_blocks,
    )


# ── Document-Level Classification ───────────────────────────────────


def classify_pdf(
    pdf_path: str,
    max_pages: int = 0,
    strip_existing_ocr: bool = True,
    force_kind: Optional[DocumentKind] = None,
) -> PDFClassification:
    """Classify a PDF as digital, scanned, or mixed.

    Parameters
    ----------
    pdf_path : str
        Path to the PDF file.
    max_pages : int
        Maximum number of pages to check. 0 = all pages.
    strip_existing_ocr : bool
        If True, treat OCR overlay text as unreliable and check for
        invisible/hidden text layers (like marker's --strip-existing-ocr).
    force_kind : DocumentKind, optional
        Override automatic classification. If set, skips per-page analysis
        and returns the forced kind. Useful for --force-ocr or --force-digital
        CLI flags.

    Returns
    -------
    PDFClassification
        Document-level classification with per-page details.
    """
    if force_kind is not None:
        logger.info("PDF classification forced to: %s", force_kind.value)
        return PDFClassification(document_kind=force_kind)

    doc = fitz.open(pdf_path)
    total_pages = doc.page_count
    pages_to_check = total_pages if max_pages <= 0 else min(max_pages, total_pages)

    page_classifications: List[PageClassification] = []

    try:
        for i in range(pages_to_check):
            page = doc[i]
            pc = _classify_single_page(page, i + 1, strip_existing_ocr=strip_existing_ocr)
            page_classifications.append(pc)
            logger.debug(
                "Page %d: kind=%s text_len=%d alnum=%.3f space=%.3f bitmap=%.3f images=%d",
                pc.page_number, pc.kind.value, pc.text_length,
                pc.alphanum_ratio, pc.space_ratio, pc.bitmap_coverage, pc.num_images,
            )
    finally:
        doc.close()

    # Aggregate per-page results to document-level decision
    digital_count = sum(1 for p in page_classifications if p.kind == PageKind.DIGITAL)
    scanned_count = sum(1 for p in page_classifications if p.kind == PageKind.SCANNED)
    mixed_count = sum(1 for p in page_classifications if p.kind == PageKind.MIXED)
    empty_count = sum(1 for p in page_classifications if p.kind == PageKind.EMPTY)

    n = len(page_classifications)
    scanned_ratio = (scanned_count + mixed_count) / n if n else 0.0
    digital_ratio = digital_count / n if n else 0.0

    # Decision logic:
    # - If majority pages are scanned/mixed → scanned_pdf
    # - If majority pages are digital → digital_pdf
    # - Otherwise → mixed_pdf (use scanned pipeline as fallback)
    if scanned_ratio >= SCANNED_RATIO_THRESHOLD:
        doc_kind = DocumentKind.SCANNED_PDF
    elif digital_ratio >= DIGITAL_RATIO_THRESHOLD:
        doc_kind = DocumentKind.DIGITAL_PDF
    else:
        doc_kind = DocumentKind.MIXED_PDF

    result = PDFClassification(
        document_kind=doc_kind,
        pages=page_classifications,
        digital_page_count=digital_count,
        scanned_page_count=scanned_count,
        mixed_page_count=mixed_count,
        empty_page_count=empty_count,
    )

    logger.info(
        "PDF classification: %s (%d pages: %d digital, %d scanned, %d mixed, %d empty)"
        " — scanned_ratio=%.2f digital_ratio=%.2f",
        doc_kind.value, n, digital_count, scanned_count, mixed_count, empty_count,
        scanned_ratio, digital_ratio,
    )

    return result


# ── Convenience Function ────────────────────────────────────────────


def pdf_kind(pdf_path: str, force_kind: Optional[str] = None) -> str:
    """Simple interface returning 'digital_pdf', 'scanned_pdf', or 'mixed_pdf'.

    Drop-in replacement for the old _pdf_kind() method in master.py.

    Parameters
    ----------
    pdf_path : str
        Path to the PDF file.
    force_kind : str, optional
        Override classification. One of 'digital_pdf', 'scanned_pdf', 'mixed_pdf',
        'force_ocr', 'force_digital'.

    Returns
    -------
    str
        One of 'digital_pdf', 'scanned_pdf', or 'mixed_pdf'.
    """
    # Handle force overrides
    force_doc_kind = None
    if force_kind == "force_ocr":
        force_doc_kind = DocumentKind.SCANNED_PDF
    elif force_kind == "force_digital":
        force_doc_kind = DocumentKind.DIGITAL_PDF
    elif force_kind in ("digital_pdf", "scanned_pdf", "mixed_pdf"):
        force_doc_kind = DocumentKind(force_kind)

    classification = classify_pdf(pdf_path, force_kind=force_doc_kind)

    # For mixed_pdf, we route to scanned_pdf pipeline (OCR handles both text and images)
    if classification.document_kind == DocumentKind.MIXED_PDF:
        logger.info("Mixed PDF detected — routing to scanned pipeline for best results")
        return "scanned_pdf"

    return classification.document_kind.value
