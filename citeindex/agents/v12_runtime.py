"""Thin v12 agent adapter runtime.

These adapters provide the Python entrypoints expected by the Rust v12
manifests. They intentionally keep behavior small and deterministic for now,
wrapping the existing Python pipeline where practical.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sys
import traceback
import uuid
from typing import Any, Callable, Dict, List, Optional


PROTOCOL_VERSION = "12.0"
ToolCaller = Callable[[str, Dict[str, Any]], Dict[str, Any]]


def _configure_logging(agent_name: str) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format=f"%(asctime)s {agent_name} %(levelname)s %(message)s",
        stream=sys.stderr,
    )


def _emit(message: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False, sort_keys=True) + "\n")
    sys.stdout.flush()


def _hash_output(output: Dict[str, Any]) -> str:
    payload = json.dumps(output, ensure_ascii=False, sort_keys=True).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _resource_usage() -> Dict[str, Any]:
    return {
        "llm_calls": 0,
        "tool_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "wall_time_ms": 0,
    }


def _get_corpus_root(inputs: Dict[str, Any]) -> str:
    return (
        inputs.get("corpus_root")
        or os.environ.get("CITEINDEX_CORPUS_ROOT")
        or "corpus"
    )


def _extract_query(inputs: Dict[str, Any]) -> str:
    for key in ("query", "sub_query", "user_query", "prompt", "goal", "topic"):
        value = inputs.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    goal_aspect = inputs.get("goal_aspect")
    if isinstance(goal_aspect, dict):
        value = goal_aspect.get("sub_query")
        if isinstance(value, str) and value.strip():
            return value.strip()

    return ""


def _extract_text(inputs: Dict[str, Any]) -> str:
    for key in ("text", "document_text", "content", "passage"):
        value = inputs.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def _split_sentences(text: str) -> List[str]:
    return [
        part.strip()
        for part in re.split(r"(?<=[.!?。！？])\s+", text)
        if part.strip()
    ]


def _detect_language(text: str) -> str:
    if re.search(r"[\u3040-\u30ff]", text):
        return "ja"
    if re.search(r"[\u4e00-\u9fff]", text):
        return "zh"
    return "en"


def _format_citation(fields: Dict[str, Any]) -> str:
    title = str(fields.get("title") or "").strip()
    authors = str(fields.get("authors") or "").strip()
    venue = str(fields.get("venue") or "").strip()
    year = fields.get("year")

    parts = []
    if authors:
        parts.append(authors)
    if year is not None:
        parts.append(f"({year})")
    if title:
        parts.append(title)
    if venue:
        parts.append(venue)
    return ". ".join(parts)


def _extract_year(issued: Any) -> int:
    if not isinstance(issued, dict):
        return 0
    date_parts = issued.get("date-parts")
    if not isinstance(date_parts, list) or not date_parts:
        return 0
    first = date_parts[0]
    if not isinstance(first, list) or not first:
        return 0
    year = first[0]
    return int(year) if isinstance(year, int) else 0


def _authors_text(authors: Any) -> str:
    if not isinstance(authors, list):
        return ""

    names: List[str] = []
    for author in authors[:3]:
        if not isinstance(author, dict):
            continue
        literal = author.get("literal")
        family = author.get("family")
        given = author.get("given")
        if isinstance(literal, str) and literal.strip():
            names.append(literal.strip())
            continue
        if isinstance(family, str) and family.strip() and isinstance(given, str) and given.strip():
            names.append(f"{family.strip()} {given.strip()}")
            continue
        if isinstance(family, str) and family.strip():
            names.append(family.strip())
            continue
        if isinstance(given, str) and given.strip():
            names.append(given.strip())
    return ", ".join(names)


def _first_document_text(value: Any) -> str:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, list):
        for item in value:
            text = _first_document_text(item)
            if text:
                return text
        return ""
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str) and text.strip():
            return text.strip()
        for nested in value.values():
            candidate = _first_document_text(nested)
            if candidate:
                return candidate
    return ""


def _index_ingested_document(result: Dict[str, Any], call_tool: ToolCaller) -> Dict[str, Any]:
    csl = result.get("standardized_csl_json") if isinstance(result.get("standardized_csl_json"), dict) else {}
    sub_outputs = result.get("sub_pipeline_outputs") if isinstance(result.get("sub_pipeline_outputs"), dict) else {}
    document_json = sub_outputs.get("document_json") if isinstance(sub_outputs.get("document_json"), dict) else {}
    merkle_tree = sub_outputs.get("merkle_tree") if isinstance(sub_outputs.get("merkle_tree"), dict) else {}

    doc_id = str(csl.get("id") or csl.get("content_hash") or "")
    title = str(csl.get("title") or document_json.get("metadata", {}).get("title") or doc_id)
    authors = _authors_text(csl.get("author"))
    abstract_text = str(csl.get("abstract") or csl.get("abstract_text") or _first_document_text(document_json))
    language = str(csl.get("language") or _detect_language(f"{title} {abstract_text}"))

    params = {
        "doc_id": doc_id,
        "title": title,
        "authors": authors,
        "year": _extract_year(csl.get("issued")),
        "doi": str(csl.get("DOI") or csl.get("doi") or ""),
        "abstract_text": abstract_text,
        "venue": str(csl.get("container-title") or csl.get("container_title") or ""),
        "doc_type": str(csl.get("type") or csl.get("source_type") or "article-journal"),
        "quality_tier": str(csl.get("ci_quality_tier") or "silver"),
        "hierarchy_path": str(csl.get("ci_hierarchy_path") or ""),
        "merkle_hash": str(merkle_tree.get("root") or csl.get("merkle_root") or ""),
        "language": language,
    }

    return call_tool("tantivy_index", params)


def _normalize_librarian_hits(tool_result: Dict[str, Any], query: str) -> Dict[str, Any]:
    hits = tool_result.get("hits") if isinstance(tool_result.get("hits"), list) else []
    results = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        fields = hit.get("fields") if isinstance(hit.get("fields"), dict) else {}
        doc_id = str(hit.get("id") or "")
        title = str(fields.get("title") or "")
        author = str(fields.get("authors") or "")
        text = str(fields.get("abstract_text") or title)
        results.append(
            {
                "node_id": doc_id,
                "doc_id": doc_id,
                "text": text,
                "total_score": float(hit.get("score") or 0.0),
                "title": title,
                "author": author,
                "formatted_citation": _format_citation(fields),
            }
        )

    return {
        "agent": "LibrarianAgent",
        "status": "ok",
        "query": query,
        "total_results": len(results),
        "results": results,
        "retrieval_debug": {
            "backend": "kernel_tantivy",
            "total_hits": int(tool_result.get("total_hits") or len(results)),
        },
    }


def handle_coordinator(inputs: Dict[str, Any], _call_tool: Optional[ToolCaller] = None) -> Dict[str, Any]:
    if inputs.get("operation") == "chat":
        from .chat import ChatPipeline

        prompt = _extract_query(inputs)
        pipeline = ChatPipeline(
            corpus_root=_get_corpus_root(inputs),
            llm_model=str(inputs.get("llm_model") or "ollama/qwen3"),
        )
        result = pipeline.chat(prompt, thread_id=str(inputs.get("thread_id") or "default"))
        return {"agent": "CoordinatorAgent", **result}

    from .query_planner import QueryPlanner

    query = _extract_query(inputs)
    planner = QueryPlanner()
    plan = planner.plan(query)
    return {
        "agent": "CoordinatorAgent",
        "query_plan": plan.to_dict(),
    }


def handle_librarian(
    inputs: Dict[str, Any],
    call_tool: Optional[ToolCaller] = None,
) -> Dict[str, Any]:
    query = _extract_query(inputs)
    if call_tool is not None and query:
        tool_result = call_tool(
            "tantivy_search",
            {
                "query": query,
                "index": "documents",
                "limit": int(inputs.get("top_k", 10)),
                "language": _detect_language(query),
            },
        )
        return _normalize_librarian_hits(tool_result, query)

    from .chat import SearchPipeline

    pipeline = SearchPipeline(corpus_root=_get_corpus_root(inputs))
    result = pipeline.search(
        query,
        top_k=int(inputs.get("top_k", 10)),
        cite_style=str(inputs.get("cite_style") or "chicago-author-date"),
    )
    return {"agent": "LibrarianAgent", **result}


def handle_ingest(inputs: Dict[str, Any], _call_tool: Optional[ToolCaller] = None) -> Dict[str, Any]:
    from ..ingestion import CiteIndexIngestionOrchestrator
    from ..ingestion.models import IngestionConfig

    input_ref = inputs.get("input_ref") or inputs.get("path") or inputs.get("url")
    if not isinstance(input_ref, str) or not input_ref.strip():
        raise ValueError("missing input_ref/path/url")

    orchestrator = CiteIndexIngestionOrchestrator(corpus_root=_get_corpus_root(inputs))
    config = IngestionConfig(doc_type_override=inputs.get("doc_type_override"))
    result = orchestrator.ingest(input_ref, config=config)
    if _call_tool is not None and result.get("status") == "ok":
        try:
            result["kernel_index"] = _index_ingested_document(result, _call_tool)
        except Exception as exc:
            logging.warning("kernel indexing after ingest failed: %s", exc)
            result["kernel_index_warning"] = str(exc)
    return {"agent": "IngestAgent", **result}


def handle_claim_extraction(
    inputs: Dict[str, Any], _call_tool: Optional[ToolCaller] = None
) -> Dict[str, Any]:
    text = _extract_text(inputs)
    claims = []
    for idx, sentence in enumerate(_split_sentences(text)[:20], start=1):
        if len(sentence) < 20:
            continue
        entities = sorted(set(re.findall(r"\b[A-Z][a-zA-Z0-9_-]+\b", sentence)))
        claims.append(
            {
                "claim_id": f"claim-{idx}",
                "claim_text": sentence,
                "section_ref": inputs.get("section_ref", ""),
                "verbatim_passage": sentence,
                "polarity_tag": "neutral",
                "entities": entities,
                "hierarchy_path": inputs.get("hierarchy_path", "/"),
            }
        )

    return {
        "agent": "ClaimExtractionAgent",
        "claims": claims,
    }


def handle_contradiction(
    inputs: Dict[str, Any], _call_tool: Optional[ToolCaller] = None
) -> Dict[str, Any]:
    claims = inputs.get("claims") or []
    if not isinstance(claims, list):
        claims = []

    edges = []
    seen = {}
    for claim in claims:
        if not isinstance(claim, dict):
            continue
        text = claim.get("claim_text", "")
        key = text.strip().lower()
        if not key:
            continue
        if key in seen:
            edges.append(
                {
                    "claim_a_id": seen[key],
                    "claim_b_id": claim.get("claim_id", ""),
                    "explanation": "duplicate claim text detected by adapter",
                }
            )
        else:
            seen[key] = claim.get("claim_id", "")

    return {
        "agent": "ContradictionAgent",
        "edges": edges,
    }


def handle_gap_identification(
    inputs: Dict[str, Any], _call_tool: Optional[ToolCaller] = None
) -> Dict[str, Any]:
    goal_state = inputs.get("goal_state") if isinstance(inputs.get("goal_state"), dict) else {}
    required = inputs.get("required_aspects") or goal_state.get("required_aspects") or []
    coverage = inputs.get("coverage_scores") or goal_state.get("aspect_coverage") or {}
    threshold = float(inputs.get("coverage_threshold") or goal_state.get("coverage_threshold") or 0.6)

    gaps = []
    for aspect in required:
        score = float(coverage.get(aspect, 0.0)) if isinstance(coverage, dict) else 0.0
        if score < threshold:
            gaps.append(
                {
                    "aspect": aspect,
                    "coverage": score,
                    "suggested_terms": [term for term in re.split(r"\W+", aspect.lower()) if term],
                }
            )

    return {
        "agent": "GapIdentificationAgent",
        "gaps": gaps,
    }


def handle_literature_review(
    inputs: Dict[str, Any], _call_tool: Optional[ToolCaller] = None
) -> Dict[str, Any]:
    from .chat import SearchPipeline

    query = _extract_query(inputs)
    pipeline = SearchPipeline(corpus_root=_get_corpus_root(inputs))
    result = pipeline.search(query, top_k=int(inputs.get("top_k", 20)))
    return {
        "agent": "LiteratureReviewAgent",
        "review": result,
    }


def handle_hierarchy_classification(
    inputs: Dict[str, Any], _call_tool: Optional[ToolCaller] = None
) -> Dict[str, Any]:
    query = (_extract_query(inputs) + " " + _extract_text(inputs)).lower()
    if any(term in query for term in ("transformer", "attention", "rnn", "nlp")):
        path = "/cs/nlp"
    elif any(term in query for term in ("church", "bible", "orthodox", "theology", "christian")):
        path = "/religion/christianity"
    else:
        path = "/general"

    return {
        "agent": "HierarchyClassificationAgent",
        "hierarchy_path": path,
        "confidence": 0.7,
    }


def handle_structure(
    inputs: Dict[str, Any], _call_tool: Optional[ToolCaller] = None
) -> Dict[str, Any]:
    claims = inputs.get("claims") or []
    if not isinstance(claims, list):
        claims = []

    nodes = []
    for idx, claim in enumerate(claims[:10], start=1):
        if not isinstance(claim, dict):
            continue
        nodes.append(
            {
                "id": f"node-{idx}",
                "heading_suggestion": claim.get("section_ref") or f"Section {idx}",
                "supporting_claims": [claim.get("claim_id", f"claim-{idx}")],
                "dependency_ids": [],
                "coverage": "Partial",
                "comparable_section_refs": [],
            }
        )

    return {
        "agent": "StructureAgent",
        "nodes": nodes,
    }


HANDLERS: Dict[str, Callable[[Dict[str, Any], Optional[ToolCaller]], Dict[str, Any]]] = {
    "CoordinatorAgent": handle_coordinator,
    "LibrarianAgent": handle_librarian,
    "IngestAgent": handle_ingest,
    "ClaimExtractionAgent": handle_claim_extraction,
    "ContradictionAgent": handle_contradiction,
    "GapIdentificationAgent": handle_gap_identification,
    "LiteratureReviewAgent": handle_literature_review,
    "HierarchyClassificationAgent": handle_hierarchy_classification,
    "StructureAgent": handle_structure,
}


def serve(agent_name: str) -> None:
    _configure_logging(agent_name)
    handler = HANDLERS[agent_name]
    logging.info("starting adapter runtime for %s", agent_name)

    while True:
        line = sys.stdin.readline()
        if line == "":
            break
        if not line.strip():
            continue

        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            logging.warning("invalid JSON input ignored")
            continue

        msg_type = message.get("type")
        if msg_type == "init":
            _emit(
                {
                    "type": "init_ack",
                    "agent_name": agent_name,
                    "protocol_version": PROTOCOL_VERSION,
                    "status": "ok",
                    "error": None,
                }
            )
            continue

        if msg_type == "shutdown":
            _emit({"type": "shutdown_ack", "agent_name": agent_name})
            break

        if msg_type == "tool_response":
            # Thin adapters do not drive tool loops yet. Ignore responses.
            continue

        if msg_type != "request":
            logging.info("ignoring message type %s", msg_type)
            continue

        task_id = message.get("task_id", "unknown")
        inputs = message.get("inputs") if isinstance(message.get("inputs"), dict) else {}

        def call_tool(tool: str, params: Dict[str, Any]) -> Dict[str, Any]:
            call_id = f"{agent_name}:{uuid.uuid4()}"
            _emit({"type": "tool_call", "call_id": call_id, "tool": tool, "params": params})

            while True:
                response_line = sys.stdin.readline()
                if response_line == "":
                    raise RuntimeError(f"EOF while waiting for tool response {call_id}")
                if not response_line.strip():
                    continue

                response = json.loads(response_line)
                if response.get("type") != "tool_response":
                    logging.info(
                        "ignoring message type %s while waiting for tool response",
                        response.get("type"),
                    )
                    continue
                if response.get("call_id") != call_id:
                    logging.warning(
                        "ignoring tool_response for %s while waiting for %s",
                        response.get("call_id"),
                        call_id,
                    )
                    continue

                error = response.get("error")
                if isinstance(error, dict) and error.get("message"):
                    raise RuntimeError(str(error["message"]))
                result = response.get("result")
                if isinstance(result, dict):
                    return result
                return {"value": result}

        try:
            _emit(
                {
                    "type": "progress",
                    "task_id": task_id,
                    "stage": "PLAN",
                    "iteration": 1,
                    "detail": f"{agent_name} adapter handling request",
                    "tool_calls_so_far": 0,
                    "llm_calls_so_far": 0,
                }
            )
            output = handler(inputs, call_tool)
            _emit(
                {
                    "type": "result",
                    "task_id": task_id,
                    "status": "ok",
                    "output": output,
                    "output_hash": _hash_output(output),
                    "resource_usage": _resource_usage(),
                }
            )
        except Exception as exc:  # pragma: no cover - defensive runtime path
            logging.error("agent adapter failed: %s", exc)
            logging.debug(traceback.format_exc())
            _emit(
                {
                    "type": "error",
                    "task_id": task_id,
                    "error_type": exc.__class__.__name__,
                    "message": str(exc),
                    "recoverable": True,
                    "partial_output": None,
                }
            )


def main(agent_name: str) -> None:
    serve(agent_name)
