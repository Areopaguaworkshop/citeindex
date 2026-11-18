"""Configuration, path safety, and audit helpers for the CiteIndex MCP server."""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_AUDIT_LOCK = threading.Lock()


@dataclass(frozen=True)
class ServerConfig:
    corpus_root: Path
    library_root: Path
    allow_write: bool
    max_url_seconds: int
    audit: bool
    input_roots: tuple[Path, ...] = ()


_CONFIG: ServerConfig | None = None


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def build_config(corpus_root: str | Path | None = None, allow_write: bool | None = None,
                 max_url_seconds: int | None = None, *, input_roots: list[str] | None = None) -> ServerConfig:
    root = Path(
        corpus_root
        or os.environ.get("CITEINDEX_CORPUS_ROOT")
        or Path.cwd() / "corpus"
    ).expanduser().resolve()
    return ServerConfig(
        corpus_root=root,
        library_root=(root.parent / "library").resolve(),
        allow_write=_env_flag("CITEINDEX_MCP_ALLOW_WRITE") if allow_write is None else allow_write,
        max_url_seconds=max(
            10,
            min(
                int(max_url_seconds or os.environ.get("CITEINDEX_MCP_MAX_URL_SECONDS", "180")),
                600,
            ),
        ),
        audit=not os.environ.get("CITEINDEX_MCP_AUDIT", "").strip() == "0",
        input_roots=tuple(Path(part).expanduser().resolve() for part in (
            input_roots if input_roots is not None
            else os.environ.get("CITEINDEX_MCP_INPUT_ROOTS", "").split(os.pathsep)
        ) if part),
    )


def configure(corpus_root: str | Path | None = None, allow_write: bool | None = None,
              max_url_seconds: int | None = None, *, input_roots: list[str] | None = None) -> ServerConfig:
    global _CONFIG
    _CONFIG = build_config(corpus_root, allow_write, max_url_seconds, input_roots=input_roots)
    return _CONFIG


def config() -> ServerConfig:
    return _CONFIG or configure()


def require_write(tool: str) -> None:
    if not config().allow_write:
        raise PermissionError(
            f"{tool} writes corpus artifacts; start the server with --allow-write "
            "or set CITEINDEX_MCP_ALLOW_WRITE=1"
        )


def resolve_input_file(path: str, *, settings: ServerConfig | None = None) -> Path:
    """Accept only regular files under a configured input root."""
    candidate = Path(path).expanduser().resolve()
    if not any(candidate != root and candidate.is_relative_to(root) for root in (settings or config()).input_roots):
        raise PermissionError("The file must be within a configured MCP input root")
    if not candidate.is_file():
        raise FileNotFoundError("The input must be an existing regular file")
    return candidate


def resolve_corpus_child(name: str) -> Path:
    """Resolve a record directory under the corpus root, rejecting traversal."""
    root = config().corpus_root
    candidate = (root / name).resolve()
    if candidate.parent != root:
        raise ValueError(f"record path escapes the corpus root: {name!r}")
    return candidate


def audit_event(tool: str, payload: dict[str, Any]) -> None:
    if not config().audit:
        return
    path = config().corpus_root / "mcp_audit.jsonl"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps({"tool": tool, **payload}, ensure_ascii=False, default=str)
        with _AUDIT_LOCK:
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
    except OSError:
        # Audit failure must not break the tool call.
        pass
