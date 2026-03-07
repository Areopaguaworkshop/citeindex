from datetime import datetime, timezone
from typing import Any, Dict, List

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


def _extract_html(url: str) -> str:
    resp = requests.get(url, timeout=30)
    resp.raise_for_status()
    return resp.text


def run(url: str) -> PipelineResult:
    source_id = make_source_id(url)
    html = _extract_html(url)

    extracted_text = trafilatura.extract(html) or ""
    if not extracted_text.strip():
        extracted_text = html

    metadata_obj = trafilatura.extract_metadata(html)
    title = (metadata_obj.title if metadata_obj else None) or url
    author = metadata_obj.author if metadata_obj else None
    date = metadata_obj.date if metadata_obj else None

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
    if author:
        csl_extra["author"] = [{"literal": author}]
    if date and len(date) >= 4 and date[:4].isdigit():
        csl_extra["issued"] = {"date-parts": [[int(date[:4])]]}

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
        extra={"html": html},
    )
