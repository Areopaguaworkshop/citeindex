"""Locate CiteIndex harness skills and expose them as MCP resources."""

from __future__ import annotations

import os
import re
from pathlib import Path

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_HARNESS_DIRS = (".agents", ".claude", ".codex", ".pi")


def _roots() -> list[Path]:
    roots: list[Path] = []
    env = os.environ.get("CITEINDEX_SKILL_ROOT")
    if env:
        roots.extend(Path(part).expanduser().resolve() for part in env.split(os.pathsep) if part.strip())
    repo = Path(__file__).resolve().parents[2]
    candidates = [Path.cwd(), repo]
    for base in candidates:
        for harness in _HARNESS_DIRS:
            roots.append((base / harness / "skills").resolve())
    roots.append((Path(__file__).resolve().parent / "skills").resolve())
    return list(dict.fromkeys(roots))


def list_skills() -> list[str]:
    names: set[str] = set()
    for root in _roots():
        if not root.is_dir():
            continue
        for entry in root.iterdir():
            if entry.is_dir() and (entry / "SKILL.md").is_file():
                names.add(entry.name)
            elif entry.is_file() and entry.suffix == ".md":
                names.add(entry.stem)
    return sorted(names)


def load_skill(name: str) -> str:
    if not _NAME_RE.match(name or ""):
        raise ValueError(f"invalid skill name: {name!r}")
    for root in _roots():
        candidate = root / name / "SKILL.md"
        if candidate.is_file():
            return candidate.read_text(encoding="utf-8")
        fallback = root / f"{name}.md"
        if fallback.is_file():
            return fallback.read_text(encoding="utf-8")
    raise FileNotFoundError(
        f"skill {name!r} not found; set CITEINDEX_SKILL_ROOT to the repository skills directory"
    )
