"""Merge two independent review passes and flag disagreements for adjudication."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def _rows(path: str) -> dict[str, dict]:
    return {row["id"]: row for row in (json.loads(line) for line in Path(path).read_text().splitlines() if line.strip())}


def main(first_path: str, second_path: str, output_path: str) -> None:
    first, second = _rows(first_path), _rows(second_path)
    result = []
    for source_id in sorted(set(first) | set(second)):
        left, right = first.get(source_id, {}), second.get(source_id, {})
        row = dict(left or right)
        row["reviewer"] = left.get("reviewer")
        row["second_reviewer"] = right.get("reviewer")
        compared = ("csl", "field_evidence", "field_status", "source_sha256", "modality", "work_family")
        if (left.get("reviewer") and right.get("reviewer") and left["reviewer"] != right["reviewer"]
                and all(left.get(key) and left.get(key) == right.get(key) for key in compared)):
            row["review_status"] = "reviewed"
            row["adjudication_status"] = "agreed"
        else:
            row["review_status"] = "needs_adjudication"
            row["adjudication_status"] = "pending"
            row["review_disagreement"] = {"first": left, "second": right}
        result.append(row)
    Path(output_path).write_text("".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in result))
    print(json.dumps({"rows": len(result), "needs_adjudication": sum(row["adjudication_status"] == "pending" for row in result)}, indent=2))


if __name__ == "__main__":
    if len(sys.argv) != 4:
        raise SystemExit("usage: adjudicate.py reviewer1.jsonl reviewer2.jsonl adjudicated.jsonl")
    main(*sys.argv[1:])
