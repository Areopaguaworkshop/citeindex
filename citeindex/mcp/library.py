"""Read-only access to CiteIndex corpus records for the MCP server."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from citeindex.ingestion.csl import export_csl
from citeindex.ingestion.master import file_sha256
from citeindex.ingestion.models import IngestionConfig

from .security import audit_event, config, resolve_corpus_child, resolve_input_file


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _record_dirs() -> list[Path]:
    root = config().corpus_root
    if not root.is_dir():
        return []
    dirs = [
        entry for entry in root.iterdir()
        if entry.is_dir() and not entry.is_symlink() and not entry.name.startswith(".")
        and (entry / "csl.json").is_file()
    ]
    return sorted(dirs, key=lambda path: (path / "csl.json").stat().st_mtime, reverse=True)


def _author_labels(csl: dict[str, Any]) -> list[str]:
    labels: list[str] = []
    for author in csl.get("author") or []:
        if not isinstance(author, dict):
            continue
        if author.get("literal"):
            labels.append(str(author["literal"]))
        else:
            name = " ".join(str(author.get(key, "")).strip() for key in ("given", "family")).strip()
            if name:
                labels.append(name)
    return labels


def _issued(csl: dict[str, Any]) -> str | None:
    try:
        parts = csl["issued"]["date-parts"][0]
    except (KeyError, IndexError, TypeError):
        return None
    return "-".join(f"{part:02d}" if index else str(part) for index, part in enumerate(parts))


def _summary(folder: Path, csl: dict[str, Any]) -> dict[str, Any]:
    return {
        "source_id": csl.get("id") or folder.name,
        "folder": folder.name,
        "type": csl.get("type"),
        "title": csl.get("title"),
        "author": _author_labels(csl),
        "issued": _issued(csl),
        "DOI": csl.get("DOI"),
        "URL": csl.get("URL"),
        "container-title": csl.get("container-title"),
        "citation_status": csl.get("_citation_status"),
        "path": str(folder),
    }


def _match(folder: Path, csl: dict[str, Any], source_id: str) -> bool:
    target = source_id.strip().casefold()
    if folder.name.casefold() == target:
        return True
    for value in (csl.get("id"), csl.get("DOI"), csl.get("URL")):
        if isinstance(value, str) and value.casefold() == target:
            return True
    return False


def find_record(source_id: str, *, exact: bool = False) -> tuple[Path, dict[str, Any]]:
    if not source_id or source_id in {".", ".."}:
        raise ValueError("source_id must be a record id or corpus folder name")
    if "/" not in source_id and "\\" not in source_id:
        direct = resolve_corpus_child(source_id)
        if direct.is_dir() and (direct / "csl.json").is_file():
            csl = _read_json(direct / "csl.json") or {}
            return direct, csl
    for folder in _record_dirs():
        csl = _read_json(folder / "csl.json") or {}
        if csl.get("id") == source_id or (not exact and _match(folder, csl, source_id)):
            return folder, csl
    raise KeyError(f"no corpus record matches source_id={source_id!r}")


def library_list(limit: int = 20, offset: int = 0, kind: str | None = None) -> dict[str, Any]:
    """List corpus records, newest first."""
    limit = max(1, min(int(limit), 100))
    offset = max(0, int(offset))
    rows: list[dict[str, Any]] = []
    for folder in _record_dirs():
        csl = _read_json(folder / "csl.json") or {}
        if kind and str(csl.get("type") or "") != kind:
            continue
        rows.append(_summary(folder, csl))
    total = len(rows)
    page = rows[offset:offset + limit]
    audit_event("library_list", {"limit": limit, "offset": offset, "kind": kind, "returned": len(page)})
    return {"total": total, "offset": offset, "limit": limit, "records": page}


def library_search(query: str, limit: int = 10) -> dict[str, Any]:
    """Search title, authors, DOI, URL, container, and folder name."""
    needle = (query or "").strip().casefold()
    if not needle:
        raise ValueError("query must not be empty")
    limit = max(1, min(int(limit), 50))
    scored: list[tuple[int, dict[str, Any]]] = []
    for folder in _record_dirs():
        csl = _read_json(folder / "csl.json") or {}
        title = str(csl.get("title") or "").casefold()
        authors = " ".join(_author_labels(csl)).casefold()
        container = str(csl.get("container-title") or "").casefold()
        score = 0
        if title == needle:
            score = 100
        elif needle in title and title:
            score = 60
        elif str(csl.get("DOI") or "").casefold() == needle:
            score = 90
        elif str(csl.get("URL") or "").casefold() == needle:
            score = 85
        elif needle in authors:
            score = 40
        elif needle in container:
            score = 30
        elif needle in folder.name.casefold():
            score = 20
        if score:
            scored.append((score, _summary(folder, csl)))
    scored.sort(key=lambda item: item[0], reverse=True)
    records = [record for _, record in scored[:limit]]
    audit_event("library_search", {"query": query, "returned": len(records)})
    return {"query": query, "total": len(scored), "records": records}


def library_get(source_id: str) -> dict[str, Any]:
    """Return the full CSL record, artifact paths, and cached verification."""
    folder, csl = find_record(source_id)
    paths = {
        name: str(folder / name)
        for name in ("csl.json", "csl-export.json", "document.json", "merkle.json",
                     "citation_verification.json", "source.html", "source.media", "ingestion_output.json",
                     "source_blocks.json", "quotation_locators.json", "pageindex_tree.json", "transcript.json")
        if (folder / name).is_file()
    }
    markdown = config().library_root / f"{folder.name}.md"
    if markdown.is_file():
        paths["library_markdown"] = str(markdown)
    verification = _read_json(folder / "citation_verification.json")
    audit_event("library_get", {"source_id": source_id, "folder": folder.name})
    return {
        "source_id": csl.get("id") or folder.name,
        "folder": folder.name,
        "csl": csl,
        "paths": paths,
        "verification": verification,
    }


def record_csl(source_id: str) -> dict[str, Any]:
    _, csl = find_record(source_id)
    return csl


def record_manifest(source_id: str) -> str:
    folder, _ = find_record(source_id)
    path = folder / "merkle.json"
    if not path.is_file():
        raise FileNotFoundError(f"no Merkle manifest for {source_id!r}")
    return path.read_text(encoding="utf-8")


def record_verification(source_id: str) -> str:
    folder, _ = find_record(source_id)
    path = folder / "citation_verification.json"
    if not path.is_file():
        return "{}"
    return path.read_text(encoding="utf-8")


def citation_verify(
    source_id: str, offline: bool = True, *, fields: list[str] | None = None,
    quotation: str | None = None, locator: dict[str, Any] | None = None,
    original_file: str | None = None,
) -> dict[str, Any]:
    """Replay extraction evidence for a pinned record and prepare the host audit."""
    folder, csl = find_record(source_id, exact=True)
    output = _read_json(folder / "ingestion_output.json") or {}
    sub = output.get("sub_pipeline_outputs") or {}
    document_json = sub.get("document_json") or _read_json(folder / "document.json")
    resource_type = sub.get("resource_type") or (output.get("ingestion_log_entry") or {}).get("resource_type") or "url_article"
    legacy = sub.get("extra") or {}
    blocks = _read_json(folder / "source_blocks.json") or sub.get("source_blocks") or legacy.get("source_blocks") or []
    blocks = [block for block in blocks if isinstance(block, dict) and block.get("id")]
    identity = sub.get("source_identity") or {}
    digest = identity.get("sha256") or sub.get("source_snapshot_digest") or csl.get("source_sha256")
    artifact = identity.get("artifact")
    if artifact is None and resource_type == "url_article":
        artifact = "source.html"
    source = resolve_input_file(original_file) if original_file else None
    if source is None and artifact in {"source.html", "source.media"}:
        candidate = folder / artifact
        if candidate.is_file() and not candidate.is_symlink():
            source = candidate
    binding = "unavailable"
    if source:
        binding = "unrecorded" if not digest else "matches" if file_sha256(str(source)) == digest else "mismatch"

    from citeindex.ingestion.citation_verification import (
        quotation_entry,
        replay_citation_evidence,
        verify_citation_metadata,
    )

    report = replay_citation_evidence(csl, document_json, resource_type, blocks, fields, quotation, locator)
    quote = report.get("quotation")
    candidates = quote.get("candidates", [quote]) if quote else []
    snapshot_conflict = any(
        (coordinates := candidate.get("locator", {})).get("snapshot_artifact") == "source.html"
        and bool(digest) and coordinates.get("source_digest") != digest
        for candidate in candidates
    )
    has_checks = bool(report["field_states"]) or quote is not None
    evidence_matches = has_checks and all(
        state == "source-supported" for state in report["field_states"].values()
    ) and (quote is None or quote["status"] == "extraction-match")
    if quote and (binding == "mismatch" or snapshot_conflict):
        report["quotation"] = {"status": "needs-review", "reason": "source_digest_mismatch"}
    elif quote:
        for candidate in candidates:
            if "locator" in candidate:
                candidate.update(quotation_entry(
                    csl.get("id") or folder.name, digest, quote["quote"], candidate["locator"],
                ))
    status = "extraction-match" if evidence_matches and binding != "mismatch" and not snapshot_conflict else "needs-review"
    report.update(status=status, scope="stored-extraction", source_sha256=digest,
                  original_source={"binding": binding, "content_review": "host-required"})
    report["registry"] = {"status": "not_requested"}
    if not offline and binding != "mismatch":
        # Replay only corpus text. Stored metadata paths are not trusted file inputs.
        safe_document = {**document_json, "metadata": {**document_json.get("metadata", {}), "source_path": None}} if document_json else None
        cfg = IngestionConfig()
        _, registry_report = verify_citation_metadata(
            csl, safe_document, resource_type, {"source_blocks": blocks}, cfg,
        )
        report["registry"] = registry_report["registry"]
        report["proposed_corrections"] = registry_report["proposed_corrections"]
    audit_event("citation_verify", {"source_id": source_id, "offline": offline, "status": status})
    return {
        "source_id": csl.get("id") or folder.name,
        "folder": folder.name,
        "status": status,
        "report": report,
        "csl": csl,
        "host_audit": {
            "skill": "citeindex://skills/citation-verification",
            "prompt": "reverify-citation",
            "lookup": {key: csl[key] for key in ("DOI", "ISBN", "URL", "title", "edition") if csl.get(key)},
        },
    }


def citation_format(source_ids: list[str], style: str = "chicago-author-date") -> dict[str, Any]:
    """Render one or more records with citeproc."""
    if not source_ids:
        raise ValueError("source_ids must contain at least one record id")
    items = []
    for source_id in source_ids:
        csl = record_csl(source_id)
        item = export_csl(csl)
        item.pop("custom", None)
        items.append(item)
    from citeindex.citation_style import format_bibliography

    bibliography, in_text = format_bibliography(items, style)
    audit_event("citation_format", {"source_ids": source_ids, "style": style})
    return {"style": style, "bibliography": bibliography, "in_text": in_text}
