"""Pattern-based and DSPy metadata extraction from document source text."""

import json
import logging
import re
from typing import Any, Dict, List, Optional

import dspy

from ...llm import get_llm_model
from ..models import IngestionConfig
from .common import PAGE_NUMBER_PATTERNS as _PAGE_NUM_PATTERNS

logger = logging.getLogger(__name__)


def select_source_evidence(
    source_text: str,
    budget: int = 8000,
    region_hints: Optional[List[str]] = None,
) -> str:
    """Select front matter plus later identity-bearing regions within a budget."""
    if len(source_text) <= budget:
        return source_text
    chunks = [source_text[: min(4000, budget)]]
    remaining = budget - len(chunks[0])
    markers = ("isbn", "copyright", "published", "university press", "doi", "cite this", *(region_hints or []))
    lowered = source_text.casefold()
    for marker in markers:
        start = lowered.find(marker.casefold())
        if start < 0:
            continue
        excerpt = source_text[max(0, start - 500): start + 1500]
        if excerpt not in chunks and remaining > 0:
            excerpt = excerpt[:remaining]
            chunks.append(excerpt)
            remaining -= len(excerpt)
    return "\n\n[additional source evidence]\n\n".join(chunks)


def build_source_blocks(page_paragraphs: List[Any]) -> List[Dict[str, Any]]:
    """Create physical-page blocks; printed page labels never replace coordinates."""
    blocks: List[Dict[str, Any]] = []
    for physical_page_index, paragraphs in enumerate(page_paragraphs):
        page_index = physical_page_index + 1
        if isinstance(paragraphs, tuple) and len(paragraphs) == 2:
            page_index, paragraphs = paragraphs
        values = paragraphs.get("paragraphs", []) if isinstance(paragraphs, dict) else paragraphs
        for paragraph_index, paragraph in enumerate(values or [], start=1):
            text = paragraph.get("text", "") if isinstance(paragraph, dict) else str(paragraph)
            if not text.strip():
                continue
            blocks.append({
                "id": f"p{physical_page_index + 1}_b{paragraph_index}",
                "physical_page_index": physical_page_index,
                "printed_page_label": str(page_index),
                "text": text.strip(),
                "page": page_index,
                "role": "heading" if isinstance(paragraph, dict) and paragraph.get("type") == "heading" else "body",
            })
    return blocks


def gather_evidence_candidates(source_blocks: List[Dict[str, Any]], query: str = "") -> List[Dict[str, Any]]:
    """Gather source blocks and rank them lexically; no extra model call required."""
    terms = {term.casefold() for term in re.findall(r"[\w\u3400-\u9fff]+", query) if len(term) > 2}
    anchors = {block.get("physical_page_index") for block in source_blocks
               if isinstance(block.get("physical_page_index"), int) and any(
                   marker in block.get("text", "").casefold() for marker in
                   ("isbn", "copyright", "published by", "出版社", "版权", "imprint", "出版发行"))}
    candidates = []
    for position, block in enumerate(source_blocks):
        text = block.get("text", "")
        lowered = text.casefold()
        score = sum(lowered.count(term) for term in terms)
        if isinstance(block.get("physical_page_index"), int) and block["physical_page_index"] < 4:
            score += 8
        page = block.get("physical_page_index")
        if isinstance(page, int):
            score += 6 if page in anchors else 3 if page - 1 in anchors or page + 1 in anchors else 0
        if any(marker in lowered for marker in ("bibliography", "references", "参考文献", "书目")):
            score -= 20
        if any(marker in lowered for marker in ("title", "copyright", "isbn", "doi", "版权", "出版")):
            score += 1
        candidates.append({**block, "score": score, "_source_order": position})
    return sorted(candidates, key=lambda item: (-item["score"], item.get("physical_page_index", 0), item["_source_order"]))


def select_candidate_regions(pageindex_result: Optional[Dict[str, Any]], max_regions: int = 12) -> List[Dict[str, Any]]:
    """Flatten PageIndex ranges into retrieval hints without treating summaries as evidence."""
    if not pageindex_result:
        return []
    regions: List[Dict[str, Any]] = []

    def visit(nodes: Any) -> None:
        for node in nodes or []:
            if not isinstance(node, dict):
                continue
            heading = node.get("heading") or node.get("title") or ""
            start = node.get("start_page") or node.get("start_index")
            end = node.get("end_page") or node.get("end_index")
            lowered = str(heading).casefold()
            role = "section"
            if any(term in lowered for term in ("bibliograph", "reference", "参考文献", "书目")):
                role = "bibliography"
            elif any(term in lowered for term in ("copyright", "imprint", "colophon", "版权", "出版")):
                role = "imprint"
            elif any(term in lowered for term in ("introduction", "title", "前言", "序")):
                role = "front_matter"
            if start is not None or end is not None or role in {"imprint", "front_matter"}:
                regions.append({"heading": heading, "start_page": start, "end_page": end, "role": role})
            visit(node.get("children") or node.get("nodes"))

    visit(pageindex_result.get("level_1") or pageindex_result.get("structure"))
    return sorted(regions, key=lambda r: r["role"] not in {"imprint", "front_matter"})[:max_regions]


# ---------------------------------------------------------------------------
# CJK detection
# ---------------------------------------------------------------------------

_CJK_RANGES = (
    ("\u4e00", "\u9fff"),   # CJK Unified Ideographs
    ("\u3400", "\u4dbf"),   # Extension A
    ("\uf900", "\ufaff"),   # Compatibility
    ("\u3000", "\u303f"),   # Punctuation
)


def _has_cjk(text: str) -> bool:
    """Return True if *text* contains any CJK character."""
    for ch in text:
        for lo, hi in _CJK_RANGES:
            if lo <= ch <= hi:
                return True
    return False


def _is_cjk_dominant(text: str) -> bool:
    """Return True if CJK characters make up a significant fraction."""
    if not text:
        return False
    cjk_count = sum(
        1 for ch in text
        for lo, hi in _CJK_RANGES
        if lo <= ch <= hi
    )
    return cjk_count / max(len(text), 1) > 0.15


# ---------------------------------------------------------------------------
# DSPy Signatures
# ---------------------------------------------------------------------------

class LocateBibliographicPages(dspy.Signature):
    """Jointly identify the supplied document's type and its bibliographic pages.
    Read the original English or Chinese page blocks. Page numbers are physical
    PDF positions, not printed folios. Classify the supplied work, not works cited
    or reviewed. A whole edited volume is book; a separately supplied contribution
    is chapter. A journal book review is article-journal, not the reviewed book.
    ISBN alone does not distinguish a chapter from its book. Length is not proof.
    Select title, copyright/imprint/版权页, colophon/刊记, article header/footer,
    chapter opening and explicit how-to-cite pages. Exclude references and adverts.
    Return type book, chapter, article-journal, entry-encyclopedia, thesis,
    or unknown. Standalone monographs, reports and manuscripts are book.
    For each selected page return page (1-based), role, block_id and an exact
    quote establishing its role. Give type_evidence as block_id/quote spans.
    If evidence is insufficient return unknown; never guess from a filename.
    """
    page_blocks: List[Dict[str, Any]] = dspy.InputField()
    type_hint: str = dspy.InputField(desc="Weak heuristic hint; not evidence.")
    type_override: str = dspy.InputField(desc="Explicit user choice, if supplied. Use it when compatible with evidence; otherwise return unknown.")
    document_type: str = dspy.OutputField()
    type_evidence: List[Dict[str, str]] = dspy.OutputField()
    selected_pages: List[Dict[str, Any]] = dspy.OutputField()


def _pages_from_blocks(blocks):
    """Group blocks into frontmatter-style page dicts {"page": 1-based, "text"}."""
    pages: List[Dict[str, Any]] = []
    by_index: Dict[int, List[str]] = {}
    for block in blocks:
        index = block.get("physical_page_index")
        if isinstance(index, int):
            by_index.setdefault(index, []).append(block.get("text", ""))
    for index in sorted(by_index):
        pages.append({"page": index + 1, "text": "\n".join(by_index[index])})
    return pages


def _deterministic_scan(blocks):
    """Marker-scan all pages; (annotations, ranked shortlist pages). Never raises."""
    try:
        from .frontmatter import scan_pages
        annotations = scan_pages(_pages_from_blocks(blocks))
        positive = [a for a in annotations if a["score"] > 0]
        if not positive:
            return [], []
        top = positive[:8]
        ranked = {a["page"] for a in top}
        for a in positive[:1]:  # best page's two neighbors
            ranked |= {a["page"] - 1, a["page"] + 1}
        return annotations, sorted(p for p in ranked if p >= 1)
    except Exception:
        logger.warning("Deterministic front-matter scan failed", exc_info=True)
        return [], []


def locate_bibliographic_pages(blocks, total_pages, config, lm, doc_type):
    """Bound the search deterministically, then jointly locate and classify."""
    from ...utils import parse_page_range

    window = set(parse_page_range(config.page_range, total_pages))
    window = {p for p in window if 1 <= p <= total_pages}
    # Phase 1 (plan v2 §3.1): union the window with marker-scan hits so
    # imprints outside the printed-folio window (pages 5-6) are searchable.
    _, scan_ranked = _deterministic_scan(blocks)
    window |= {p for p in scan_ranked if 1 <= p <= total_pages}
    candidates = [b for b in blocks if isinstance(b.get("physical_page_index"), int)
                  and b["physical_page_index"] + 1 in window]
    # Reserve space for every page so a verbose cover cannot crowd out the colophon.
    page_budget = max(1, 18000 // max(len(window), 1))
    previews, remaining = [], {}
    for block in candidates:
        page = block["physical_page_index"] + 1
        available = remaining.setdefault(page, page_budget)
        text = block["text"][:available]
        if text:
            previews.append({**block, "text": text, "page": page})
            remaining[page] -= len(text)
    audit = {"search_pages": sorted(window), "selected_pages": [],
             "document_type": "unknown", "type_evidence": [],
             "status": "needs_review"}
    if not previews:
        return [], audit
    by_id = {b["id"]: b for b in previews}

    def supported(span):
        if not isinstance(span, dict):
            return False
        block = by_id.get(span.get("block_id"))
        quote = span.get("quote")
        return bool(block and isinstance(quote, str) and quote.strip()
                    and quote in block["text"])

    with dspy.context(lm=lm):
        result = dspy.Predict(LocateBibliographicPages)(
            page_blocks=previews, type_hint=doc_type,
            type_override=config.doc_type_override or "")
    for item in result.selected_pages:
        if supported(item) and item.get("page") == by_id[item["block_id"]]["page"]:
            audit["selected_pages"].append(item)
    pages = {item["page"] for item in audit["selected_pages"]}
    evidence = [span for span in result.type_evidence if supported(span)
                and by_id[span["block_id"]]["page"] in pages][:8]
    if result.document_type in {"book", "chapter", "article-journal", "thesis",
                                "entry-encyclopedia", "report", "manuscript"} and evidence:
        # Two-type model: standalone reports/manuscripts fold into book.
        audit["document_type"] = {"report": "book", "manuscript": "book"}.get(
            result.document_type, result.document_type)
        audit["type_evidence"] = evidence
    override = {"bookchapter": "chapter", "journal": "article-journal"}.get(
        config.doc_type_override, config.doc_type_override)
    if override and override != audit["document_type"]:
        audit["document_type"] = "unknown"
    if pages and audit["document_type"] != "unknown":
        audit["status"] = "selected"
    return [b for b in candidates if b["physical_page_index"] + 1 in pages], audit


class ExtractDocumentMetadata(dspy.Signature):
    """Extract ONLY the ingested source's own citation metadata, never its works cited.
    Use supplied original blocks, not summaries. Distinguish author/editor/translator.
    Preserve name order and CJK literal names. Read complete titles across adjacent
    blocks, keeping subtitle separate when printed separately. Distinguish revised
    publication dates from original dates, and co-publishers from series titles.
    The author is printed on the work's own title/imprint pages; never take an
    author from a subject heading, a series/collection title, or a work cited
    inside. A Latin-genitive collection title (Gregorii Nysseni Opera) is
    collection-title; its author is the named person (Gregory of Nyssa).
    'Compiled by X' on a bibliography makes X its author. When several presses
    are printed together on the imprint, emit all of them in printed order
    joined with '; '. EDIDIT, RECENSUIT, CURAVIT and 'edited by' mark an editor.
    'translated by X' makes X a translator even when X also appears as author.
    A leading quantity printed in the title block belongs to the title
    ('160 Unpublished Homilies of Jacob of Serugh').
    Determine whether the host is a book, chapter, report or article from its
    title and imprint pages; the doc_type input is only a hint. Do not infer
    publisher place or dates. For edited books, editor may be present without
    author; translators belong in translator, not author.
    For article-journal use the article title/author and journal container-title,
    volume, issue and printed page range; never use a reviewed book's metadata.
    For chapter use chapter title/author, book container-title and book editors,
    publisher and chapter printed page range. For book use the whole volume title
    and contributors, not an internal chapter or series heading. Physical PDF
    positions are evidence locators, never CSL page values.
    Omit unknown fields. For each value cite a block_id and exact supporting quote.
    A quoted reference to another work is NOT evidence of the host's identity.
    """
    source_blocks: List[Dict[str, Any]] = dspy.InputField()
    bibliographic_pages: List[Dict[str, Any]] = dspy.InputField(
        desc="Selected pages with physical page, bibliographic role and evidence quote; use these roles to identify the host title and contributors.")
    doc_type: str = dspy.InputField(desc="Heuristic hint, not authoritative classification.")
    missing_fields: List[str] = dspy.InputField()
    csl: Dict[str, Any] = dspy.OutputField(desc="CSL metadata: typed name arrays, issued date-parts, other fields strings.")
    field_evidence: Dict[str, Any] = dspy.OutputField(desc="CSL field -> {block_id, quote} or list of exact spans when names/title cross blocks. No invented IDs or quotations.")


class ReconcileMetadata(dspy.Signature):
    """Reconcile document metadata from two sources: GROBID and MinerU+DSPy.

    Choose the most accurate value for each field. GROBID is deterministic
    and reliable for standard Western academic PDFs. MinerU+DSPy may be
    better for CJK text, non-standard layouts, or fields GROBID missed.
    """

    grobid_json = dspy.InputField(
        desc="JSON string of GROBID-extracted metadata (may be empty '{}')."
    )
    dspy_json = dspy.InputField(
        desc="JSON string of DSPy-extracted metadata from MinerU output."
    )
    doc_type = dspy.InputField(desc="Document type.")

    final_title = dspy.OutputField(desc="Best title from either source.")
    final_author = dspy.OutputField(desc="Best author(s), semicolon-separated.")
    final_container_title = dspy.OutputField(desc="Best container title (or empty).")
    final_publisher = dspy.OutputField(desc="Best publisher (or empty).")
    final_year = dspy.OutputField(desc="Best publication year YYYY (or empty).")
    final_volume = dspy.OutputField(desc="Best volume (or empty).")
    final_issue = dspy.OutputField(desc="Best issue (or empty).")
    final_pages = dspy.OutputField(desc="Best page range (or empty).")
    final_doi = dspy.OutputField(desc="Best DOI (or empty).")
    final_abstract = dspy.OutputField(desc="Best abstract (or empty).")
    provenance = dspy.OutputField(
        desc=(
            "For each field, note which source was chosen: 'grobid', 'dspy', or 'both'. "
            "Format: 'title:grobid; author:dspy; year:both; ...'"
        )
    )


# ---------------------------------------------------------------------------
# Author parsing
# ---------------------------------------------------------------------------

def _parse_authors(author_str: str) -> List[Dict[str, str]]:
    """Parse an author string into a CSL author list.

    Handles:
    - CJK names separated by spaces: "刘文锁 王泽祥 王龙"
    - Western names with commas/semicolons: "John Smith, Jane Doe"
    - Mixed formats
    - Semicolons always split first (universal delimiter)
    """
    if not author_str or not author_str.strip():
        return []

    # Step 1: split on semicolons (universal delimiter)
    segments = re.split(r"[;；]", author_str)
    authors: List[Dict[str, str]] = []

    for segment in segments:
        segment = segment.strip()
        if not segment:
            continue

        if _is_cjk_dominant(segment):
            # CJK segment — split on whitespace; each token is one author
            names = segment.split()
            for name in names:
                name = name.strip()
                if name:
                    authors.append({"literal": name})
        elif "," in segment:
            # Western names separated by commas
            parts = [p.strip() for p in segment.split(",") if p.strip()]
            # Heuristic: "Smith, John" (family, given) vs "John Smith, Jane Doe"
            # If exactly 2 parts and the second looks like a first name, treat as single author
            if len(parts) == 2 and " " not in parts[0] and " " not in parts[1]:
                authors.append({"family": parts[0], "given": parts[1]})
            else:
                for part in parts:
                    authors.extend(_parse_single_western_name(part))
        else:
            authors.extend(_parse_single_western_name(segment))

    return authors


def _parse_single_western_name(name: str) -> List[Dict[str, str]]:
    """Parse a single Western-style name into CSL format."""
    name = name.strip()
    if not name:
        return []

    if _has_cjk(name):
        return [{"literal": name}]

    if " " in name:
        parts = name.rsplit(" ", 1)
        return [{"family": parts[-1], "given": parts[0]}]

    return [{"literal": name}]


# ---------------------------------------------------------------------------
# Pattern-based extraction helpers
# ---------------------------------------------------------------------------

# DOI
_DOI_RE = re.compile(r"(?:DOI|doi)\s*[:：]?\s*(10\.\d{4,}/\S+)")

# Chinese article code: 文章编号: 1002—4743 ( 2022) 01—0074—07
_ARTICLE_CODE_RE = re.compile(
    r"文章编号\s*[:：]\s*"
    r"(\d{4})\s*[—\-]\s*(\d{4})"      # ISSN first half
    r"\s*\(\s*(\d{4})\s*\)"            # year
    r"\s*(\d{1,2})"                    # issue
    r"\s*[—\-]\s*(\d{4})"             # start page (padded)
    r"\s*[—\-]\s*(\d{2})"             # page count
)

# ISSN
_ISSN_RE = re.compile(r"ISSN\s*[:：]?\s*(\d{4}[\-—]\d{3}[\dXx])")

# Running headers — journal name must be the *main content* of a short discarded block
# e.g. "《西域研究》 2022 年第 1 期" — whole block is basically just the header
_CN_JOURNAL_RE = re.compile(r"^[《](.+?)[》]")
_CN_HEADER_YEAR_ISSUE_RE = re.compile(
    r"(\d{4})\s*年\s*第?\s*(\d{1,2})\s*期"
)

# Abstract / keywords labels (multi-language)
_ABSTRACT_LABELS = re.compile(
    r"^(?:内容提要|摘\s*要|Abstract|ABSTRACT|Zusammenfassung|Résumé)\s*[:：]?\s*",
    re.IGNORECASE,
)
_KEYWORD_LABELS = re.compile(
    r"^(?:关键词|关\s*键\s*词|Keywords?|KEYWORDS?|Schlüsselwörter|Mots[- ]clés)\s*[:：]?\s*",
    re.IGNORECASE,
)

def _get_text(item: Dict[str, Any]) -> str:
    """Extract text from a content_list item."""
    return (item.get("text") or "").strip()


def _extract_doi(texts: List[str]) -> Optional[str]:
    """Find a DOI in a list of text strings."""
    for t in texts:
        m = _DOI_RE.search(t)
        if m:
            doi = m.group(1).rstrip(".,;:")
            return doi
    return None


def _extract_abstract(items: List[Dict[str, Any]], start_idx: int) -> Optional[str]:
    """Starting from *start_idx*, collect contiguous text as abstract."""
    text = _get_text(items[start_idx])
    body = _ABSTRACT_LABELS.sub("", text).strip()
    # Possibly runs across the next block if it's still on the same page
    page = items[start_idx].get("page_idx", -1)
    idx = start_idx + 1
    while idx < len(items):
        nxt = items[idx]
        if nxt.get("page_idx", -1) != page:
            break
        nxt_text = _get_text(nxt)
        # Stop if this looks like a new section
        if _KEYWORD_LABELS.match(nxt_text) or nxt.get("text_level") == 1:
            break
        body += " " + nxt_text
        idx += 1
    return body.strip() if body.strip() else None


def _extract_keywords(text: str) -> Optional[str]:
    """Return keyword string from a keyword-labelled block.

    Stops before Chinese bibliographic codes like 中图分类号, 文献标识码, 文章编号.
    """
    body = _KEYWORD_LABELS.sub("", text).strip()
    # Truncate at Chinese classification codes that follow keywords
    for stop in ("中图分类号", "文献标识码", "文章编号"):
        idx = body.find(stop)
        if idx > 0:
            body = body[:idx].strip()
    return body if body else None


def _parse_article_code(text: str) -> Dict[str, Any]:
    """Parse Chinese article code into ISSN, year, issue, page range."""
    m = _ARTICLE_CODE_RE.search(text)
    if not m:
        return {}
    issn_first, issn_second, year, issue, start_page_raw, page_count_raw = m.groups()
    start_page = int(start_page_raw)
    page_count = int(page_count_raw)
    end_page = start_page + page_count - 1
    return {
        "ISSN": f"{issn_first}-{issn_second}",
        "issued": {"date-parts": [[int(year)]]},
        "issue": issue.lstrip("0") or "1",
        "page": f"{start_page}-{end_page}",
    }


def _extract_journal_from_header(text: str) -> Optional[str]:
    """Extract journal name from running header like 《西域研究》2022年第1期.

    Only accept short blocks (< 80 chars) that look like actual running headers,
    not footnotes containing embedded book references like 《丝路探险》.
    """
    if len(text) > 80:
        return None
    m = _CN_JOURNAL_RE.search(text)
    if m:
        return m.group(1).strip()
    return None


def _extract_year_issue_from_header(text: str) -> Dict[str, Any]:
    """Extract year and issue from running header."""
    m = _CN_HEADER_YEAR_ISSUE_RE.search(text)
    if not m:
        return {}
    year, issue = m.groups()
    return {
        "issued": {"date-parts": [[int(year)]]},
        "issue": issue.lstrip("0") or "1",
    }


def _extract_page_from_block(text: str) -> Optional[int]:
    """Try each page-number pattern against *text*, return int or None."""
    for pat in _PAGE_NUM_PATTERNS:
        m = pat.search(text)
        if m:
            try:
                num = int(m.group(1))
                if 1 <= num <= 9999:
                    return num
            except ValueError:
                continue
    return None


# ---------------------------------------------------------------------------
# Public API: pattern-based extraction from content_list
# ---------------------------------------------------------------------------

def extract_metadata_from_content_list(
    content_list: List[Dict],
    discarded_blocks: Optional[List[Dict]] = None,
) -> Dict[str, Any]:
    """Pattern-based extraction from MinerU content_list. Returns CSL dict.

    Parameters
    ----------
    content_list : list of dict
        Items from MinerU's ``content_list.json``.  Each item has at least
        ``text``, ``page_idx``, and optionally ``text_level``, ``type``.
    discarded_blocks : list of dict, optional
        Blocks MinerU discarded (running headers, page numbers, etc.).
    """
    if not content_list:
        return {}

    csl: Dict[str, Any] = {}
    all_texts: List[str] = [_get_text(it) for it in content_list]
    discarded_texts: List[str] = (
        [_get_text(b) for b in discarded_blocks] if discarded_blocks else []
    )
    combined_texts = all_texts + discarded_texts

    # --- Title: first text_level==1 on page_idx 0 --------------------------
    title_idx: Optional[int] = None
    for i, item in enumerate(content_list):
        if item.get("page_idx", -1) == 0 and item.get("text_level") == 1:
            text = _get_text(item)
            if text:
                csl["title"] = text
                title_idx = i
                break

    # --- Authors: next text block after title on page_idx 0 -----------------
    if title_idx is not None:
        for i in range(title_idx + 1, len(content_list)):
            item = content_list[i]
            if item.get("page_idx", -1) != 0:
                break
            text = _get_text(item)
            if not text:
                continue
            # Skip if it's an abstract or keyword line
            if _ABSTRACT_LABELS.match(text) or _KEYWORD_LABELS.match(text):
                break
            # Skip if it looks like a DOI line
            if _DOI_RE.match(text):
                break
            csl["author"] = _parse_authors(text)
            break

    # --- Abstract -----------------------------------------------------------
    for i, item in enumerate(content_list):
        text = _get_text(item)
        if _ABSTRACT_LABELS.match(text):
            abstract = _extract_abstract(content_list, i)
            if abstract:
                csl["abstract"] = abstract
            break

    # --- Keywords -----------------------------------------------------------
    for item in content_list:
        text = _get_text(item)
        if _KEYWORD_LABELS.match(text):
            kw = _extract_keywords(text)
            if kw:
                csl["keyword"] = kw
            break

    # --- DOI ----------------------------------------------------------------
    doi = _extract_doi(combined_texts)
    if doi:
        csl["DOI"] = doi

    # --- ISSN ---------------------------------------------------------------
    for t in combined_texts:
        m = _ISSN_RE.search(t)
        if m:
            csl["ISSN"] = m.group(1)
            break

    # --- Article code (Chinese journals) ------------------------------------
    for t in combined_texts:
        ac = _parse_article_code(t)
        if ac:
            for k, v in ac.items():
                if k not in csl:
                    csl[k] = v
            break

    # --- Running headers from discarded blocks ------------------------------
    for t in discarded_texts:
        if not t:
            continue
        # Journal name
        if "container-title" not in csl:
            jname = _extract_journal_from_header(t)
            if jname:
                csl["container-title"] = jname
        # Year / issue from header
        yi = _extract_year_issue_from_header(t)
        if yi:
            if "issued" not in csl and "issued" in yi:
                csl["issued"] = yi["issued"]
            if "issue" not in csl and "issue" in yi:
                csl["issue"] = yi["issue"]

    # --- Page numbers from discarded blocks ---------------------------------
    if "page" not in csl and discarded_texts:
        page_nums: List[int] = []
        for t in discarded_texts:
            pn = _extract_page_from_block(t)
            if pn is not None:
                page_nums.append(pn)
        if page_nums:
            csl["page"] = f"{min(page_nums)}-{max(page_nums)}"

    csl["_extraction_method"] = "pattern"
    logger.info("Pattern extraction produced %d fields", len(csl) - 1)
    return csl


# ---------------------------------------------------------------------------
# Public API: page number extraction
# ---------------------------------------------------------------------------

def extract_page_numbers_from_content_list(
    content_list: List[Dict],
) -> Dict[int, int]:
    """Map page_idx → actual journal page number from discarded blocks.

    Scans discarded blocks for page-number patterns
    (``· N ·``, ``— N —``, ``第N页``, ``Page N``, ``S. N``, ``p. N``),
    then selects the best continuous sequence to filter out spurious
    numbers from footnote references or unrelated pages.
    """
    # Collect all candidates per page_idx: {page_idx: [candidate_nums]}
    candidates: Dict[int, List[int]] = {}

    for item in content_list:
        page_idx = item.get("page_idx")
        if page_idx is None:
            continue

        item_type = item.get("type", "")
        text = _get_text(item)
        if not text:
            continue

        # Only consider short discarded blocks (actual headers/footers, not footnotes)
        if item_type in ("discarded", "header", "footer", "page_number"):
            if len(text) > 20:
                # Long discarded blocks are likely footnotes, not page numbers
                continue
            pn = _extract_page_from_block(text)
            if pn is not None:
                candidates.setdefault(page_idx, []).append(pn)

    if not candidates:
        return {}

    # Select best continuous sequence
    return _select_continuous_sequence(candidates)


def _select_continuous_sequence(
    candidates: Dict[int, List[int]],
) -> Dict[int, int]:
    """Pick the best set of page numbers that forms a continuous sequence.

    For each page_idx that has candidates, try to find a consistent offset
    (actual_page = page_idx + offset) that satisfies the most pages.
    """
    from collections import Counter

    # Compute all possible offsets: offset = candidate - page_idx
    offset_votes: Counter = Counter()
    for page_idx, nums in candidates.items():
        for num in nums:
            offset = num - page_idx
            offset_votes[offset] += 1

    if not offset_votes:
        return {}

    # Pick the offset with the most votes
    best_offset, best_count = offset_votes.most_common(1)[0]

    # Build final map using this offset — only include pages that agree
    page_map: Dict[int, int] = {}
    for item_list in candidates.values():
        pass  # just need the keys
    for page_idx, nums in candidates.items():
        expected = page_idx + best_offset
        if expected in nums:
            page_map[page_idx] = expected
        else:
            # Accept the candidate closest to expected
            closest = min(nums, key=lambda n: abs(n - expected))
            if abs(closest - expected) <= 1:
                page_map[page_idx] = closest

    # Fill gaps using the offset for pages without candidates
    if page_map:
        max_idx = max(candidates.keys())
        for idx in range(max_idx + 1):
            if idx not in page_map:
                page_map[idx] = idx + best_offset

    return page_map


# ---------------------------------------------------------------------------
# Public API: DSPy fallback
# ---------------------------------------------------------------------------

def extract_metadata_with_dspy_fallback(
    content_list: List[Dict],
    mineru_markdown: str,
    doc_type: str,
    config: Optional[IngestionConfig] = None,
    discarded_blocks: Optional[List[Dict]] = None,
) -> Dict[str, Any]:
    """Pattern first, DSPy fallback for missing critical fields (title/author).

    Returns a CSL-compatible dict.
    """
    # Step 1: pattern extraction
    csl = extract_metadata_from_content_list(content_list, discarded_blocks)

    # Step 2: check if critical fields are present
    has_title = bool(csl.get("title"))
    has_author = bool(csl.get("author"))

    if has_title and has_author:
        return csl

    # Step 3: DSPy fallback
    logger.info("Pattern extraction missing %s — invoking DSPy fallback",
                "title+author" if not has_title and not has_author
                else ("title" if not has_title else "author"))

    dspy_csl = _run_dspy_extraction(mineru_markdown, doc_type, config)
    if not dspy_csl:
        return csl

    # Merge: pattern results take priority, DSPy fills gaps
    for key, value in dspy_csl.items():
        if key.startswith("_"):
            continue
        if key not in csl or not csl[key]:
            csl[key] = value

    csl["_extraction_method"] = "pattern+dspy"
    return csl


def _rejection_reason(field: str, value: Any, evidence: Any) -> str:
    """Short human-readable reason why validate_block_evidence rejected a field."""
    from ..csl import valid_host_value
    if not valid_host_value(field, value):
        return f"invalid {field} value"
    spans = evidence.get("spans") if isinstance(evidence, dict) and "spans" in evidence else evidence
    if not isinstance(spans, list) or not 1 <= len(spans) <= 8:
        return f"invalid evidence spans for {field}"
    for span in spans:
        if not isinstance(span, dict) or not str(span.get("quote") or "").strip():
            return f"missing quote for {field}"
    return f"quote for {field} not found in source blocks or does not support value"


def _filter_repeated_lines(blocks, ranked, scan_ranked):
    """Drop watermark lines recurring on ≥3 pages from non-ranked pages.

    A running header can repeat the real title; the ranked shortlist keeps
    its pages' copies. Blocks on non-ranked pages whose whole normalized
    text is a repeated line ("Crack by RAOGY."-style) are removed from the
    LLM's candidate pool.
    """
    if not blocks:
        return blocks, ranked
    try:
        from .frontmatter import repeated_lines, _normalize_block_key
        repeated = repeated_lines(_pages_from_blocks(
            [b for b in blocks if isinstance(b.get("physical_page_index"), int)]))
        if not repeated:
            return blocks, ranked
        ranked_pages = set(scan_ranked)

        def keep(block):
            page = block.get("physical_page_index")
            if isinstance(page, int) and page + 1 in ranked_pages:
                return True
            return _normalize_block_key(block.get("text", "")) not in repeated

        blocks = [b for b in blocks if keep(b)]
        ranked = [b for b in ranked if any(nb["id"] == b["id"] for nb in blocks)] \
            if ranked else ranked
        return blocks, ranked
    except Exception:
        logger.warning("Repeated-line filter failed", exc_info=True)
        return blocks, ranked


_CIP_FIELD_MAP = {
    "title": "title", "subtitle": "subtitle", "authors": "author",
    "author": "author",  # LoC parser emits CSL-shaped dict values directly
    "place": "publisher-place", "publisher": "publisher", "year": "issued",
    "ISBN": "ISBN", "series": "collection-title", "translator": "translator",
    "editor": "editor",
}
# Online reference works: the cite-this-page block prints a high-precision year.
_ONLINE_YEAR_RE = re.compile(r"First published online\s*[:：]?\s*((?:19|20)\d{2})")
# Latin imprint copyright lines. Variants seen on the scanned corpus:
# benedict 'Copyright© 2006 St. Bede's Publications' (an earlier '©2001'
# French-copyright line in the same block carries the original edition and
# must NOT win), book-step1 'Translation copyrighted, Gstercian
# Publications, Inc. 2004', anthony1-letters 'first pub  \nlished in 1995'
# (OCR break inside 'published', and the 'Copyright ©1990, 1995' line in
# the same block records the earlier edition). Branch priority is handled
# by the caller: edition-first-publication > Copyright-prefixed > bare ©.
_COPYRIGHT_YEAR_RE = re.compile(
    r"(?P<copyright>Copyright(?:ed)?)\b[^\n]{0,80}?(?P<year>(?:19|20)\d{2})"
    r"|first\s+pub\s*li\s*shed\s+(?:in\s+)?(?P<year2>(?:19|20)\d{2})"
    r"|©\s*(?:(?P<name2>[A-ZÀ-ÖØ-Þ][\w&'’.\-, ]{0,60}?),?\s+)?(?P<year3>(?:19|20)\d{2})",
    re.IGNORECASE,
)
# Scholarly Latin role verbs marking an editor on the title page.
_EDITOR_VERB_RE = re.compile(r"\b(EDIDIT|RECENSUIT|CURAVIT|Herausgegeben|HERAUSGEGEBEN)\b")
# Translator credit lines on scanned title/copyright pages:
# 'Translated, with an Introduction and Notes by Robert A. Kitchen and
# Martien F. G. Parmentier' (book-step1), 'Translation and Notes: Fr.
# MAXIMOS CONSTAS' (aimilianos), '[translated] by Adam Lehto' (aphrahat
# LoC block). A line ending at 'by'/'Translation:' with the name in the
# next block is handled by the caller ('Translated by' / 'Gerald
# Malsbary', benedict). CJK 译 credits put the name before the marker and
# are handled separately.
_TRANSLATOR_LINE_RE = re.compile(
    r"Translat(?:ed|ion)[^\n]{0,60}?\s*(?:by\b|[:：])\s*"
    r"(?P<names>[A-ZÀ-ÖØ-Þ][\w'’.\-]*(?:\s+[A-ZÀ-ÖØ-Þ][\w'’.\-]*){0,5}"
    r"(?:\s+and\s+[A-ZÀ-ÖØ-Þ][\w'’.\-]*(?:\s+[A-ZÀ-ÖØ-Þ][\w'’.\-]*){0,5})*)",
)
# Series-shaped title-page headings route to collection-title, never title.
_SERIES_VOCAB_RE = re.compile(
    r"serie|series|studien|studies|corpus|scriptorum|monumenta|bibliothek"
    r"|library|council|schriftsteller|丛书|译丛|文庫|文库",
    re.IGNORECASE,
)
# Front-matter section headings are never the book title.
_FRONTMATTER_VOCAB_RE = re.compile(
    r"preface|foreword|acknowledg|introduction|contents|appendix|index"
    r"|bibliography|about the author|prooemium|praefatio|vorwort|目录|前言|序言|导论"
    r"|how to use|note on the text",
    re.IGNORECASE,
)
# OCR-garbled headings must not become titles (astudy: 'THE DIVINE
# LI+URGY OF ST JON CHRSOSTOM' — the LLM's corrected title wins instead).
_HEADING_GARBLE_RE = re.compile(
    r"[A-Za-z]\+|\+[A-Za-z]|\w+·\w+|[A-Z]{2,}[a-z]\w*|\w*[a-z][A-Z]{2,}\w*|\"|“|”"
)
# Imprint lines name the publisher, optionally followed by a place
# (book-step1 'CISTERCIAN PUBLICATIONS KALAMAZOO, MICHIGAN'; benedict
# "St. Bede's Publications Petersham, MA"; clemens "J.C. HINRICHS'SCHE
# BUCHHANDLUNG"). 'edition' is deliberately not a suffix — it matches the
# book-type line 'Third Edition'.
_PUBLISHER_SUFFIX_WORDS = "publications|publishers|press|verlag|publishing|buchhandlung|books"
_IMPRINT_BLOCK_RE = re.compile(
    rf"^(?P<pub>[A-Z][\w&'’.\- ]{{1,60}}?\s(?:{_PUBLISHER_SUFFIX_WORDS})\.?)"
    rf"(?:,?\s+(?P<place>[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ0-9.,\- ]{{0,39}}?))?$",
    re.IGNORECASE,
)


def _latin_person_value(raw: str):
    """CSL-shaped name from a Latin-script person string, else None.

    The final token is the family name ('Robert A. Kitchen' → given
    'Robert A.', family 'Kitchen'; 'G. GARITTE' → given 'G.', family
    'GARITTE'), a single token becomes family. OCR glues nobiliary particles
    to the surname ('Adalbert deVogüé' → 'Adalbert de Vogüé'; the LoC block
    confirms 'Vogüé, Adalbert de'). Digits and CJK are rejected.
    """
    if not raw:
        return None
    name = re.sub(r"\s+", " ", raw.strip()).strip(" .,")
    name = " ".join(re.sub(r"^(de|van|von|del|della|di|la|le)(?=[A-ZÀ-Þ])", r"\1 ", token)
                    for token in name.split())
    if not name or _has_cjk(name) or re.search(r"\d", name):
        return None
    tokens = name.split()
    if len(tokens) == 1:
        return {"family": name}
    return {"given": " ".join(tokens[:-1]), "family": tokens[-1]}


def _split_person_list(names: str) -> list[dict]:
    """'Robert A. Kitchen and Martien F. G. Parmentier' → two CSL names."""
    return [p for p in (_latin_person_value(part) for part in re.split(r"\s+and\s+", names.strip())) if p]


# Role and institution words that can never open a person name on a scanned
# title page (clemens 'VON' / 'IM AUFTRAGE ...'; fiey 'HERAUSGEGEBEN VOM
# ORIENT-INSTITUT ...').
_NAME_LEAD_STOP = {"VON", "DER", "DEN", "DIE", "DAS", "IM", "AM", "IN", "AN", "BEI",
                   "VOM", "ZUM", "ET", "EX", "DE", "AUFTRAGE", "PROFESSOR", "PROIESSOR",
                   "EDIDIT", "RECENSUIT", "CURAVIT", "HERAUSGEGEBEN"}


def _person_leading(text: str):
    """CSL name at the start of a scanned credit block, else None.

    'G. GARITTE' → given G., family GARITTE; 'DR. OTTO STÄHLIN PROIESSOR ...'
    → given OTTO, family STÄHLIN (the trailing office is dropped). Bare role
    and institution words are rejected.
    """
    if not text:
        return None
    rest = re.sub(r"^(?:DR|PROF|D)\.?\s+", "", text.strip(), flags=re.IGNORECASE)
    parts = [t for t in re.split(r"\s+", rest) if t]
    taken = []
    for token in parts:
        word = token.rstrip(".,;:")
        if not word or word.upper() in _NAME_LEAD_STOP \
                or not re.fullmatch(r"[A-Za-zÀ-ÖØ-Þà-öø-ÿ][\w'’\-]*", word):
            break
        taken.append(token.rstrip(";:"))
        if len(taken) == 2:
            break
    return _latin_person_value(" ".join(taken)) if len(taken) == 2 else None


def _roman_year(text: str):
    """'MDCCCCXLIX' → 1949 when the Roman numeral is a plausible imprint year."""
    m = re.fullmatch(r"(?P<num>[MDCLXVI]{6,18})", text.strip())
    if not m:
        return None
    values = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}
    num = m.group("num")
    total = sum(values[c] for c in num)
    for a, b in zip(num, num[1:]):
        if values[a] < values[b]:
            total -= 2 * values[a]
    return total if 1400 <= total <= 2030 else None


def _heading_title_candidates(content_list):
    """Heading-based title/subtitle/author candidates (scanned).

    Guards derived from the 12-source probe (2026-09-26): front-matter,
    garbled and series-shaped headings are skipped; only headings inside the
    front block of sampled pages can be a title (back-matter chapters carry
    their own L1s — astudy p112, orthodox p241). Same-page L1 pairs are
    title+subtitle; a single heading splits on a medial ' The ' (book-step1)
    or takes the immediately following L2 heading as its subtitle
    (antonyletters). Classical editions that print the author as a two-token
    all-caps heading beside an editor-verb get it back as an author literal
    (clemens 'CLEMENS ALEXANDRINUS').
    """
    items = [it for it in (content_list or [])
             if isinstance(it, dict) and (it.get("text") or "").strip()]
    pages: dict[int, list[dict]] = {}
    for idx, item in enumerate(items):
        if item.get("text_level") == 1 and isinstance(item.get("page_idx"), int):
            pages.setdefault(item["page_idx"], []).append(
                {"text": item["text"].strip(), "index": idx})
    if not pages:
        return []
    # Front block: the leading run of sampled pages (the citation-pages
    # sampler takes pages 1-10 plus the last 3). Titles live here; the tail
    # run is back matter.
    sampled = sorted({it["page_idx"] for it in items if isinstance(it.get("page_idx"), int)})
    front: set[int] = set()
    for p in sampled:
        if front and p - max(front) > 3:
            break
        front.add(p)
    cands: list[dict] = []
    for page in sorted(pages):
        if page not in front:
            continue
        heads = pages[page]
        first = heads[0]["text"]
        if _FRONTMATTER_VOCAB_RE.search(first) or _HEADING_GARBLE_RE.search(first):
            continue
        if _SERIES_VOCAB_RE.search(first):
            continue
        page_texts = "\n".join((it.get("text") or "") for it in items
                               if it.get("page_idx") == page)
        if len(heads) >= 2 and not _HEADING_GARBLE_RE.search(heads[1]["text"]):
            cands.append({"field": "title", "value": first, "confidence": 0.95, "quote": first})
            cands.append({"field": "subtitle", "value": heads[1]["text"],
                          "confidence": 0.95, "quote": heads[1]["text"]})
        elif (split := re.match(r"^(?P<t>.+?)\s(?P<s>The\s.+)$", first)) \
                and len(split.group("t").split()) >= 2 and len(split.group("s").split()) >= 2:
            cands.append({"field": "title", "value": split.group("t"),
                          "confidence": 0.95, "quote": first})
            cands.append({"field": "subtitle", "value": split.group("s"),
                          "confidence": 0.95, "quote": first})
        else:
            title = {"field": "title", "value": first, "confidence": 0.95, "quote": first}
            nxt = items[heads[0]["index"] + 1] if heads[0]["index"] + 1 < len(items) else None
            if nxt and nxt.get("page_idx") == page and nxt.get("text_level") == 2 \
                    and not _HEADING_GARBLE_RE.search(nxt.get("text") or "") \
                    and not _FRONTMATTER_VOCAB_RE.search(nxt.get("text") or ""):
                cands.append(title)
                cands.append({"field": "subtitle", "value": nxt["text"].strip(),
                              "confidence": 0.95, "quote": nxt["text"].strip()})
            else:
                # Sole heading, no printed subtitle partner: a later LLM
                # subtitle would compose into a different title.
                title["_no_subtitle"] = True
                cands.append(title)
        toks = first.split()
        if (len(toks) == 2 and first == first.upper() and all(t.isalpha() for t in toks)
                and _EDITOR_VERB_RE.search(page_texts)
                and not re.search(r"\bby\s+[A-ZÀ-ÖØ-Þ]", page_texts)):
            # Author printed as the title-page heading in a critical edition.
            cands.append({"field": "authors", "value": first.title(),
                          "confidence": 0.95, "quote": first})
        return cands
    return cands
_CIP_ALLOWED_BY_TYPE = {
    # Whole-volume types take the CIP's own identity; contributions only
    # take the container imprint fields (CIP describes the host's book).
    "book": ("title", "subtitle", "authors", "author", "translator", "editor", "place", "publisher", "year", "ISBN", "series"),
    "thesis": ("title", "subtitle", "authors", "author", "translator", "editor", "place", "publisher", "year", "ISBN", "series"),
    # Article-category contributions (chapter, encyclopedia entry): the CIP
    # describes the host container, so only imprint fields are safe to seed.
    "chapter": ("place", "publisher", "year", "ISBN"),
    "entry-encyclopedia": ("place", "publisher", "year", "ISBN"),
    "article-journal": ("place", "publisher", "year", "ISBN"),
}


def _seed_deterministic_candidates(pre_blocks, scan_annotations, scan_ranked,
                                   selection, accepted, evidence, content_list=None):
    """Seed evidence-gated CIP/LoC candidates into ``accepted``/``evidence``.

    Candidates carry confidence ≥0.95 (anchored CIP 0.99 / LoC RDA 0.98;
    unanchored 0.9 is excluded). Values must pass ``validate_block_evidence``
    like any LLM proposal — deterministic provenance is not a bypass.
    Returns the list of seeded (csl_field, candidate) pairs for the audit.
    """
    from ..citation_verification import validate_block_evidence

    if selection is None or not pre_blocks:
        return []
    doc_type = selection.get("document_type")
    allowed = _CIP_ALLOWED_BY_TYPE.get(doc_type)
    if not allowed:
        return []
    seeded_fields = []
    try:
        from .frontmatter import parse_cip, parse_loc_cip, validate_isbn13
        candidates = []
        seen = set()
        # Scanned-only rules: content_list is the scanned pipeline's OCR
        # payload. The digital path passes None and keeps its frozen
        # behaviour. Heading candidates go first so a title-page heading
        # beats a CIP/LoC transcription of the same field.
        scanned = content_list is not None
        if scanned:
            for cand in _heading_title_candidates(content_list):
                key = (cand["field"],
                       json.dumps(cand["value"], sort_keys=True, ensure_ascii=False))
                if key not in seen:
                    seen.add(key)
                    candidates.append(cand)
            # astudy-style LoC entry line: 'Hostetter, Jr., William Taylor A
            # Study of The Divine Liturgy of St John Chrysostom'. The title
            # page's own heading is OCR-garbled ('LI+URGY'), so the catalog
            # entry is the only clean printed title. ponytail: anchored to the
            # LIBRARY OF CONGRESS page + surname-first entry; upgrade when a
            # source prints a clean heading after a garbled one.
            for block in pre_blocks:
                text = (block.get("text") or "").strip()
                entry = re.match(
                    r"(?P<surname>[A-ZÀ-ÖØ-Þ][\w'’\-]+),\s*"
                    r"(?:(?:Jr|Sr|II|III|IV)\.?,\s*)?"
                    r"[A-ZÀ-ÖØ-Þ][\w'’.\- ]{1,40}?\s+(?P<title>(?:A|An|The)\s+\S.*)$",
                    text)
                if not entry:
                    continue
                page = block.get("physical_page_index")
                if not any("LIBRARY OF CONGRESS" in (b.get("text") or "").upper()
                           for b in pre_blocks if b.get("physical_page_index") == page):
                    continue
                title = entry.group("title")
                for sep in (" / ", " -- ", " ; ", " (", " ["):
                    title = title.split(sep)[0]
                title = title.strip(" .")
                if len(title.split()) < 4 or _HEADING_GARBLE_RE.search(title):
                    continue
                key = ("title", json.dumps(title, sort_keys=True, ensure_ascii=False))
                if key not in seen:
                    seen.add(key)
                    candidates.append({"field": "title", "value": title,
                                       "confidence": 0.95, "quote": text})
                break
        # C2 (scanned two-type plan 2026-09-26): parse EVERY sampled page,
        # not just the marker-ranked shortlist — the CJK colophon or LoC CIP
        # can sit on any of the ≤13 sampled pages (使徒教父著作: CIP on p2).
        # Adjacent-page joins try every consecutive pair (CIP spanning two
        # pages), not just the top-2 ranked pages.
        page_text = {}
        for block in pre_blocks:
            page = block.get("physical_page_index")
            if isinstance(page, int):
                page_text.setdefault(page, []).append(block.get("text", ""))
        joined = {p: "\n".join(ts) for p, ts in page_text.items()}
        parse_units = []
        for p in sorted(joined):
            parse_units.append((f"page {p}", joined[p]))
        for p in sorted(joined):
            if p + 1 in joined:
                parse_units.append((f"pages {p}-{p+1}", joined[p] + "\n" + joined[p + 1]))
        for _where, text in parse_units:
            for parsed in (parse_cip(text), parse_loc_cip(text)):
                for cand in parsed:
                    # ponytail: json key — dict values (LoC author) aren't hashable
                    key = (cand["field"], json.dumps(cand["value"], sort_keys=True, ensure_ascii=False))
                    if cand["confidence"] >= 0.95 and key not in seen:
                        seen.add(key)
                        candidates.append(cand)
        # Online reference works print 'First published online: YYYY' in the
        # cite-this-page block — a high-precision deterministic year source
        # that sits off the marker-ranked shortlist.
        if doc_type in {"entry-encyclopedia", "article-journal"}:
            for block in pre_blocks:
                m = _ONLINE_YEAR_RE.search(block.get("text", ""))
                if m:
                    key = ("year", json.dumps(int(m.group(1))))
                    if key not in seen:
                        seen.add(key)
                        candidates.append({"field": "year", "value": int(m.group(1)),
                                           "confidence": 0.99,
                                           "quote": block.get("text", "")})
                    break
        # Scanned books: Latin-script imprint lines are high-precision year
        # sources. Branch priority matters (scanned two-type plan 2026-09-26):
        # an edition-first-publication statement beats a Copyright-prefixed
        # line, which beats a bare © notice. anthony1-letters carries both
        # 'first pub lished in 1995' and 'Copyright ©1990, 1995' (the 1990
        # earlier edition); benedict carries '©2001 Abbaye de Bellefontaine'
        # (the French original) and 'Copyright© 2006 St. Bede's Publications'
        # (this edition). CJK blocks are excluded — ©-years there are often
        # reprint years.
        if scanned and doc_type in {"book", "thesis"}:
            year_block = None
            for priority in ("year2", "copyright", None):
                for block in pre_blocks:
                    text = block.get("text", "")
                    if _has_cjk(text):
                        continue
                    matches = list(_COPYRIGHT_YEAR_RE.finditer(text))
                    if not matches:
                        continue
                    chosen = matches[0] if priority is None \
                        else next((m for m in matches if m.group(priority)), None)
                    if chosen is None:
                        continue
                    year_block = (chosen.group("year") or chosen.group("year2")
                                  or chosen.group("year3"), text)
                    break
                if year_block:
                    break
            if year_block:
                year = int(year_block[0])
                key = ("year", json.dumps(year))
                if key not in seen:
                    seen.add(key)
                    candidates.append({"field": "year", "value": year,
                                       "confidence": 0.95, "quote": year_block[1]})
            else:
                # Scholarly imprints print the year as a Roman numeral
                # (anthony-coptic 'PARISIIS MDCCCCXLIX' → 1949). Only a
                # standalone numeral counts; embedded ones are dates in prose.
                for block in pre_blocks:
                    text = block.get("text", "")
                    if _has_cjk(text):
                        continue
                    year = next((y for line in text.splitlines()
                                 if (y := _roman_year(line))), None)
                    if year:
                        key = ("year", json.dumps(year))
                        if key not in seen:
                            seen.add(key)
                            candidates.append({"field": "year", "value": year,
                                               "confidence": 0.95, "quote": text})
                        break
        # Imprint lines name the publisher and usually a place (book-step1
        # 'CISTERCIAN PUBLICATIONS KALAMAZOO, MICHIGAN'; benedict "St. Bede's
        # Publications Petersham, MA"). The title-page garbles
        # ('Gstercian Publications', 'paBLfcatíons') fail the digit/garble
        # guards, so only a clean imprint seeds. 'edition' is not a suffix
        # word (it would match 'Third Edition').
        if scanned and doc_type in {"book", "thesis"}:
            for block in pre_blocks:
                text = block.get("text", "").strip()
                m = _IMPRINT_BLOCK_RE.match(text)
                if not m:
                    continue
                pub = m.group("pub").strip(" .,")
                place = (m.group("place") or "").strip(" .,")
                # A digit anywhere means it is not a bare imprint line
                # ('Gstercian Publications, Inc. 2004' — the year lands in
                # the place group and the publisher is garbled).
                if re.search(r"\d", pub) or (place and re.search(r"\d", place)) \
                        or re.search(r"\bby\b", text, re.IGNORECASE) \
                        or _HEADING_GARBLE_RE.search(pub):
                    continue
                for field, value in (("publisher", pub), ("place", place)):
                    if not value:
                        continue
                    key = (field, json.dumps(value, sort_keys=True, ensure_ascii=False))
                    if key not in seen:
                        seen.add(key)
                        candidates.append({"field": field, "value": value,
                                           "confidence": 0.95, "quote": text})
                break
        # Title pages print the author on a 'by <name>' line (benedict p0
        # 'by Adalbert deVogüé' → Adalbert de Vogüé; the OCR glues the
        # nobiliary particle, so only this seed yields the canonical form).
        # Some title lines carry the author inline ('The Demonstrations of
        # Aphrahat, the Persian Sage' → Aphrahat, the Persian Sage); the LoC
        # catalog block spells the same person its own way ('Aphraates') and
        # must not win.
        if scanned and doc_type in {"book", "thesis"} \
                and "author" not in {cand["field"] for cand in candidates} \
                and "authors" not in {cand["field"] for cand in candidates}:
            for block in pre_blocks:
                text = block.get("text", "").strip()
                byline = re.fullmatch(r"(?i)by\s+(?P<name>[^\n]{2,60})", text)
                person = _latin_person_value(byline.group("name")) if byline else None
                if not person and len(text) <= 80:
                    epithet = re.fullmatch(
                        r"The\s+.+?\sof\s+(?P<person>[A-ZÀ-ÖØ-Þ][\w'’.\- ]+,\s+the\s+"
                        r"[A-ZÀ-ÖØ-Þ][\w'’.\- ]+)", text)
                    if epithet:
                        person = {"literal": epithet.group("person")}
                if person:
                    key = ("authors", json.dumps(person, sort_keys=True, ensure_ascii=False))
                    if key not in seen:
                        seen.add(key)
                        candidates.append({"field": "authors", "value": person,
                                           "confidence": 0.95, "quote": text})
                    break
        # Translator credits on scanned title pages. The name usually sits on
        # the same line (book-step1 'Translated, ... by Robert A. Kitchen and
        # Martien F. G. Parmentier' → two names) but may be a bare 'Translated
        # by' block followed by the name (benedict 'Gerald Malsbary'). CJK
        # colophons put the name before 译 (使徒教父著作 '高陈宝婵 等译').
        if scanned and doc_type in {"book", "thesis"} \
                and "translator" not in {cand["field"] for cand in candidates}:
            texts = [b.get("text", "") for b in pre_blocks]
            for i, text in enumerate(texts):
                m = _TRANSLATOR_LINE_RE.search(text)
                if m and (people := _split_person_list(m.group("names"))):
                    key = ("translator", json.dumps(people, sort_keys=True, ensure_ascii=False))
                    if key not in seen:
                        seen.add(key)
                        candidates.append({"field": "translator", "value": people,
                                           "confidence": 0.95, "quote": text})
                    break
                if re.match(r"(?i)Translat(?:ed|ion)\b[^\n]*\bby\s*$", text.strip()) \
                        and i + 1 < len(texts) and (person := _person_leading(texts[i + 1])):
                    key = ("translator", json.dumps([person], sort_keys=True, ensure_ascii=False))
                    if key not in seen:
                        seen.add(key)
                        candidates.append({"field": "translator", "value": [person],
                                           "confidence": 0.95, "quote": texts[i + 1]})
                    break
                cjk = re.search(r"(?P<n>[\u4e00-\u9fff]{2,4}(?:\s*等)?)译", text)
                if cjk:
                    literal = re.sub(r"\s+", "", cjk.group("n"))
                    key = ("translator", json.dumps([{"literal": literal}],
                                                    sort_keys=True, ensure_ascii=False))
                    if key not in seen:
                        seen.add(key)
                        candidates.append({"field": "translator", "value": [{"literal": literal}],
                                           "confidence": 0.95, "quote": text})
                    break
        # A bare role verb opens a title-page editor credit (anthony-coptic
        # 'EDIDIT' / 'G. GARITTE'; clemens 'HERAUSGEGEBEN' ... 'VON' / 'DR.
        # OTTO STÄHLIN...'). Non-bare lines (fiey/acoptic series editors) are
        # left alone, and the lookahead stays on the verb's page.
        if scanned and doc_type in {"book", "thesis"} \
                and "editor" not in {cand["field"] for cand in candidates}:
            texts = [(b.get("text", ""), b.get("physical_page_index")) for b in pre_blocks]
            for i, (text, page) in enumerate(texts):
                if not re.fullmatch(r"(?i)(EDIDIT|RECENSUIT|CURAVIT|HERAUSGEGEBEN)\.?",
                                    text.strip()):
                    continue
                for j in range(i + 1, min(i + 6, len(texts))):
                    cand_text, cand_page = texts[j]
                    if cand_page != page:
                        break
                    if re.fullmatch(r"(?i)von\.?", cand_text.strip()):
                        nxt = texts[j + 1] if j + 1 < len(texts) \
                            and texts[j + 1][1] == page else None
                        person = _person_leading(nxt[0]) if nxt else None
                        quote = nxt[0] if nxt else cand_text
                    else:
                        person = _person_leading(cand_text)
                        quote = cand_text
                    if not person:
                        continue
                    key = ("editor", json.dumps(person, sort_keys=True, ensure_ascii=False))
                    if key not in seen:
                        seen.add(key)
                        candidates.append({"field": "editor", "value": person,
                                           "confidence": 0.95, "quote": quote})
                    break
        # CJK chief-editor credits (主编) name the edition's editor
        # (使徒教父著作 '黄锡木主编' → 黄锡木). Only a line that ends at the
        # marker counts — the general '主编…副主编…' masthead does not.
        if scanned and doc_type in {"book", "thesis"} \
                and "editor" not in {cand["field"] for cand in candidates}:
            for block in pre_blocks:
                text = block.get("text", "").strip()
                if not text.endswith("主编"):
                    continue
                people = [{"literal": p} for p in re.split(r"\s+", text[:-2].strip())
                          if len(p) >= 2 and _has_cjk(p)]
                if people:
                    key = ("editor", json.dumps(people, sort_keys=True, ensure_ascii=False))
                    if key not in seen:
                        seen.add(key)
                        candidates.append({"field": "editor", "value": people,
                                           "confidence": 0.95, "quote": text})
                break
        # A sole-heading title (no printed subtitle partner) must not gain a
        # subtitle from another layer: the composed title would diverge.
        heading_no_subtitle = any(c.get("_no_subtitle") for c in candidates
                                  if c["field"] == "title")
        for cand in candidates:
            source_field = cand["field"]
            if source_field not in allowed:
                continue
            if source_field == "subtitle" and heading_no_subtitle:
                continue
            csl_field = _CIP_FIELD_MAP[source_field]
            if csl_field in accepted:
                continue
            value = cand["value"]
            # ponytail: a value ending mid-phrase means the printed line was
            # OCR-truncated (caridi: "...in the Catholic and"); skip the seed
            # and let the LLM read the complete block. Upgrade: LoC parser
            # joins RDA continuation lines across blocks.
            if source_field in ("title", "subtitle") and isinstance(value, str):
                stripped = value.rstrip()
                if stripped.endswith(("/", ":", "：", "-", "—", ",", "，")) or \
                        stripped.casefold().endswith((" and", " or", " of", " the", "与", "和", "及", "或")):
                    logger.warning("Skipping truncated deterministic %s=%r",
                                   source_field, stripped[:50])
                    continue
            if source_field == "year":
                value = {"date-parts": [[int(value)]]}
            elif source_field in ("authors", "translator", "editor"):
                # Heading/credit seeds may already emit CSL-shaped names.
                if isinstance(value, str):
                    value = [{"literal": value}]
                elif isinstance(value, dict):
                    value = [value]
            elif source_field == "author" and isinstance(value, dict):
                value = [value]  # LoC Names: CSL-shaped, needs the list wrap
            elif source_field == "ISBN" and not validate_isbn13(value):
                logger.warning("Rejected deterministic ISBN (checksum): %s", value)
                continue
            if csl_field == "ISBN":
                value = str(value)
            # Locate the block whose text contains the candidate's quote.
            # The parser's quote spans the whole CIP main line, which
            # build_source_blocks may split across paragraph blocks — fall
            # back to any block containing the value's own strings.
            from ..citation_verification import _value_strings
            quote = cand["quote"]
            host = next((b for b in pre_blocks if quote and quote in b.get("text", "")), None)
            if host is None:
                value_strs = [s for s in _value_strings(value) if s and s.strip()]
                host = next((b for b in pre_blocks
                             if any(s in b.get("text", "") for s in value_strs)), None)
                if host is not None:
                    quote = host["text"]
            if host is None:
                logger.warning("Deterministic candidate %s=%r lacks a host block",
                               csl_field, value)
                continue
            spans = [{"block_id": host["id"], "quote": quote}]
            supported = validate_block_evidence(csl_field, value, spans, pre_blocks)
            if not supported:
                logger.warning("Rejected deterministic candidate %s=%r: %s",
                               csl_field, value,
                               _rejection_reason(csl_field, value, spans))
                continue
            accepted[csl_field] = value
            evidence[csl_field] = supported
            seeded_fields.append((csl_field, cand))
    except Exception:
        logger.warning("Deterministic candidate seeding failed", exc_info=True)
    return seeded_fields


def _equivalent_candidate_values(csl_field, deterministic, llm):
    """Loose equivalence for the conflict audit (no false conflicts)."""
    from ..citation_verification import _value_strings
    det = {v.casefold().strip() for v in _value_strings(deterministic) if v.strip()}
    llm_s = {v.casefold().strip() for v in _value_strings(llm) if v.strip()}
    return bool(det and det <= llm_s or llm_s and llm_s <= det) or (
        isinstance(deterministic, str) and isinstance(llm, str)
        and deterministic.strip() in llm or llm.strip() in deterministic)


def _run_dspy_extraction(
    source_text: str,
    doc_type: str,
    config: Optional[IngestionConfig] = None,
    region_hints: Optional[List[str]] = None,
    source_blocks: Optional[List[Dict[str, Any]]] = None,
    candidate_regions: Optional[List[Dict[str, Any]]] = None,
    total_pages: Optional[int] = None,
    content_list: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Locate/classify PDF pages, extract and validate, with at most one refinement."""
    from ..citation_verification import HOST_FIELDS, validate_block_evidence
    from ..csl import evaluation_fields
    from .common import doc_type_to_csl_type
    cfg = config or IngestionConfig()
    blocks = source_blocks if source_blocks is not None else [
        {"id": f"text_{i}", "text": text, "char_start": start}
        for i, (start, text) in enumerate(
            ((m.start(), m.group()) for m in re.finditer(r"[^\n]+", source_text))
        ) if text.strip()
    ]
    if not blocks:
        return {}
    selection = None
    lm = None
    pre_blocks = blocks  # full pre-locate block list for deterministic candidates
    if total_pages is not None:
        try:
            lm = get_llm_model(cfg.llm_model, temperature=0.1)
            blocks, selection = locate_bibliographic_pages(blocks, total_pages, cfg, lm, doc_type)
        except Exception:
            logger.warning("Bibliographic page selection failed", exc_info=True)
            return {"_bibliographic_selection": {"status": "failed"},
                    "_citation_status": "incomplete"}
        if not blocks or selection["document_type"] == "unknown":
            return {"_bibliographic_selection": selection, "_citation_status": "incomplete"}
        doc_type = {"chapter": "bookchapter", "article-journal": "journal"}.get(
            selection["document_type"], selection["document_type"])
    ranked = ([{**b, "score": 0} for b in blocks] if selection else
              gather_evidence_candidates(blocks, " ".join(region_hints or [])))
    for block in ranked:
        physical = block.get("physical_page_index")
        for region in ([] if selection else candidate_regions or []):
            start, end = region.get("start_page"), region.get("end_page")
            if isinstance(physical, int) and isinstance(start, int) and isinstance(end, int) and start <= physical + 1 <= end:
                block["score"] += 12 if region["role"] in {"imprint", "front_matter"} else -20 if region["role"] == "bibliography" else 0
    ranked.sort(key=lambda block: -block["score"])
    accepted, evidence, used = {}, {}, set()
    if selection:
        accepted["type"] = selection["document_type"]
        evidence["type"] = validate_block_evidence(
            "type", accepted["type"], selection["type_evidence"], blocks)
    # Phase 2 (plan v2 §3.5-3.7): deterministic CIP/LoC candidates seeded
    # with evidence-gated confidence; recurring watermark lines dropped from
    # non-ranked pages before the LLM sees them.
    scan_annotations, scan_ranked = _deterministic_scan(pre_blocks)
    blocks, ranked = _filter_repeated_lines(blocks, ranked, scan_ranked)
    seeded = _seed_deterministic_candidates(
        pre_blocks, scan_annotations, scan_ranked, selection, accepted, evidence,
        content_list=content_list)
    profile = evaluation_fields({"type": doc_type_to_csl_type(doc_type)}, "digital_pdf")
    profile |= {"publisher-place", "subtitle", "collection-title", "collection-number"}
    # A scanned title seeded from a lone heading with no printed subtitle
    # partner (使徒教父著作, PSALMS AND THE LIFE OF FAITH) must not gain an
    # LLM-invented subtitle: the composed title would then diverge.
    if any(csl_field == "title" and cand.get("_no_subtitle") for csl_field, cand in seeded):
        profile.discard("subtitle")
    carry = []
    last_values = None
    try:
        if lm is None:
            lm = get_llm_model(cfg.llm_model, temperature=0.1)
        predictor = dspy.Predict(ExtractDocumentMetadata)
        for attempt in range(2):
            selected, remaining = list(carry), 12000 - sum(len(block["text"]) for block in carry)
            page_allowance = {}
            page_count = len({b.get("physical_page_index") for b in ranked})
            for block in ranked:
                if remaining <= 0:
                    break
                if block["id"] in used or block["score"] < 0:
                    continue
                available = remaining
                if selection:
                    page = block["physical_page_index"]
                    available = min(remaining, page_allowance.setdefault(page, 12000 // max(page_count, 1)))
                text = block["text"][:available]
                if not text:
                    continue
                selected.append({**block, "text": text})
                used.add(block["id"])
                remaining -= len(text)
                if selection:
                    page_allowance[page] -= len(text)
            if not selected:
                break
            if attempt == 0:
                carry = selected[:4]
            with dspy.context(lm=lm):
                result = predictor(source_blocks=selected, doc_type=doc_type,
                                   bibliographic_pages=selection["selected_pages"] if selection else [],
                                   missing_fields=[f for f in sorted(profile) if f not in accepted])
            values, citations = result.csl, result.field_evidence
            if not isinstance(values, dict) or not isinstance(citations, dict):
                continue
            for field in HOST_FIELDS:
                if field in accepted or field not in values:
                    continue
                if field == "type" and selection and values[field] != selection["document_type"]:
                    continue
                supported = validate_block_evidence(field, values[field], citations.get(field), selected)
                if supported:
                    accepted[field], evidence[field] = values[field], supported
                else:
                    logger.warning("Rejected host field %s (%s doc_type=%s): %s",
                                   field, len(selected), doc_type,
                                   _rejection_reason(field, values[field], citations.get(field)))
            essential = {"title", "issued", "type"}
            if doc_type in {"book", "bookchapter"}:
                essential.add("publisher")
            if essential <= accepted.keys() and ("author" in accepted or "editor" in accepted):
                break
            last_values = values
    except Exception:
        logger.warning("DSPy host metadata extraction failed", exc_info=True)
    # Phase 2 §3.7 audit: deterministic seeds win over LLM proposals by
    # confidence; disagreements are recorded, never silently dropped.
    if seeded:
        conflicts = []
        for csl_field, cand in seeded:
            llm_value = (last_values or {}).get(csl_field)
            if llm_value and not _equivalent_candidate_values(csl_field, cand["value"], llm_value):
                conflicts.append({
                    "field": csl_field,
                    "deterministic_value": cand["value"],
                    "llm_value": llm_value,
                    "confidence": cand["confidence"],
                })
        if conflicts:
            # Audit-only key (underscore namespace, never a CSL field).
            accepted["_metadata_conflicts"] = conflicts
    if accepted:
        accepted["_field_evidence"] = evidence
    # OCR numeral repair is deterministic: the LLM transcribes printed
    # ordinals faithfully but keeps the OCR junk letter ('g9th' for '9th').
    if "title" in accepted or "subtitle" in accepted:
        from .frontmatter import repair_ocr_numerals
        for field in ("title", "subtitle", "container-title"):
            if isinstance(accepted.get(field), str):
                accepted[field] = repair_ocr_numerals(accepted[field])
    if selection is not None:
        accepted["_bibliographic_selection"] = selection
        if not accepted.get("title"):
            accepted["_citation_status"] = "incomplete"
    return accepted


# ---------------------------------------------------------------------------
# Backwards-compatible alias used by digital_pdf.py
# ---------------------------------------------------------------------------

def extract_metadata_from_mineru(
    mineru_markdown: str,
    doc_type: str,
    config: Optional[IngestionConfig] = None,
) -> Dict[str, Any]:
    """Legacy entry point — delegates to DSPy extraction directly."""
    return _run_dspy_extraction(mineru_markdown, doc_type, config)


def extract_metadata_with_dspy_priority(
    content_list: List[Dict],
    normalized_markdown: str,
    doc_type: str,
    config: Optional[IngestionConfig] = None,
    discarded_blocks: Optional[List[Dict]] = None,
    source_blocks: Optional[List[Dict[str, Any]]] = None,
    candidate_regions: Optional[List[Dict[str, Any]]] = None,
    region_hints: Optional[List[str]] = None,
    total_pages: Optional[int] = None,
) -> Dict[str, Any]:
    """Pattern extraction followed by DSPy, allowing DSPy to overwrite fields.

    This is intended for scanned-document backends where structured OCR output
    is authoritative and DSPy should be allowed to refine deterministic parsing.
    """
    # Restrict heuristic fallbacks to front matter; body DOIs belong to cited works.
    front = [item for item in content_list if 0 <= item.get("page_idx", 999) < 4]
    pattern_csl = extract_metadata_from_content_list(front, discarded_blocks)
    blocks = source_blocks or [
        {"id": f"ocr_{i}", "text": _get_text(item), "physical_page_index": item.get("page_idx"),
         "role": item.get("type", "text")}
        for i, item in enumerate(content_list) if _get_text(item)
    ]
    dspy_csl = _run_dspy_extraction(normalized_markdown, doc_type, config,
                                    source_blocks=blocks, candidate_regions=candidate_regions,
                                    region_hints=region_hints, total_pages=total_pages,
                                    content_list=content_list)
    if total_pages is not None:
        # Page-bounded extraction must not reintroduce unselected heuristic
        # fields. C1 (scanned two-type plan 2026-09-26): the pattern layer
        # fills ONLY fields the DSPy run left empty — it never overwrites
        # LLM values or deterministic seeds.
        merged = dict(dspy_csl)
        for key, value in pattern_csl.items():
            if key.startswith("_") or value is None:
                continue
            if isinstance(value, str) and not value.strip():
                continue
            if isinstance(value, list) and not value:
                continue
            # Never pin OCR garble as a title: the pattern layer reads the
            # same garbled heading the LLM rejects (astudy), and a later
            # clean reading would then be blocked by the merge.
            if key in {"title", "subtitle"} and isinstance(value, str) \
                    and _HEADING_GARBLE_RE.search(value):
                logger.warning("Dropping garbled pattern %s=%r", key, value[:50])
                continue
            if merged.get(key) in (None, "", []):
                merged[key] = value
        merged["_extraction_method"] = "bibliographic_pages+dspy+pattern_fill"
        return merged
    if not dspy_csl:
        pattern_csl.setdefault("_extraction_method", pattern_csl.get("_extraction_method", "pattern"))
        return pattern_csl

    merged = dict(pattern_csl)
    for key, value in dspy_csl.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, list) and not value:
            continue
        merged[key] = value

    merged["_extraction_method"] = "pattern+dspy_priority"
    return merged


# ---------------------------------------------------------------------------
# Public API: reconciliation
# ---------------------------------------------------------------------------

def reconcile_grobid_and_dspy(
    grobid_csl: Dict[str, Any],
    pattern_dspy_csl: Dict[str, Any],
    doc_type: str,
    config: Optional[IngestionConfig] = None,
) -> Dict[str, Any]:
    """Merge GROBID metadata with pattern+DSPy metadata.

    Strategy:
    - For CJK content: prefer pattern/DSPy results (GROBID is weak on Chinese).
    - For Western content: prefer GROBID.
    """
    # Fast path: if one source is empty, return the other
    if not grobid_csl:
        if pattern_dspy_csl:
            pattern_dspy_csl.setdefault("_extraction_method", "pattern+dspy")
        return pattern_dspy_csl or {}
    if not pattern_dspy_csl:
        grobid_csl["_extraction_method"] = "grobid"
        return grobid_csl

    # Detect whether content is CJK-dominant
    cjk_content = False
    for field in ("title", "abstract"):
        val = pattern_dspy_csl.get(field) or grobid_csl.get(field) or ""
        if isinstance(val, str) and _is_cjk_dominant(val):
            cjk_content = True
            break

    # Choose primary / secondary source based on script
    if cjk_content:
        primary, secondary = pattern_dspy_csl, grobid_csl
        primary_label, secondary_label = "pattern_dspy", "grobid"
    else:
        primary, secondary = grobid_csl, pattern_dspy_csl
        primary_label, secondary_label = "grobid", "pattern_dspy"

    merged: Dict[str, Any] = {}
    provenance: Dict[str, str] = {}

    simple_fields = [
        "title", "publisher", "volume", "issue", "page", "DOI",
        "abstract", "container-title", "ISSN", "keyword",
    ]

    for field in simple_fields:
        p_val = primary.get(field)
        s_val = secondary.get(field)

        if p_val:
            merged[field] = p_val
            provenance[field] = primary_label
        elif s_val:
            merged[field] = s_val
            provenance[field] = secondary_label

    # Author — prefer primary; fall back to secondary; prefer longer list
    p_authors = primary.get("author", [])
    s_authors = secondary.get("author", [])
    if p_authors:
        merged["author"] = p_authors
        provenance["author"] = primary_label
    elif s_authors:
        merged["author"] = s_authors
        provenance["author"] = secondary_label
    # If both have authors, prefer the one with more entries
    if p_authors and s_authors and len(s_authors) > len(p_authors):
        merged["author"] = s_authors
        provenance["author"] = secondary_label

    # Issued date
    p_issued = primary.get("issued")
    s_issued = secondary.get("issued")
    if p_issued:
        merged["issued"] = p_issued
        provenance["issued"] = primary_label
    elif s_issued:
        merged["issued"] = s_issued
        provenance["issued"] = secondary_label

    merged["_extraction_method"] = "grobid+pattern_dspy"
    merged["_provenance"] = provenance
    return merged


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _set_if_present(d: Dict[str, Any], key: str, value: Any) -> None:
    if value and isinstance(value, str) and value.strip():
        d[key] = value.strip()
