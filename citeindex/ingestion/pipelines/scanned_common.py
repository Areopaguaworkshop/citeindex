import logging
import os
import tempfile
from typing import Any, Dict, List, Optional, Tuple

from ..deterministic import build_hierarchical_merkle_tree
from ..models import IngestionConfig, PipelineResult
from .common import (
    build_merkle_for_nodes,
    build_nodes_with_granularity,
    determine_doc_type,
    make_basic_csl,
    make_source_id,
)
from .digital_pdf import _annotate_document_with_pageindex
from .dspy_extract import (
    extract_metadata_with_dspy_priority,
    extract_page_numbers_from_content_list,
)
from .mineru import _infer_heading_level
from .pageindex_tree import pageindex_to_citeindex_tree, run_pageindex_md_tree

logger = logging.getLogger(__name__)


def doc_type_to_csl_type(doc_type: str) -> str:
    mapping = {
        "book": "book",
        "thesis": "thesis",
        "journal": "article-journal",
        "bookchapter": "chapter",
    }
    return mapping.get(doc_type, "document")


def build_page_number_map_from_content_list(content_list: List[Dict[str, Any]]) -> Dict[int, int]:
    page_number_map = extract_page_numbers_from_content_list(content_list)
    if page_number_map:
        return page_number_map

    max_page_idx = max((item.get("page_idx", 0) for item in content_list), default=-1)
    return {idx: idx + 1 for idx in range(max_page_idx + 1)}


def _heading_level_for_item(item: Dict[str, Any], text: str) -> int:
    explicit_level = item.get("heading_level")
    if isinstance(explicit_level, int) and explicit_level > 0:
        return max(1, min(explicit_level, 6))

    text_level = item.get("text_level")
    if isinstance(text_level, int) and text_level > 0:
        return max(1, min(text_level, 6))

    return _infer_heading_level(text)


def _image_overlap_ratio(item_bbox: List[float], image_bbox: List[float]) -> float:
    if len(item_bbox) < 4 or len(image_bbox) < 4:
        return 0.0

    overlap_x = max(0.0, min(item_bbox[2], image_bbox[2]) - max(item_bbox[0], image_bbox[0]))
    overlap_y = max(0.0, min(item_bbox[3], image_bbox[3]) - max(item_bbox[1], image_bbox[1]))
    area_item = max(0.0, (item_bbox[2] - item_bbox[0]) * (item_bbox[3] - item_bbox[1]))
    if area_item <= 0:
        return 0.0

    return (overlap_x * overlap_y) / area_item


def attach_image_paths_to_content_list(
    content_list: List[Dict[str, Any]],
    images: Optional[List[Dict[str, Any]]],
) -> None:
    if not images:
        return

    for item in content_list:
        if item.get("type") != "image" or item.get("image_path"):
            continue

        page_idx = item.get("page_idx", 0)
        item_bbox = item.get("bbox", [])
        fallback_path: Optional[str] = None

        for image in images:
            if image.get("page_idx") != page_idx:
                continue

            fallback_path = fallback_path or image.get("relative_path")
            image_bbox = image.get("bbox", [])
            if _image_overlap_ratio(item_bbox, image_bbox) > 0.5:
                item["image_path"] = image.get("relative_path")
                break

        if not item.get("image_path") and fallback_path:
            item["image_path"] = fallback_path


def content_list_to_markdown(
    content_list: List[Dict[str, Any]],
    page_number_map: Dict[int, int],
) -> str:
    lines: List[str] = []
    current_page: Optional[int] = None

    for item in content_list:
        page_idx = item.get("page_idx", 0)
        page_number = page_number_map.get(page_idx, page_idx + 1)
        if page_number != current_page:
            if current_page is not None:
                lines.extend(["", "---", ""])
            lines.append(f"<!-- page:{page_number} -->")
            lines.append("")
            current_page = page_number

        item_type = item.get("type", "")
        text = (item.get("text") or "").strip()
        if not text and item_type != "image":
            continue

        if item_type == "discarded":
            continue

        if item.get("text_level") is not None and text:
            level = _heading_level_for_item(item, text)
            lines.append(f"{'#' * max(1, min(level, 6))} {text}")
            lines.append("")
            continue

        if item_type == "image":
            image_path = item.get("image_path")
            if image_path:
                alt = text or "image"
                lines.append(f"![{alt}]({image_path})")
                lines.append("")
            elif text:
                lines.append(f"*{text}*")
                lines.append("")
            continue

        if text:
            lines.append(text)
            lines.append("")

    return "\n".join(lines).strip()


def build_scanned_pipeline_result(
    *,
    pdf_path: str,
    backend_name: str,
    source_type: str,
    config: IngestionConfig,
    content_list: List[Dict[str, Any]],
    document_structure: Dict[str, Any],
    page_paragraphs: List[Tuple[int, List[str]]],
    markdown_text: Optional[str] = None,
    images_tmpdir: Optional[str] = None,
    images_list: Optional[List[Dict[str, Any]]] = None,
) -> PipelineResult:
    source_id = make_source_id(pdf_path)
    num_pages = max(len(document_structure.get("pages", [])), len(page_paragraphs))
    doc_type = config.doc_type_override or determine_doc_type(pdf_path, num_pages)
    page_number_map = build_page_number_map_from_content_list(content_list)
    normalized_markdown = markdown_text or content_list_to_markdown(content_list, page_number_map)

    extracted_csl = extract_metadata_with_dspy_priority(
        content_list=content_list,
        normalized_markdown=normalized_markdown,
        doc_type=doc_type,
        config=config,
    )
    initial_title = extracted_csl.get("title") or os.path.basename(pdf_path)
    csl = make_basic_csl(
        source_id=source_id,
        title=initial_title,
        csl_type=doc_type_to_csl_type(doc_type),
        extra={"genre": source_type, "_extraction_backend": backend_name},
    )
    for key, value in extracted_csl.items():
        if key == "id":
            continue
        if value is not None:
            csl[key] = value

    nodes = build_nodes_with_granularity(source_id, page_paragraphs, is_primary=config.is_primary)
    merkle_tree = build_merkle_for_nodes(nodes)
    if document_structure.get("pages"):
        hierarchical = build_hierarchical_merkle_tree(document_structure)
        merkle_tree["hierarchical_root"] = hierarchical.get("root")
        merkle_tree["proof_tree"] = hierarchical.get("proof_tree")

    extra: Dict[str, Any] = {}
    if config.use_pageindex and normalized_markdown.strip():
        try:
            pi_result = run_pageindex_md_tree(normalized_markdown, model=config.pageindex_model)
            if pi_result and pi_result.get("structure"):
                ci_tree = pageindex_to_citeindex_tree(
                    pi_result=pi_result,
                    doc_id=source_id,
                    csl_data=csl,
                    page_number_map=page_number_map,
                    merkle_root=merkle_tree.get("root"),
                )
                extra["pageindex_tree"] = ci_tree
                if not document_structure.get("section_tree"):
                    _annotate_document_with_pageindex(document_structure, ci_tree)
        except Exception:
            logger.warning("PageIndex failed for scanned backend", exc_info=True)

    if images_tmpdir and images_list:
        extra["_images_tmpdir"] = images_tmpdir
        extra["_images_list"] = images_list

    document_json: Dict[str, Any] = {
        "source_id": source_id,
        "source_type": source_type,
        "metadata": {
            "title": csl.get("title") or initial_title,
            "page_count": num_pages,
            "source_path": os.path.abspath(pdf_path),
            "ocr_engine": backend_name,
            "ocr_backend": backend_name,
            "document_type": doc_type,
        },
        "structure": document_structure,
        "nodes": nodes,
    }

    if backend_name == "glm-ocr":
        document_json["metadata"]["ocr_model"] = config.ocr_model
        document_json["metadata"]["ollama_host"] = config.ollama_host
    if backend_name == "mineru":
        document_json["metadata"]["mineru_backend"] = config.mineru_backend

    return PipelineResult(
        status="ok",
        source_id=source_id,
        resource_type=source_type,
        csl_json=csl,
        document_json=document_json,
        merkle_tree=merkle_tree,
        extra=extra,
    )


def make_images_tmpdir(prefix: str) -> str:
    return tempfile.mkdtemp(prefix=prefix)
