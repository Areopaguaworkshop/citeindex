"""GLM-OCR scanned PDF backend.

Normalizes layout-aware OCR output into the same MinerU-like ``content_list``
shape consumed by the shared scanned-document pipeline builder.
"""

import base64
import json
import logging
import os
import shutil
import urllib.error
import urllib.request
from typing import Any, Dict, Iterable, List, Optional, Tuple

import fitz
import numpy as np

from ..models import IngestionConfig, PipelineResult
from .common import make_source_id
from .mineru import content_list_to_document_structure, content_list_to_paragraphs

logger = logging.getLogger(__name__)

_LAYOUT_MODEL_NAME = "PP-DocLayout_plus-L"
_TEXT_LABELS = ("text", "paragraph", "reference", "formula", "caption", "list")
_HEADING_LABELS = ("title", "chapter", "section")
_TABLE_LABELS = ("table",)
_IMAGE_LABELS = ("figure", "image", "illustration", "picture")
_DISCARDED_LABELS = ("header", "footer", "page", "footnote")


def _get_layout_detector() -> Any:
    try:
        from paddleocr import LayoutDetection
    except ImportError as exc:
        raise RuntimeError(
            "GLM-OCR backend requires paddleocr LayoutDetection support. "
            "Install the PaddleOCR layout extras first."
        ) from exc

    return LayoutDetection(model_name=_LAYOUT_MODEL_NAME)


def _layout_result_to_dict(result: Any) -> Dict[str, Any]:
    payload = getattr(result, "json", result)
    if callable(payload):
        payload = payload()

    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except json.JSONDecodeError:
            return {}

    return payload if isinstance(payload, dict) else {}


def _detect_layout_boxes(detector: Any, image_array: np.ndarray) -> List[Dict[str, Any]]:
    raw_results = detector.predict(image_array, batch_size=1)
    if isinstance(raw_results, dict):
        raw_results = [raw_results]

    boxes: List[Dict[str, Any]] = []
    for result in raw_results:
        payload = _layout_result_to_dict(result)
        for box in payload.get("boxes", []):
            coordinate = box.get("coordinate") or box.get("bbox") or []
            if len(coordinate) < 4:
                continue

            label = str(box.get("label") or "text")
            boxes.append(
                {
                    "label": label.strip().lower(),
                    "coordinate": [float(coordinate[0]), float(coordinate[1]), float(coordinate[2]), float(coordinate[3])],
                    "score": float(box.get("score") or 0.0),
                }
            )

    return boxes


def _sort_layout_boxes(boxes: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(boxes, key=lambda box: (box["coordinate"][1], box["coordinate"][0]))


def _normalize_bbox(bbox: List[float], width: int, height: int) -> Optional[List[float]]:
    if len(bbox) < 4:
        return None

    x0 = max(0.0, min(float(bbox[0]), float(width)))
    y0 = max(0.0, min(float(bbox[1]), float(height)))
    x1 = max(0.0, min(float(bbox[2]), float(width)))
    y1 = max(0.0, min(float(bbox[3]), float(height)))
    if x1 <= x0 or y1 <= y0:
        return None

    return [x0, y0, x1, y1]


def _image_bbox_to_pdf_rect(page: fitz.Page, bbox: List[float], zoom: float) -> Optional[fitz.Rect]:
    if len(bbox) < 4:
        return None

    rect = fitz.Rect(bbox[0] / zoom, bbox[1] / zoom, bbox[2] / zoom, bbox[3] / zoom)
    clipped = rect & page.rect
    return clipped if not clipped.is_empty else None


def _label_matches(label: str, candidates: Tuple[str, ...]) -> bool:
    return any(candidate in label for candidate in candidates)


def _is_heading_label(label: str) -> bool:
    return _label_matches(label, _HEADING_LABELS)


def _is_table_label(label: str) -> bool:
    return _label_matches(label, _TABLE_LABELS)


def _is_image_label(label: str) -> bool:
    return _label_matches(label, _IMAGE_LABELS)


def _is_discarded_label(label: str) -> bool:
    return _label_matches(label, _DISCARDED_LABELS)


def _ocr_prompt_for_label(label: str) -> str:
    if _is_table_label(label):
        return "Table Recognition:"
    if _is_image_label(label):
        return "Figure Recognition:"
    return "Text Recognition:"


def _clean_ocr_response(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```") and cleaned.endswith("```"):
        cleaned = cleaned.strip("`").strip()
        if "\n" in cleaned:
            cleaned = cleaned.split("\n", 1)[1].strip()
    return cleaned.strip()


def _call_ollama_generate(image_bytes: bytes, prompt: str, config: IngestionConfig) -> str:
    payload = {
        "model": config.ocr_model,
        "prompt": prompt,
        "images": [base64.b64encode(image_bytes).decode("ascii")],
        "stream": False,
    }
    request = urllib.request.Request(
        f"{config.ollama_host.rstrip('/')}/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Failed to reach Ollama at {config.ollama_host}: {exc}") from exc

    return _clean_ocr_response(str(body.get("response") or ""))


def _ocr_crop(page: fitz.Page, rect: fitz.Rect, prompt: str, config: IngestionConfig) -> str:
    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect, alpha=False)
    image_bytes = pix.tobytes("png")
    return _call_ollama_generate(image_bytes, prompt, config)


def _save_image_crop(
    page: fitz.Page,
    rect: fitz.Rect,
    bbox: List[float],
    output_dir: str,
    source_id: str,
    page_idx: int,
    image_index: int,
) -> Dict[str, Any]:
    images_dir = os.path.join(output_dir, "images")
    os.makedirs(images_dir, exist_ok=True)

    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2), clip=rect, alpha=False)
    filename = f"{source_id}_p{page_idx + 1}_img{image_index}.png"
    filepath = os.path.join(images_dir, filename)
    pix.save(filepath)

    return {
        "page_idx": page_idx,
        "bbox": [float(v) for v in bbox],
        "relative_path": f"images/{filename}",
        "width": pix.width,
        "height": pix.height,
        "filename": filename,
    }


def _fallback_text_box(width: int, height: int) -> Dict[str, Any]:
    return {
        "label": "text",
        "coordinate": [0.0, 0.0, float(width), float(height)],
        "score": 1.0,
    }


def _extract_page_items(
    page: fitz.Page,
    page_idx: int,
    detector: Any,
    config: IngestionConfig,
    source_id: str,
    images_tmpdir: str,
    promote_title: bool,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], bool]:
    zoom = 2.0
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    image_array = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    layout_boxes = _detect_layout_boxes(detector, image_array)
    if not layout_boxes:
        layout_boxes = [_fallback_text_box(pix.width, pix.height)]

    content_items: List[Dict[str, Any]] = []
    images: List[Dict[str, Any]] = []
    image_index = 0
    title_promoted = False

    for box in _sort_layout_boxes(layout_boxes):
        bbox = _normalize_bbox(box["coordinate"], pix.width, pix.height)
        if not bbox:
            continue

        rect = _image_bbox_to_pdf_rect(page, bbox, zoom)
        if rect is None:
            continue

        label = box["label"]
        prompt = _ocr_prompt_for_label(label)

        if _is_image_label(label):
            image_index += 1
            images.append(_save_image_crop(page, rect, bbox, images_tmpdir, source_id, page_idx, image_index))
            figure_text = _ocr_crop(page, rect, prompt, config)
            item: Dict[str, Any] = {
                "type": "image",
                "page_idx": page_idx,
                "bbox": bbox,
            }
            if figure_text:
                item["text"] = figure_text
            content_items.append(item)
            continue

        text = _ocr_crop(page, rect, prompt, config)
        if not text:
            continue

        item_type = "discarded" if _is_discarded_label(label) else "text"
        item = {
            "type": item_type,
            "page_idx": page_idx,
            "bbox": bbox,
            "text": text,
        }

        if item_type == "text" and _is_heading_label(label):
            heading_level = 1 if promote_title and not title_promoted else 2
            item["text_level"] = heading_level
            item["heading_level"] = heading_level
            title_promoted = title_promoted or heading_level == 1

        content_items.append(item)

    return content_items, images, title_promoted


def run(
    pdf_path: str,
    source_type: str = "scanned_pdf",
    config: Optional[IngestionConfig] = None,
) -> PipelineResult:
    from .scanned_common import (
        attach_image_paths_to_content_list,
        build_page_number_map_from_content_list,
        build_scanned_pipeline_result,
        make_images_tmpdir,
    )

    cfg = config or IngestionConfig()
    detector = _get_layout_detector()
    source_id = make_source_id(pdf_path)
    images_tmpdir = make_images_tmpdir(prefix="citeindex_glm_imgs_")

    try:
        doc = fitz.open(pdf_path)
        total_pages = doc.page_count
        try:
            content_list: List[Dict[str, Any]] = []
            images_list: List[Dict[str, Any]] = []
            title_promoted = False

            logger.info("[glm-ocr] Step 1/4: Processing %d pages with GLM-OCR...", total_pages)
            for page_idx in range(total_pages):
                page_num = page_idx + 1
                if page_num == 1 or page_num % 5 == 0 or page_num == total_pages:
                    logger.info("[glm-ocr]   Processing page %d/%d...", page_num, total_pages)
                page = doc[page_idx]
                page_items, page_images, page_promoted = _extract_page_items(
                    page,
                    page_idx,
                    detector,
                    cfg,
                    source_id,
                    images_tmpdir,
                    promote_title=page_idx == 0 and not title_promoted,
                )
                content_list.extend(page_items)
                images_list.extend(page_images)
                title_promoted = title_promoted or page_promoted
        finally:
            doc.close()

        if not content_list:
            raise RuntimeError("GLM-OCR did not produce any content items")

        logger.info("[glm-ocr] Step 2/4: Extracting images and attaching to content...")

        logger.info("[glm-ocr] Step 2/4: Extracting images and attaching to content...")
        if not images_list:
            shutil.rmtree(images_tmpdir, ignore_errors=True)
            images_tmpdir = None

        attach_image_paths_to_content_list(content_list, images_list)
        page_number_map = build_page_number_map_from_content_list(content_list)
        logger.info("[glm-ocr] Step 3/4: Building document structure...")
        document_structure = content_list_to_document_structure(
            content_list,
            page_number_map,
            images=images_list,
        )
        page_paragraphs = content_list_to_paragraphs(content_list, page_number_map)

        logger.info("[glm-ocr] Step 4/4: Building pipeline result (metadata + PageIndex)...")
        result = build_scanned_pipeline_result(
            pdf_path=pdf_path,
            backend_name="glm-ocr",
            source_type=source_type,
            config=cfg,
            content_list=content_list,
            document_structure=document_structure,
            page_paragraphs=page_paragraphs,
            images_tmpdir=images_tmpdir,
            images_list=images_list,
        )
        if result.document_json is not None:
            result.document_json["metadata"]["original_pdf_path"] = os.path.abspath(pdf_path)
        return result
    except Exception:
        if images_tmpdir:
            shutil.rmtree(images_tmpdir, ignore_errors=True)
        raise
