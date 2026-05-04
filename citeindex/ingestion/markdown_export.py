"""Generate a human-readable Markdown file from ingestion artifacts.

Each ingestion produces a `.md` file in the ``library/`` folder (sibling of
``corpus/``).  The file contains:

* YAML front-matter with CSL metadata
* A full inline citation at the top
* Page/section/timestamp headers with CSL-level detail
* Full extracted text under each header
* Extracted footnotes in a footer block
"""

import logging
import os
import re
from typing import Any, Dict, List, Optional

from .storage import csl_folder_name, ensure_dir

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CSL formatting helpers
# ---------------------------------------------------------------------------

def _format_authors(csl: Dict[str, Any]) -> str:
    authors = csl.get("author") or []
    parts: List[str] = []
    for a in authors:
        if a.get("literal"):
            parts.append(a["literal"])
        elif a.get("family"):
            given = a.get("given", "")
            parts.append(f"{a['family']}, {given}".strip(", "))
    return "; ".join(parts) if parts else "Unknown"


def _format_year(csl: Dict[str, Any]) -> str:
    try:
        return str(csl["issued"]["date-parts"][0][0])
    except (KeyError, IndexError, TypeError):
        return "n.d."


def _format_inline_citation(csl: Dict[str, Any]) -> str:
    """Build a full inline citation string from CSL data."""
    author = _format_authors(csl)
    title = csl.get("title", "Untitled")
    year = _format_year(csl)
    publisher = csl.get("publisher", "")
    publisher_place = csl.get("publisher-place", "")
    container = csl.get("container-title", "")
    collection = csl.get("collection-title", "")
    url = csl.get("URL", "")

    parts = [f"{author}."]

    # Title formatting
    csl_type = csl.get("type", "")
    if csl_type in ("book", "thesis"):
        parts.append(f"*{title}*.")
    elif csl_type == "webpage":
        parts.append(f"「{title}」.")
    else:
        parts.append(f"《{title}》.")

    if collection:
        parts.append(f"{collection}.")
    if container:
        parts.append(f"*{container}*.")

    # Publisher block
    pub_parts: List[str] = []
    if publisher_place:
        pub_parts.append(publisher_place)
    if publisher:
        pub_parts.append(publisher)
    if pub_parts:
        parts.append(f"{'：'.join(pub_parts)}, {year}.")
    else:
        parts.append(f"{year}.")

    if url:
        parts.append(f"URL: {url}")

    return " ".join(parts)


def _yaml_escape(value: str) -> str:
    if any(ch in value for ch in (':', '#', '"', "'", '\n', '{', '}', '[', ']')):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
    return value


# ---------------------------------------------------------------------------
# Front-matter
# ---------------------------------------------------------------------------

def _build_front_matter(csl: Dict[str, Any]) -> str:
    lines = ["---"]
    lines.append(f"title: {_yaml_escape(csl.get('title', 'Untitled'))}")
    lines.append(f"author: {_yaml_escape(_format_authors(csl))}")
    lines.append(f"date: {_format_year(csl)}")
    if csl.get("publisher"):
        lines.append(f"publisher: {_yaml_escape(csl['publisher'])}")
    if csl.get("container-title"):
        lines.append(f"container-title: {_yaml_escape(csl['container-title'])}")
    lines.append(f"type: {csl.get('type', 'document')}")
    if csl.get("content_hash"):
        lines.append(f"content_hash: {csl['content_hash'][:16]}")
    if csl.get("URL"):
        lines.append(f"url: {_yaml_escape(csl['URL'])}")
    lines.append("---")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Per-resource-type body builders
# ---------------------------------------------------------------------------

def _index_section_levels(
    sections: List[Dict[str, Any]],
    levels: Dict[str, int],
    base_level: int = 2,
) -> None:
    """Walk a section_tree and record each heading → markdown level.

    Top-level sections get ``base_level`` (default ##), their children
    get ``base_level + 1`` (default ###), etc.
    """
    for section in sections:
        heading = section.get("heading") or section.get("title")
        if heading and heading not in levels:
            levels[heading] = base_level
        children = section.get("children", [])
        if children:
            _index_section_levels(children, levels, base_level=base_level + 1)


def _format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h:02d}:{m:02d}:{s:02d}"
    return f"{m:02d}:{s:02d}"


def _normalize_heading_line(text: str) -> str:
    return re.sub(r"\s+", " ", text.strip()).casefold()


def _strip_duplicate_heading_lines(text: str, headings: List[str]) -> str:
    """Remove standalone heading lines already emitted as markdown headings."""
    if not text or not headings:
        return text

    heading_set = {_normalize_heading_line(heading) for heading in headings if heading.strip()}
    if not heading_set:
        return text

    kept_lines: List[str] = []
    for line in text.splitlines():
        if _normalize_heading_line(line) in heading_set:
            continue
        kept_lines.append(line)

    cleaned = "\n".join(kept_lines).strip()
    return cleaned or text


def _build_body_document(
    csl: Dict[str, Any],
    document_json: Dict[str, Any],
    resource_type: str,
) -> str:
    """Build MD body for PDF and URL article documents.

    Handles both MinerU structure (``page.paragraphs[]`` with ``type`` field)
    and fitz legacy structure (``page.columns[].paragraphs[]`` with plain text).
    Images with ``image_path`` render as ``![caption](path)``.
    """
    lines: List[str] = []
    structure = document_json.get("structure", {})
    pages = structure.get("pages", [])
    section_tree = structure.get("section_tree", [])
    footnotes_all: List[Dict[str, Any]] = []

    author = _format_authors(csl)
    title = csl.get("title", "Untitled")
    year = _format_year(csl)
    publisher = csl.get("publisher", "")

    is_url = resource_type == "url_article"

    # ── Build section-tree index for heading-rich rendering ──────────
    section_levels: Dict[str, int] = {}
    if section_tree:
        _index_section_levels(section_tree, section_levels, base_level=2)

    # ── Detect MinerU vs fitz structure ─────────────────────────────
    is_mineru = any(
        "paragraphs" in page and page.get("paragraphs", [])
        or "sections" in page
        for page in pages
    )

    # Build a short citation prefix for page markers
    cite_prefix = f"{author}, 《{title}》"
    if publisher:
        cite_prefix += f", {publisher}"
    cite_prefix += f", {year}"

    # ── Render pages ─────────────────────────────────────────────────
    for page_idx, page in enumerate(pages):
        page_num = page.get("page_number", "?")

        # ── Visible page separator with citation ──────────────────────
        lines.append("")
        lines.append(f"======page:{page_num} | {cite_prefix}, p.{page_num} ========")
        lines.append("")

        if is_mineru:
            # MinerU path: page.paragraphs[] with types
            paragraphs = page.get("paragraphs", [])
            has_mineru_headings = any(p.get("type") == "heading" for p in paragraphs)
            page_heading_texts = [
                p.get("text", "").strip()
                for p in paragraphs
                if p.get("type") == "heading" and p.get("text", "").strip()
            ]

            if not has_mineru_headings:
                # No headings: fallback page label
                section_title = page.get("section_title", "")
                if is_url:
                    label = f"Section {page_num}: {section_title}" if section_title else f"Section {page_num}"
                else:
                    label = f"Page {page_num}: {section_title}" if section_title else f"Page {page_num}"
                lines.append(f"## {label}")
                lines.append("")

            for para in paragraphs:
                text = para.get("text", "").strip()
                para_type = para.get("type", "")
                image_path = para.get("image_path")

                # ── Heading ──────────────────────────────────────
                if para_type == "heading" and text:
                    level = para.get("level") or section_levels.get(text, 2)
                    lines.append(f"{'#' * level} {text}")
                    lines.append("")
                    continue

                # ── Image ────────────────────────────────────────
                if para_type == "image_caption" and image_path:
                    alt_text = text if text else "image"
                    lines.append(f"![{alt_text}]({image_path})")
                    lines.append("")
                    continue

                if para_type == "image_caption" and text and not image_path:
                    lines.append(f"*{text}*")
                    lines.append("")
                    continue

                # ── Text ─────────────────────────────────────────
                if text:
                    text = _strip_duplicate_heading_lines(text, page_heading_texts)
                if text:
                    lines.append(text)
                    lines.append("")

        else:
            # Fitz fallback path: page.columns[].paragraphs[]
            columns = page.get("columns", [])
            if not columns:
                # Direct paragraphs list (older format)
                columns = [{"paragraphs": page.get("paragraphs", [])}]

            # Flatten columns into a single page
            flat_paras: List[str] = []
            for col in columns:
                for para in col.get("paragraphs", []):
                    if isinstance(para, dict):
                        flat_paras.append(para.get("text", "").strip())
                    elif isinstance(para, str):
                        flat_paras.append(para.strip())

            # Page label
            if is_url:
                lines.append(f"## Section {page_num}")
            else:
                lines.append(f"## Page {page_num}")
            lines.append("")

            for text in flat_paras:
                if text:
                    lines.append(text)
                    lines.append("")

        # Collect footnotes
        for fn in page.get("footnotes", []):
            footnotes_all.append(fn)

    return "\n".join(lines), footnotes_all


def _build_body_media(
    csl: Dict[str, Any],
    transcript_json: Dict[str, Any],
) -> str:
    """Build MD body for media (audio/video) with timestamp headers."""
    lines: List[str] = []
    segments = transcript_json.get("segments", [])
    speaker_segments = transcript_json.get("speaker_segments", [])

    author = _format_authors(csl)
    title = csl.get("title", "Untitled")
    year = _format_year(csl)

    if not segments:
        lines.append("*No transcript available.*")
        return "\n".join(lines), []

    # Build a speaker lookup for timestamp ranges
    def _find_speaker(start: float) -> Optional[str]:
        for ss in speaker_segments:
            if ss["start"] <= start <= ss["end"]:
                return ss.get("speaker")
        return None

    # Group segments into chunks (~60 seconds each) for readability
    chunk_duration = 60.0
    chunk_start = segments[0].get("start", 0.0)
    chunk_texts: List[str] = []
    chunk_end = chunk_start

    def flush_chunk():
        nonlocal chunk_texts, chunk_start
        if not chunk_texts:
            return
        ts_start = _format_timestamp(chunk_start)
        ts_end = _format_timestamp(chunk_end)
        cite_suffix = f"{author}, 「{title}」, {ts_start}-{ts_end}, {year}"
        lines.append(f"## [{ts_start} - {ts_end}] {{{cite_suffix}}}")
        lines.append("")
        lines.append(" ".join(chunk_texts))
        lines.append("")
        chunk_texts = []

    for seg in segments:
        seg_start = seg.get("start", 0.0)
        seg_end = seg.get("end", 0.0)
        text = seg.get("text", "").strip()
        if not text:
            continue

        # Start a new chunk if we exceed the duration
        if seg_start - chunk_start >= chunk_duration and chunk_texts:
            flush_chunk()
            chunk_start = seg_start

        speaker = _find_speaker(seg_start)
        if speaker:
            chunk_texts.append(f"**[{speaker}]** {text}")
        else:
            chunk_texts.append(text)
        chunk_end = seg_end

    flush_chunk()

    return "\n".join(lines), []


# ---------------------------------------------------------------------------
# Footnotes block
# ---------------------------------------------------------------------------

def _build_footnotes_block(footnotes: List[Dict[str, Any]]) -> str:
    if not footnotes:
        return ""
    lines = ["---", ""]
    for i, fn in enumerate(footnotes, start=1):
        text = ""
        if isinstance(fn, dict):
            text = fn.get("text", "") or fn.get("content", "") or str(fn)
        elif isinstance(fn, str):
            text = fn
        else:
            text = str(fn)
        lines.append(f"[^{i}]: {text.strip()}")
    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate_library_markdown(
    csl_json: Dict[str, Any],
    document_json: Optional[Dict[str, Any]],
    transcript_json: Optional[Dict[str, Any]],
    resource_type: str,
) -> str:
    """Generate a complete Markdown string for the library file."""
    parts: List[str] = []

    # 1. Front-matter
    parts.append(_build_front_matter(csl_json))
    parts.append("")

    # 2. Title + inline citation
    parts.append(f"# {csl_json.get('title', 'Untitled')}")
    parts.append("")
    parts.append(_format_inline_citation(csl_json))
    parts.append("")

    # 3. Body (per resource type)
    footnotes: List[Dict[str, Any]] = []
    if resource_type == "media" and transcript_json:
        parts.append("---")
        parts.append("")
        body, footnotes = _build_body_media(csl_json, transcript_json)
    elif document_json:
        body, footnotes = _build_body_document(csl_json, document_json, resource_type)
    else:
        parts.append("---")
        parts.append("")
        body = "*No content available.*"

    parts.append(body)

    # 4. Footnotes
    fn_block = _build_footnotes_block(footnotes)
    if fn_block:
        parts.append(fn_block)

    return "\n".join(parts)


def write_library_markdown(
    corpus_root: str,
    csl_json: Dict[str, Any],
    document_json: Optional[Dict[str, Any]],
    transcript_json: Optional[Dict[str, Any]],
    resource_type: str,
) -> str:
    """Generate and write the library MD file. Returns the file path."""
    # library/ is a sibling of corpus/
    library_root = os.path.join(os.path.dirname(corpus_root), "library")
    ensure_dir(library_root)

    folder_name = csl_folder_name(csl_json)
    md_filename = f"{folder_name}.md"
    md_path = os.path.join(library_root, md_filename)

    # ── Rewrite image paths to be relative from library/ to corpus/<slug>/images/ ──
    # markdown is at library/<slug>.md, images at corpus/<slug>/images/<file>
    # relative path: ../corpus/<slug>/images/<file>
    if document_json:
        image_prefix = f"../corpus/{folder_name}/"
        _rewrite_image_paths(document_json, image_prefix)

    content = generate_library_markdown(
        csl_json=csl_json,
        document_json=document_json,
        transcript_json=transcript_json,
        resource_type=resource_type,
    )

    with open(md_path, "w", encoding="utf-8") as f:
        f.write(content)

    logger.info("Library markdown written: %s", md_path)
    return md_path


def _rewrite_image_paths(document_json: Dict[str, Any], prefix: str) -> None:
    """Rewrite image_path fields in document_json paragraphs to use relative paths.

    The image paths stored by the pipeline are like ``images/filename.jpeg``
    (relative to the corpus/<slug>/ directory).  We need them relative to
    the library/ directory instead, so we prepend ``../corpus/<slug>/``.
    """
    structure = document_json.get("structure", {})
    for page in structure.get("pages", []):
        for para in page.get("paragraphs", []):
            img_path = para.get("image_path")
            if img_path and not img_path.startswith(("http://", "https://", "/")):
                para["image_path"] = prefix + img_path
