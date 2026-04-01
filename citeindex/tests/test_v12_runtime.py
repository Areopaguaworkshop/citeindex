import hashlib
import json

from citeindex.agents.v12_runtime import (
    _hash_output,
    _index_ingested_document,
    handle_coordinator,
    handle_librarian,
)


def test_hash_output_uses_contract_prefix() -> None:
    output = {"b": 2, "a": 1}
    expected = "sha256:" + hashlib.sha256(
        json.dumps(output, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()

    assert _hash_output(output) == expected


def test_handle_coordinator_keeps_query_plan_mode() -> None:
    result = handle_coordinator({"query": "attention in neural translation"})

    assert result["agent"] == "CoordinatorAgent"
    assert "query_plan" in result


def test_handle_librarian_uses_kernel_tool_shape() -> None:
    seen = {}

    def fake_tool(tool: str, params: dict) -> dict:
        seen["tool"] = tool
        seen["params"] = params
        return {
            "total_hits": 1,
            "hits": [
                {
                    "id": "doc-1",
                    "score": 0.75,
                    "fields": {
                        "title": "Church History",
                        "authors": "Aji Ap",
                        "abstract_text": "A study of church history.",
                        "year": 2024,
                        "venue": "Journal of History",
                    },
                }
            ],
        }

    result = handle_librarian({"query": "church history", "top_k": 5}, fake_tool)

    assert seen["tool"] == "tantivy_search"
    assert seen["params"]["index"] == "documents"
    assert result["agent"] == "LibrarianAgent"
    assert result["results"][0]["node_id"] == "doc-1"
    assert "Church History" in result["results"][0]["formatted_citation"]


def test_index_ingested_document_builds_tantivy_index_payload() -> None:
    seen = {}

    def fake_tool(tool: str, params: dict) -> dict:
        seen["tool"] = tool
        seen["params"] = params
        return {"status": "ok", "doc_id": params["doc_id"]}

    result = _index_ingested_document(
        {
            "status": "ok",
            "standardized_csl_json": {
                "id": "doc-1",
                "title": "从新英格兰到英格兰",
                "issued": {"date-parts": [[2021]]},
                "type": "webpage",
                "merkle_root": "root-hash",
            },
            "sub_pipeline_outputs": {
                "document_json": {
                    "nodes": [{"text": "正文内容"}],
                },
                "merkle_tree": {"root": "root-hash"},
            },
        },
        fake_tool,
    )

    assert seen["tool"] == "tantivy_index"
    assert seen["params"]["doc_id"] == "doc-1"
    assert seen["params"]["title"] == "从新英格兰到英格兰"
    assert seen["params"]["abstract_text"] == "正文内容"
    assert seen["params"]["language"] == "zh"
    assert result["status"] == "ok"
