import argparse
import json
import logging
import sys

from citeindex.ingestion import CiteIndexIngestionOrchestrator


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s - %(levelname)s - %(message)s",
    )


def _run_extract(args: argparse.Namespace) -> int:
    from citeindex.citation_style import format_bibliography
    from citeindex.main import CitationExtractor

    is_document_file = args.input.lower().endswith((".pdf", ".djvu"))
    if args.text_direction != "horizontal" and not is_document_file:
        print(
            "Warning: --text-direction is only applicable to PDF or DJVU files and will be ignored.",
            file=sys.stderr,
        )

    try:
        if args.verbose:
            print(f"Using LLM model: {args.llm}")

        extractor = CitationExtractor(llm_model=args.llm)
        print(f"Processing: {args.input}")
        csl_data = extractor.extract_citation(
            args.input,
            output_dir=args.output_dir,
            doc_type_override=args.type,
            lang=args.lang,
            text_direction=args.text_direction,
            vertical_lang=args.vertical_lang,
            page_range=args.page_range,
        )

        if not csl_data:
            print("Failed to extract citation information.", file=sys.stderr)
            return 1

        print("\n" + "=" * 50)
        print("CITATION EXTRACTED SUCCESSFULLY")
        print("=" * 50)
        for key, value in csl_data.items():
            print(f"{key.replace('_', ' ').title()}: {value}")
        print(f"\nCitation files saved to: {args.output_dir}")

        print("\n" + "=" * 50)
        print(f"FORMATTED BIBLIOGRAPHY ({args.citation_style})")
        print("=" * 50)
        bibliography, in_text_citation = format_bibliography(
            [csl_data], args.citation_style
        )
        print(bibliography)

        print("\n" + "=" * 50)
        print("IN-TEXT CITATION")
        print("=" * 50)
        print(in_text_citation)
        return 0
    except KeyboardInterrupt:
        print("\nOperation cancelled by user.", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"An unexpected error occurred: {e}", file=sys.stderr)
        if args.verbose:
            import traceback

            traceback.print_exc()
        return 1


def _run_ingest(args: argparse.Namespace) -> int:
    from citeindex.ingestion.models import IngestionConfig

    config = IngestionConfig(
        llm_model=args.llm,
        text_direction=args.text_direction,
        vertical_lang=args.vertical_lang,
        lang=args.lang,
        page_range=args.page_range,
        doc_type_override=args.type,
        use_layout_analysis=not args.no_layout,
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


def _build_extract_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="citeindex",
        description="Extract citations from documents/URLs or run ingest subcommands.",
    )
    parser.add_argument("input", help="Path to source file or URL")
    parser.add_argument(
        "--type",
        "-t",
        choices=["book", "thesis", "journal", "bookchapter"],
        help="Document type (overrides automatic detection based on page count)",
    )
    parser.add_argument(
        "--output-dir",
        "-o",
        default="example",
        help="Output directory for citation files (default: example)",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose logging"
    )
    parser.add_argument(
        "--lang",
        "-l",
        default="auto",
        help="Language for OCR. Default is 'auto'. Example: 'eng+chi_sim'.",
    )
    parser.add_argument(
        "--text-direction",
        "-td",
        choices=["horizontal", "auto", "vertical"],
        default="horizontal",
        help=(
            "Text direction for PDF processing. 'auto' or 'vertical' is recommended for "
            "documents with vertical text, common in Chinese and Japanese."
        ),
    )
    parser.add_argument(
        "--vertical-lang",
        choices=["ch", "japan"],
        default="ch",
        help="Primary language for vertical text OCR (default: ch)",
    )
    parser.add_argument(
        "--page-range",
        "-p",
        default="1-5, -3",
        help='Page range for OCR. Example: "1-5, -3"',
    )
    parser.add_argument(
        "--llm",
        default="ollama/qwen3",
        help=(
            "LLM model to use for citation extraction (default: ollama/qwen3). "
            "Examples: ollama/qwen3, gemini/gemini-1.5-flash."
        ),
    )
    parser.add_argument(
        "--citation-style",
        "-cs",
        default="chicago-author-date",
        help="Citation style for formatted output (default: chicago-author-date).",
    )
    return parser


def _build_ops_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="citeindex", description="Unified citeindex CLI"
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

    search_parser = subparsers.add_parser(
        "search", help="Deterministic search (placeholder)"
    )
    search_parser.add_argument("query", help="Search query")
    search_parser.add_argument(
        "--verbose", "-v", action="store_true", help="Verbose logs"
    )

    chat_parser = subparsers.add_parser("chat", help="Deterministic chat (placeholder)")
    chat_parser.add_argument("--thread", default="default", help="Chat thread id")
    chat_parser.add_argument(
        "--verbose", "-v", action="store_true", help="Verbose logs"
    )

    return parser


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] in {"ingest", "search", "chat"}:
        parser = _build_ops_parser()
        args = parser.parse_args(argv)
        _configure_logging(getattr(args, "verbose", False))

        if args.command == "ingest":
            code = _run_ingest(args)
        elif args.command == "search":
            code = _run_search(args)
        else:
            code = _run_chat(args)
        sys.exit(code)

    parser = _build_extract_parser()
    args = parser.parse_args(argv)
    _configure_logging(args.verbose)
    sys.exit(_run_extract(args))


if __name__ == "__main__":
    main()
