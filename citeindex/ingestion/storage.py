import json
import os
from typing import Any, Dict

from .deterministic import canonical_json_dumps


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def write_json(path: str, data: Any) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(canonical_json_dumps(data))
        f.write("\n")


def append_jsonl(path: str, data: Dict[str, Any]) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, sort_keys=True))
        f.write("\n")


def store_corpus_artifacts(corpus_root: str, document_hash: str, artifacts: Dict[str, Any]) -> str:
    doc_dir = os.path.join(corpus_root, document_hash)
    ensure_dir(doc_dir)

    if artifacts.get("csl_json") is not None:
        write_json(os.path.join(doc_dir, "csl.json"), artifacts["csl_json"])
    if artifacts.get("document_json") is not None:
        write_json(os.path.join(doc_dir, "document.json"), artifacts["document_json"])
    if artifacts.get("transcript_json") is not None:
        write_json(os.path.join(doc_dir, "transcript.json"), artifacts["transcript_json"])
    if artifacts.get("merkle_tree") is not None:
        write_json(os.path.join(doc_dir, "merkle.json"), artifacts["merkle_tree"])
    if artifacts.get("media_metadata") is not None:
        write_json(os.path.join(doc_dir, "media_metadata.json"), artifacts["media_metadata"])
    if artifacts.get("retrieval_index") is not None:
        write_json(os.path.join(doc_dir, "index.json"), artifacts["retrieval_index"])

    return doc_dir
