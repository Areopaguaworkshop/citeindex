import json
import os
import re
import shutil
import tempfile
from typing import Any, Dict

from .deterministic import canonical_json_dumps, hash_payload


def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)


def write_json(path: str, data: Any) -> None:
    ensure_dir(os.path.dirname(path))
    directory = os.path.dirname(path) or "."
    fd, temporary_path = tempfile.mkstemp(prefix=".citeindex-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(canonical_json_dumps(data))
            f.write("\n")
        os.replace(temporary_path, path)
    finally:
        if os.path.exists(temporary_path):
            os.remove(temporary_path)


def append_jsonl(path: str, data: Dict[str, Any]) -> None:
    ensure_dir(os.path.dirname(path))
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(data, ensure_ascii=False, sort_keys=True))
        f.write("\n")


def _is_cjk(ch: str) -> bool:
    return "\u4e00" <= ch <= "\u9fff"


def _slugify(text: str, max_len: int = 0) -> str:
    result = re.sub(r"[^\w\u4e00-\u9fff]", "_", text, flags=re.UNICODE)
    result = re.sub(r"_+", "_", result).strip("_")
    parts = []
    for ch in result:
        if _is_cjk(ch):
            parts.append(ch)
        else:
            parts.append(ch.lower())
    result = "".join(parts)
    if max_len and len(result) > max_len:
        result = result[:max_len].rstrip("_")
    return result


def csl_folder_name(csl_json: Dict[str, Any]) -> str:
    author_part = ""
    authors = csl_json.get("author") or []
    if authors:
        first = authors[0]
        if "literal" in first:
            author_part = first["literal"]
        elif "family" in first:
            author_part = first["family"]

    year_part = ""
    try:
        year_part = str(csl_json["issued"]["date-parts"][0][0])
    except (KeyError, IndexError, TypeError):
        pass

    no_part = ""
    if csl_json.get("issue"):
        no_part = str(csl_json["issue"])
    elif csl_json.get("volume"):
        no_part = str(csl_json["volume"])

    title_part = ""
    raw_title = csl_json.get("title") or ""
    if raw_title:
        title_part = _slugify(raw_title[:30])

    publisher_part = ""
    raw_publisher = csl_json.get("publisher") or ""
    if raw_publisher:
        publisher_part = _slugify(raw_publisher[:15])

    if not author_part and not title_part:
        return hash_payload(csl_json)[:16]

    author_slug = _slugify(author_part)
    no_slug = _slugify(no_part) if no_part else ""

    segments = [s for s in [author_slug, year_part, no_slug, title_part, publisher_part] if s]
    name = "_".join(segments)

    if len(name) > 64:
        name = name[:64].rstrip("_")

    identity = str(csl_json.get("content_hash") or hash_payload(csl_json))
    return f"{name}_{identity[:12]}"


def store_corpus_artifacts(corpus_root: str, folder_name: str, artifacts: Dict[str, Any]) -> str:
    if os.path.basename(folder_name) != folder_name or folder_name in {"", ".", ".."}:
        raise ValueError("folder_name must be a single safe directory name")
    doc_dir = os.path.join(corpus_root, folder_name)
    staging_dir = None if os.path.exists(doc_dir) else tempfile.mkdtemp(
        prefix=f".{folder_name}.", dir=corpus_root,
    )
    target_dir = staging_dir or doc_dir
    ensure_dir(target_dir)

    try:
        if artifacts.get("csl_json") is not None:
            write_json(os.path.join(target_dir, "csl.json"), artifacts["csl_json"])
        if artifacts.get("document_json") is not None:
            write_json(os.path.join(target_dir, "document.json"), artifacts["document_json"])
        if artifacts.get("transcript_json") is not None:
            write_json(os.path.join(target_dir, "transcript.json"), artifacts["transcript_json"])
        if artifacts.get("merkle_tree") is not None:
            write_json(os.path.join(target_dir, "merkle.json"), artifacts["merkle_tree"])
        if artifacts.get("media_metadata") is not None:
            write_json(os.path.join(target_dir, "media_metadata.json"), artifacts["media_metadata"])
        if artifacts.get("pageindex_tree") is not None:
            write_json(os.path.join(target_dir, "pageindex_tree.json"), artifacts["pageindex_tree"])
        if artifacts.get("citation_verification") is not None:
            write_json(os.path.join(target_dir, "citation_verification.json"), artifacts["citation_verification"])
        if staging_dir:
            os.replace(staging_dir, doc_dir)
            staging_dir = None
    finally:
        if staging_dir:
            shutil.rmtree(staging_dir, ignore_errors=True)

    return doc_dir
