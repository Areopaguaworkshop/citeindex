import logging
import os
from typing import Callable, Optional

from ..models import IngestionConfig, PipelineResult

logger = logging.getLogger(__name__)


def _resolve_backend(name: str) -> Callable[[str, str, Optional[IngestionConfig]], PipelineResult]:
    if name == "mineru":
        from . import mineru
        return mineru.run
    if name == "glm-ocr":
        from . import glm_ocr
        return glm_ocr.run
    raise ValueError(f"Unsupported scanned OCR engine: {name}")


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
