"""Validate reviewed benchmark labels and reviewer adjudication fields."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def validate_rows(rows: list[dict]) -> list[str]:
    from run import evaluated_fields
    from citeindex.ingestion.csl import valid_host_value
    errors: list[str] = []
    if not rows:
        errors.append("manifest is empty")
    if len({row.get('id') for row in rows}) != len(rows):
        errors.append("duplicate IDs")
    for index, row in enumerate(rows, start=1):
        for key in ("id", "modality", "source_path", "source_sha256", "csl", "field_evidence"):
            if key not in row:
                errors.append(f"row {index}: missing {key}")
        if row.get("review_status") != "reviewed":
            errors.append(f"row {index}: review_status is not reviewed")
        if not row.get("reviewer"):
            errors.append(f"row {index}: missing reviewer")
        if not row.get("second_reviewer"):
            errors.append(f"row {index}: missing second_reviewer")
        if row.get("reviewer") == row.get("second_reviewer"):
            errors.append(f"row {index}: reviewers must be independent")
        if row.get("adjudication_status") not in {"agreed", "adjudicated"}:
            errors.append(f"row {index}: missing adjudication_status")
        if not re.fullmatch(r"[a-fA-F0-9]{64}", str(row.get("source_sha256", ""))):
            errors.append(f"row {index}: source checksum required (HTML snapshot for URLs)")
        if row.get("modality") not in {"digital_pdf", "scanned_pdf", "media", "url_article"}:
            errors.append(f"row {index}: invalid modality")
        if not row.get("work_family") or row.get("split") not in {"development", "validation", "test"}:
            errors.append(f"row {index}: work_family and frozen split required")
        csl, evidence, states = row.get("csl", {}), row.get("field_evidence", {}), row.get("field_status", {})
        if not isinstance(csl, dict) or not csl.get("title") or not isinstance(evidence, dict) or not isinstance(states, dict):
            errors.append(f"row {index}: nonempty CSL title and evidence/status objects required")
            continue
        if not valid_host_value("type", csl.get("type")):
            errors.append(f"row {index}: supported bibliographic type required")
        for field in evaluated_fields(row):
            state = states.get(field)
            if state not in {"present", "absent", "illegible", "outside_scope"}:
                errors.append(f"row {index}: missing status for {field}")
            if state == "present":
                item = evidence.get(field, {})
                if not valid_host_value(field, csl.get(field)) or not item.get("quote") or not item.get("locator"):
                    errors.append(f"row {index}: {field} needs value, quote and locator")
            elif state == "absent" and csl.get(field) is not None:
                errors.append(f"row {index}: absent {field} has a value")
    families = {}
    for row in rows:
        families.setdefault(row.get("work_family"), set()).add(row.get("split"))
    if any(len(splits) > 1 for splits in families.values()):
        errors.append("work-family leakage across splits")
    return errors


def main(manifest_path: str) -> None:
    rows = [json.loads(line) for line in Path(manifest_path).read_text().splitlines() if line.strip()]
    errors = validate_rows(rows)
    if errors:
        print("\n".join(errors))
        raise SystemExit(1)
    print(f"validated {len(rows)} reviewed, adjudicated rows")


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: validate_manifest.py manifest.jsonl")
    main(sys.argv[1])
