"""D5: authority gold-set integrity + offline evaluator math."""

from __future__ import annotations

import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent.parent / "benchmarks" / "authority"
sys.path.insert(0, str(BENCH))

import score  # noqa: E402

ROWS = [json.loads(line) for line in (BENCH / "gold.jsonl").open(encoding="utf-8") if line.strip()]


def _row(entity_type: str, label_id: str, present: bool) -> dict:
    return {
        "id": f"test-{entity_type}",
        "entity_type": entity_type,
        "source": {},
        "label": {"system": "test", "id": label_id},
        "label_status": "present" if present else "absent",
        "origin": "test",
        "provenance": "test",
    }


class TestGoldSet:
    def test_size_and_schema(self):
        assert len(ROWS) >= 500
        for row in ROWS:
            assert {"id", "entity_type", "source", "label", "label_status", "origin", "provenance"} <= row.keys()
            assert row["label_status"] in {"present", "absent"}
            assert {"system", "id"} <= row["label"].keys()
            assert row["provenance"], "every label must carry provenance"

    def test_entity_type_coverage(self):
        counts: dict[str, int] = {}
        for row in ROWS:
            counts[row["entity_type"]] = counts.get(row["entity_type"], 0) + 1
        assert counts["person"] >= 400
        assert counts["work"] >= 100
        assert counts["place"] >= 15
        assert counts["journal"] >= 5

    def test_absent_rows_are_only_premodern_persons(self):
        """Absent labels must be pre-modern persons — never guessed absences."""
        for row in ROWS:
            if row["label_status"] == "absent":
                assert row["entity_type"] == "person"
                assert row["origin"] == "premodern-seed"
        assert sum(1 for r in ROWS if r["label_status"] == "absent") >= 75

    def test_premodern_script_coverage(self):
        rows = [row for row in ROWS if row["origin"] == "premodern-seed"]
        scripts = {row.get("script") for row in rows}
        assert {"latin", "greek", "syriac"} <= scripts
        assert all(row.get("script") in {"latin", "greek", "syriac"} for row in rows)


class TestEvaluatorMath:
    def test_score_row_outcomes(self):
        row = _row("person", "0000-0000-0000-0001", present=True)
        assert score.score_row({"status": "linked", "id": "0000-0000-0000-0001"}, row) == "tp"
        assert score.score_row({"status": "linked", "id": "0000-0000-0000-0002"}, row) == "fp"
        assert score.score_row({"status": "no_match"}, row) == "fn"
        absent = _row("person", "0000-0000-0000-0001", present=False)
        assert score.score_row({"status": "no_match"}, absent) == "tn"
        assert score.score_row({"status": "linked", "id": "0000-0000-0000-0001"}, absent) == "fp"

    def test_aggregate_metrics(self):
        rows = [
            (_row("person", "A", True), {"status": "linked", "id": "A"}),      # tp
            (_row("person", "B", True), {"status": "linked", "id": "X"}),      # fp
            (_row("person", "C", True), {"status": "no_match"}),                # fn
            (_row("person", "D", False), {"status": "no_match"}),               # tn
            (_row("person", "E", False), {"status": "linked", "id": "A"}),     # fp
        ]
        outcomes = [("new", decision, row) for row, decision in rows]
        metrics = score.aggregate(outcomes)["person"]
        assert metrics["rows"] == 5 and metrics["present"] == 3 and metrics["absent"] == 2
        assert metrics["tp"] == 1 and metrics["fp"] == 2 and metrics["fn"] == 1 and metrics["tn"] == 1
        assert metrics["precision"] == round(1 / 3, 4)
        assert metrics["recall"] == round(1 / 3, 4)
        assert metrics["no_match_rate"] == round(2 / 5, 4)
        assert metrics["correct_no_match_rate"] == 0.5

    def test_aggregate_empty_absent(self):
        rows = [(_row("work", "10.1/x", True), {"status": "no_match"})]
        metrics = score.aggregate([("new", d, r) for r, d in rows])["work"]
        assert metrics["correct_no_match_rate"] is None

    def test_perfect_system(self):
        rows = [
            (_row("work", "10.1/x", True), {"status": "linked", "id": "10.1/x"}),
            (_row("work", "10.1/y", False), {"status": "no_match"}),
        ]
        metrics = score.aggregate([("new", d, r) for r, d in rows])["work"]
        assert metrics["precision"] == 1.0 and metrics["recall"] == 1.0 and metrics["f1"] == 1.0
        assert metrics["correct_no_match_rate"] == 1.0
