"""Compare two saved benchmark reports without running ingestion."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main(baseline_path: str, candidate_path: str) -> None:
    baseline = json.loads(Path(baseline_path).read_text())
    candidate = json.loads(Path(candidate_path).read_text())
    for key in ("manifest_sha256", "scorer_version"):
        if not baseline.get(key) or baseline[key] != candidate.get(key):
            raise SystemExit(f"cannot compare reports with different or missing {key}")
    fields = set(baseline.get("fields", {})) | set(candidate.get("fields", {}))
    delta = {}
    for field in sorted(fields):
        old = baseline.get("fields", {}).get(field, {}).get("recall")
        new = candidate.get("fields", {}).get(field, {}).get("recall")
        delta[field] = {"baseline_recall": old, "candidate_recall": new, "delta": None if old is None or new is None else new - old}
    print(json.dumps({"baseline": baseline_path, "candidate": candidate_path, "field_recall_delta": delta}, indent=2, sort_keys=True))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: compare.py baseline-report.json candidate-report.json")
    main(*sys.argv[1:])
