import argparse
import json
import logging
import sys


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )


def _run_ingest(args: argparse.Namespace) -> int:
    from citeindex.ingestion import CiteIndexIngestionOrchestrator
    from citeindex.ingestion.models import IngestionConfig

    config = IngestionConfig(
        llm_model=args.llm,
        text_direction=args.text_direction,
        vertical_lang=args.vertical_lang,
        lang=args.lang,
        page_range=args.page_range,
        doc_type_override=args.type,
        use_layout_analysis=not args.no_layout,
        is_primary=args.is_primary,
    )
    orchestrator = CiteIndexIngestionOrchestrator(
        corpus_root=args.corpus_root,
        schema_version=args.schema_version,
    )
    output = orchestrator.ingest(args.input, config=config)
    print(json.dumps(output, indent=2, ensure_ascii=False, sort_keys=True))
    return 1 if output.get("status") == "blocked" else 0


def _run_search(args: argparse.Namespace) -> int:
    print(
        json.dumps(
            {
                "status": "todo",
                "message": "search pipeline not implemented yet",
                "query": args.query,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def _run_chat(args: argparse.Namespace) -> int:
    print(
        json.dumps(
            {
                "status": "todo",
                "message": "chat pipeline not implemented yet",
                "thread": args.thread,
            },
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="citeindex", description="CiteIndex — AI research knowledge infrastructure"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ingest_parser = subparsers.add_parser(
        "ingest", help="Deterministic CiteIndex ingestion"
    )
    ingest_parser.add_argument("input", help="Input file path or URL")
    ingest_parser.add_argument(
        "--corpus-root", default="corpus", help="Corpus output root"
    )
    ingest_parser.add_argument(
        "--schema-version", default="1.0.0", help="Schema version tag"
    )
    ingest_parser.add_argument(
        "--verbose", "-v", action="store_true", help="Verbose logs"
    )
    ingest_parser.add_argument(
        "--llm",
        default="ollama/qwen3",
        help="LLM model for citation extraction (default: ollama/qwen3)",
    )
    ingest_parser.add_argument(
        "--text-direction",
        "-td",
        choices=["horizontal", "auto", "vertical"],
        default="horizontal",
        help="Text direction for PDF processing",
    )
    ingest_parser.add_argument(
        "--vertical-lang",
        choices=["ch", "japan"],
        default="ch",
        help="Primary language for vertical text OCR (default: ch)",
    )
    ingest_parser.add_argument(
        "--lang",
        "-l",
        default="auto",
        help="OCR language (default: auto-detect)",
    )
    ingest_parser.add_argument(
        "--page-range",
        "-p",
        default="1-5, -3",
        help='Page range for extraction (default: "1-5, -3")',
    )
    ingest_parser.add_argument(
        "--type",
        "-t",
        choices=["book", "thesis", "journal", "bookchapter"],
        help="Override automatic document type detection",
    )
    ingest_parser.add_argument(
        "--no-layout",
        action="store_true",
        help="Disable layout analysis (column/footnote detection)",
    )
    ingest_parser.add_argument(
        "--is-primary",
        action="store_true",
        help="Mark source as primary (line-level granularity). Default: secondary (paragraph-level)",
    )
    ingest_parser.add_argument(
        "--citation-style",
        "-cs",
        default="chicago-author-date",
        help="Citation style for formatted output (default: chicago-author-date)",
    )

    search_parser = subparsers.add_parser(
        "search", help="Deterministic search"
    )
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument(
        "--verbose", "-v", action="store_true", help="Verbose logs"
    )

    chat_parser = subparsers.add_parser("chat", help="Deterministic chat")
    chat_parser.add_argument("--thread", default="default", help="Chat thread id")
    chat_parser.add_argument(
        "--verbose", "-v", action="store_true", help="Verbose logs"
    )

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()
    _configure_logging(getattr(args, "verbose", False))

    if args.command == "ingest":
        code = _run_ingest(args)
    elif args.command == "search":
        code = _run_search(args)
    else:
        code = _run_chat(args)
    sys.exit(code)


if __name__ == "__main__":
    main()
