import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import requests
import trafilatura

from ..models import PipelineResult
from .common import (
    build_merkle_for_nodes,
    build_nodes,
    build_retrieval_index,
    make_basic_csl,
    make_source_id,
    split_paragraphs,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Fetching: Playwright primary, requests fallback
# ---------------------------------------------------------------------------

def _fetch_with_playwright(url: str) -> Optional[str]:
    """Fetch a URL rendering JavaScript via Playwright."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=30000)
            html = page.content()
            browser.close()
            return html
    except Exception:
        logger.info("Playwright fetch failed or not installed, falling back to requests")
        return None


def _fetch_with_requests(url: str) -> str:
    """Simple HTTP GET fallback."""
    resp = requests.get(url, timeout=30, headers={"User-Agent": "CiteIndex/0.11"})
    resp.raise_for_status()
    return resp.text


def _fetch_html(url: str) -> str:
    """Fetch URL with Playwright primary, requests fallback."""
    html = _fetch_with_playwright(url)
    if html:
        return html
    return _fetch_with_requests(url)


# ---------------------------------------------------------------------------
# Content extraction: trafilatura primary, readability-lxml fallback
# ---------------------------------------------------------------------------

def _extract_content(html: str) -> str:
    """Extract main content, with readability-lxml fallback."""
    extracted = trafilatura.extract(html)
    if extracted and extracted.strip():
        return extracted

    logger.info("trafilatura returned empty, trying readability-lxml fallback")
    try:
        from readability import Document

        doc = Document(html)
        summary_html = doc.summary()
        # Strip HTML tags for plain text
        from lxml import etree

        tree = etree.HTML(summary_html)
        text = " ".join(tree.itertext()).strip() if tree is not None else ""
        if text:
            return text
    except Exception:
        logger.info("readability-lxml fallback failed or not installed")

    return html


# ---------------------------------------------------------------------------
# Metadata extraction: zotero-translator or trafilatura
# ---------------------------------------------------------------------------

def _extract_metadata_zotero(url: str) -> Dict[str, Any]:
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
                meta: Dict[str, Any] = {}
                if item.get("title"):
                    meta["title"] = item["title"]
                if item.get("creators"):
                    authors = []
                    for c in item["creators"]:
                        if c.get("lastName"):
                            name: Dict[str, str] = {"family": c["lastName"]}
                            if c.get("firstName"):
                                name["given"] = c["firstName"]
                            authors.append(name)
                    if authors:
                        meta["author"] = authors
                if item.get("date"):
                    meta["date"] = item["date"]
                if item.get("publicationTitle"):
                    meta["container-title"] = item["publicationTitle"]
                if item.get("language"):
                    meta["language"] = item["language"]
                return meta
    except Exception:
        logger.info("zotero-translator not available, falling back to trafilatura metadata")
    return {}


def _extract_metadata(html: str, url: str) -> Dict[str, Any]:
    """Extract metadata with zotero primary, trafilatura fallback."""
    meta = _extract_metadata_zotero(url)
    if meta.get("title"):
        return meta

    metadata_obj = trafilatura.extract_metadata(html)
    return {
        "title": (metadata_obj.title if metadata_obj else None) or url,
        "author": metadata_obj.author if metadata_obj else None,
        "date": metadata_obj.date if metadata_obj else None,
    }


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

def run(url: str) -> PipelineResult:
    source_id = make_source_id(url)
    html = _fetch_html(url)

    extracted_text = _extract_content(html)
    metadata = _extract_metadata(html, url)

    title = metadata.get("title") or url
    author = metadata.get("author")
    date = metadata.get("date")

    paragraphs = split_paragraphs(extracted_text)
    page_paragraphs = [(1, paragraphs)]
    nodes = build_nodes(source_id, page_paragraphs)
    merkle_tree = build_merkle_for_nodes(nodes)
    retrieval_index = build_retrieval_index(nodes)

    csl_extra: Dict[str, Any] = {
        "URL": url,
        "accessed": {
            "date-parts": [
                [
                    datetime.now(timezone.utc).year,
                    datetime.now(timezone.utc).month,
                    datetime.now(timezone.utc).day,
                ]
            ]
        },
    }

    # Author handling — supports both zotero structured and trafilatura string
    if author:
        if isinstance(author, list):
            csl_extra["author"] = author
        else:
            csl_extra["author"] = [{"literal": author}]

    if date:
        if isinstance(date, str) and len(date) >= 4 and date[:4].isdigit():
            csl_extra["issued"] = {"date-parts": [[int(date[:4])]]}

    container_title = metadata.get("container-title")
    if container_title:
        csl_extra["container-title"] = container_title

    csl_json = make_basic_csl(source_id, title, "webpage", csl_extra)

    document_json: Dict[str, Any] = {
        "source_id": source_id,
        "source_type": "url_article",
        "metadata": {
            "title": title,
            "url": url,
            "author": author,
            "publication_date": date,
        },
        "structure": {
            "sections": [
                {
                    "section_id": "sec1",
                    "title": title,
                    "paragraphs": [
                        {"paragraph_id": f"p{i+1}", "text": paragraph}
                        for i, paragraph in enumerate(paragraphs)
                    ],
                }
            ]
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
        retrieval_index=retrieval_index,
    )
