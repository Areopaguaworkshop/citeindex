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


def _build_body_document(
    csl: Dict[str, Any],
    document_json: Dict[str, Any],
    resource_type: str,
) -> str:
    """Build MD body for PDF and URL article documents.

    When the document structure contains heading paragraphs (from MinerU
    layout analysis), renders them with proper heading levels (##, ###, etc.)
    instead of flat page-number headers. Images with ``image_path`` are
    rendered as ``![caption](path)``.
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
    # If section_tree has entries, build a lookup: section title → level
    section_levels: Dict[str, int] = {}
    if section_tree:
        _index_section_levels(section_tree, section_levels, base_level=2)

    # ── Render pages ─────────────────────────────────────────────────
    has_headings = any(
        para.get("type") == "heading"
        for page in pages
        for para in page.get("paragraphs", [])
    )

    for page in pages:
        page_num = page.get("page_number", "?")
        paragraphs = page.get("paragraphs", [])

        if not paragraphs:
            continue

        # ── Page anchor comment (invisible but useful for cross-ref) ──
        lines.append(f"<!-- page:{page_num} -->")

        for para in paragraphs:
            text = para.get("text", "").strip()
            para_type = para.get("type", "")
            image_path = para.get("image_path")

            # ── Heading ──────────────────────────────────────────────
            if para_type == "heading" and text:
                # Determine heading level from section_tree or fallback
                level = section_levels.get(text, 2)
                heading_marker = "#" * level
                cite_suffix = f"{author}, 《{title}》, p.{page_num}"
                if publisher:
                    cite_suffix += f", {publisher}"
                cite_suffix += f", {year}"
                lines.append(f"{heading_marker} {text} {{{cite_suffix}}}")
                lines.append("")
                continue

            # ── Image with caption ───────────────────────────────────
            if para_type == "image_caption" and image_path:
                alt_text = text if text else "image"
                lines.append(f"![{alt_text}]({image_path})")
                lines.append("")
                continue

            # ── Image caption without image path (caption only) ──────
            if para_type == "image_caption" and text and not image_path:
                lines.append(f"*{text}*")
                lines.append("")
                continue

            # ── Regular text ──────────────────────────────────────────
            if text:
                lines.append(text)
                lines.append("")

        # Collect footnotes
        for fn in page.get("footnotes", []):
            footnotes_all.append(fn)

    # ── If no headings were found, fall back to page-number headers ───
    if not has_headings and not section_tree:
        fallback_lines: List[str] = []
        for page in pages:
            page_num = page.get("page_number", "?")
            section_title = page.get("section_title", "")

            if is_url:
                header_label = f"Section {page_num}: {section_title}" if section_title and section_title != "(intro)" else f"Section {page_num}"
                cite_suffix = f"{author}, 「{title}」, §{page_num}"
            else:
                header_label = f"Page {page_num}: {section_title}" if section_title and section_title != "(intro)" else f"Page {page_num}"
                cite_suffix = f"{author}, 《{title}》, p.{page_num}"
                if publisher:
                    cite_suffix += f", {publisher}"
                cite_suffix += f", {year}"

            fallback_lines.append(f"## [{header_label}] {{{cite_suffix}}}")
            fallback_lines.append("")

            for para in page.get("paragraphs", []):
                text = para.get("text", "").strip()
                para_type = para.get("type", "")
                image_path = para.get("image_path")

                if para_type == "image_caption" and image_path:
                    alt_text = text if text else "image"
                    fallback_lines.append(f"![{alt_text}]({image_path})")
                    fallback_lines.append("")
                elif text:
                    fallback_lines.append(text)
                    fallback_lines.append("")

            for fn in page.get("footnotes", []):
                footnotes_all.append(fn)

        lines = fallback_lines

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
    parts.append("---")
    parts.append("")

    # 3. Body (per resource type)
    footnotes: List[Dict[str, Any]] = []
    if resource_type == "media" and transcript_json:
        body, footnotes = _build_body_media(csl_json, transcript_json)
    elif document_json:
        body, footnotes = _build_body_document(csl_json, document_json, resource_type)
    else:
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
