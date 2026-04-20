"""PageIndex tree-building pipeline.

Uses VectifyAI/PageIndex (vendored) to build LLM-driven section hierarchies,
then converts the output to CiteIndex's PageIndexTree JSON format.

Data flow:
    PDF ──→ PageIndex page_index_main() ──→ PageIndex tree
    MinerU ──→ page_extractor ──→ page_number_map
    GROBID ──→ csl.json
                ↓
    pageindex_to_citeindex_tree()  ──→  {doc_id}.citeindex.json
"""

import json
import logging
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Default model for PageIndex operations (separate from CiteIndex's LLM)
PAGEINDEX_MODEL = "ollama/qwen3.5:cloud"


def run_pageindex_tree(
    pdf_path: str,
    model: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Run PageIndex tree-building on a PDF.

    Returns the raw PageIndex result dict or None on failure.
    """
    try:
        from .pageindex import page_index_main
        from .pageindex.utils import ConfigLoader

        opt = ConfigLoader().load({
            "model": model or PAGEINDEX_MODEL,
            "if_add_node_id": "yes",
            "if_add_node_summary": "yes",
            "if_add_node_text": "no",
            "if_add_doc_description": "no",
        })

        logger.info("Running PageIndex tree-building on %s (model=%s)", pdf_path, opt.model)
        result = page_index_main(pdf_path, opt)
        structure = result.get("structure", [])
        logger.info("PageIndex tree built: %d top-level nodes", len(structure))
        return result
    except Exception:
        logger.warning("PageIndex tree-building failed, will fall back", exc_info=True)
        return None


def pageindex_to_citeindex_tree(
    pi_result: Dict[str, Any],
    doc_id: str,
    csl_data: Dict[str, Any],
    page_number_map: Dict[int, int],
    merkle_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Convert a PageIndex result to CiteIndex PageIndexTree JSON.

    Parameters
    ----------
    pi_result : dict
        Raw output from ``page_index_main()``.
    doc_id : str
        CiteIndex document ID (e.g. ``"Bai-Wengu-2003"``).
    csl_data : dict
        CSL-JSON metadata for level_0.
    page_number_map : dict
        Mapping from 1-based physical PDF page index → actual page number.
    merkle_root : str, optional
        Merkle root hash if available.

    Returns
    -------
    dict
        A CiteIndex PageIndexTree JSON structure matching the schema in
        ``citeindex-rs/crates/kernel/src/types/tree.rs``.
    """
    structure = pi_result.get("structure", [])
    level_1 = _convert_nodes(structure, doc_id, page_number_map, depth=0)

    return {
        "citeindex_version": "12.0",
        "tree_version": "1.0",
        "level_0": _build_level0(csl_data, doc_id, merkle_root),
        "level_1": level_1,
    }


def _convert_nodes(
    nodes: List[Dict[str, Any]],
    doc_id: str,
    page_number_map: Dict[int, int],
    depth: int,
) -> List[Dict[str, Any]]:
    """Recursively convert PageIndex nodes to CiteIndex section/subsection/locator nodes."""
    result = []
    for node in nodes:
        pi_node_id = node.get("node_id", "")
        title = node.get("title", "")
        start_idx = node.get("start_index")
        end_idx = node.get("end_index")
        summary = node.get("summary")
        children_raw = node.get("nodes", [])

        start_page = _map_page(start_idx, page_number_map)
        end_page = _map_page(end_idx, page_number_map)
        page_range = _format_page_range(start_page, end_page)

        ci_node_id = f"{doc_id}:section:{pi_node_id}" if pi_node_id else f"{doc_id}:section:{title}"

        if depth == 0:
            # Level 1: SectionNode
            if children_raw:
                children = _convert_nodes(children_raw, doc_id, page_number_map, depth=1)
            else:
                # Leaf section → create a single subsection with a locator
                children = [_make_leaf_subsection(ci_node_id, title, start_page, end_page)]

            section = {
                "node_id": ci_node_id,
                "heading": title,
                "section_number": None,
                "section_type": "section",
                "page_range": page_range,
                "children": children,
            }
            if summary:
                section["summary"] = summary
            result.append(section)

        elif depth == 1:
            # Level 2: SubsectionNode
            if children_raw:
                locators = _convert_nodes(children_raw, doc_id, page_number_map, depth=2)
            else:
                locators = [_make_locator(ci_node_id, start_page, end_page)]

            subsection = {
                "node_id": ci_node_id,
                "heading": title,
                "section_number": None,
                "children": locators,
            }
            if summary:
                subsection["summary"] = summary
            result.append(subsection)

        else:
            # Level 3+: LocatorNode (flatten deeper nesting into locators)
            locator = _make_locator(ci_node_id, start_page, end_page)
            if summary:
                locator["text"] = summary
            result.append(locator)

            # Flatten any deeper children as additional locators
            if children_raw:
                result.extend(_convert_nodes(children_raw, doc_id, page_number_map, depth=2))

    return result


def _make_leaf_subsection(
    parent_id: str, title: str,
    start_page: Optional[int], end_page: Optional[int],
) -> Dict[str, Any]:
    """Create a subsection node for a leaf section (no children in PageIndex tree)."""
    return {
        "node_id": f"{parent_id}:sub",
        "heading": title,
        "section_number": None,
        "children": [_make_locator(f"{parent_id}:loc", start_page, end_page)],
    }


def _make_locator(
    node_id: str,
    start_page: Optional[int],
    end_page: Optional[int],
) -> Dict[str, Any]:
    """Create a LocatorNode."""
    return {
        "node_id": f"{node_id}:loc" if not node_id.endswith(":loc") else node_id,
        "locator_type": "page",
        "page_number": start_page,
        "page_label": str(start_page) if start_page else None,
        "text_blocks": [],
        "figures": [],
        "tables": [],
        "paragraph_number": None,
        "paragraph_id": None,
        "text": None,
        "start_time": None,
        "end_time": None,
        "speaker": None,
        "transcript_text": None,
        "children": [],
    }


def _map_page(physical_idx: Optional[int], page_number_map: Dict[int, int]) -> Optional[int]:
    """Map a 1-based physical page index to an actual page number."""
    if physical_idx is None:
        return None
    # PageIndex uses 1-based physical indices
    # page_number_map keys are 0-based (from page_extractor.py)
    mapped = page_number_map.get(physical_idx - 1)
    if mapped is not None:
        return mapped
    # Fall back: page_number_map might be 1-based
    mapped = page_number_map.get(physical_idx)
    if mapped is not None:
        return mapped
    # No mapping available — return the physical index as-is
    return physical_idx


def _format_page_range(start: Optional[int], end: Optional[int]) -> Optional[str]:
    if start is None:
        return None
    if end is None or end == start:
        return str(start)
    return f"{start}-{end}"


def _build_level0(
    csl_data: Dict[str, Any],
    doc_id: str,
    merkle_root: Optional[str] = None,
) -> Dict[str, Any]:
    """Build Level 0 (CSL metadata root) for the CiteIndex PageIndexTree."""
    return {
        "id": csl_data.get("id", doc_id),
        "type": csl_data.get("type", "article-journal"),
        "title": csl_data.get("title", doc_id),
        "author": csl_data.get("author", []),
        "editor": csl_data.get("editor", []),
        "issued": csl_data.get("issued"),
        "DOI": csl_data.get("DOI") or csl_data.get("doi"),
        "ISBN": csl_data.get("ISBN"),
        "URL": csl_data.get("URL"),
        "container-title": csl_data.get("container-title") or csl_data.get("container_title"),
        "volume": csl_data.get("volume"),
        "issue": csl_data.get("issue"),
        "page": csl_data.get("page"),
        "publisher": csl_data.get("publisher"),
        "publisher-place": csl_data.get("publisher-place"),
        "abstract": csl_data.get("abstract"),
        "language": csl_data.get("language"),
        "keyword": csl_data.get("keyword"),
        "ci_doc_id": doc_id,
        "ci_quality_tier": csl_data.get("ci_quality_tier", "silver"),
        "ci_hierarchy_path": csl_data.get("ci_hierarchy_path"),
        "ci_merkle_hash": merkle_root,
        "ci_source_type": csl_data.get("source_type"),
        "ci_ingested_at": csl_data.get("ingestion_timestamp"),
        "ci_structure_confidence": None,
        "ci_indexed_at": None,
        "ci_project_ids": [],
        "ci_claim_anchors": [],
    }
