"""URL article ingestion pipeline.

Workflow:
  1. Fetch HTML (Playwright → requests)
  2. Extract content as markdown with headings (trafilatura → readability)
  3. Extract metadata (Zotero → trafilatura)
  4. Pattern-scan for in-page citation guidance (若要引用/Cite this/etc.)
  5. Parse citation string (regex first, DSPy fallback)
  6. Reconcile: citation guidance wins over Zotero/trafilatura
  7. Build section-hierarchical paragraphs from markdown headings
  8. Merkle tree (no retrieval index)
"""

import hashlib
import logging
import os
import re
import tempfile
from datetime import UTC, datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin

import requests

try:
    import trafilatura
except ImportError:
    trafilatura = None

from ..csl import evaluation_fields, valid_host_value
from ..models import IngestionConfig, PipelineResult
from ..url_security import MAX_REDIRECTS, MAX_RESPONSE_BYTES, UnsafeUrlError, fetch_text, validate_public_url
from .common import (
    attach_evidence_locators,
    build_merkle_for_nodes,
    build_nodes,
    make_basic_csl,
    make_source_id,
    split_paragraphs,
)
from .multimodal_metadata import extract_multimodal_metadata, metadata_blocks, web_source_blocks

logger = logging.getLogger(__name__)


def _write_html_snapshot(html: str) -> str:
    fd, path = tempfile.mkstemp(prefix="citeindex_url_", suffix=".html")
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(html)
    return path


# ---------------------------------------------------------------------------
# Fetching: Playwright primary, requests fallback
# ---------------------------------------------------------------------------

def _fetch_with_playwright(url: str) -> str | None:
    """Fetch a URL rendering JavaScript via Playwright."""
    try:
        from playwright.sync_api import sync_playwright

        validate_public_url(url)
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                page = browser.new_page()

                def guard_request(route: Any) -> None:
                    request_url = route.request.url
                    if request_url.startswith(("http://", "https://")):
                        validate_public_url(request_url)
                    route.continue_()

                page.route("**/*", guard_request)
                page.goto(url, wait_until="networkidle", timeout=30000)
                html = page.content()
                if len(html.encode("utf-8")) > MAX_RESPONSE_BYTES:
                    raise UnsafeUrlError("rendered page exceeds the configured size limit")
                return html
            finally:
                browser.close()
    except Exception:
        logger.info("Playwright fetch failed or not installed, falling back to requests")
        return None


def _fetch_with_requests(url: str) -> str:
    """Bounded HTTP GET fallback with redirect and address validation."""
    return fetch_text(url, timeout=30)


def _fetch_html(url: str) -> str:
    """Fetch URL with Playwright primary, requests fallback."""
    html = _fetch_with_playwright(url)
    if html:
        return html
    return _fetch_with_requests(url)


def _meta_refresh_target(html: str, base_url: str) -> str | None:
    class Refresh(HTMLParser):
        target = None
        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            attr_map = dict(attrs)
            if tag != "meta" or str(attr_map.get("http-equiv", "")).casefold() != "refresh":
                return
            match = re.fullmatch(r"\s*\d+(?:\.\d+)?\s*;\s*url\s*=\s*['\"]?([^'\";]+)['\"]?\s*", attr_map.get("content") or "", re.I)
            if match:
                self.target = urljoin(base_url, match.group(1).strip())
    parser = Refresh()
    parser.feed(html)
    return parser.target


def _fetch_article(url: str) -> tuple[str, str, list[dict]]:
    """Follow bounded HTML meta refreshes using the same public-URL guard."""
    current = url
    visited: set[str] = set()
    redirects: list[dict] = []
    for _ in range(MAX_REDIRECTS + 1):
        if current in visited:
            raise UnsafeUrlError("meta refresh cycle")
        visited.add(current)
        html = _fetch_html(current)
        target = _meta_refresh_target(html, current)
        if not target:
            visible = " ".join(block["text"] for block in web_source_blocks(html) if not block.get("metadata_key"))
            if not visible.strip() or (len(visible) < 300 and "redirecting" in visible.casefold()):
                raise ValueError("URL returned no article text")
            return current, html, redirects
        validate_public_url(target)
        redirects.append({"url": current, "target_url": target,
                          "source_digest": hashlib.sha256(html.encode()).hexdigest(), "html": html})
        current = target
    raise UnsafeUrlError("too many meta refreshes")


# ---------------------------------------------------------------------------
# Content extraction: trafilatura markdown (preserves headings)
# ---------------------------------------------------------------------------

def _extract_markdown(html: str) -> str:
    """Extract main content as markdown, preserving headings."""
    if trafilatura is not None:
        extracted = trafilatura.extract(
            html,
            output_format="markdown",
            include_links=True,
            include_tables=True,
        )
        if extracted and extracted.strip():
            return extracted

        # Fallback: plain text
        logger.info("trafilatura markdown returned empty, trying plain text")
        plain = trafilatura.extract(html)
        if plain and plain.strip():
            return plain

    logger.info("trafilatura returned empty, trying readability-lxml fallback")
    try:
        from readability import Document

        doc = Document(html)
        summary_html = doc.summary()
        from lxml import etree

        tree = etree.HTML(summary_html)
        text = " ".join(tree.itertext()).strip() if tree is not None else ""
        if text:
            return text
    except Exception:
        logger.info("readability-lxml fallback failed or not installed")

    return html


# ---------------------------------------------------------------------------
# Metadata extraction: Zotero → trafilatura
# ---------------------------------------------------------------------------

def _extract_metadata_zotero(url: str) -> dict[str, Any]:
    """Try zotero translation-server for rich metadata."""
    try:
        resp = requests.post(
            "http://localhost:1969/web",
            headers={"Content-Type": "text/plain"},
            data=url,
            timeout=15,
        )
        if resp.status_code == 200:
            items = resp.json()
            if items and isinstance(items, list) and items[0]:
                item = items[0]
                meta: dict[str, Any] = {}
                meta["type"] = {"journalArticle": "article-journal", "newspaperArticle": "article-newspaper",
                                "magazineArticle": "article-magazine", "blogPost": "post-weblog",
                                "book": "book", "bookSection": "chapter", "interview": "interview"}.get(item.get("itemType"), "webpage")
                if item.get("title"):
                    meta["title"] = item["title"]
                if item.get("creators"):
                    authors = []
                    for c in item["creators"]:
                        if c.get("lastName"):
                            name: dict[str, str] = {"family": c["lastName"]}
                            if c.get("firstName"):
                                name["given"] = c["firstName"]
                            role = c.get("creatorType", "author")
                            if role == "author":
                                authors.append(name)
                            elif role in {"editor", "translator", "interviewer", "director", "producer"}:
                                meta.setdefault(role, []).append(name)
                    if authors:
                        meta["author"] = authors
                if item.get("date"):
                    meta["date"] = item["date"]
                if item.get("publicationTitle"):
                    meta["container-title"] = item["publicationTitle"]
                if item.get("language"):
                    meta["language"] = item["language"]
                for key in ("DOI", "ISBN", "ISSN", "volume", "issue", "publisher"):
                    if item.get(key):
                        meta[key] = item[key]
                return meta
    except Exception:
        logger.info("zotero-translator not available, falling back to html/trafilatura metadata")
    return {}


class _HtmlMetadataParser(HTMLParser):
    """Zero-dependency parser for HTML meta tags and schema.org JSON-LD scripts."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.meta_tags: list[dict[str, str]] = []
        self.title_text: list[str] = []
        self.json_ld_scripts: list[str] = []
        self._in_title = False
        self._curr_script: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        attr_dict = {k.lower(): (v or "").strip() for k, v in attrs if k}
        if tag_lower == "meta":
            self.meta_tags.append(attr_dict)
        elif tag_lower == "title":
            self._in_title = True
        elif tag_lower == "script" and attr_dict.get("type", "").lower() == "application/ld+json":
            self._curr_script = []

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_text.append(data)
        if self._curr_script is not None:
            self._curr_script.append(data)

    def handle_endtag(self, tag: str) -> None:
        tag_lower = tag.lower()
        if tag_lower == "title":
            self._in_title = False
        elif tag_lower == "script" and self._curr_script is not None:
            self.json_ld_scripts.append("".join(self._curr_script))
            self._curr_script = None


def _format_author_dict(raw: Any) -> dict[str, str] | None:
    """Format an author name into CSL family/given or literal dict."""
    if isinstance(raw, dict):
        if raw.get("family"):
            res = {"family": str(raw["family"]).strip()}
            if raw.get("given"):
                res["given"] = str(raw["given"]).strip()
            return res
        if raw.get("name"):
            raw = str(raw["name"]).strip()
        elif raw.get("literal"):
            return {"literal": str(raw["literal"]).strip()}
        else:
            return None
    if isinstance(raw, str):
        raw = raw.strip()
        if not raw:
            return None
        if "," in raw:
            parts = raw.split(",", 1)
            family, given = parts[0].strip(), parts[1].strip()
            return {"family": family, "given": given} if given else {"literal": family}
        if re.search(r"[\u4e00-\u9fff]", raw):
            return {"literal": raw}
        if " " in raw:
            parts = raw.rsplit(" ", 1)
            return {"family": parts[1].strip(), "given": parts[0].strip()}
        return {"literal": raw}
    return None


def _extract_html_head_metadata(html: str) -> dict[str, Any]:
    """Deterministically extract citation metadata from HTML meta tags and JSON-LD.

    Extracts Highwire Press (Google Scholar), Dublin Core, OpenGraph, Prism,
    and schema.org ScholarlyArticle/Article JSON-LD structures.
    """
    import json

    parser = _HtmlMetadataParser()
    try:
        parser.feed(html)
    except Exception:
        logger.debug("Failed parsing HTML with _HtmlMetadataParser", exc_info=True)
        return {}

    meta: dict[str, Any] = {}
    authors: list[dict[str, str]] = []

    # 1. Parse meta tags
    meta_dict: dict[str, list[str]] = {}
    for tag_attrs in parser.meta_tags:
        key = (tag_attrs.get("name") or tag_attrs.get("property") or tag_attrs.get("http-equiv") or "").lower()
        content = tag_attrs.get("content", "").strip()
        if key and content:
            meta_dict.setdefault(key, []).append(content)

    # Highwire Press / Google Scholar
    hw_title = meta_dict.get("citation_title")
    if hw_title:
        meta["title"] = hw_title[0]

    for hw_author in meta_dict.get("citation_author", []):
        parsed_a = _format_author_dict(hw_author)
        if parsed_a and parsed_a not in authors:
            authors.append(parsed_a)

    hw_date = (
        meta_dict.get("citation_publication_date")
        or meta_dict.get("citation_date")
        or meta_dict.get("citation_online_date")
        or meta_dict.get("citation_cover_date")
    )
    if hw_date:
        meta["date"] = hw_date[0]

    hw_journal = meta_dict.get("citation_journal_title")
    hw_conference = meta_dict.get("citation_conference_title")
    if hw_journal:
        meta["container-title"] = hw_journal[0]
        meta["type"] = "article-journal"
    elif hw_conference:
        meta["container-title"] = hw_conference[0]
        meta["type"] = "paper-conference"

    hw_publisher = meta_dict.get("citation_publisher")
    if hw_publisher:
        meta["publisher"] = hw_publisher[0]

    hw_doi = meta_dict.get("citation_doi")
    if hw_doi:
        doi_val = hw_doi[0].strip()
        doi_val = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi_val, flags=re.I)
        doi_val = re.sub(r"^doi:\s*", "", doi_val, flags=re.I)
        meta["DOI"] = doi_val
        meta.setdefault("type", "article-journal")

    hw_issn = meta_dict.get("citation_issn")
    if hw_issn:
        meta["ISSN"] = hw_issn[0]

    hw_isbn = meta_dict.get("citation_isbn")
    if hw_isbn:
        meta["ISBN"] = hw_isbn[0]

    hw_vol = meta_dict.get("citation_volume")
    if hw_vol:
        meta["volume"] = hw_vol[0]

    hw_issue = meta_dict.get("citation_issue")
    if hw_issue:
        meta["issue"] = hw_issue[0]

    hw_fp = meta_dict.get("citation_firstpage")
    hw_lp = meta_dict.get("citation_lastpage")
    if hw_fp:
        meta["page"] = f"{hw_fp[0]}-{hw_lp[0]}" if hw_lp else hw_fp[0]

    # Dublin Core fallback
    if "title" not in meta:
        dc_title = meta_dict.get("dc.title") or meta_dict.get("dcterms.title")
        if dc_title:
            meta["title"] = dc_title[0]

    if not authors:
        for dc_creator in meta_dict.get("dc.creator", []) + meta_dict.get("dcterms.creator", []):
            parsed_a = _format_author_dict(dc_creator)
            if parsed_a and parsed_a not in authors:
                authors.append(parsed_a)

    if "date" not in meta:
        dc_date = meta_dict.get("dc.date") or meta_dict.get("dcterms.date") or meta_dict.get("dcterms.issued")
        if dc_date:
            meta["date"] = dc_date[0]

    if "publisher" not in meta:
        dc_pub = meta_dict.get("dc.publisher")
        if dc_pub:
            meta["publisher"] = dc_pub[0]

    if "DOI" not in meta:
        for dc_id in meta_dict.get("dc.identifier", []):
            m = re.search(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", dc_id)
            if m:
                meta["DOI"] = m.group(0)
                break

    # OpenGraph & Prism fallback
    if "title" not in meta:
        og_title = meta_dict.get("og:title")
        if og_title:
            meta["title"] = og_title[0]

    if "container-title" not in meta:
        og_site = meta_dict.get("og:site_name") or meta_dict.get("prism.publicationname")
        if og_site:
            meta["container-title"] = og_site[0]

    if "date" not in meta:
        art_time = meta_dict.get("article:published_time") or meta_dict.get("prism.publicationdate")
        if art_time:
            meta["date"] = art_time[0]

    if not authors:
        for art_author in meta_dict.get("article:author", []):
            parsed_a = _format_author_dict(art_author)
            if parsed_a and parsed_a not in authors:
                authors.append(parsed_a)

    if "DOI" not in meta:
        prism_doi = meta_dict.get("prism.doi")
        if prism_doi:
            meta["DOI"] = prism_doi[0].strip()

    # 2. Parse Schema.org JSON-LD scripts
    for script_content in parser.json_ld_scripts:
        if not script_content.strip():
            continue
        try:
            data = json.loads(script_content)
            items = data if isinstance(data, list) else [data]
            flattened: list[dict[str, Any]] = []
            for item in items:
                if isinstance(item, dict):
                    graph = item.get("@graph")
                    if isinstance(graph, list):
                        flattened.extend(g for g in graph if isinstance(g, dict))
                    else:
                        flattened.append(item)

            for entry in flattened:
                types = entry.get("@type", [])
                types = [types] if isinstance(types, str) else types if isinstance(types, list) else []
                types_set = {str(t).rsplit("/", 1)[-1] for t in types if isinstance(t, str)}
                if not types_set & {
                    "Article", "NewsArticle", "BlogPosting", "ScholarlyArticle", "TechArticle", "Book", "Chapter", "WebPage"
                }:
                    continue

                if "ScholarlyArticle" in types_set:
                    meta["type"] = "article-journal"
                elif "NewsArticle" in types_set:
                    meta.setdefault("type", "article-newspaper")
                elif "BlogPosting" in types_set:
                    meta.setdefault("type", "post-weblog")
                elif "Book" in types_set:
                    meta.setdefault("type", "book")
                elif "Chapter" in types_set:
                    meta.setdefault("type", "chapter")

                ld_title = entry.get("name") or entry.get("headline")
                if isinstance(ld_title, str) and ("title" not in meta or len(ld_title) > len(meta.get("title", ""))):
                    meta["title"] = ld_title.strip()

                if not authors:
                    raw_ld_authors = entry.get("author")
                    if raw_ld_authors:
                        ld_author_list = raw_ld_authors if isinstance(raw_ld_authors, list) else [raw_ld_authors]
                        for a in ld_author_list:
                            parsed_a = _format_author_dict(a)
                            if parsed_a and parsed_a not in authors:
                                authors.append(parsed_a)

                if "date" not in meta:
                    ld_date = entry.get("datePublished") or entry.get("dateCreated")
                    if isinstance(ld_date, str):
                        meta["date"] = ld_date.strip()

                if "publisher" not in meta:
                    ld_pub = entry.get("publisher")
                    if isinstance(ld_pub, dict) and ld_pub.get("name"):
                        meta["publisher"] = str(ld_pub["name"]).strip()
                    elif isinstance(ld_pub, str):
                        meta["publisher"] = ld_pub.strip()

                if "container-title" not in meta:
                    ld_part = entry.get("isPartOf")
                    if isinstance(ld_part, dict) and ld_part.get("name"):
                        meta["container-title"] = str(ld_part["name"]).strip()
                    elif isinstance(ld_part, str):
                        meta["container-title"] = ld_part.strip()

                if "DOI" not in meta:
                    ld_doi = entry.get("doi") or entry.get("identifier")
                    if isinstance(ld_doi, str) and "10." in ld_doi:
                        m = re.search(r"10\.\d{4,9}/[-._;()/:A-Za-z0-9]+", ld_doi)
                        if m:
                            meta["DOI"] = m.group(0)
        except Exception:
            continue

    if authors:
        meta["author"] = authors

    # Fallback to <title> tag text
    if "title" not in meta and parser.title_text:
        title_cand = "".join(parser.title_text).strip()
        if title_cand:
            meta["title"] = title_cand

    return meta


def _extract_metadata(html: str, url: str) -> dict[str, Any]:
    """Extract metadata with Zotero primary, HTML meta/JSON-LD secondary, and trafilatura fallback."""
    meta = _extract_metadata_zotero(url)
    if meta.get("title"):
        logger.info("Zotero metadata extraction succeeded")
        return meta

    # Deterministic HTML <head> meta tag and schema.org JSON-LD extraction
    head_meta = _extract_html_head_metadata(html)

    # Trafilatura fallback for fields missing from head tags
    try:
        metadata_obj = trafilatura.extract_metadata(html)
    except Exception:
        metadata_obj = None

    merged = dict(head_meta)
    if not merged.get("title"):
        merged["title"] = (metadata_obj.title if metadata_obj else None) or url
    if not merged.get("author") and metadata_obj and metadata_obj.author:
        merged["author"] = metadata_obj.author
    if not merged.get("date") and metadata_obj and metadata_obj.date:
        merged["date"] = metadata_obj.date

    return merged


# ---------------------------------------------------------------------------
# Citation guidance extraction (pattern-based)
# ---------------------------------------------------------------------------

# Patterns to find citation guidance blocks
_CITE_GUIDANCE_PATTERNS = [
    re.compile(r"若要转载请参考如下格式[：:]\s*(.*?)(?:[。]|$)", re.DOTALL),
    # Copyright-policy preamble, used by gcdfl.org / ctcfol.org:
    #   版权声明：若要转载或引用此文，请用以下格式：…
    #   版权申明：若要引用，请采用以下格式：…
    re.compile(
        r"版权(?:声明|申明)\s*[：:]\s*若要(?:转载或引用|引用)(?:本文|此文)?[，,]?\s*"
        r"(?:请(?:按|参考|采用|用)以下格式[：:]?\s*)?(.*?)(?:[。]|$)",
        re.DOTALL,
    ),
    re.compile(r"若要转载或引用此文[，,]?\s*请用以下格式[：:]?\s*(.*?)(?:[。]|$)", re.DOTALL),
    re.compile(r"若参考了这篇中译[，,]?\s*请注明引用格式如下[：:]?\s*(.*?)(?:[。]|$)", re.DOTALL),
    # Chinese repositories (CNKI / Wanfang / VIP) usually label the GB/T 7714
    # export block as 引用本文/引用此文, without a 若要… preamble.
    re.compile(
        r"引用本文\s*[：:]\s*([^\n]+(?:\n(?!\s*\n)[^\n]+){0,2})",
        re.DOTALL,
    ),
    re.compile(
        r"引用此文\s*[：:]\s*([^\n]+(?:\n(?!\s*\n)[^\n]+){0,2})",
        re.DOTALL,
    ),
    # Chinese: 引用格式
    re.compile(r"引用格式\s*[：:]\s*(.*?)(?:[。]|$)", re.DOTALL),
    # Chinese: 版权申明.*转载.*
    re.compile(
        r"版权申明\s*[：:]\s*若您想转载此文[，,]?\s*(.*?)(?:[。]|请按|$)",
        re.DOTALL,
    ),
    # Chinese: 若要引用本文 / 若要引用此文 / 若要引用，请采用以下格式
    # Keep last among the Chinese patterns so a policy heading such as
    # "若要引用本文，请参考版权申明" cannot pre-empt the real guidance block.
    re.compile(
        r"若要引用(?:本文|此文)?[，,]?\s*(?:请(?:按|参考|采用)以下格式[：:]?\s*)?(.*?)(?:[。]|也请参考|$)",
        re.DOTALL,
    ),
    # English: Cite this article/entry
    re.compile(
        r"Cite\s+this\s+(?:article|entry)\s*[：:\n]\s*(.*?)(?:\n\n|\n##|\Z)",
        re.DOTALL | re.IGNORECASE,
    ),
    re.compile(
        r"This\s+(?:article|work|topic page)\s+can\s+be\s+cited\s+as[：:]\s*`?([^\n`]+)",
        re.IGNORECASE,
    ),
    # German: Zitierweise
    re.compile(r"Zitierweise\s*[：:]\s*(.*?)(?:\n\n|\Z)", re.DOTALL),
    # French: Pour citer
    re.compile(r"Pour\s+citer\s*[：:]\s*(.*?)(?:\n\n|\Z)", re.DOTALL | re.IGNORECASE),
]

# Chinese citation string parsers:
#   Author，《Title》，Series（Place：Publisher，Date）
#   Author译《Title》（Place：Publisher，Year）
# Two patterns: 《》 (guillemets, may contain inner <>) and bare <>
_CN_TRANSLATION_IN_RE = re.compile(
    r"^\s*(?P<translator>[^，,《]+?)译[，,]\s*《(?P<title>[^》]+)》\s*in\s*《(?P<container>[^》]+)》\s*"
    r"[（(](?P<place>[^：:）)]+)[：:](?P<publisher>[^，,）)]+)[，,](?P<date>[^）)]+)[）)]",
    re.IGNORECASE,
)
_CN_CITE_BOOK_RE = re.compile(
    r"^\s*(?P<author>[^，,《]+?)[，,]?\s*"
    r"(?:译\s*)?"
    r"《(?P<title>.+?)》[，,]\s*"
    r"(?P<series>.+?)"
    r"[（(](?P<place>[^：:）)]+)[：:](?P<publisher>[^，,）)]+)[，,]"
    r"(?P<date>[^）)]+)[）)]"
)
_CN_CITE_SIMPLE_RE = re.compile(
    r"^\s*(?P<author>[^，,《<]+?)[，,]?\s*"
    r"(?:译\s*)?"
    r"[《<](?P<title>[^》>]+)[》>]\s*"
    r"[（(](?P<place>[^：:）)]+)[：:](?P<publisher>[^，,）)]+)[，,]"
    r"(?P<date>[^）)]+)[）)]"
)

# GB/T 7714 export strings used by CNKI / Wanfang / VIP reference exporters:
#   张三, 李四. 题名[J]. 期刊, 2020, 46(3): 45-67.
#   张三. 题名[D]. 北京: 北京大学, 2021.
#   李明. 书名[M]. 上海: 复旦大学出版社, 2019: 88-90.
#   王五. 题名[C]//论文集. 北京: 科学出版社, 2022: 12-20.
_GBT_RE = re.compile(
    r"^\s*(?P<authors>[^.．]+?)\s*[.．]\s*(?P<title>.+?)\s*"
    r"\[\s*(?P<kind>EB/OL|DB/OL|J/OL|M/OL|J|D|M|C|N|R|S|P|A|G|Z)\s*\]\s*"
    r"(?P<rest>.*)$",
    re.S | re.I,
)
_GBT_JOURNAL_RE = re.compile(
    r"^\s*[.．]?\s*(?P<container>[^,，]+?)\s*[,，]\s*(?P<year>\d{4})"
    r"(?:\s*[,，]\s*(?P<volume>[^,，(（:：]+))?"
    r"(?:\s*[（(]\s*(?P<issue>[^)）]+)\s*[)）])?"
    r"(?:\s*[:：]\s*(?P<page>[^.,，。]+))?",
    re.S,
)
_GBT_IMPRINT_RE = re.compile(
    r"^\s*[.．]?\s*(?P<place>[^:：.]+?)\s*[:：]\s*(?P<publisher>[^,，]+?)"
    r"\s*[,，]\s*(?P<year>\d{4})"
    r"(?:\s*[:：]\s*(?P<page>[^.,，。]+))?",
    re.S,
)
_GBT_CONFERENCE_RE = re.compile(
    r"^\s*//\s*(?P<container>[^.．]+?)\s*[.．]\s*"
    r"(?P<place>[^:：.]+?)\s*[:：]\s*(?P<publisher>[^,，]+?)"
    r"\s*[,，]\s*(?P<year>\d{4})"
    r"(?:\s*[:：]\s*(?P<page>[^.,，。]+))?",
    re.S,
)
_GBT_CONTAINER_RE = re.compile(r"^\s*//\s*(?P<container>[^.．]+?)\s*[.．]\s*(?P<imprint>.*)$", re.S)
_GBT_YEAR_RE = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")
_GBT_PAGE_RE = re.compile(r"[:：]\s*([A-Za-z]?\d+(?:\s*[-–—]\s*[A-Za-z]?\d+)?)\s*[.．]?\s*$")
_GBT_URL_RE = re.compile(r"(?P<url>https?://\S+)", re.I)

_GBT_TYPES = {
    "J": "article-journal",
    "D": "thesis",
    "M": "book",
    "C": "paper-conference",
    "N": "article-newspaper",
    "R": "report",
    "S": "report",
    "P": "document",
    "A": "document",
    "G": "document",
    "Z": "document",
    "EB": "webpage",
    "DB": "webpage",
}


def _format_gbt_authors(raw: str) -> list[dict[str, str]]:
    """Parse a GB/T 7714 author list ("张三, 李四" / "VASWANI A, SHAZEER N")."""
    authors: list[dict[str, str]] = []
    for token in re.split(r"[,，;；]\s*|\s+and\s+", raw.strip().rstrip(".． ")):
        token = token.strip()
        if not token:
            continue
        if re.search(r"[\u4e00-\u9fff]", token):
            entry = {"literal": token}
        else:
            words = token.replace(".", " ").split()
            # GB/T puts the family name first for pinyin/Latin names ("ZHANG San").
            if len(words) >= 2 and words[0].isupper() and len(words[0]) > 1:
                entry = {"family": words[0].title(), "given": " ".join(words[1:])}
            else:
                parsed = _format_author_dict(token)
                if not parsed:
                    continue
                entry = parsed
        if entry not in authors:
            authors.append(entry)
    return authors


def _parse_gbt_citation(cite_str: str) -> dict[str, Any] | None:
    """Parse a GB/T 7714 reference string (CNKI / Wanfang / VIP export format)."""
    cite_str = re.sub(r"^(?:引用本文|引用此文|本文引用格式|引用格式)\s*[：:]\s*", "", cite_str.strip())
    match = _GBT_RE.search(cite_str)
    if not match:
        return None
    kind = match.group("kind").upper().split("/")[0]
    title = match.group("title").strip().strip(".． ")
    if not title:
        return None

    result: dict[str, Any] = {"title": title, "type": _GBT_TYPES.get(kind, "document")}
    authors = _format_gbt_authors(match.group("authors"))
    if authors:
        result["author"] = authors

    rest = match.group("rest").strip().lstrip(".． ").strip()

    def add_year_page(text: str) -> None:
        year = _GBT_YEAR_RE.search(text)
        if year and "issued" not in result:
            result["issued"] = {"date-parts": [[int(year.group(1))]]}
        page = _GBT_PAGE_RE.search(text)
        if page and "page" not in result:
            result["page"] = re.sub(r"\s+", "", page.group(1))

    def add_imprint(text: str) -> bool:
        imprint = _GBT_IMPRINT_RE.search(text)
        if not imprint:
            return False
        result["publisher-place"] = imprint.group("place").strip()
        result["publisher"] = imprint.group("publisher").strip()
        if imprint.group("year"):
            result["issued"] = {"date-parts": [[int(imprint.group("year"))]]}
        if imprint.group("page"):
            result["page"] = re.sub(r"\s+", "", imprint.group("page").strip().rstrip(".．"))
        return True

    if kind == "J":
        journal = _GBT_JOURNAL_RE.search(rest)
        if journal:
            result["container-title"] = journal.group("container").strip()
            for key in ("volume", "issue"):
                value = journal.group(key)
                if value:
                    result[key] = value.strip()
            if journal.group("page"):
                result["page"] = re.sub(r"\s+", "", journal.group("page").strip().rstrip(".．"))
            if journal.group("year"):
                result["issued"] = {"date-parts": [[int(journal.group("year"))]]}
        else:
            add_year_page(rest)
    elif kind == "C":
        conference = _GBT_CONFERENCE_RE.search(rest)
        if conference:
            result["container-title"] = conference.group("container").strip()
            result["publisher-place"] = conference.group("place").strip()
            result["publisher"] = conference.group("publisher").strip()
            result["issued"] = {"date-parts": [[int(conference.group("year"))]]}
            if conference.group("page"):
                result["page"] = re.sub(r"\s+", "", conference.group("page").strip().rstrip(".．"))
        else:
            container = _GBT_CONTAINER_RE.search(rest)
            if container:
                result["container-title"] = container.group("container").strip()
                imprint = container.group("imprint")
                if not add_imprint(imprint):
                    add_year_page(imprint)
            else:
                add_year_page(rest)
    elif kind in {"M", "D", "R"}:
        if not add_imprint(rest):
            add_year_page(rest)
    elif kind in {"EB", "DB"}:
        url = _GBT_URL_RE.search(rest)
        if url:
            result["URL"] = url.group("url").rstrip(".,，。")
        add_year_page(rest)
    else:
        add_year_page(rest)

    return result

# Western Chicago-style: Author. "Title." *Journal*. Published Date. URL
_CHICAGO_CITE_RE = re.compile(
    r"(?P<author>[^.]+?)\.\s*"
    r'["""\u201c\u00ab](?P<title>[^"""\u201d\u00bb]+)["""\u201d\u00bb]\s*\.?\s*'
    r"(?:\*(?P<journal>[^*]+)\*\s*\.?\s*)?"
    r"(?:Published\s+)?(?P<date>[A-Z][a-z]+ \d{1,2},?\s+\d{4}|\d{4}[-/]\d{2}[-/]\d{2}|\d{4})"
)


def _find_citation_guidance(text: str) -> str | None:
    """Scan text for citation guidance blocks, return the citation string."""
    for pat in _CITE_GUIDANCE_PATTERNS:
        m = pat.search(text)
        if m:
            cite_str = m.group(1).strip()
            # Clean up markdown/HTML artifacts
            cite_str = re.sub(r"<[^>]+>", "", cite_str)
            cite_str = re.sub(r"\[#\].*$", "", cite_str, flags=re.MULTILINE).strip()
            cite_str = re.sub(r"\[([^\]]+)\]\[[^\]]*\]", r"\1", cite_str)
            cite_str = re.sub(r"\[([^\]]+)\]\([^\)]*\)", r"\1", cite_str)
            # A policy pointer ("若要引用本文，请参考版权申明") is not the citation.
            if re.match(r"^(?:请(?:参考|参阅|见)?\s*)?\[?\s*版权(?:声明|申明)", cite_str):
                continue
            if len(cite_str) > 10:
                return cite_str
    return None


def _normalize_title(value: str) -> str:
    return re.sub(r"[\s\W_]+", "", str(value)).casefold()


def _titles_match(left: str, right: str) -> bool:
    first, second = _normalize_title(left), _normalize_title(right)
    if not first or not second:
        return False
    return (
        first == second
        or (len(first) >= 8 and first in second)
        or (len(second) >= 8 and second in first)
    )


def _find_unlabeled_gbt_self_citation(text: str, page_title: str) -> tuple[str, dict[str, Any]] | None:
    """Find an unlabeled GB/T 7714 line that cites the page itself.

    Reference lists contain other works; only a line whose title matches the
    page title (extracted from <head>/trafilatura) is accepted as the page's
    own citation.
    """
    if not page_title or not text:
        return None
    lines = [line.strip() for line in text.splitlines()]
    for index, line in enumerate(lines):
        if not line:
            continue
        for span in (1, 2, 3):
            window = [part for part in lines[index:index + span] if part]
            if not window:
                continue
            candidate = " ".join(window).strip()
            if len(candidate) > 600 or "[" not in candidate:
                continue
            parsed = _parse_gbt_citation(candidate)
            if parsed and _titles_match(parsed.get("title", ""), page_title):
                return candidate, parsed
    return None


def _parse_citation_string(cite_str: str) -> dict[str, Any]:
    """Parse a citation guidance string into CSL fields (regex first)."""
    gbt = _parse_gbt_citation(cite_str)
    if gbt:
        return gbt

    m = _CN_TRANSLATION_IN_RE.search(cite_str)
    if m:
        result = {"translator": [{"literal": m.group("translator").strip()}],
                  "title": m.group("title").strip(), "container-title": m.group("container").strip(),
                  "publisher-place": m.group("place").strip(), "publisher": m.group("publisher").strip()}
        issued = _parse_date_string(m.group("date"))
        if issued:
            result["issued"] = issued
        return result
    # Try Chinese format — book style with series (《Title》，Series（Place：Publisher，Date）)
    m = _CN_CITE_BOOK_RE.search(cite_str)
    if not m:
        # Simpler format: 《Title》（Place：Publisher，Date）
        m = _CN_CITE_SIMPLE_RE.search(cite_str)

    if m:
        result = {}
        result["author"] = [{"literal": m.group("author").strip()}]
        result["title"] = m.group("title").strip()
        if m.group("publisher"):
            result["publisher"] = m.group("publisher").strip()
        if m.group("place"):
            result["publisher-place"] = m.group("place").strip()
        try:
            series = m.group("series")
            if series and series.strip():
                series = series.strip().rstrip("，,")
                if series.endswith(("中译", "译")) and "，" not in series:
                    result["translator"] = [{"literal": series.removesuffix("中译").removesuffix("译").strip()}]
                elif series.lower().startswith("in《") and series.endswith("》"):
                    result["container-title"] = series[3:-1]
                else:
                    result["collection-title"] = series
        except IndexError:
            pass
        date_str = m.group("date").strip()
        result["_raw_date"] = date_str
        parsed_date = _parse_date_string(date_str)
        if parsed_date:
            result["issued"] = parsed_date
        return result

    # Try Chicago format
    m = _CHICAGO_CITE_RE.search(cite_str)
    if m:
        result = {}
        author_raw = m.group("author").strip()
        if "," in author_raw:
            parts = author_raw.split(",", 1)
            result["author"] = [{"family": parts[0].strip(), "given": parts[1].strip()}]
        else:
            result["author"] = [{"literal": author_raw}]
        result["title"] = m.group("title").strip()
        if m.group("journal"):
            result["container-title"] = m.group("journal").strip()
        date_str = m.group("date").strip()
        parsed_date = _parse_date_string(date_str)
        if parsed_date:
            result["issued"] = parsed_date
        return result

    return {}


def _parse_date_string(date_str: str) -> dict[str, Any] | None:
    """Parse various date formats into CSL date-parts."""
    # Chinese: 2025年10月29日
    m = re.search(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日", date_str)
    if m:
        return {"date-parts": [[int(m.group(1)), int(m.group(2)), int(m.group(3))]]}

    # ISO: 2025-10-29 or 2025/10/29
    m = re.search(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", date_str)
    if m:
        return {"date-parts": [[int(m.group(1)), int(m.group(2)), int(m.group(3))]]}

    # English: March 4, 2011
    m = re.search(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+"
        r"(\d{1,2}),?\s+(\d{4})",
        date_str,
        re.IGNORECASE,
    )
    if m:
        months = {
            "january": 1, "february": 2, "march": 3, "april": 4,
            "may": 5, "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12,
        }
        return {"date-parts": [[int(m.group(3)), months[m.group(1).lower()], int(m.group(2))]]}

    # Preserve month precision when the source has no day.
    m = re.search(r"(\d{4})(?:[-/]|\s*年\s*)(\d{1,2})(?:月)?(?!\d)", date_str)
    if m:
        return {"date-parts": [[int(m.group(1)), int(m.group(2))]]}
    m = re.search(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{4})", date_str, re.IGNORECASE)
    if m:
        import calendar
        month = next(i for i in range(1, 13) if calendar.month_name[i].casefold() == m.group(1).casefold())
        return {"date-parts": [[int(m.group(2)), month]]}
    # Year only: 2025
    m = re.search(r"(\d{4})", date_str)
    if m:
        return {"date-parts": [[int(m.group(1))]]}

    return None


def _extract_citation_from_guidance(
    text: str,
    config: IngestionConfig | None = None,
) -> dict[str, Any]:
    """Find and parse in-page citation guidance. DSPy fallback for unparseable strings."""
    cite_str = _find_citation_guidance(text)
    if not cite_str:
        return {}

    logger.info("Found citation guidance: %s", cite_str[:120])

    # Try regex parsing first
    parsed = _parse_citation_string(cite_str)
    if parsed.get("title"):
        logger.info("Regex-parsed citation guidance successfully")
        parsed["_citation_source"] = "in_page_guidance"
        return parsed

    # DSPy fallback
    logger.info("Regex could not parse citation string, trying DSPy")
    cfg = config or IngestionConfig()
    try:
        import dspy

        from ...llm import get_llm_model
        from ...model import ParseCitationString

        lm = get_llm_model(cfg.llm_model, temperature=0.1)
        with dspy.context(lm=lm):
            predictor = dspy.Predict(ParseCitationString)
            result = predictor(citation_string=cite_str)

        parsed = {}
        if result.author:
            parsed["author"] = [{"literal": result.author.strip()}]
        if result.title:
            parsed["title"] = result.title.strip()
        if result.publisher:
            parsed["publisher"] = result.publisher.strip()
        if result.publication_date:
            pd = _parse_date_string(result.publication_date)
            if pd:
                parsed["issued"] = pd
        if parsed.get("title"):
            parsed["_citation_source"] = "in_page_guidance_dspy"
            return parsed
    except Exception:
        logger.warning("DSPy citation parsing failed", exc_info=True)

    return {}


# ---------------------------------------------------------------------------
# Section-hierarchical structure from markdown
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)(?:\s*\[#\].*)?$")


def _parse_markdown_sections(
    markdown_text: str, url: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[tuple[int, list[str]]]]:
    """Parse markdown into section-hierarchical pages and paragraph list.

    Returns:
        (pages, section_tree, page_paragraphs)

        pages: list of page dicts with section_number as page_number
        section_tree: nested section hierarchy
        page_paragraphs: [(section_number, [paragraph_texts])] for nodes
    """
    lines = markdown_text.split("\n")

    # First pass: identify sections and their content
    sections: list[dict[str, Any]] = []
    current_section: dict[str, Any] = {
        "title": "(intro)",
        "level": 0,
        "paragraphs": [],
        "anchor": "",
    }

    buf: list[str] = []

    def flush_buf():
        if buf:
            text = "\n".join(buf).strip()
            if text:
                current_section["paragraphs"].append(text)
            buf.clear()

    for line in lines:
        m = _HEADING_RE.match(line)
        if m:
            flush_buf()
            # Save current section
            if current_section["paragraphs"]:
                sections.append(current_section)

            level = len(m.group(1))
            heading_text = m.group(2).strip()
            # Extract anchor from [#](anchor) if present
            anchor_m = re.search(r"\[#\]\(#([^)]+)\)", line)
            anchor = anchor_m.group(1) if anchor_m else re.sub(
                r"[^\w\u4e00-\u9fff]+", "-", heading_text.lower()
            ).strip("-")

            current_section = {
                "title": heading_text,
                "level": level,
                "paragraphs": [],
                "anchor": anchor,
            }
        else:
            stripped = line.strip()
            if not stripped:
                flush_buf()
            else:
                buf.append(stripped)

    flush_buf()
    if current_section["paragraphs"]:
        sections.append(current_section)

    # Build pages (one per section), section_tree, and page_paragraphs
    pages: list[dict[str, Any]] = []
    page_paragraphs: list[tuple[int, list[str]]] = []
    section_tree: list[dict[str, Any]] = []
    tree_stack: list[dict[str, Any]] = []

    for sec_idx, sec in enumerate(sections):
        sec_num = sec_idx + 1
        sec_url = f"{url}#{sec['anchor']}" if sec["anchor"] else url

        # Build paragraphs for this section-page
        para_list: list[dict[str, Any]] = []
        para_texts: list[str] = []
        for pi, para_text in enumerate(sec["paragraphs"], start=1):
            # Split further by double-newline within the paragraph
            sub_paras = split_paragraphs(para_text)
            for si, sp in enumerate(sub_paras):
                para_id = f"s{sec_num}_para{pi}" if len(sub_paras) == 1 else f"s{sec_num}_para{pi}_{si+1}"
                para_list.append({
                    "paragraph_id": para_id,
                    "text": sp,
                    "section": sec["title"],
                })
                para_texts.append(sp)

        pages.append({
            "page_number": sec_num,
            "section_title": sec["title"],
            "section_level": sec["level"],
            "section_url": sec_url,
            "paragraphs": para_list,
            "footnotes": [],
        })
        page_paragraphs.append((sec_num, para_texts))

        # Build section tree
        tree_node: dict[str, Any] = {
            "title": sec["title"],
            "level": sec["level"],
            "section_number": sec_num,
            "url": sec_url,
            "children": [],
        }

        # Find parent in stack
        while tree_stack and tree_stack[-1]["level"] >= sec["level"]:
            tree_stack.pop()

        if tree_stack:
            tree_stack[-1]["children"].append(tree_node)
        else:
            section_tree.append(tree_node)

        tree_stack.append(tree_node)

    return pages, section_tree, page_paragraphs


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

def run(
    url: str,
    config: IngestionConfig | None = None,
) -> PipelineResult:
    cfg = config or IngestionConfig()
    source_id = make_source_id(url)

    # ── Step 1: Fetch ──────────────────────────────────────────────
    final_url, html, redirects = _fetch_article(url)
    now = datetime.now(UTC)

    # ── Step 2: Extract content as markdown (preserves headings) ──
    markdown_text = _extract_markdown(html)

    # ── Step 3: Extract metadata (Zotero first) ───────────────────
    metadata = _extract_metadata(html, final_url)
    title = metadata.get("title") or final_url
    author = metadata.get("author")
    date = metadata.get("date")

    # ── Step 4-5: Find & parse in-page citation guidance ──────────
    # Keep regex candidates, but use the dedicated web signature for model work.
    cite_text = _find_citation_guidance(markdown_text) or _find_citation_guidance(html)
    guidance_csl = _parse_citation_string(cite_text) if cite_text else {}
    if not guidance_csl and title and title != final_url:
        for body in (markdown_text, html):
            fallback = _find_unlabeled_gbt_self_citation(body, title)
            if fallback:
                cite_text, guidance_csl = fallback
                guidance_csl["_citation_source"] = "in_page_gbt_reference"
                break
    source_blocks = web_source_blocks(html)
    retrieval_metadata = {"URL": final_url, "accessed": now.date().isoformat(), "requested_url": url}
    source_blocks += metadata_blocks(retrieval_metadata, "retrieval_metadata.json")

    # ── Step 6: Reconcile — citation guidance wins ────────────────
    csl_extra: dict[str, Any] = {
        "URL": final_url,
        "accessed": {
            "date-parts": [[now.year, now.month, now.day]],
        },
    }

    # Start with Zotero/trafilatura metadata
    if author:
        if isinstance(author, list):
            csl_extra["author"] = author
        else:
            csl_extra["author"] = [{"literal": author}]

    if isinstance(date, str):
        parsed_date = _parse_date_string(date)
        if valid_host_value("issued", parsed_date):
            csl_extra["issued"] = parsed_date
    for key, value in metadata.items():
        if key not in {"title", "author", "date", "type"} and valid_host_value(key, value):
            csl_extra[key] = value

    container_title = metadata.get("container-title")
    if container_title:
        csl_extra["container-title"] = container_title

    extracted = (
        extract_multimodal_metadata("web", source_blocks, cfg)
        if cfg.use_llm_metadata
        else {}
    )
    csl_extra.update({key: value for key, value in extracted.items() if key not in {"type", "title", "_field_evidence"}})
    title = extracted.get("title") or title
    field_evidence = dict(extracted.get("_field_evidence", {}))
    source_type = extracted.get("type") or metadata.get("type") or "webpage"
    if not valid_host_value("type", source_type):
        source_type = "webpage"

    # Link source block evidence for metadata originating from HTML meta tags or JSON-LD
    _META_FIELD_CANDIDATES = {
        "title": ("citation_title", "dc.title", "dcterms.title", "og:title", "jsonld:headline", "jsonld:name"),
        "DOI": ("citation_doi", "prism.doi", "dc.identifier"),
        "container-title": (
            "citation_journal_title",
            "citation_conference_title",
            "prism.publicationname",
            "og:site_name",
            "jsonld:ispartof",
        ),
        "publisher": ("citation_publisher", "dc.publisher", "jsonld:publisher"),
        "issued": (
            "citation_publication_date",
            "citation_date",
            "citation_online_date",
            "article:published_time",
            "dc.date",
            "dcterms.date",
            "jsonld:datepublished",
        ),
        "author": ("citation_author", "dc.creator", "dcterms.creator", "article:author", "jsonld:author"),
        "volume": ("citation_volume", "prism.volume"),
        "issue": ("citation_issue", "prism.number"),
        "ISSN": ("citation_issn", "prism.issn"),
        "ISBN": ("citation_isbn",),
        "type": ("citation_journal_title", "citation_conference_title", "jsonld:@type"),
    }
    for field, candidate_keys in _META_FIELD_CANDIDATES.items():
        if field in field_evidence:
            continue
        if field == "type" and source_type == "webpage":
            # A declared schema.org Article does not establish the fallback
            # "webpage" type; only bind types the extractor actually mapped.
            continue
        val = title if field == "title" else source_type if field == "type" else csl_extra.get(field)
        if val is None:
            continue
        for block in source_blocks:
            mk = str(block.get("metadata_key", "")).lower()
            if any(mk == ck or mk.startswith(f"{ck}:") for ck in candidate_keys):
                quote = block.get("text", "")
                if quote:
                    field_evidence[field] = {
                        "block_id": block["id"],
                        "quote": quote,
                        "locator": {
                            "metadata_key": block.get("metadata_key", mk),
                            "snapshot_artifact": "source.html",
                            "source_digest": block.get("source_digest", ""),
                        },
                    }
                    break

    # Composite page evidence: first/last page tags are adjacent metadata
    # blocks inside one snapshot; bind both endpoints when the range is real.
    if "page" not in field_evidence:
        page_value = csl_extra.get("page")
        if isinstance(page_value, str) and page_value.strip():
            first = next((block for block in source_blocks
                          if str(block.get("metadata_key", "")).lower()
                          in {"citation_firstpage", "prism.startingpage"}), None)
            last = next((block for block in source_blocks
                         if str(block.get("metadata_key", "")).lower()
                         in {"citation_lastpage", "prism.endingpage"}), None)
            spans = [{"block_id": first["id"], "quote": first["text"]}] if first else []
            if first and last and re.search(r"[-–—]", page_value):
                try:
                    adjacent = source_blocks.index(last) == source_blocks.index(first) + 1
                except ValueError:
                    adjacent = False
                if adjacent:
                    spans.append({"block_id": last["id"], "quote": last["text"]})
                else:
                    spans = []
            if spans:
                field_evidence["page"] = {
                    "spans": spans,
                    "quote": " ".join(span["quote"] for span in spans),
                    "locator": {
                        "metadata_key": first.get("metadata_key", ""),
                        "snapshot_artifact": "source.html",
                        "source_digest": first.get("source_digest", ""),
                    },
                }

    if guidance_csl:
        logger.info("Overriding metadata and DSPy with in-page citation guidance")
        guidance_block = next((block for block in source_blocks if cite_text and cite_text in block["text"]), None)
        if guidance_block is None and guidance_csl.get("title"):
            normalized = _normalize_title(guidance_csl["title"])
            guidance_block = next(
                (block for block in source_blocks
                 if normalized and _GBT_RE.search(block.get("text", ""))
                 and normalized in _normalize_title(block["text"])),
                None,
            )
        for field in ("author", "issued", "publisher", "publisher-place", "container-title",
                      "collection-title", "title", "volume", "issue", "page", "DOI", "type"):
            value = guidance_csl.get(field)
            if not value:
                continue
            if field == "author" and field_evidence.get("author"):
                existing: Any = csl_extra.get("author")
                surname = existing[0].get("family") if isinstance(existing, list) and len(existing) == 1 else None
                if (surname and existing[0].get("given") and isinstance(value, list) and len(value) == 1 and
                        re.search(rf"\b{re.escape(surname)}\b", value[0].get("literal", ""), re.I) and
                        not re.search(re.escape(existing[0]["given"]), value[0].get("literal", ""), re.I)):
                    continue  # Guidance abbreviates a source-supported full byline.
            if (title if field == "title" else csl_extra.get(field)) != value:
                field_evidence.pop(field, None)
            if field == "title":
                title = value
            elif field == "type":
                if not valid_host_value("type", value):
                    continue
                source_type = value
                csl_extra["type"] = value
            else:
                csl_extra[field] = value
            if guidance_block:
                from ..citation_verification import validate_block_evidence
                supported = validate_block_evidence(field, value, {"block_id": guidance_block["id"], "quote": guidance_block["text"]}, source_blocks)
                if supported:
                    field_evidence[field] = supported
        csl_extra["_citation_source"] = guidance_csl.get("_citation_source", "in_page_guidance")
    if field_evidence:
        csl_extra["_field_evidence"] = field_evidence
    if not valid_host_value("type", source_type):
        source_type = "webpage"
    csl_json = make_basic_csl(source_id, title, source_type, csl_extra)
    csl_json["_field_status"] = {
        field: ("source-supported" if field in field_evidence else
                "unverified" if csl_json.get(field) is not None else "missing")
        for field in evaluation_fields(csl_json, "url_article")
    }
    csl_json["_field_status"]["accessed"] = "observed"
    csl_json["_field_status"]["URL"] = "observed"
    csl_json["_citation_status"] = "incomplete" if (
        csl_json["_field_status"].get("title") != "source-supported" or
        not any(csl_json["_field_status"].get(role) == "source-supported" for role in ("author", "editor", "translator"))
    ) else "source-supported"

    # ── Step 7: Build section-hierarchical structure ───────────────
    pages, section_tree, page_paragraphs = _parse_markdown_sections(markdown_text, final_url)
    nodes = build_nodes(source_id, page_paragraphs)
    attach_evidence_locators({"pages": pages}, nodes)

    # ── Step 8: Merkle tree (no retrieval index) ──────────────────
    merkle_tree = build_merkle_for_nodes(nodes)

    # ── PageIndex enhancement (optional) ──────────────────────────
    html_digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
    extra: dict[str, Any] = {
        "source_snapshot_path": _write_html_snapshot(html),
        "source_snapshot_digest": html_digest,
        "cleanup_source_snapshot": True,
        "source_blocks": source_blocks,
        "web_metadata": metadata,
        "retrieval_metadata": retrieval_metadata,
        "redirect_snapshots": redirects,
    }
    if cfg.use_pageindex and markdown_text.strip():
        from .pageindex_tree import pageindex_to_citeindex_tree, run_pageindex_md_tree
        pi_result = run_pageindex_md_tree(markdown_text, model=cfg.pageindex_model)
        if pi_result and pi_result.get("structure"):
            ci_tree = pageindex_to_citeindex_tree(
                pi_result=pi_result,
                doc_id=source_id,
                csl_data=csl_json,
                page_number_map={},  # URLs have no physical page numbers
                merkle_root=merkle_tree.get("root"),
                page_layouts=None,  # URL articles have no layout analysis
            )
            extra["pageindex_tree"] = ci_tree
            logger.info("PageIndex tree built for URL: %d sections",
                        len(ci_tree.get("level_1", [])))

    document_json: dict[str, Any] = {
        "source_id": source_id,
        "source_type": "url_article",
        "metadata": {
            "title": title,
            "url": final_url,
            "author": csl_extra.get("author"),
            "publication_date": date,
        },
        "structure": {
            "pages": pages,
            "section_tree": section_tree,
        },
        "nodes": nodes,
    }

    return PipelineResult(
        status="ok",
        source_id=source_id,
        resource_type="url_article",
        csl_json=csl_json,
        document_json=document_json,
        merkle_tree=merkle_tree,
        extra=extra,
    )
