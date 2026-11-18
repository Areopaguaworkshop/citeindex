"""Write the unfiltered ScienceMetaBench manifest: every textbook and ebook row.

``manifest.py`` writes one modality at a time. This is the "all" variant used by
``census.py``: it reads ``data/rows.jsonl`` and emits one manifest row per
textbook/ebook record, built by ``manifest.build_row`` and therefore identical
in shape to a filtered manifest (``id``, ``modality``, ``source_path``,
``source_sha256``, ``csl``, ``field_status``, ``field_evidence``,
``review_status``, ``gold``). Nothing is dropped except non-book kinds and
duplicate record ids.

Usage:
    python build_manifest.py --rows data/rows.jsonl --out data/manifest-all.jsonl

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import manifest  # noqa: E402

MODALITIES = ("textbook", "ebook")


def build_all(rows: list[dict], pdf_directory: Path) -> tuple[list[dict], int, int]:
    """All textbook/ebook manifest rows, plus (other kinds, duplicate ids) counts."""
    selected = [row for row in rows if row.get("kind") in MODALITIES]
    skipped = len(rows) - len(selected)
    built: list[dict] = []
    seen: set[str] = set()
    duplicates = 0
    for row in selected:
        entry = manifest.build_row(row, row["kind"], pdf_directory)
        if entry["id"] in seen:
            duplicates += 1
            continue
        seen.add(entry["id"])
        built.append(entry)
    return built, skipped, duplicates


def main(rows_path: Path, out_path: Path) -> int:
    rows = [json.loads(line) for line in rows_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    manifest_rows, skipped, duplicates = build_all(rows, rows_path.parent / "pdf")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as stream:
        for row in manifest_rows:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    counts = {modality: sum(row["modality"] == modality for row in manifest_rows) for modality in MODALITIES}
    outside = sum(state == "outside_scope" for row in manifest_rows for state in row["field_status"].values())
    print(f"wrote {len(manifest_rows)} rows to {out_path} "
          f"({counts['textbook']} textbook, {counts['ebook']} ebook, {skipped} rows of other kinds "
          f"skipped, {duplicates} duplicate ids dropped, {outside} gold-absent fields marked outside_scope)")
    missing = [row["id"] for row in manifest_rows if not Path(row["source_path"]).exists()]
    if missing:
        print(f"warning: {len(missing)} PDFs not downloaded yet: "
              f"{', '.join(sha[:12] for sha in missing[:5])}{' ...' if len(missing) > 5 else ''}")
    return 0 if manifest_rows else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--rows", default="data/rows.jsonl")
    parser.add_argument("--out", default="data/manifest-all.jsonl")
    arguments = parser.parse_args()
    raise SystemExit(main(Path(arguments.rows), Path(arguments.out)))
