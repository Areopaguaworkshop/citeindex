"""CiteIndex CLI — ingest sources with proper citation.

Usage:
    citeindex <path_or_url>              # ingest a file or URL
    citeindex <path_or_url> [options]    # ingest with options
"""

import argparse
from email.utils import parseaddr
import json
import logging
import sys

from citeindex.ingestion import CiteIndexIngestionOrchestrator
from citeindex.ingestion.models import IngestionConfig


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def _mineru_timeout_seconds(value: str) -> int:
    timeout = int(value)
    if timeout < 1 or timeout > 3600:
        raise argparse.ArgumentTypeError("must be between 1 and 3600 seconds")
    return timeout


def _non_negative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be 0 or greater")
    return parsed


def _mineru_chunk_pages(value: str) -> int | str:
    normalized = value.strip().lower()
    if normalized == "auto":
        return "auto"
    return _non_negative_int(value)


def _provider_qualified_model(value: str) -> str:
    model = value.strip()
    if not model or "/" not in model or model.startswith("/") or model.endswith("/"):
        raise argparse.ArgumentTypeError("must be a non-empty provider-qualified model, e.g. openai/gpt-5")
    return model


def _contact_email(value: str) -> str:
    address = value.strip()
    _, parsed = parseaddr(address)
    if not address or parsed != address or "@" not in parsed:
        raise argparse.ArgumentTypeError("must be a valid email address")
    local, _, domain = parsed.partition("@")
    if not local or not domain or "." not in domain:
        raise argparse.ArgumentTypeError("must be a valid email address")
    return address


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="citeindex",
        description="CiteIndex — ingest sources with proper citation",
    )
    parser.add_argument(
        "input",
        help="Input file path or URL to ingest",
    )
    parser.add_argument(
        "--corpus-root",
        default="corpus",
        help="Corpus output root directory (default: corpus)",
    )
    parser.add_argument(
        "--schema-version",
        default="1.0.0",
        help="Schema version tag (default: 1.0.0)",
    )
    parser.add_argument(
        "--llm",
        default="ollama/glm-5.3-flash:cloud",
        help="LLM model for citation extraction (default: ollama/glm-5.3-flash:cloud)",
    )
    parser.add_argument(
        "--ocr-engine",
        choices=["mineru", "glm-ocr"],
        default="mineru",
        help="Scanned PDF OCR backend (default: mineru)",
    )
    parser.add_argument(
        "--ocr-model",
        default="glm-ocr:latest",
        help="Model name for model-backed OCR engines such as glm-ocr (default: glm-ocr:latest)",
    )
    parser.add_argument(
        "--ollama-host",
        default="http://localhost:11434",
        help="Local Ollama host for glm-ocr requests (default: http://localhost:11434)",
    )
    parser.add_argument(
        "--mineru-backend",
        default="pipeline",
        help="MinerU execution backend passed to the CLI (default: pipeline)",
    )
    parser.add_argument(
        "--mineru-timeout",
        type=_mineru_timeout_seconds,
        default=3600,
        help="MinerU subprocess timeout in seconds, up to 3600 (default: 3600)",
    )
    parser.add_argument(
        "--mineru-chunk-pages",
        type=_mineru_chunk_pages,
        default="auto",
        help="Split large PDFs into MinerU chunks: auto, a page count, or 0 to disable (default: auto)",
    )
    parser.add_argument(
        "--text-direction",
        "-td",
        choices=["horizontal", "auto", "vertical"],
        default="horizontal",
        help="Text direction for PDF processing (default: horizontal)",
    )
    parser.add_argument(
        "--vertical-lang",
        choices=["ch", "japan"],
        default="ch",
        help="Language for vertical text OCR: ch or japan (default: ch)",
    )
    parser.add_argument(
        "--lang",
        "-l",
        default="auto",
        help="OCR language (default: auto-detect)",
    )
    parser.add_argument(
        "--page-range",
        "-p",
        default="1-5, -3",
        help='Page range for extraction (default: "1-5, -3")',
    )
    parser.add_argument(
        "--type",
        "-t",
        choices=["book", "thesis", "journal", "bookchapter"],
        help="Override automatic document type detection",
    )
    parser.add_argument(
        "--no-layout",
        action="store_true",
        help="Disable layout analysis (column/footnote detection)",
    )
    parser.add_argument(
        "--is-primary",
        action="store_true",
        help="Mark source as primary (line-level granularity). Default: secondary (paragraph-level)",
    )
    parser.add_argument(
        "--no-pageindex",
        action="store_true",
        help="Disable PageIndex LLM-driven tree building for section hierarchy",
    )
    parser.add_argument(
        "--force-ocr",
        action="store_true",
        help="Force scanned PDF pipeline (OCR) regardless of PDF type detection",
    )
    parser.add_argument(
        "--force-digital",
        action="store_true",
        help="Force digital PDF pipeline regardless of PDF type detection",
    )
    parser.add_argument(
        "--pageindex-model",
        default="ollama/glm-5.3-flash:cloud",
        help="LLM model for PageIndex tree building (default: ollama/glm-5.3-flash:cloud)",
    )
    parser.add_argument(
        "--verify-citations",
        action="store_true",
        help="Verify citation metadata against source evidence (default: disabled)",
    )
    parser.add_argument(
        "--citation-verifier-model",
        type=_provider_qualified_model,
        help="Provider-qualified strong model for citation conflicts, e.g. openai/gpt-5",
    )
    parser.add_argument(
        "--no-crossref",
        dest="crossref_enabled",
        action="store_false",
        default=True,
        help="Disable Crossref DOI lookup during citation verification",
    )
    parser.add_argument(
        "--offline-verification",
        action="store_true",
        help="Block registry and verifier-model requests during citation verification",
    )
    parser.add_argument(
        "--registry-contact-email",
        type=_contact_email,
        help="Contact email for polite registry requests",
    )
    parser.add_argument(
        "--all-url-article",
        "-aua",
        action="store_true",
        help="Crawl the input URL and ingest all discovered article pages",
    )
    parser.add_argument(
        "--update-url-article",
        "-uua",
        action="store_true",
        help="Crawl and compare content hashes; skip unchanged, re-ingest updated",
    )
    parser.add_argument(
        "--crawl-depth",
        type=int,
        default=2,
        help="Max BFS crawl depth for --all-url-article (default: 2)",
    )
    parser.add_argument(
        "--crawl-max-pages",
        type=int,
        default=100,
        help="Max pages for --all-url-article (default: 100)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        default=True,
        help="Enable verbose/debug logging (default: enabled)",
    )
    parser.add_argument(
        "--quiet",
        "-q",
        dest="verbose",
        action="store_false",
        help="Disable verbose/debug logging",
    )

    args = parser.parse_args()
    _configure_logging(args.verbose)

    # Determine force_pdf_kind from mutually exclusive flags
    force_pdf_kind = None
    if args.force_ocr:
        force_pdf_kind = "force_ocr"
    elif args.force_digital:
        force_pdf_kind = "force_digital"

    config = IngestionConfig(
        llm_model=args.llm,
        ocr_engine=args.ocr_engine,
        ocr_model=args.ocr_model,
        ollama_host=args.ollama_host,
        mineru_backend=args.mineru_backend,
        mineru_timeout=args.mineru_timeout,
        mineru_chunk_pages=args.mineru_chunk_pages,
        text_direction=args.text_direction,
        vertical_lang=args.vertical_lang,
        lang=args.lang,
        page_range=args.page_range,
        doc_type_override=args.type,
        use_layout_analysis=not args.no_layout,
        is_primary=args.is_primary,
        use_pageindex=not args.no_pageindex,
        pageindex_model=args.pageindex_model,
        verify_citations=args.verify_citations,
        citation_verifier_model=args.citation_verifier_model,
        crossref_enabled=args.crossref_enabled,
        offline_verification=args.offline_verification,
        registry_contact_email=args.registry_contact_email,
        force_pdf_kind=force_pdf_kind,
    )

    orchestrator = CiteIndexIngestionOrchestrator(
        corpus_root=args.corpus_root,
        schema_version=args.schema_version,
    )

    if args.all_url_article or args.update_url_article:
        output = orchestrator.ingest_all_urls(
            root_url=args.input,
            config=config,
            update=args.update_url_article,
            max_depth=args.crawl_depth,
            max_pages=args.crawl_max_pages,
        )
    else:
        output = orchestrator.ingest(args.input, config=config)

    print(json.dumps(output, indent=2, ensure_ascii=False, sort_keys=True))
    sys.exit(1 if output.get("status") == "blocked" else 0)


if __name__ == "__main__":
    main()
