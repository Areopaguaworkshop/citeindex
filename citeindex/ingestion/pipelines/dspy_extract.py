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
    candidates = []
    for block in source_blocks:
        text = block.get("text", "")
        lowered = text.casefold()
        score = sum(lowered.count(term) for term in terms)
        if isinstance(block.get("physical_page_index"), int) and block["physical_page_index"] < 4:
            score += 8
        if any(marker in lowered for marker in ("bibliography", "references", "参考文献", "书目")):
            score -= 20
        if any(marker in lowered for marker in ("title", "copyright", "isbn", "doi", "版权", "出版")):
            score += 1
        candidates.append({**block, "score": score})
    return sorted(candidates, key=lambda item: (-item["score"], item.get("page", 0), item["id"]))


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

class ExtractDocumentMetadata(dspy.Signature):
    """Extract ONLY the ingested source's own citation metadata, never its works cited.
    Use supplied original blocks, not summaries. Distinguish author/editor/translator.
    Preserve name order and CJK literal names. Do not infer publisher place or dates.
    Omit unknown fields. For each value cite a block_id and exact supporting quote.
    A quoted reference to another work is NOT evidence of the host's identity.
    """
    source_blocks: List[Dict[str, Any]] = dspy.InputField()
    doc_type: str = dspy.InputField(desc="Heuristic hint, not authoritative classification.")
    missing_fields: List[str] = dspy.InputField()
    csl: Dict[str, Any] = dspy.OutputField(desc="CSL metadata: typed name arrays, issued date-parts, other fields strings.")
    field_evidence: Dict[str, Dict[str, str]] = dspy.OutputField(desc="CSL field -> {block_id, quote}. No invented IDs or quotations.")


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


def _run_dspy_extraction(
    source_text: str,
    doc_type: str,
    config: Optional[IngestionConfig] = None,
    region_hints: Optional[List[str]] = None,
    source_blocks: Optional[List[Dict[str, Any]]] = None,
    candidate_regions: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Gather/rank original blocks; extract and validate; at most one refinement."""
    from ..citation_verification import HOST_FIELDS, validate_block_evidence
    cfg = config or IngestionConfig()
    blocks = source_blocks if source_blocks is not None else [
        {"id": f"text_{i}", "text": text, "char_start": start}
        for i, (start, text) in enumerate(
            ((m.start(), m.group()) for m in re.finditer(r"[^\n]+", source_text))
        ) if text.strip()
    ]
    if not blocks:
        return {}
    ranked = gather_evidence_candidates(blocks, " ".join(region_hints or []))
    for block in ranked:
        physical = block.get("physical_page_index")
        for region in candidate_regions or []:
            start, end = region.get("start_page"), region.get("end_page")
            if isinstance(physical, int) and isinstance(start, int) and isinstance(end, int) and start <= physical + 1 <= end:
                block["score"] += 12 if region["role"] in {"imprint", "front_matter"} else -20 if region["role"] == "bibliography" else 0
    ranked.sort(key=lambda block: -block["score"])
    accepted, evidence, used = {}, {}, set()
    try:
        lm = get_llm_model(cfg.llm_model, temperature=0.1)
        predictor = dspy.Predict(ExtractDocumentMetadata)
        for attempt in range(2):
            selected, remaining = [], 12000
            for block in ranked:
                if block["id"] in used or block["score"] < 0:
                    continue
                text = block["text"][:remaining]
                if not text:
                    break
                selected.append({**block, "text": text})
                used.add(block["id"])
                remaining -= len(text)
            if not selected:
                break
            with dspy.context(lm=lm):
                result = predictor(source_blocks=selected, doc_type=doc_type,
                                   missing_fields=[f for f in ("title", "author", "issued") if f not in accepted])
            values, citations = result.csl, result.field_evidence
            if not isinstance(values, dict) or not isinstance(citations, dict):
                continue
            for field in HOST_FIELDS:
                if field in accepted or field not in values:
                    continue
                supported = validate_block_evidence(field, values[field], citations.get(field), selected)
                if supported:
                    accepted[field], evidence[field] = values[field], supported
            if all(field in accepted for field in ("title", "author", "issued")):
                break
    except Exception:
        logger.warning("DSPy host metadata extraction failed", exc_info=True)
    if accepted:
        accepted["_field_evidence"] = evidence
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
                                    region_hints=region_hints)
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
