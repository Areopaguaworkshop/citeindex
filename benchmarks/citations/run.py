"""Score saved CiteIndex CSL predictions against reviewed JSONL labels.

Usage: python benchmarks/citations/run.py manifest.jsonl predictions.jsonl
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

FIELDS = ("author", "title", "issued", "publisher", "publisher-place", "DOI", "ISBN")


def _records(path: Path) -> dict[str, dict]:
    return {row["id"]: row for row in (json.loads(line) for line in path.read_text().splitlines() if line.strip())}


def main(manifest_path: str, predictions_path: str) -> None:
    gold, predictions = _records(Path(manifest_path)), _records(Path(predictions_path))
    report = {"attempted": len(gold), "predicted": len(predictions), "fields": {}}
    for field in FIELDS:
        tp = fp = fn = 0
        for source_id, item in gold.items():
            expected = item.get("csl", {}).get(field)
            actual = predictions.get(source_id, {}).get("csl", {}).get(field)
            if expected == actual and expected is not None:
                tp += 1
            elif actual is not None:
                fp += 1
                fn += expected is not None
            elif expected is not None:
                fn += 1
        precision = tp / (tp + fp) if tp + fp else None
        recall = tp / (tp + fn) if tp + fn else None
        report["fields"][field] = {"tp": tp, "fp": fp, "fn": fn, "precision": precision, "recall": recall}
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: run.py manifest.jsonl predictions.jsonl")
    main(*sys.argv[1:])
