"""Create and validate quotation-backed agent audit proposals."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main(source_path: str, output_path: str) -> None:
    source = Path(source_path).expanduser().resolve()
    if not source.is_file():
        raise SystemExit("audit requires a local original file or saved URL snapshot")
    payload = {
        "source_path": str(source),
        "source_sha256": _sha256(source) if source.is_file() else None,
        "proposals": [],
        "instructions": "Add status=proposed entries with field, old_value, proposed_value, quote, exact locator, and reason identifying the host. Copy locator from source_blocks evidence; never cite generated metadata.",
    }
    with Path(output_path).open("x", encoding="utf-8") as output:
        output.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    print(output_path)


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: audit.py source-path proposal.json")
    main(*sys.argv[1:])
