import logging
import os
from typing import Callable, Dict, Optional

from ..models import IngestionConfig, PipelineResult

logger = logging.getLogger(__name__)


def _resolve_backend(name: str) -> Callable[[str, str, Optional[IngestionConfig]], PipelineResult]:
    backends: Dict[str, Callable[[str, str, Optional[IngestionConfig]], PipelineResult]] = {}

    from . import mineru
    from . import glm_ocr

    backends["mineru"] = mineru.run
    backends["glm-ocr"] = glm_ocr.run

    try:
        return backends[name]
    except KeyError as exc:
        raise ValueError(f"Unsupported scanned OCR engine: {name}") from exc


def run(
    pdf_path: str,
    config: Optional[IngestionConfig] = None,
) -> PipelineResult:
    cfg = config or IngestionConfig()
    backend_name = cfg.ocr_engine or "mineru"
    backend = _resolve_backend(backend_name)
    logger.info("Starting scanned PDF pipeline: backend=%s, file=%s", backend_name, os.path.basename(pdf_path))
    result = backend(pdf_path, source_type="scanned_pdf", config=cfg)
    logger.info("Scanned PDF pipeline completed: backend=%s", backend_name)
    return result
