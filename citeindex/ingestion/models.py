import math
import re
from dataclasses import asdict, dataclass, field
from typing import Any, Literal, get_args

# How much of the artifact set a run persists.  The Merkle tree and nodes are
# computed in every mode; only their persistence (and the optional stages that
# produce the other artifacts) is skipped, so ``csl.json`` — its ``id``,
# ``content_hash`` and ``merkle_root`` — is the same in all three modes.  A
# scoped run (``pages`` set) is the exception: it builds neither and anchors
# identity to the source file and page window instead.
OutputMode = Literal["full", "csl_evidence", "csl_only"]
OUTPUT_MODES: tuple[str, ...] = get_args(OutputMode)

# One comma-separated part of a page selection: "6", "1-6", or "-3".
_PAGE_PART_RE = re.compile(r"^(?:\d+\s*-\s*\d+|-\s*\d+|\d+)$")

# Upper bound used only to validate the shape of a selection at config
# construction, before a document's page count is known.  ``parse_page_range``
# needs a total to resolve "-3" and to clamp, so a spec is expanded against the
# largest page number it mentions (and no more than this cap, which no real PDF
# reaches) purely to prove that it selects something.
_PAGES_VALIDATION_BOUND = 100_000


def validate_pages_spec(pages: str) -> None:
    """Reject a page selection that is empty, malformed, or selects no page."""
    if not isinstance(pages, str) or not pages.strip():
        raise ValueError("pages must be a non-empty page range such as '1-6'")
    parts = [part.strip() for part in pages.split(",") if part.strip()]
    if not parts:
        raise ValueError(f"pages {pages!r} is empty; give a range such as '1-6'")
    for part in parts:
        if not _PAGE_PART_RE.match(part):
            raise ValueError(
                f"pages {pages!r} is invalid: {part!r} is not a page, a range like '1-6', "
                "or a trailing count like '-3'"
            )
    # Shape is proven above; parse_page_range is the single authority on which
    # pages a selection resolves to (clamping included).
    from ..utils import parse_page_range

    explicit = [int(value) for value in re.findall(r"\d+", pages)]
    total_pages = min(max(explicit or [1]) + 1, _PAGES_VALIDATION_BOUND)
    if not parse_page_range(pages, total_pages):
        raise ValueError(f"pages {pages!r} selects no page; use a range such as '1-6'")


@dataclass
class IngestionFailure:
    status: str
    stage: str
    source_id: str
    error_code: str
    error_message: str
    next_action: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IngestionLogEntry:
    input_ref: str
    resource_type: str
    csl_id: str
    merkle_root: str
    ingestion_timestamp: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class IngestionConfig:
    """Configuration parameters passed through the ingestion pipeline."""
    llm_model: str = "ollama/deepseek-v4.1-flash:cloud"
    ocr_engine: str = "mineru"
    ocr_model: str = "glm-ocr:latest"
    ollama_host: str = "http://localhost:11434"
    mineru_backend: str = "pipeline"
    mineru_timeout: int = 3600
    mineru_chunk_pages: int | str = "auto"
    text_direction: str = "horizontal"
    vertical_lang: str = "ch"
    lang: str = "auto"
    page_range: str = "1-10, -3"
    # Scoped extraction: process only these pages and anchor the record
    # identity to the source file instead of the page Merkle tree.
    pages: str | None = None
    citation_style: str = "chicago-author-date"
    doc_type_override: str | None = None
    use_layout_analysis: bool = True
    is_primary: bool = False
    output_mode: OutputMode = "full"
    use_pageindex: bool = True  # default changed to True
    use_llm_metadata: bool = True  # MCP/offline runs can disable DSPy metadata extraction
    pageindex_model: str = "ollama/deepseek-v4.1-flash:cloud"
    verify_citations: bool = False
    citation_verifier_model: str | None = None
    crossref_enabled: bool = True
    openlibrary_enabled: bool = True
    offline_verification: bool = False
    registry_contact_email: str | None = None
    force_pdf_kind: str | None = None  # 'force_ocr', 'force_digital', or None for auto-detect
    strip_existing_ocr: bool = False  # optionally reject existing OCR layers during classification
    repair_proposal: str | None = None  # JSON proposal file; applied through normal finalization
    # Identifier-first fill: authority data only for fields the source left empty.
    registry_fill: bool = False
    review_threshold: float = 0.80
    evidence_window: str = "range"
    zone_ranking: bool = True
    # Default-on release is gated on the independent enrichment benchmark.
    online_enrich: bool = False
    online_enrich_providers: tuple[str, ...] = ("crossref", "openalex", "openlibrary", "datacite")
    online_enrich_min_score: float = 0.90
    online_enrich_timeout: float = 20.0
    online_enrich_cache_ttl: float = 604800.0
    online_enrich_ai_fallback: bool = False
    online_enrich_ai_provider: str | None = None
    online_enrich_ai_model: str | None = None
    online_enrich_ai_domains: tuple[str, ...] = ("doi.org", "crossref.org", "openlibrary.org", "datacite.org")
    enrich_proposal: str | None = None
    media_asr_backend: str = "whisperx"  # whisperx or opt-in wenbi
    wenbi_asr_provider: str = "funasr"  # explicit; never Wenbi auto/implicit cloud
    wenbi_whisper_model: str = "large-v3-turbo"
    wenbi_speaker_labels: bool = False
    wenbi_python: str | None = None  # optional isolated Wenbi environment interpreter

    def __post_init__(self) -> None:
        if self.output_mode not in OUTPUT_MODES:
            raise ValueError(
                f"output_mode must be one of {', '.join(OUTPUT_MODES)}; got {self.output_mode!r}"
            )
        if self.pages is not None:
            validate_pages_spec(self.pages)
            self.pages = self.pages.strip()
        if not isinstance(self.registry_fill, bool):
            raise ValueError("registry_fill must be a boolean")
        if self.evidence_window not in {"locator", "range"}:
            raise ValueError("evidence_window must be locator or range")
        if not 0.0 <= float(self.review_threshold) <= 1.0:
            raise ValueError("review_threshold must be between 0 and 1")
        providers = self.online_enrich_providers
        # Keep in sync with metadata_registry._PROVIDER_URLS (authority names added by P2 D1).
        supported = {"crossref", "openalex", "openlibrary", "datacite", "orcid", "wikidata", "geonames"}
        if (not isinstance(providers, (tuple, list)) or not providers
                or any(not isinstance(p, str) or p not in supported for p in providers)
                or len(set(providers)) != len(providers)):
            raise ValueError("online_enrich_providers must contain unique supported registry names")
        self.online_enrich_providers = tuple(providers)
        for name in ("online_enrich_min_score", "online_enrich_timeout", "online_enrich_cache_ttl"):
            value = getattr(self, name)
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.online_enrich_min_score > 1:
            raise ValueError("online_enrich_min_score must be at most 1")
        if self.online_enrich_ai_fallback:
            from .ai_discovery import validate_ai_config
            validate_ai_config(self)
        if self.enrich_proposal and self.offline_verification:
            raise ValueError("--enrich-proposal requires online registry verification")


@dataclass
class PipelineResult:
    status: str
    source_id: str
    resource_type: str
    csl_json: dict[str, Any]
    document_json: dict[str, Any] | None = None
    transcript_json: dict[str, Any] | None = None
    merkle_tree: dict[str, Any] | None = None
    media_metadata: dict[str, Any] | None = None
    retrieval_index: dict[str, Any] | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.update(payload.pop("extra", {}))
        return payload


def validate_ingestion_input(payload: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if "schema_version" not in payload:
        errors.append("missing schema_version")
    if "source_manifest" not in payload:
        errors.append("missing source_manifest")
        return errors

    if not isinstance(payload["source_manifest"], list) or not payload["source_manifest"]:
        errors.append("source_manifest must be a non-empty array")
        return errors

    for item in payload["source_manifest"]:
        if "source_id" not in item:
            errors.append("source item missing source_id")
        if "source_path" not in item:
            errors.append("source item missing source_path")
    return errors
