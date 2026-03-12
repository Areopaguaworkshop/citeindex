"""MinerU (magic-pdf) integration for layout analysis.

Invokes MinerU via CLI subprocess to produce:
  - middle JSON  (block-level layout with bboxes)
  - markdown     (reading-order text)
  - content_list (ordered content items)

Falls back to the legacy fitz-based layout analysis when MinerU is unavailable.
"""

import json
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


def _resolve_mineru_cli() -> Optional[str]:
    """Return the available MinerU CLI executable name, if any."""
    for cli_name in ("magic-pdf", "mineru"):
        if shutil.which(cli_name):
            return cli_name
    return None


def is_mineru_available() -> bool:
    """Check whether a supported MinerU CLI is on PATH."""
    return _resolve_mineru_cli() is not None


def run_mineru(
    pdf_path: str,
    output_dir: Optional[str] = None,
    parse_method: str = "auto",
) -> Dict[str, Any]:
    """Run MinerU on a PDF and return parsed outputs.

    Parameters
    ----------
    pdf_path : str
        Absolute path to the input PDF.
    output_dir : str, optional
        Directory for MinerU output.  A temp dir is created when *None*.
    parse_method : str
        ``"auto"`` (default), ``"ocr"``, or ``"txt"``.

    Returns
    -------
    dict with keys:
        ``middle_json``  – parsed middle JSON (list of page dicts)
        ``markdown``     – full markdown text
        ``content_list`` – parsed content_list.json (list of items)
        ``output_dir``   – path to the MinerU output directory
    """
    mineru_cli = _resolve_mineru_cli()
    if not mineru_cli:
        raise RuntimeError("MinerU CLI not found on PATH (expected 'magic-pdf' or 'mineru')")

    pdf_path = os.path.abspath(pdf_path)
    if not os.path.isfile(pdf_path):
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    cleanup_temp = False
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="mineru_")
        cleanup_temp = True

    try:
        cmd = [
            mineru_cli,
            "-p", pdf_path,
            "-o", output_dir,
            "-m", parse_method,
        ]
        logger.info("Running MinerU: %s", " ".join(cmd))

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=300,
        )

        if result.returncode != 0:
            logger.error("MinerU failed (rc=%d): %s", result.returncode, result.stderr)
            raise RuntimeError(f"MinerU failed: {result.stderr[:500]}")

        return _collect_mineru_outputs(output_dir, pdf_path)

    except subprocess.TimeoutExpired:
        raise RuntimeError("MinerU timed out after 300s")
    except Exception:
        if cleanup_temp and os.path.isdir(output_dir):
            shutil.rmtree(output_dir, ignore_errors=True)
        raise


def _collect_mineru_outputs(
    output_dir: str,
    pdf_path: str,
) -> Dict[str, Any]:
    """Walk the MinerU output tree and load the key artifacts."""

    pdf_stem = Path(pdf_path).stem
    # MinerU writes to <output_dir>/<pdf_stem>/{auto,ocr,txt}/
    base = Path(output_dir)

    # Find the method subdirectory (auto / ocr / txt)
    candidates = list(base.glob(f"{pdf_stem}/*"))
    if not candidates:
        # Sometimes MinerU nests one level deeper
        candidates = list(base.glob(f"**/{pdf_stem}/*"))
    if not candidates:
        raise FileNotFoundError(
            f"No MinerU output found under {output_dir} for {pdf_stem}"
        )

    method_dir = candidates[0]

    middle_json = _load_json(method_dir / f"{pdf_stem}_middle.json")
    content_list = _load_json(method_dir / f"{pdf_stem}_content_list.json")
    markdown = _load_text(method_dir / f"{pdf_stem}.md")

    return {
        "middle_json": middle_json,
        "markdown": markdown,
        "content_list": content_list,
        "output_dir": str(method_dir),
    }


# ---------------------------------------------------------------------------
# Helpers to convert MinerU structures into the pipeline's page-layout format
# ---------------------------------------------------------------------------

def mineru_to_page_layouts(middle_json: Any) -> List[Dict[str, Any]]:
    """Convert MinerU middle JSON into the ``page_layouts`` list format
    expected by ``build_layout_document_structure`` and downstream code.

    Each page dict has:
        page_number, columns, footnotes, ordered_text
    """
    pdf_info = middle_json.get("pdf_info", []) if isinstance(middle_json, dict) else middle_json
    if not pdf_info:
        return []

    page_layouts: List[Dict[str, Any]] = []
    for page_info in pdf_info:
        page_idx = page_info.get("page_idx", 0)
        page_number = page_idx + 1

        paragraphs: List[Dict[str, Any]] = []
        footnotes: List[Dict[str, Any]] = []
        ordered_texts: List[str] = []

        for block in page_info.get("para_blocks", []):
            text = _extract_block_text(block)
            if not text:
                continue

            block_type = block.get("type", "text")
            bbox = block.get("bbox", [])

            if block_type == "footnote":
                footnotes.append({
                    "footnote_id": f"p{page_number}_fn{len(footnotes) + 1}",
                    "text": text,
                    "bbox": bbox,
                })
            else:
                paragraphs.append({
                    "paragraph_id": f"p{page_number}_c0_para{len(paragraphs) + 1}",
                    "text": text,
                    "lines": [{"text": line, "bbox": []} for line in text.split("\n") if line.strip()],
                    "bbox": bbox,
                })

            ordered_texts.append(text)

        page_layouts.append({
            "page_number": page_number,
            "columns": [{
                "column_id": 0,
                "paragraphs": paragraphs,
            }] if paragraphs else [],
            "footnotes": footnotes,
            "ordered_text": "\n\n".join(ordered_texts),
        })

    return page_layouts


def mineru_to_paragraphs(
    middle_json: Any,
) -> List[Tuple[int, List[str]]]:
    """Convert MinerU middle JSON to ``(page_number, [paragraph_texts])``."""
    layouts = mineru_to_page_layouts(middle_json)
    result: List[Tuple[int, List[str]]] = []
    for pl in layouts:
        texts: List[str] = []
        for col in pl.get("columns", []):
            for para in col.get("paragraphs", []):
                t = para.get("text", "").strip()
                if t:
                    texts.append(t)
        for fn in pl.get("footnotes", []):
            t = fn.get("text", "").strip()
            if t:
                texts.append(t)
        result.append((pl["page_number"], texts))
    return result


def _extract_block_text(block: Dict[str, Any]) -> str:
    """Extract text from a MinerU para_block (handles spans/lines structure)."""
    # Direct text field
    if "text" in block and block["text"]:
        return block["text"].strip()

    # Lines → spans → content (standard MinerU middle JSON structure)
    lines = block.get("lines", [])
    parts: List[str] = []
    for line in lines:
        if isinstance(line, dict):
            spans = line.get("spans", [])
            for span in spans:
                if isinstance(span, dict) and "content" in span:
                    parts.append(span["content"])
                elif isinstance(span, str):
                    parts.append(span)
            if not spans and "text" in line:
                parts.append(line["text"])
        elif isinstance(line, str):
            parts.append(line)

    return "\n".join(parts).strip()


def _load_json(path: Path) -> Any:
    """Load JSON file, return empty dict on failure."""
    try:
        if path.is_file():
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        logger.warning("Failed to load JSON: %s", path)
    return {}


def _load_text(path: Path) -> str:
    """Load text file, return empty string on failure."""
    try:
        if path.is_file():
            return path.read_text(encoding="utf-8")
    except Exception:
        logger.warning("Failed to load text: %s", path)
    return ""
