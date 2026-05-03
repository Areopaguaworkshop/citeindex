import logging
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
    logger.info("Running scanned PDF backend: %s", backend_name)
    return backend(pdf_path, source_type="scanned_pdf", config=cfg)
