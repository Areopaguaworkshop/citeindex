"""Create a candidate benchmark manifest from a local source tree.

This never invents gold CSL labels. Review each row before scoring.
Usage: python prepare_manifest.py SOURCE_ROOT manifest.jsonl --limit 40
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(source_root: str, manifest_path: str, limit: int) -> None:
    root = Path(source_root).expanduser().resolve()
    candidates = sorted({path for path in root.rglob("*") if path.is_file() and path.suffix.casefold() == ".pdf"}, key=lambda p: str(p).casefold())
    groups: dict[str, list[Path]] = {}
    for path in candidates:
        relative = path.relative_to(root)
        group = relative.parts[0] if len(relative.parts) > 1 else "root"
        groups.setdefault(group, []).append(path)
    paths = []
    while groups and len(paths) < limit:
        for group in sorted(list(groups)):
            paths.append(groups[group].pop(0))
            if not groups[group]:
                del groups[group]
            if len(paths) == limit:
                break
    if len(paths) < limit:
        raise SystemExit(f"found only {len(paths)} PDFs under {root}; need {limit}")
    output = Path(manifest_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8") as stream:
        for path in paths[:limit]:
            row = {
                "id": path.stem.casefold().replace(" ", "-")[:80],
                "modality": "needs_review",
                "source_path": str(path),
                "source_sha256": _sha256(path),
                "review_status": "needs_review",
                "csl": {},
                "field_evidence": {},
            }
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    print(f"wrote {limit} candidate rows to {output}; review modality, CSL, and evidence before scoring")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source_root")
    parser.add_argument("manifest_path")
    parser.add_argument("--limit", type=int, default=40)
    args = parser.parse_args()
    main(args.source_root, args.manifest_path, args.limit)
