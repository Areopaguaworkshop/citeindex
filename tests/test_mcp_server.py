"""Tests for the optional CiteIndex MCP adapter."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

pytest.importorskip("mcp")

from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

from citeindex.mcp import library, server, skills
from citeindex.mcp.security import configure


def _write_record(root: Path, folder: str, csl: dict, blocks: list[dict] | None = None) -> Path:
    record = root / folder
    record.mkdir(parents=True)
    (record / "csl.json").write_text(json.dumps(csl, ensure_ascii=False), encoding="utf-8")
    (record / "ingestion_output.json").write_text(json.dumps({
        "status": "ok",
        "standardized_csl_json": csl,
        "sub_pipeline_outputs": {
            "resource_type": "url_article",
            "document_json": {"metadata": {"title": csl.get("title"), "url": csl.get("URL")}},
            "extra": {},
            "csl_json": csl,
        },
        "ingestion_log_entry": {"resource_type": "url_article"},
    }, ensure_ascii=False), encoding="utf-8")
    if blocks is not None:
        (record / "source_blocks.json").write_text(json.dumps(blocks, ensure_ascii=False), encoding="utf-8")
    if csl.get("_field_status"):
        (record / "citation_verification.json").write_text(json.dumps({
            "status": "needs_review",
            "field_states": csl["_field_status"],
        }), encoding="utf-8")
    return record


@pytest.fixture()
def corpus(tmp_path: Path) -> Path:
    root = tmp_path / "corpus"
    root.mkdir()
    demo = {
        "id": "src_demo",
        "type": "article-journal",
        "title": "Demo Article",
        "author": [{"family": "Doe", "given": "Jane"}],
        "issued": {"date-parts": [[2024, 5]]},
        "container-title": "Journal of Tests",
        "volume": "1",
        "issue": "2",
        "page": "3-4",
        "DOI": "10.1000/demo",
        "_field_status": {"title": "source-supported", "author": "source-supported"},
        "_citation_status": "source-supported",
    }
    _write_record(root, "Doe_2024_Demo_Article_abcd1234", demo, [
        {"id": "html:1", "text": "Demo Article", "metadata_key": "citation_title",
         "snapshot_artifact": "source.html", "source_digest": "a" * 64, "section_index": 1},
        {"id": "html:2", "text": "Doe, Jane", "metadata_key": "citation_author",
         "snapshot_artifact": "source.html", "source_digest": "a" * 64, "section_index": 2},
    ])
    _write_record(root, "Roe_2023_Other_Book_efgh5678", {
        "id": "src_other",
        "type": "book",
        "title": "Other Book",
        "author": [{"literal": "Roe"}],
        "issued": {"date-parts": [[2023]]},
    })
    configure(corpus_root=root)
    return root


def test_info_and_library_tools(corpus: Path):
    info = server.citeindex_info()
    assert info["version"]
    assert info["corpus_root"] == str(corpus)
    assert "citation-verification" in info["skills"]

    listing = library.library_list(limit=10)
    assert listing["total"] == 2
    assert {record["source_id"] for record in listing["records"]} == {"src_demo", "src_other"}

    search = library.library_search("demo")
    assert search["total"] == 1
    assert search["records"][0]["source_id"] == "src_demo"

    record = library.library_get("10.1000/demo")
    assert record["csl"]["title"] == "Demo Article"
    assert "csl.json" in record["paths"]
    assert record["verification"]["status"] == "needs_review"

    formatted = library.citation_format(["src_demo"])
    assert "Demo Article" in formatted["bibliography"]


def test_citation_verify_offline(corpus: Path):
    result = library.citation_verify("src_demo", offline=True)
    assert result["source_id"] == "src_demo"
    assert result["report"]["field_states"]["title"] == "source-supported"
    assert result["csl"]["title"] == "Demo Article"


def test_ingest_url_requires_write_permission(corpus: Path):
    configure(corpus_root=corpus, allow_write=False)
    with pytest.raises(PermissionError):
        server.ingest_url("https://example.org/article")


def test_ingest_url_returns_record(corpus: Path, monkeypatch: pytest.MonkeyPatch):
    configure(corpus_root=corpus, allow_write=True)
    csl = {"id": "src_new", "type": "article-journal", "title": "New Article",
           "_citation_status": "source-supported"}

    def fake_ingest(url: str, corpus_root: str, config):
        return {"status": "ok", "document_path": str(Path(corpus_root) / "New_Article"),
                "standardized_csl_json": csl, "sub_pipeline_outputs": {"csl_json": csl},
                "citation_verification": {"status": "verified"}}

    monkeypatch.setattr(server, "ingest", fake_ingest)
    result = server.ingest_url("https://example.org/article")
    assert result["source_id"] == "src_new"
    assert result["citation_status"] == "source-supported"
    assert result["verification"] == {"status": "verified"}


def test_skill_resource_and_prompts(corpus: Path):
    text = skills.load_skill("citation-verification")
    assert "citation-verification" in text
    assert "needs-review" in text

    prompt = asyncio.run(server.mcp.get_prompt("cite-this-source", {"input_ref": "https://example.org/a"}))
    rendered = " ".join(message.content.text for message in prompt.messages)
    assert "citation-verification" in rendered


def test_protocol_tools_and_resource(corpus: Path):
    async def run():
        tools = await server.mcp.list_tools()
        assert {"library_search", "citation_verify", "ingest_url"} <= {tool.name for tool in tools}
        result = await server.mcp.call_tool("library_search", {"query": "demo"})
        assert result.structured_content["total"] == 1
        content = await server.mcp.read_resource("citeindex://skills/citation-verification")
        assert "citation-verification" in content[0].content

    asyncio.run(run())


def test_stdio_round_trip(corpus: Path):
    async def run():
        params = StdioServerParameters(
            command=sys.executable,
            args=["-m", "citeindex.mcp.server", "--corpus-root", str(corpus)],
            env={"CITEINDEX_MCP_AUDIT": "0"},
        )
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                assert "citeindex_info" in {tool.name for tool in tools.tools}
                result = await session.call_tool("citeindex_info", {})
                assert result.structured_content["corpus_root"] == str(corpus)
                search = await session.call_tool("library_search", {"query": "demo"})
                assert search.structured_content["total"] == 1

    asyncio.run(run())
