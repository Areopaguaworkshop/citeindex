"""Route PDFs by usable text, including searchable scans with existing OCR.

Digital and mixed pages both supply usable text. Reuse it when at least 90%
of nonblank pages qualify, tolerating occasional image-only or sparse pages.
Clearly corrupt text still requires OCR. Mixed documents fall back to OCR.
"""

import logging
import re
from dataclasses import dataclass, field
from enum import StrEnum

import pymupdf as fitz

logger = logging.getLogger(__name__)

# ── Enums & Data Classes ────────────────────────────────────────────


class PageKind(StrEnum):
    """Classification result for a single page."""
    DIGITAL = "digital"
    SCANNED = "scanned"
    MIXED = "mixed"          # page has both meaningful text AND significant images
    EMPTY = "empty"           # page has neither text nor images


class DocumentKind(StrEnum):
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
    reason: str = ""


@dataclass
class PDFClassification:
    """Result of classifying an entire PDF document."""
    document_kind: DocumentKind
    pages: list[PageClassification] = field(default_factory=list)
    digital_page_count: int = 0
    scanned_page_count: int = 0
    mixed_page_count: int = 0
    empty_page_count: int = 0

    @property
    def total_pages(self) -> int:
        return len(self.pages)

    @property
    def scanned_ratio(self) -> float:
        """Fraction of nonblank pages without usable text."""
        t = self.total_pages - self.empty_page_count
        return self.scanned_page_count / t if t else 0.0

    @property
    def digital_ratio(self) -> float:
        """Fraction of nonblank pages with usable text, including mixed pages."""
        t = self.total_pages - self.empty_page_count
        return (self.digital_page_count + self.mixed_page_count) / t if t else 0.0


# ── Threshold Constants ────────────────────────────────────────────
# Inspired by docling (bitmap_area_threshold=0.05, BITMAP_COVERAGE_TRESHOLD=0.75)
# and marker (alphanum_threshold=0.3, space_threshold=0.7, image_threshold=0.65)

# Bitmap coverage thresholds (docling-inspired)
BITMAP_COVERAGE_MIXED = 0.05      # >=5% → page has meaningful images

# Text quality thresholds (marker-inspired)
ALPHANUM_THRESHOLD = 0.3          # <30% alphanumeric → garbled text
SPACE_RATIO_THRESHOLD = 0.7       # >70% spaces → bad OCR overlay
NEWLINE_RATIO_THRESHOLD = 0.6     # >60% newlines → broken extraction
REPLACEMENT_RATIO_THRESHOLD = 0.05  # missing Unicode mappings
MIN_OVERLAY_ALPHANUM = 50         # a page number/caption is not a full text layer

# Large image coverage (marker-inspired)
IMAGE_DOMINANCE_THRESHOLD = 0.65  # require substantial text on image-dominated pages

# Document-level decision thresholds
DIGITAL_RATIO_THRESHOLD = 0.9     # tolerate <=10% image-only/sparse nonblank pages
SCANNED_RATIO_THRESHOLD = 0.5     # >=50% pages need OCR → scanned_pdf


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


def _get_bitmap_coverage(page) -> tuple[float, int]:
    """Calculate fraction of page area covered by image objects.

    Use displayed image occurrences (including inline images) without decoding
    image data or counting both XObjects and text-dictionary image blocks.
    """
    try:
        page_rect = page.rect
        page_area = page_rect.width * page_rect.height
        if page_area <= 0:
            return 0.0, 0

        rects = set()
        for info in page.get_image_info():
            rect = fitz.Rect(info["bbox"]) & page_rect
            if rect.width >= 32 and rect.height >= 32:
                rects.add(tuple(rect))
        # ponytail: partial overlaps overestimate coverage; use a rectangle union
        # if overlapping illustrations become a classification problem.
        total_image_area = sum(fitz.Rect(rect).get_area() for rect in rects)
        return min(total_image_area / page_area, 1.0), len(rects)

    except Exception:
        logger.warning("Failed to compute bitmap coverage", exc_info=True)
        return 0.0, 0


def _detect_ocr_layer(page) -> tuple[bool, bool, bool]:
    """Report text-layer provenance; invisibility alone does not mean bad text."""
    try:
        spans = page.get_texttrace()
        fonts = page.get_fonts()
        return (
            any(span["type"] == 3 for span in spans),
            bool(fonts) and all(font[1] in ("", "n/a") for font in fonts),
            bool(spans) and all("glyphless" in span["font"].lower() for span in spans),
        )
    except Exception:
        logger.debug("OCR layer detection failed", exc_info=True)
        return False, False, False


def _classify_single_page(
    page,
    page_number: int,
    strip_existing_ocr: bool = False,
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
        text_dict = page.get_text("dict", flags=fitz.TEXTFLAGS_DICT & ~fitz.TEXT_PRESERVE_IMAGES)
        num_text_blocks = sum(
            1 for b in text_dict.get("blocks", []) if b.get("type") == 0
        )
    except Exception:
        num_text_blocks = 0

    if text_length == 0 and bitmap_coverage < BITMAP_COVERAGE_MIXED:
        kind, reason = PageKind.EMPTY, "blank"
    elif not raw_text:
        kind, reason = PageKind.SCANNED, "no_text"
    elif (
        alnum_ratio < ALPHANUM_THRESHOLD
        or space_ratio > SPACE_RATIO_THRESHOLD
        or newline_ratio > NEWLINE_RATIO_THRESHOLD
        or raw_text.count("\ufffd") / text_length > REPLACEMENT_RATIO_THRESHOLD
    ):
        kind, reason = PageKind.SCANNED, "bad_text"
    elif strip_existing_ocr and (has_invisible or has_glyphless):
        kind, reason = PageKind.SCANNED, "ocr_rejected"
    elif (
        (bitmap_coverage >= IMAGE_DOMINANCE_THRESHOLD or has_invisible or has_glyphless)
        and sum(c.isalnum() for c in raw_text) < MIN_OVERLAY_ALPHANUM
    ):
        kind, reason = PageKind.SCANNED, "sparse_text"
    else:
        kind = PageKind.MIXED if bitmap_coverage >= BITMAP_COVERAGE_MIXED else PageKind.DIGITAL
        reason = "usable_text"

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
        reason=reason,
    )


# ── Document-Level Classification ───────────────────────────────────


def classify_pdf(
    pdf_path: str,
    max_pages: int = 0,
    strip_existing_ocr: bool = False,
    force_kind: DocumentKind | None = None,
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

    page_classifications: list[PageClassification] = []

    try:
        for i in range(pages_to_check):
            page = doc[i]
            pc = _classify_single_page(page, i + 1, strip_existing_ocr=strip_existing_ocr)
            page_classifications.append(pc)
            logger.debug(
                "Page %d: kind=%s text_len=%d alnum=%.3f space=%.3f bitmap=%.3f images=%d reason=%s",
                pc.page_number, pc.kind.value, pc.text_length,
                pc.alphanum_ratio, pc.space_ratio, pc.bitmap_coverage, pc.num_images, pc.reason,
            )
    finally:
        doc.close()

    # Aggregate per-page results to document-level decision
    digital_count = sum(1 for p in page_classifications if p.kind == PageKind.DIGITAL)
    scanned_count = sum(1 for p in page_classifications if p.kind == PageKind.SCANNED)
    mixed_count = sum(1 for p in page_classifications if p.kind == PageKind.MIXED)
    empty_count = sum(1 for p in page_classifications if p.kind == PageKind.EMPTY)

    n = len(page_classifications)
    nonblank_count = n - empty_count
    scanned_ratio = scanned_count / nonblank_count if nonblank_count else 0.0
    digital_ratio = (digital_count + mixed_count) / nonblank_count if nonblank_count else 0.0
    doubtful_text = any(p.reason in ("bad_text", "ocr_rejected") for p in page_classifications)

    if doubtful_text:
        logger.info("Existing text failed quality checks or was explicitly rejected; routing to OCR")
        doc_kind = DocumentKind.SCANNED_PDF
    elif digital_ratio >= DIGITAL_RATIO_THRESHOLD:
        doc_kind = DocumentKind.DIGITAL_PDF
        if scanned_count:
            logger.info("Reusing existing text; skipping OCR on %d image-only/sparse pages", scanned_count)
    elif scanned_ratio >= SCANNED_RATIO_THRESHOLD:
        doc_kind = DocumentKind.SCANNED_PDF
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


def pdf_kind(pdf_path: str, force_kind: str | None = None) -> str:
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
