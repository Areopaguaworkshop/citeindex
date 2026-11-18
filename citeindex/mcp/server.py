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
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from citeindex import __version__, ingest
from citeindex.ingestion.models import IngestionConfig
from citeindex.ingestion.url_security import validate_public_url

from . import library, skills
from .security import audit_event, configure, require_write
from .security import config as server_config

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=False)
READ_NETWORK = ToolAnnotations(read_only_hint=True, open_world_hint=True)
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
        "Load the citation-verification skill resource for the full audit workflow."
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
                  "citation_verify", "citation_format", "ingest_url"],
        "resources": ["citeindex://library/{source_id}/csl",
                      "citeindex://corpus/{source_id}/manifest",
                      "citeindex://corpus/{source_id}/verification",
                      "citeindex://skills/{name}"],
        "skills": skills.list_skills(),
        "capabilities": {"url_ingest": cfg.allow_write, "file_ingest": False, "jobs": False},
    }
    audit_event("citeindex_info", {})
    return info


@mcp.tool(annotations=READ_ONLY)
def library_list(limit: int = 20, offset: int = 0, kind: str | None = None) -> dict[str, Any]:
    """List corpus records, newest first. kind filters by CSL type."""
    return library.library_list(limit=limit, offset=offset, kind=kind)


@mcp.tool(annotations=READ_ONLY)
def library_search(query: str, limit: int = 10) -> dict[str, Any]:
    """Search records by title, author, DOI, URL, container, or folder name."""
    return library.library_search(query=query, limit=limit)


@mcp.tool(annotations=READ_ONLY)
def library_get(source_id: str) -> dict[str, Any]:
    """Return a full CSL record, artifact paths, and any cached verification."""
    return library.library_get(source_id)


@mcp.tool(annotations=READ_ONLY)
def citation_verify(source_id: str, offline: bool = True) -> dict[str, Any]:
    """Run the evidence audit on an existing record. Offline by default."""
    return library.citation_verify(source_id, offline=offline)


@mcp.tool(annotations=READ_ONLY)
def citation_format(source_ids: list[str], style: str = "chicago-author-date") -> dict[str, Any]:
    """Render stored records as a bibliography and in-text citation."""
    return library.citation_format(source_ids, style=style)


@mcp.tool(annotations=CORPUS_WRITE)
def ingest_url(url: str, verify: bool = False, deep: bool = False, llm: bool = False) -> dict[str, Any]:
    """Ingest one URL article and write corpus/library artifacts.

    Requires write permission. Deterministic HTML metadata is used by default;
    set llm=True for DSPy metadata extraction and deep=True for PageIndex.
    """
    require_write("ingest_url")
    validate_public_url(url)
    cfg = server_config()
    ingestion_config = IngestionConfig(
        use_pageindex=deep,
        use_llm_metadata=llm,
        verify_citations=verify,
    )
    pool = ThreadPoolExecutor(max_workers=1)
    future = pool.submit(
        ingest,
        url,
        corpus_root=str(cfg.corpus_root),
        config=ingestion_config,
    )
    try:
        result = future.result(timeout=cfg.max_url_seconds)
    except FutureTimeout as exc:
        raise TimeoutError(
            f"ingest_url exceeded {cfg.max_url_seconds}s; the worker may still finish in the background"
        ) from exc
    finally:
        pool.shutdown(wait=False, cancel_futures=True)

    sub = result.get("sub_pipeline_outputs") or {}
    csl = result.get("standardized_csl_json") or sub.get("csl_json") or {}
    payload = {
        "status": result.get("status", "ok"),
        "source_id": csl.get("id"),
        "folder": result.get("document_path"),
        "csl": csl,
        "citation_status": csl.get("_citation_status"),
        "verification": result.get("citation_verification"),
    }
    audit_event("ingest_url", {"url": url, "source_id": payload["source_id"],
                               "status": payload["status"]})
    return payload


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


@mcp.prompt(name="cite-this-source")
def cite_this_source(input_ref: str) -> str:
    """Ingest a source, audit evidence, and report only supported citations."""
    return (
        f"Use CiteIndex to ingest this source: {input_ref}\n\n"
        "1. Call ingest_url (write permission required) or library_search for an existing record.\n"
        "2. Call citation_verify on the resulting source_id.\n"
        "3. Present the CSL fields with _field_status and evidence quotes; mark conflicts as needs-review.\n"
        "4. Never claim citation accuracy without source evidence.\n\n"
        + skills.load_skill("citation-verification")
    )


@mcp.prompt(name="ingest-verified")
def ingest_verified(input_ref: str) -> str:
    """Full ingest + independent evidence review workflow."""
    return (
        f"Ingest {input_ref} with CiteIndex, then run the evidence review before reporting.\n\n"
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
    parser.add_argument("--allow-write", action="store_true", help="Enable ingest_url writes")
    parser.add_argument("--max-url-seconds", type=int, help="Synchronous URL ingest timeout (max 600)")
    parser.add_argument("--transport", choices=["stdio", "streamable-http"], default="stdio")
    parser.add_argument("--http", type=_parse_http, help="Serve Streamable HTTP at HOST:PORT (path /mcp)")
    args = parser.parse_args(argv)

    configure(corpus_root=args.corpus_root, allow_write=True if args.allow_write else None,
              max_url_seconds=args.max_url_seconds)
    transport = "streamable-http" if args.http else args.transport
    if transport == "streamable-http":
        host, port = args.http or ("127.0.0.1", 8765)
        mcp.run(transport="streamable-http", host=host, port=port)
    else:
        mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    sys.exit(main())
