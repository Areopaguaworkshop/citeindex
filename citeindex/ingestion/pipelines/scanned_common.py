import logging
import os
import tempfile
from typing import Any

from ..deterministic import build_hierarchical_merkle_tree
from ..models import IngestionConfig, PipelineResult
from .common import (
    attach_evidence_locators,
    build_merkle_for_nodes,
    build_nodes_with_granularity,
    determine_doc_type,
    doc_type_to_csl_type,
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


def build_page_number_map_from_content_list(content_list: list[dict[str, Any]]) -> dict[int, int]:
    page_number_map = extract_page_numbers_from_content_list(content_list)
    if page_number_map:
        return page_number_map

    max_page_idx = max((item.get("page_idx", 0) for item in content_list), default=-1)
    return {idx: idx + 1 for idx in range(max_page_idx + 1)}


def _heading_level_for_item(item: dict[str, Any], text: str) -> int:
    explicit_level = item.get("heading_level")
    if isinstance(explicit_level, int) and explicit_level > 0:
        return max(1, min(explicit_level, 6))

    text_level = item.get("text_level")
    if isinstance(text_level, int) and text_level > 0:
        return max(1, min(text_level, 6))

    return _infer_heading_level(text)


def _image_overlap_ratio(item_bbox: list[float], image_bbox: list[float]) -> float:
    if len(item_bbox) < 4 or len(image_bbox) < 4:
        return 0.0

    overlap_x = max(0.0, min(item_bbox[2], image_bbox[2]) - max(item_bbox[0], image_bbox[0]))
    overlap_y = max(0.0, min(item_bbox[3], image_bbox[3]) - max(item_bbox[1], image_bbox[1]))
    area_item = max(0.0, (item_bbox[2] - item_bbox[0]) * (item_bbox[3] - item_bbox[1]))
    if area_item <= 0:
        return 0.0

    return (overlap_x * overlap_y) / area_item


def attach_image_paths_to_content_list(
    content_list: list[dict[str, Any]],
    images: list[dict[str, Any]] | None,
) -> None:
    if not images:
        return

    for item in content_list:
        if item.get("type") != "image" or item.get("image_path"):
            continue

        page_idx = item.get("page_idx", 0)
        item_bbox = item.get("bbox", [])
        fallback_path: str | None = None

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
    content_list: list[dict[str, Any]],
    page_number_map: dict[int, int],
) -> str:
    lines: list[str] = []
    current_page: int | None = None

    for item in content_list:
        page_idx = item.get("page_idx", 0)
        page_number = page_number_map.get(page_idx, page_idx + 1)
        if page_number != current_page:
            if current_page is not None:
                lines.append("")
            lines.append(f"======page:{page_number} ========")
            lines.append("")
            current_page = page_number

        item_type = item.get("type", "")
        raw_text = item.get("text")
        text = raw_text.strip() if isinstance(raw_text, str) else ""
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
    content_list: list[dict[str, Any]],
    document_structure: dict[str, Any],
    page_paragraphs: list[tuple[int, list[str]]],
    markdown_text: str | None = None,
    images_tmpdir: str | None = None,
    images_list: list[dict[str, Any]] | None = None,
) -> PipelineResult:
    source_id = make_source_id(pdf_path)
    import fitz
    with fitz.open(pdf_path) as pdf:
        num_pages = pdf.page_count
        page_indices = {item.get("page_idx") for item in content_list}
        pdf_labels = {
            index: pdf[index].get_label() for index in page_indices
            if isinstance(index, int) and 0 <= index < num_pages
        } if pdf.get_page_labels() else {}
    logger.info("[%s] Building result: %d pages, determining doc type...", backend_name, num_pages)
    doc_type = config.doc_type_override or determine_doc_type(pdf_path, num_pages)
    detected_page_numbers = extract_page_numbers_from_content_list(content_list)
    page_number_map = detected_page_numbers or build_page_number_map_from_content_list(content_list)
    normalized_markdown = markdown_text or content_list_to_markdown(content_list, page_number_map)

    from .dspy_extract import select_candidate_regions
    source_blocks = [
        {"id": f"ocr_{i}", "text": item["text"], "physical_page_index": item.get("page_idx"),
         "role": item.get("type", "text")}
        for i, item in enumerate(content_list) if isinstance(item.get("text"), str) and item["text"].strip()
    ]
    labels = {
        index: {"printed_page_label": str(label), "page_label_source": "ocr-sequence"}
        for index, label in detected_page_numbers.items()
    }
    labels.update({
        index: {"printed_page_label": label, "page_label_source": "pdf-page-label"}
        for index, label in pdf_labels.items() if label
    })
    for block in source_blocks:
        block.update(labels.get(block["physical_page_index"], {}))
    pi_result = None
    if config.use_pageindex and not config.pages and normalized_markdown.strip():
        try:
            pi_result = run_pageindex_md_tree(normalized_markdown, model=config.pageindex_model)
        except Exception:
            logger.warning("PageIndex failed; using OCR blocks without tree hints", exc_info=True)
    regions = select_candidate_regions(pi_result)
    # Markdown indices are not PDF coordinates. Use only headings to rank OCR text.
    region_hints = [r["heading"] for r in regions if r["role"] in {"imprint", "front_matter"}]
    logger.info("[%s] Extracting metadata via DSPy...", backend_name)
    extracted_csl = extract_metadata_with_dspy_priority(
        content_list=content_list,
        normalized_markdown=normalized_markdown,
        doc_type=doc_type,
        config=config,
        source_blocks=source_blocks,
        region_hints=region_hints,
        total_pages=num_pages,
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
    from ..csl import evaluation_fields
    supported = csl.get("_field_evidence", {})
    csl["_field_status"] = {
        field: "source-supported" if field in supported else "unverified" if csl.get(field) is not None else "missing"
        for field in evaluation_fields(csl, "scanned_pdf")
    }
    if csl.get("title") == os.path.basename(pdf_path):
        csl["_field_status"]["title"] = "provisional-filename"
    csl["_citation_status"] = "source-supported" if (
        csl["_field_status"].get("title") == "source-supported" and
        any(csl["_field_status"].get(role) == "source-supported" for role in ("author", "editor")) and
        csl["_field_status"].get("issued") == "source-supported"
    ) else "incomplete"

    logger.info("[%s] Building content nodes and Merkle tree...", backend_name)
    if config.pages:
        # A scoped record is anchored to the file digest plus the page window;
        # nodes only exist to feed the Merkle root, so neither is built.
        nodes: list[dict[str, Any]] = []
        merkle_tree: dict[str, Any] | None = None
    else:
        nodes = build_nodes_with_granularity(source_id, page_paragraphs, is_primary=config.is_primary)
        merkle_tree = build_merkle_for_nodes(nodes)
        if document_structure.get("pages"):
            hierarchical = build_hierarchical_merkle_tree(document_structure)
            merkle_tree["hierarchical_root"] = hierarchical.get("root")
            merkle_tree["proof_tree"] = hierarchical.get("proof_tree")

    extra: dict[str, Any] = {}
    extra["source_blocks"] = source_blocks
    # Layout cue capture (pages 1-6) needs the OCR geometry.  The key is
    # private so master.py can strip it before anything is persisted.
    extra["_layout_content_list"] = content_list
    if config.use_pageindex and not config.pages and normalized_markdown.strip():
        try:
            logger.info("[%s] Running PageIndex tree generation...", backend_name)
            if pi_result and pi_result.get("structure"):
                ci_tree = pageindex_to_citeindex_tree(
                    pi_result=pi_result,
                    doc_id=source_id,
                    csl_data=csl,
                    page_number_map=page_number_map,
                    merkle_root=(merkle_tree or {}).get("root"),
                    page_layouts=None,  # scanned PDFs don't have layout analysis
                )
                extra["pageindex_tree"] = ci_tree
                extra["candidate_regions"] = [{"heading": r["heading"], "role": r["role"]} for r in regions]
                if not document_structure.get("section_tree"):
                    _annotate_document_with_pageindex(document_structure, ci_tree)
        except Exception:
            logger.warning("PageIndex failed for scanned backend", exc_info=True)

    for page in document_structure.get("pages", []):
        physical_index = page.get("physical_page_index", page.get("page_idx"))
        page.update(labels.get(physical_index, {}))
    attach_evidence_locators(document_structure, nodes)

    if images_tmpdir and images_list:
        extra["_images_tmpdir"] = images_tmpdir
        extra["_images_list"] = images_list

    document_json: dict[str, Any] = {
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
