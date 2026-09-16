"""Assign reproducible development/validation/test splits to a reviewed manifest."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


def main(manifest_path: str, output_path: str) -> None:
    rows = [json.loads(line) for line in Path(manifest_path).read_text().splitlines() if line.strip()]
    groups: dict[str, list[dict]] = {}
    for row in rows:
        if not row.get("work_family") or row.get("modality") not in {"digital_pdf", "scanned_pdf", "media", "url_article"}:
            raise ValueError("review work_family and modality before freezing splits")
        family = str(row.get("work_family") or row.get("id", "")).casefold()
        groups.setdefault(family, []).append(row)
    ordered = sorted(groups.items(), key=lambda item: hashlib.sha256(item[0].encode()).hexdigest())
    split_names = ("development", "validation", "test")
    targets = (0.5, 0.1667, 0.3333)
    counts = [0, 0, 0]
    strata = {}
    total = len(rows)
    for _, group in ordered:
        # ponytail: greedy group stratification; tiny strata cannot have exact ratios.
        stratum = tuple(sorted({(r["modality"], r.get("language", "unknown")) for r in group}))
        bucket = strata.setdefault(stratum, [0, 0, 0])
        index = min(range(3), key=lambda i: (bucket[i] / targets[i], counts[i] / targets[i]))
        bucket[index] += len(group)
        for row in group:
            row["split"] = split_names[index]
            counts[index] += 1
    with Path(output_path).open("x", encoding="utf-8") as output:
        output.write("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in rows))
    print(json.dumps({"rows": total, "counts": dict(zip(split_names, counts))}, indent=2))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: split.py manifest.jsonl split-manifest.jsonl")
    main(*sys.argv[1:])
