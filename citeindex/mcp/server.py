"""CiteIndex MCP server.

Exposes the existing Python ingestion API and corpus records as MCP tools,
resources, and prompts for OpenCode, Codex, Pi, and OMP.

Run:
    citeindex-mcp --corpus-root /path/to/corpus
    citeindex-mcp --http 127.0.0.1:8765
"""

from __future__ import annotations

import argparse
import json
import sys
from concurrent.futures import Future, ThreadPoolExecutor, wait
from dataclasses import replace
from pathlib import Path
from threading import Lock
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import uuid4

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from citeindex import __version__, ingest
from citeindex.ingestion.models import IngestionConfig, OutputMode
from citeindex.ingestion.url_security import validate_public_url

from . import library, skills
from .security import ServerConfig, audit_event, configure, require_write, resolve_input_file
from .security import config as server_config

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
READ_NETWORK = ToolAnnotations(read_only_hint=True, open_world_hint=True)
_INGEST_WORKER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="citeindex-ingest")
_JOBS: dict[str, tuple[Path, str, IngestionConfig, Future[dict[str, Any]]]] = {}
_JOBS_LOCK = Lock()
_JOB_LIMIT = 64
CORPUS_WRITE = ToolAnnotations(
    read_only_hint=False,
    destructive_hint=False,
    idempotent_hint=False,
    open_world_hint=True,
)

mcp = MCPServer(
    "citeindex",
    version=__version__,
    instructions=(
        "CiteIndex ingests sources into evidence-backed CSL records. "
        "Read tools are safe by default; ingestion requires write permission. "
        "After ingesting or loading a record, verify source support before presenting citation accuracy. "
        "Use the host's web or browser tools to open the publisher or original source when evidence is missing. "
        "Load the citation-verification skill for source and edition checks. Use Shutong for corpus discovery and research Q&A."
    ),
)


@mcp.tool(annotations=READ_ONLY)
def citeindex_info() -> dict[str, Any]:
    """Return version, corpus paths, write permission, and available skills."""
    cfg = server_config()
    info = {
        "name": "citeindex",
        "version": __version__,
        "corpus_root": str(cfg.corpus_root),
        "library_root": str(cfg.library_root),
        "allow_write": cfg.allow_write,
        "max_url_seconds": cfg.max_url_seconds,
        "tools": ["citeindex_info", "library_list", "library_search", "library_get",
                  "citation_verify", "citation_format", "ingest_source", "ingestion_status", "ingest_url"],
        "resources": ["citeindex://library/{source_id}/csl",
                      "citeindex://corpus/{source_id}/manifest",
                      "citeindex://corpus/{source_id}/verification",
                      "citeindex://corpus/{source_id}/quotations",
                      "citeindex://skills/{name}"],
        "skills": skills.list_skills(),
        "capabilities": {"url_ingest": cfg.allow_write, "file_ingest": cfg.allow_write and bool(cfg.input_roots), "jobs": True},
    }
    audit_event("citeindex_info", {})
    return info


@mcp.tool(annotations=READ_ONLY)
def library_list(limit: int = 20, offset: int = 0, kind: str | None = None) -> dict[str, Any]:
    """Compatibility tool. Prefer Shutong for discovery. Lists records by CSL type."""
    return library.library_list(limit=limit, offset=offset, kind=kind)


@mcp.tool(annotations=READ_ONLY)
def library_search(query: str, limit: int = 10) -> dict[str, Any]:
    """Compatibility tool. Prefer Shutong for discovery. Searches corpus metadata."""
    return library.library_search(query=query, limit=limit)


@mcp.tool(annotations=READ_ONLY)
def library_get(source_id: str) -> dict[str, Any]:
    """Return a full CSL record, artifact paths, and any cached verification."""
    return library.library_get(source_id)


@mcp.tool(annotations=READ_NETWORK)
def citation_verify(
    source_id: str, offline: bool = True, fields: list[str] | None = None,
    quotation: str | None = None, locator: dict[str, Any] | None = None,
    original_file: str | None = None,
) -> dict[str, Any]:
    """Check stored CSL and a supplied quotation for an exact corpus ID or folder.

    Replay is offline by default. The host independently checks the original
    and can use web tools to resolve missing or conflicting evidence. Pass an
    empty fields list to check only the supplied quotation. Omit its locator
    to check the whole record. If there are several matches, ask the user to
    select one and repeat the check with the selected locator.
    """
    return library.citation_verify(
        source_id, offline=offline, fields=fields, quotation=quotation,
        locator=locator, original_file=original_file,
    )


@mcp.tool(annotations=READ_ONLY)
def citation_format(source_ids: list[str], style: str = "chicago-author-date") -> dict[str, Any]:
    """Render stored records as a bibliography and in-text citation."""
    return library.citation_format(source_ids, style=style)


def _run_ingestion(input_ref: str, settings: ServerConfig, options: IngestionConfig) -> dict[str, Any]:
    # Revalidate queued paths against the submitting server's input roots.
    try:
        if urlsplit(input_ref).scheme not in {"http", "https"}:
            input_ref = str(resolve_input_file(input_ref, settings=settings))
        if options.repair_proposal:
            proposal = resolve_input_file(options.repair_proposal, settings=settings)
            options = replace(options, repair_proposal=str(proposal))
    except (OSError, ValueError) as exc:
        return {"status": "blocked", "error_code": "input_unavailable", "error_message": str(exc)}
    result = ingest(input_ref, corpus_root=str(settings.corpus_root), config=options)
    if result.get("status") != "ok":
        return result
    sub = result.get("sub_pipeline_outputs") or {}
    csl = result.get("standardized_csl_json") or sub.get("csl_json") or {}
    return {
        "status": "ok",
        "source_id": csl.get("id"),
        "folder": result.get("document_path"),
        "csl": csl,
        "citation_status": csl.get("_citation_status"),
        "verification": result.get("citation_verification"),
    }


def _submit_ingestion(input_ref: str, options: IngestionConfig) -> tuple[str, Future[dict[str, Any]]]:
    cfg = server_config()
    with _JOBS_LOCK:
        for job_id, (root, source, config, future) in _JOBS.items():
            if not future.done() and (root, source, config) == (cfg.corpus_root, input_ref, options):
                return job_id, future
        if len(_JOBS) >= _JOB_LIMIT:
            completed = next((key for key, job in _JOBS.items() if job[3].done()), None)
            if completed is None:
                raise RuntimeError("The ingestion queue is full")
            del _JOBS[completed]
        job_id = uuid4().hex
        future = _INGEST_WORKER.submit(_run_ingestion, input_ref, cfg, options)
        _JOBS[job_id] = (cfg.corpus_root, input_ref, options, future)
    audit_event("ingestion_submitted", {"job_id": job_id})
    return job_id, future


def _job_result(job_id: str, future: Future[dict[str, Any]], timeout: float = 0) -> dict[str, Any]:
    wait((future,), timeout=timeout)
    if future.done():
        return {"job_id": job_id, **future.result()}
    return {"job_id": job_id, "status": "running" if future.running() else "queued"}


@mcp.tool(annotations=CORPUS_WRITE)
def ingest_source(
    input_ref: str, verify: bool = False, deep: bool | None = None, llm: bool | None = None,
    pages: str | None = None, ocr_engine: Literal["mineru", "glm-ocr"] = "mineru",
    media_asr_backend: Literal["whisperx", "wenbi"] = "whisperx",
    repair_proposal: str | None = None, output_mode: OutputMode = "full",
) -> dict[str, Any]:
    """Queue a PDF, Office, DjVu, media file or public URL for ingestion.

    Set output_mode to csl_only for citation-only ingestion or csl_evidence to keep evidence blocks.
    Local sources and repair proposal files require a configured input root.
    Poll ingestion_status with the returned job ID. deep and llm default to
    the shared pipeline settings. Review citation evidence after completion.
    """
    require_write("ingest_source")
    if urlsplit(input_ref).scheme in {"http", "https"}:
        validate_public_url(input_ref)
    else:
        input_ref = str(resolve_input_file(input_ref))
    options = IngestionConfig(
        verify_citations=verify, pages=pages, ocr_engine=ocr_engine,
        media_asr_backend=media_asr_backend, output_mode=output_mode,
        repair_proposal=str(resolve_input_file(repair_proposal)) if repair_proposal else None,
    )
    if deep is not None:
        options.use_pageindex = deep
    if llm is not None:
        options.use_llm_metadata = llm
    job_id, future = _submit_ingestion(input_ref, options)
    return _job_result(job_id, future)


@mcp.tool(annotations=READ_ONLY)
def ingestion_status(job_id: str) -> dict[str, Any]:
    """Return the state or result of a recent ingestion job on this server."""
    with _JOBS_LOCK:
        job = _JOBS.get(job_id)
    if job is None or job[0] != server_config().corpus_root:
        raise KeyError("Unknown or expired ingestion job")
    return _job_result(job_id, job[3])


@mcp.tool(annotations=CORPUS_WRITE)
def ingest_url(
    url: str, verify: bool = False, deep: bool = False, llm: bool = False,
    output_mode: OutputMode = "full",
) -> dict[str, Any]:
    """Ingest a public URL. Set output_mode to csl_only for citation-only ingestion.

    If still running, poll the returned job ID.
    """
    require_write("ingest_url")
    validate_public_url(url)
    job_id, future = _submit_ingestion(
        url, IngestionConfig(use_pageindex=deep, use_llm_metadata=llm, verify_citations=verify, output_mode=output_mode),
    )
    return _job_result(job_id, future, server_config().max_url_seconds)


@mcp.resource("citeindex://library/{source_id}/csl")
def library_csl_resource(source_id: str) -> str:
    """Processor-ready CSL JSON for one record."""
    return json.dumps(library.record_csl(source_id), ensure_ascii=False, indent=2)


@mcp.resource("citeindex://corpus/{source_id}/manifest")
def corpus_manifest_resource(source_id: str) -> str:
    """Merkle manifest for one record."""
    return library.record_manifest(source_id)


@mcp.resource("citeindex://corpus/{source_id}/verification")
def corpus_verification_resource(source_id: str) -> str:
    """Latest citation verification report for one record."""
    return library.record_verification(source_id)


@mcp.resource("citeindex://skills/{name}")
def skill_resource(name: str) -> str:
    """Citation verification or ingestion skill shipped with CiteIndex."""
    return skills.load_skill(name)


@mcp.resource("citeindex://corpus/{source_id}/quotations")
def corpus_quotations_resource(source_id: str) -> str:
    """Return versioned quotation anchors for one exact record."""
    folder, _ = library.find_record(source_id, exact=True)
    path = folder / "quotation_locators.json"
    return path.read_text(encoding="utf-8") if path.is_file() and not path.is_symlink() else "[]"


@mcp.prompt(name="cite-this-source")
def cite_this_source(input_ref: str) -> str:
    """Ingest a source, audit evidence, and report only supported citations."""
    return (
        f"Use CiteIndex to ingest this source: {input_ref}\n\n"
        "1. Call ingest_source and poll ingestion_status, or supply an exact CiteIndex record ID.\n"
        "2. Call citation_verify on the resulting source_id.\n"
        "3. Use host web or browser tools to inspect the matching original or publisher when needed.\n"
        "4. Report evidence quotes, locators and URLs. Mark conflicts as needs-review.\n"
        "5. Report registry corroboration and original-source confirmation distinctly.\n\n"
        + skills.load_skill("citation-verification")
    )


@mcp.prompt(name="ingest-verified")
def ingest_verified(input_ref: str) -> str:
    """Full ingest + independent evidence review workflow."""
    return (
        f"Call ingest_source for {input_ref}, poll ingestion_status if needed, then run citation_verify.\n"
        "Audit the original with the host tools before reporting citation accuracy.\n\n"
        + skills.load_skill("citation-verification")
    )


@mcp.prompt(name="reverify-citation")
def reverify_citation(source_id: str) -> str:
    """Review an existing work with local and online sources in the agent host."""
    return (
        f"Re-verify CiteIndex record {source_id!r}. Call citation_verify with its exact ID. "
        "For a quotation, pass the exact text and any known locator. Ask the user to choose if several matches remain. "
        "Inspect the original source. "
        "Use the host's web or browser tools to resolve missing or conflicting evidence. "
        "Check work and edition identity, then capture the supporting passage, locator and URL.\n\n"
        + skills.load_skill("citation-verification")
    )


def _parse_http(value: str) -> tuple[str, int]:
    host, _, port = value.rpartition(":")
    if not host or not port.isdigit():
        raise argparse.ArgumentTypeError("--http expects HOST:PORT, e.g. 127.0.0.1:8765")
    return host, int(port)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="citeindex-mcp", description="CiteIndex MCP server")
    parser.add_argument("--corpus-root", help="Corpus output root (default: ./corpus or CITEINDEX_CORPUS_ROOT)")
    parser.add_argument("--allow-write", action="store_true", help="Enable corpus ingestion writes")
    parser.add_argument("--input-root", action="append", help="Allow files within this root (repeatable)")
    parser.add_argument("--max-url-seconds", type=int, help="Synchronous URL ingest timeout (max 600)")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--http", type=_parse_http, help="Serve Streamable HTTP at HOST:PORT (path /mcp)")
    args = parser.parse_args(argv)

    configure(corpus_root=args.corpus_root, allow_write=True if args.allow_write else None,
              max_url_seconds=args.max_url_seconds, input_roots=args.input_root)
    transport = "streamable-http" if args.http else args.transport
    if transport == "streamable-http":
        host, port = args.http or ("127.0.0.1", 8765)
        mcp.run(transport="streamable-http", host=host, port=port)
    else:
        mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
