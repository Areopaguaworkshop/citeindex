"""Convert fetched ScienceMetaBench rows into CiteIndex manifest rows.

The manifest row carries the keys ``benchmarks/citations/run.py`` consumes —
``id``, ``modality``, ``source_path``, ``source_sha256``, ``csl`` and
``field_status`` — so the same manifest can feed CiteIndex ingestion.

Gold mapping (books only: ``textbook`` / ``ebook``):

===========================  ==========================================
``isbn``                     ``ISBN`` (single value; first if a list)
``title``                    ``title``
``author``                   ``author`` as ``[{"literal": name}, ...]``
``publisher``                ``publisher``
``pub_time``                 ``issued`` as ``{"date-parts": [[year]]}``
``category``                 ``gold.extra.category`` only
===========================  ==========================================

``abstract``, ``doi`` and ``keyword`` are provenance: they live under
``gold.extra`` and are never scored. ``type`` is a routing hint for CiteIndex
(``book``), not a gold field, so it is absent from ``field_status``.

``field_status`` records ``present`` for gold-supplied fields and
``outside_scope`` for fields the dataset leaves empty, so the scorer excludes
them from every denominator.

Usage:
    python manifest.py --rows data/rows.jsonl --out manifest.jsonl \
        --modality textbook

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

SCORED_FIELDS = ("title", "author", "publisher", "issued", "ISBN")
EXTRA_FIELDS = ("abstract", "doi", "keyword", "category")
TYPE_BY_KIND = {"textbook": "book", "ebook": "book", "paper": "article-journal"}


def _text(value: object) -> str:
    """Collapse whitespace; the dataset has newlines and stray spaces."""
    return " ".join(str(value or "").split())


def year(pub_time: object) -> int | None:
    """A four-digit publication year, or None when the value is absent/garbled."""
    match = re.search(r"(?<!\d)(\d{4})(?!\d)", _text(pub_time))
    if not match:
        return None
    value = int(match.group(1))
    return value if 1000 <= value <= 2999 else None


def authors(value: object) -> list[dict]:
    """Comma-separated author string -> ordered CSL literal names."""
    return [{"literal": name} for name in (_text(part) for part in _text(value).split(",")) if name]


def isbn(value: object) -> str:
    """CSL ISBN is a single identifier; the dataset sometimes lists several."""
    return _text(value).split(",")[0].strip()


def build_row(row: dict, modality: str, pdf_directory: Path | None = None) -> dict:
    sha256 = _text(row.get("sha256")).lower()
    csl: dict = {"type": TYPE_BY_KIND.get(modality, "book")}
    title = _text(row.get("title"))
    if title:
        csl["title"] = title
    names = authors(row.get("author"))
    if names:
        csl["author"] = names
    publisher = _text(row.get("publisher"))
    if publisher:
        csl["publisher"] = publisher
    published = year(row.get("pub_time"))
    if published is not None:
        csl["issued"] = {"date-parts": [[published]]}
    identifier = isbn(row.get("isbn"))
    if identifier:
        csl["ISBN"] = identifier
    extra = {field: _text(row.get(field)) for field in EXTRA_FIELDS if _text(row.get(field))}
    return {
        "id": sha256,
        "modality": modality,
        "source_path": str((pdf_directory or Path(".")) / f"{sha256}.pdf"),
        "source_sha256": sha256,
        "csl": csl,
        "field_status": {field: "present" if csl.get(field) is not None else "outside_scope"
                         for field in SCORED_FIELDS},
        "field_evidence": {},
        "review_status": "dataset_gold",
        "gold": {"origin": "opendatalab/ScienceMetaBench",
                 "kind": row.get("kind"), "batch": row.get("batch"), "extra": extra},
    }


def main(rows_path: Path, out_path: Path, modality: str) -> int:
    rows = [json.loads(line) for line in rows_path.read_text(encoding="utf-8").split("\n") if line.strip()]
    selected = [row for row in rows if row.get("kind") == modality]
    skipped = len(rows) - len(selected)
    pdf_directory = rows_path.parent / "pdf"
    manifest = [build_row(row, modality, pdf_directory) for row in selected]
    missing = [row["id"] for row in manifest if not Path(row["source_path"]).exists()]
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as stream:
        for row in manifest:
            stream.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
    outside = sum(state == "outside_scope" for row in manifest for state in row["field_status"].values())
    print(f"wrote {len(manifest)} {modality} rows to {out_path} "
          f"({skipped} rows of other kinds skipped, {outside} gold-absent fields marked outside_scope)")
    if missing:
        print(f"warning: {len(missing)} PDFs not downloaded yet: {', '.join(sha[:12] for sha in missing[:5])}"
              f"{' ...' if len(missing) > 5 else ''}")
    return 0 if manifest else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--rows", default="data/rows.jsonl")
    parser.add_argument("--out", default="manifest.jsonl")
    parser.add_argument("--modality", required=True, choices=("textbook", "ebook"))
    arguments = parser.parse_args()
    raise SystemExit(main(Path(arguments.rows), Path(arguments.out), arguments.modality))
