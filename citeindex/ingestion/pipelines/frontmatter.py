"""Deterministic front-matter locator and CIP/LoC parsers (Phase 1, plan v2 §3).

Pure functions: no LLM, no network, stdlib ``re`` only (``fitz`` imported
inside :func:`load_pdf_pages`). Replaces LLM-blind page locating for a
book's own imprint/CIP metadata with a weighted marker scan over ALL pages
plus GB/T 12451—2023 / LoC RDA block parsing that emits confidence-scored
CSL field candidates with verbatim quotes.

Ported regex ideas (not copied wholesale):
- ``charlesilcn/PDF2BOOK`` cip_extractor.py — OCR-tolerant classes
  (``[．.·一。]``, ``[（(]``, ``[：:]``, ``[，,]``), ``一`` artifact strip.
- ``sabercomo/MEFinder`` bibliographic_metadata.py — confidence-scored
  candidates, LoC RDA labels, publisher-suffix regex.
"""

from __future__ import annotations

import logging
import re

__all__ = [
    "scan_pages",
    "rank_candidate_pages",
    "repeated_lines",
    "parse_cip",
    "parse_loc_cip",
    "validate_isbn13",
    "load_pdf_pages",
    "repair_ocr_numerals",
]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 1. Marker lexicon (weighted). ZH anchors strongest per plan v2 §3.1.
# ---------------------------------------------------------------------------

MARKERS: tuple[tuple[re.Pattern[str], float], ...] = tuple(
    (re.compile(pattern, re.IGNORECASE), weight)
    for pattern, weight in [
        (r"图书在版编目", 10),
        (r"CIP数据核字", 10),
        (r"版权所有|侵权必究", 4),
        (r"出版发行", 4),
        (r"责任编辑", 3),
        (r"定价", 2),
        (r"开本", 2),
        (r"版次", 2),
        (r"©", 6),
        (r"Copyright", 6),
        (r"All rights reserved", 5),
        (r"First published|First edition", 5),
        (r"Printed in", 4),
        (r"Library of Congress", 4),
        (r"Cataloguing-in-Publication|Cataloging-in-Publication", 5),
        (r"ISBN", 5),
        (r"Reprinted", 3),
    ]
)

# Hyphen/space-tolerant ISBN-13 shape; validated separately by checksum.
ISBN_RE = re.compile(r"97[89](?:-?\s?\d){10}")

_CIP_HEADER_RE = re.compile(r"图书在版编目\s*[（(]\s*CIP\s*[)）]\s*数据", re.IGNORECASE)
_CIP_CERT_RE = re.compile(r"CIP数据核字\s*[（(]\s*\d{4}\s*[)）]\s*第\s*\d+\s*号")
# OCR-garbled anchors (augustin 2005: 'ffl书在版编目（C I P ) 数据', cert
# '第0739】】号'). Matched against whitespace-collapsed text only.
_CIP_HEADER_FUZZY_RE = re.compile(r".{0,3}书在版编目[（(]CIP[)）]数据")
_CIP_CERT_FUZZY_RE = re.compile(r"CIP数据核字[（(]\d{4}[)）]第?\d+[】\]]*号")
_ISBN_LINE_RE = re.compile(r"ISBN\s*[:：]?\s*([\d\-–\s]+)")
_SERIES_RE = re.compile(r"[（(]\s*(?P<series>[^（）()]{2,30}?)\s*[/／;；,，]\s*[^（）()]{0,12}?\s*[)）]")

# GB/T 12451 main line:
#   正书名 : 其他书名信息 / 责任说明 .-- 出版地 : 出版者 , 出版日期
# OCR-tolerant separator classes ported from PDF2BOOK (period may be
# misrecognized as ．·一。; dash may be —–― or 一; CJK slash ／).
_CIP_MAIN_RE = re.compile(
    r"(?P<title>[^\s/／:：].{1,120}?)"
    r"(?:\s*[：:]\s*(?P<subtitle>[^/／]{1,120}?))?"
    r"\s*[/／]\s*"
    r"(?P<resp>[^．.·一。]{2,120}?)"
    r"\s*[．.·一。]\s*[—–―-]{0,2}\s*"
    r"(?P<place>[^：:．.·一。—–―-]{1,20}?)"
    r"\s*[：:]\s*"
    r"(?P<publisher>[^，,]{2,40}?)"
    r"\s*[，,]\s*"
    r"(?P<year>(?:19|20)\d{2})"
)
# Variant without the place-publisher colon (real records: 北京.九州出版社,2018.10).
_CIP_MAIN_NOCOLON_RE = re.compile(
    r"(?P<title>[^\s/／:：].{1,120}?)"
    r"(?:\s*[：:]\s*(?P<subtitle>[^/／]{1,120}?))?"
    r"\s*[/／]\s*"
    r"(?P<resp>[^．.·一。]{2,120}?)"
    r"\s*[．.·一。]\s*[—–―-]{0,2}\s*"
    r"(?P<place>[^．.·一。，,]{1,12}?)"
    r"[．.·一。]\s*"
    r"(?P<publisher>[^，,]{2,40}?)"
    r"\s*[，,]\s*"
    r"(?P<year>(?:19|20)\d{2})"
)

_CHINESE_PUBLISHER_SUFFIX = (
    r"(?:出版(?:集团|集團)(?:股份有限公司|有限公司)?|出版中心|出版社|印书馆|印書館|书局|書局)"
)
_PUBLISHER_SUFFIX_RE = re.compile(_CHINESE_PUBLISHER_SUFFIX)
# Longest-first alternation so 主编 is consumed whole, never 编 alone. The
# 等 et-al marker stays with the name (高翔等 is a CSL literal), never stripped.
_ROLE_SUFFIX_RE = re.compile(
    r"\s*(?:校订|校訂|校译|校譯|译注|譯注|编译|編譯|主编|主編|编著|編著|编写|編寫|"
    r"改编|改編|整理|摄影|绘|繪|著|编|編|译|譯|校|撰)\s*$"
)
# Leading nationality bracket （美）诺曼著 → 诺曼: not part of the CSL name.
_COUNTRY_PREFIX_RE = re.compile(r"^[\[［【(（〔][^\]］】)）〕]{1,8}[\]］】)）〕]\s*")


def _clean_person(name: str) -> str:
    name = _COUNTRY_PREFIX_RE.sub("", name.strip())
    name = name.strip(" •·・,，、;；")  # OCR separator noise (augustin: '译•')
    prev = None
    while prev != name:
        prev = name
        name = _ROLE_SUFFIX_RE.sub("", name)
    return name.strip()


# GB/T responsibility markers: 著/编 → author, 译 → translator.
_TRANSLATOR_ROLES = {"译", "譯", "译注", "譯注", "校译", "校譯", "翻译", "翻譯"}


def _person_with_role(person: str) -> tuple[str, str]:
    """Return (cleaned name, trailing responsibility marker)."""
    person = person.strip(" •·・,，、;；")
    match = _ROLE_SUFFIX_RE.search(person)
    role = match.group(0).strip() if match else ""
    return _clean_person(person), role


# OCR attaches a stray lowercase letter to a printed ordinal ('g9th'); the
# pattern requires one letter directly before a numeric ordinal, so 'x86'
# and 'COVID-19' are untouched.
_OCR_ORDINAL_RE = re.compile(r"(?<![A-Za-z])[a-z](?=\d+(?:st|nd|rd|th)\b)")


def repair_ocr_numerals(text: str) -> str:
    """Drop OCR junk letters glued to ordinals: '(7th—g9th Century)' → 9th."""
    return _OCR_ORDINAL_RE.sub("", text) if text else text

_LOC_ANCHOR_RE = re.compile(
    r"Library\s+of\s+Congress\s+Catalog(?:uing|ing)[- ]in[- ]Publication(?:\s+Data)?"
    r"|Catalog(?:uing|ing)[- ]in[- ]Publication\s+Data",
    re.IGNORECASE,
)

# Repeated-block normalization: collapse whitespace, strip punctuation.
_REPEAT_STRIP_RE = re.compile(r"[\W_]+", re.UNICODE)


def _normalize_block_key(line: str) -> str:
    """Position-free normalized key for the repeated-block watermark heuristic."""
    return _REPEAT_STRIP_RE.sub("", line).casefold() if len(line.strip()) < 60 else ""


# ---------------------------------------------------------------------------
# 2. Page scanning
# ---------------------------------------------------------------------------


def scan_pages(pages: list[dict]) -> list[dict]:
    """Annotate every page with imprint-marker evidence, sorted best-first.

    ``pages`` is the ALL-pages list ``{"page": int (1-based), "text": str}``
    in document order. Returns one annotation per input page, sorted by
    descending score: ``{"page", "score", "markers", "isbn_candidates",
    "repeated_block"}``. Repeated watermark lines never contribute to
    ``score``.
    """
    if not pages:
        return []

    # Pass 1: per-page marker hits on lines not part of a repeated block.
    line_keys: dict[str, set[int]] = {}
    page_lines: list[list[str]] = []
    for page in pages:
        text = str(page.get("text") or "")
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        page_lines.append(lines)
        for ln in lines:
            key = _normalize_block_key(ln)
            if key:
                line_keys.setdefault(key, set()).add(int(page.get("page", 0)))

    repeated_keys = {key for key, pagenos in line_keys.items() if len(pagenos) >= 3}
    repeated_pages: set[int] = set()
    for key in repeated_keys:
        repeated_pages |= line_keys[key]

    results: list[dict] = []
    for page, lines in zip(pages, page_lines):
        pageno = int(page.get("page", 0))
        score = 0.0
        markers: list[str] = []
        isbns: list[str] = []
        for ln in lines:
            key = _normalize_block_key(ln)
            if key and key in repeated_keys:
                continue  # watermark line: no marker credit
            for pattern, weight in MARKERS:
                m = pattern.search(ln)
                if m:
                    score += weight
                    markers.append(m.group(0))
            isbns.extend(ISBN_RE.findall(ln))

        # ISBN candidates: a single list per page, dedup, order preserved.
        seen: set[str] = set()
        isbn_candidates = [x for x in isbns if not (x in seen or seen.add(x))]
        results.append(
            {
                "page": pageno,
                "score": score,
                "markers": markers,
                "isbn_candidates": isbn_candidates,
                "repeated_block": pageno in repeated_pages,
            }
        )

    results.sort(key=lambda r: (-r["score"], r["page"]))
    return results


def rank_candidate_pages(pages: list[dict], top_k: int = 8) -> list[int]:
    """Top-scoring pages plus the best page's two neighbors, capped at ``top_k``."""
    ranked = scan_pages(pages)
    if not ranked:
        return []
    best = ranked[0]["page"]
    total = len(pages)
    selected = {best, best - 1, best + 1}
    for entry in ranked:
        if len(selected) >= top_k:
            break
        selected.add(entry["page"])
    valid = {p for p in selected if 1 <= p <= total}
    return sorted(valid)


def repeated_lines(pages: list[dict]) -> set[str]:
    """Normalized text appearing on ≥3 distinct pages, <60 chars (watermark set).

    Same normalization as :func:`scan_pages` — one implementation, shared.
    """
    line_keys: dict[str, set[int]] = {}
    for page in pages:
        text = str(page.get("text") or "")
        for ln in text.splitlines():
            key = _normalize_block_key(ln)
            if key:
                line_keys.setdefault(key, set()).add(int(page.get("page", 0)))
    return {key for key, pagenos in line_keys.items() if len(pagenos) >= 3}


# ---------------------------------------------------------------------------
# 3. Chinese CIP parsing (GB/T 12451—2023)
# ---------------------------------------------------------------------------


def _clean_cjk(s: str) -> str:
    """Strip whitespace inserted between CJK chars by text extraction."""
    return re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[\u3400-\u9fff])", "", s.strip())


def _candidate(field: str, value, confidence: float, quote: str) -> dict | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if field == "year":
        try:
            value = int(text)
        except ValueError:
            return None
    else:
        value = _clean_cjk(text)
        if not value:
            return None
    return {"field": field, "value": value, "confidence": confidence, "quote": quote}


def parse_cip(text: str) -> list[dict]:
    """Parse a Chinese CIP block (GB/T 12451—2023 grammar, OCR-tolerant).

    Returns candidate CSL fields with confidence 0.99 when the header anchor
    ``图书在版编目`` is present, else 0.9; ``[]`` when nothing CIP-shaped is
    found. Works on whatever text the caller passes, including a CIP block
    spanning two pages (caller concatenates).
    """
    if not text:
        return []
    collapsed = re.sub(r"\s+", "", text)
    strict = bool(_CIP_HEADER_RE.search(text) or _CIP_CERT_RE.search(text))
    fuzzy = not strict and bool(
        _CIP_HEADER_FUZZY_RE.search(collapsed) or _CIP_CERT_FUZZY_RE.search(collapsed))
    # ponytail: without the header anchor we accept any main-line-shaped
    # statement; upgrade path is requiring the anchor once false-positive
    # rates are measured on the real corpus.
    confidence = 0.99 if strict or fuzzy else 0.9

    match = None
    if strict:
        match = _CIP_MAIN_RE.search(text) or _CIP_MAIN_NOCOLON_RE.search(text)
    if match is None and (strict or fuzzy):
        # OCR recovery (augustin 论三位一体, 2005): on the garbled raw text
        # the resp-class cannot cross the '.' inside a Latin parenthetical,
        # so the no-colon variant mis-splits place into publisher. Never
        # search the raw text for fuzzy blocks — re-parse the collapsed
        # body after the header with the Latin parenthetical stripped and
        # the '；'→'I' OCR artifact fixed. The seeding host lookup falls
        # back to value-string containment, so a collapsed quote still
        # resolves to its block.
        header = _CIP_HEADER_RE.search(collapsed) or _CIP_HEADER_FUZZY_RE.search(collapsed)
        body = collapsed[header.end():] if header else collapsed
        body = re.sub(r"[(（][^()（）]*?[A-Za-z][^()（）]*?[)）]", "", body)
        body = re.sub(r"(?<=[著编译校撰订辑理])I", "；", body)
        match = _CIP_MAIN_RE.search(body) or _CIP_MAIN_NOCOLON_RE.search(body)
    elif match is None:
        match = _CIP_MAIN_RE.search(text) or _CIP_MAIN_NOCOLON_RE.search(text)
    if not match:
        return []

    quote = match.group(0).strip()
    fields: list[dict] = []
    title = match.group("title")
    # Series 丛书名 lives inside （） outside the main statement (before or after).
    series_match = next(
        (
            m
            for m in _SERIES_RE.finditer(text)
            if not (m.start() < match.end() and m.end() > match.start())
        ),
        None,
    )

    if series_match:
        cand = _candidate("series", series_match.group("series"), confidence, series_match.group(0))
        if cand:
            fields.append(cand)

    cand = _candidate("title", title, confidence, quote)
    if cand:
        fields.append(cand)
    if match.group("subtitle"):
        cand = _candidate("subtitle", match.group("subtitle"), confidence, quote)
        if cand:
            fields.append(cand)

    resp = match.group("resp") or ""
    for person in re.split(r"[;；]", resp):
        name, role = _person_with_role(person)
        if not name:
            continue
        cand = _candidate("translator" if role in _TRANSLATOR_ROLES else "authors",
                          name, confidence, quote)
        if cand:
            fields.append(cand)

    place = match.group("place") or ""
    if place.startswith("一"):
        place = place[1:]
    cand = _candidate("place", place, confidence, quote)
    if cand:
        fields.append(cand)

    publisher = re.sub(r"\s+", "", match.group("publisher") or "")
    if _PUBLISHER_SUFFIX_RE.search(publisher) or re.search(r"[A-Za-z]", publisher):
        cand = _candidate("publisher", publisher, confidence, quote)
        if cand:
            fields.append(cand)

    cand = _candidate("year", match.group("year"), confidence, quote)
    if cand:
        fields.append(cand)

    for m in _ISBN_LINE_RE.finditer(text):
        raw = re.sub(r"[\s–]", "", m.group(1))
        if validate_isbn13(raw):
            cand = _candidate("ISBN", raw, confidence, m.group(0).strip())
            if cand:
                fields.append(cand)
            break

    if fuzzy:
        # OCR-noisy anchor: the title/series garble (论二位一体) must not
        # seed, but the responsibility statement's names are structured and
        # stay seedable.
        fields = [c for c in fields
                  if c["field"] in ("place", "publisher", "year", "ISBN", "authors", "translator")]
    return fields


# ---------------------------------------------------------------------------
# 4. Library of Congress RDA block parsing
# ---------------------------------------------------------------------------


def parse_loc_cip(text: str) -> list[dict]:
    """Parse an English LoC Cataloging-in-Publication RDA block.

    Labeled fields (``Names:``, ``Title:``, ``Description:``,
    ``Identifiers:``, …) separated by ``|``. Confidence 0.98. Returns
    ``[]`` without the LoC CIP anchor (both modern and ``… Data`` spellings).
    """
    if not text or not _LOC_ANCHOR_RE.search(text):
        return []
    confidence = 0.98

    def labeled(label: str) -> list[str]:
        # RDA segments repeat after `|` without re-stating the label:
        # `Description: A | B` = two Description values.
        m = re.search(rf"(?:^|\n)\s*{label}\s*:\s*(?P<val>[^\n]+)", text)
        if not m:
            return []
        return [seg.strip() for seg in m.group("val").split("|") if seg.strip()]

    fields: list[dict] = []

    # Title: X / Y — author goes to Names:, remainder may carry a subtitle.
    title_parts = labeled("Title")
    title = ""
    if title_parts:
        m = re.match(r"(?P<title>.+?)\s+/\s+", title_parts[0])
        raw_title = m.group("title") if m else title_parts[0]
        title = re.sub(r"\s+([:;,.])", r"\1", raw_title).strip(" .")
        # Subtitle after ':' stays inside title per RDA transcription; keep
        # first colon segment as title, rest as subtitle.
        # ponytail: single-colon split; multi-colon RDA subtitles are rare,
        # upgrade to MEFinder-style full transcription if corpus shows them.
        if "：" in title:
            title, _, subtitle = title.partition("：")
        elif ":" in title:
            title, _, subtitle = title.partition(":")
        else:
            subtitle = ""
        if title:
            fields.append(
                {"field": "title", "value": title, "confidence": confidence, "quote": f"Title: {title_parts[0]}"}
            )
        if subtitle.strip():
            fields.append(
                {
                    "field": "subtitle",
                    "value": subtitle.strip(),
                    "confidence": confidence,
                    "quote": f"Title: {title_parts[0]}",
                }
            )

    # Names: first entry is the book's own author.
    for name in labeled("Names"):
        # 'Family, Given' with OCR-drifted spacing (benedict
        # 'Vogüé,Adalbert de'). The particle stays inside the family
        # position ('de Vogüé' style records keep it in family); the
        # given portion follows the comma.
        m = re.match(r"([A-ZÀ-ÖØ-Þ][\w'’.-]*),\s*([A-ZÀ-ÖØ-Þ][\w'’.\- ]*?)\s*$",
                     name.strip(" ."))
        if m:
            family, given = m.group(1), m.group(2).strip()
            fields.append(
                {
                    "field": "author",
                    "value": {"family": family, "given": given},
                    "confidence": confidence,
                    "quote": f"Names: {name}",
                }
            )
        break  # ponytail: first name only; add relator parsing if corpus needs it

    # Description: any segment may carry place : publisher, year.
    for desc in labeled("Description"):
        m = re.search(
            r"(?P<place>[A-Z][A-Za-z .\-]{1,40}(?:\s*[;,]\s*[A-Z][A-Za-z .\-]{1,40}){0,2})\s*:\s*"
            r"(?P<publisher>[A-Z][\w&'’.,\- ]{1,80}?)"
            r"\s*,\s*(?:\[)?(?P<year>(?:19|20)\d{2})\]?",
            desc,
        )
        if m:
            place = re.split(r"\s*;\s*", m.group("place"))[0].strip()
            fields.append(
                {"field": "place", "value": place, "confidence": confidence, "quote": desc}
            )
            publisher = m.group("publisher").strip(" ,.")
            fields.append(
                {"field": "publisher", "value": publisher, "confidence": confidence, "quote": desc}
            )
            fields.append(
                {"field": "year", "value": int(m.group("year")), "confidence": confidence, "quote": desc}
            )
            break

    # Identifiers: LCCN + ISBN(s).
    for ident in labeled("Identifiers"):
        isbn_m = re.search(r"ISBN\s*[:：]?\s*([\d\-]+)", ident, re.IGNORECASE)
        if isbn_m:
            raw = re.sub(r"-", "", isbn_m.group(1))
            if validate_isbn13(raw):
                fields.append(
                    {
                        "field": "ISBN",
                        "value": raw,
                        "confidence": confidence,
                        "quote": isbn_m.group(0).strip(),
                    }
                )
        lccn_m = re.search(r"LCCN\s+(\d+)", ident)
        if lccn_m:
            fields.append(
                {
                    "field": "LCCN",
                    "value": lccn_m.group(1),
                    "confidence": confidence,
                    "quote": lccn_m.group(0).strip(),
                }
            )
        if any(f["field"] == "ISBN" for f in fields):
            break  # first valid ISBN wins; later segments are other formats

    # Subjects / Classification: kept verbatim, one candidate per segment.
    for subject in labeled("Subjects"):
        fields.append(
            {"field": "subjects", "value": subject, "confidence": confidence, "quote": subject}
        )
        break
    for classification in labeled("Classification"):
        fields.append(
            {"field": "classification", "value": classification, "confidence": confidence, "quote": classification}
        )
        break

    return fields


# ---------------------------------------------------------------------------
# 5. ISBN checksum (GB/T 5795-2006 / mod-10 weighted)
# ---------------------------------------------------------------------------


def validate_isbn13(s: str) -> bool:
    """ISBN-13: 13 digits, 978/979 prefix, mod-10 checksum with weights 1,3."""
    digits = re.sub(r"[\s\-–]", "", str(s or ""))
    if len(digits) != 13 or not digits.isdigit():
        return False
    if not digits.startswith(("978", "979")):
        return False
    total = sum(int(d) * (1 if i % 2 == 0 else 3) for i, d in enumerate(digits[:12]))
    return (10 - total % 10) % 10 == int(digits[12])


# ---------------------------------------------------------------------------
# 6. PDF loading (fitz imported here only)
# ---------------------------------------------------------------------------


def load_pdf_pages(pdf_path) -> list[dict]:
    """Extract one ``{"page": i+1, "text": str}`` per page via PyMuPDF."""
    import fitz  # local import: module stays importable without PyMuPDF

    doc = fitz.open(str(pdf_path))
    try:
        return [{"page": i + 1, "text": page.get_text()} for i, page in enumerate(doc)]
    finally:
        doc.close()