import copy
import json
import subprocess
import sys

import pytest

from benchmarks.citations.enrichment import candidate_digest, mask_candidate, score


def _case(key="one", script="Latin"):
    before = {"type": "book", "title": "Host", "publisher": "Wrong"}
    after = {"type": "book", "title": "Host", "publisher": "Press", "issued": {"date-parts": [[2020]]}}
    row = {"id": key, "source_sha256": "a" * 64, "source_review_status": "reviewed",
           "registry_review_status": "reviewed", "registry_gold_origin": "independent_review",
           "source_csl": {"type": "book", "title": "Host"}, "registry_csl": copy.deepcopy(after),
           "modality": "digital_pdf", "script": script, "split": "heldout", "work_family": key,
           "eligible": True, "masked_fields": ["issued"], "safety_review_status": "reviewed",
           "safety_checks_passed": True, "change_review": {"review_status": "reviewed",
           "prediction_digest": candidate_digest(after), "same_work_and_edition": True}}
    prediction = {"id": key, "source_sha256": "a" * 64, "status": "ok", "before_csl": before, "csl": after,
                  "online_enrichment": {"request_count": 1, "cache_hits": 0, "elapsed_seconds": .2,
                  "decisions": [{"field": field, "action": action, "tier": "T1", "old_value": before.get(field),
                                 "new_value": after[field], "provenance": {"provider": "crossref",
                                 "request_identifier": "10.1000/host", "response_digest": "b" * 64}}
                                for field, action in (("publisher", "overwrite"), ("issued", "fill"))]}}
    return row, prediction


def test_masking_removes_value_evidence_status_identifiers_and_cached_leakage():
    candidate = {"title": "Host", "publisher": "Press", "DOI": "10.1000/host", "ISBN": "9780199535569",
                 "_field_status": {"title": "source-supported", "publisher": "verified", "DOI": "verified"},
                 "_field_evidence": {"title": {"quote": "Host"}, "publisher": {"quote": "Press"}},
                 "_citation_status": "verified", "registry_candidate": {"publisher": "Press"},
                 "online_enrichment": {"candidate_input_snapshot": {"publisher": "Press"}}}
    original = copy.deepcopy(candidate)
    masked = mask_candidate(candidate, ["publisher"], no_identifiers=True)
    assert masked == {"title": "Host", "_field_status": {"title": "source-supported"},
                      "_field_evidence": {"title": {"quote": "Host"}}}
    assert candidate == original
    with pytest.raises(ValueError, match="unknown"):
        mask_candidate(candidate, ["bogus"])


def test_release_requires_independent_review_and_sufficient_changed_cohorts():
    pairs = [_case(str(index), "CJK" if index < 50 else "Latin") for index in range(300)]
    report = score([row for row, _ in pairs], [pred for _, pred in pairs])
    assert report["default_on_release_eligible"] is True
    assert report["counts"]["correct_corrections"] == 300
    assert report["wrong_identity_95pct_upper_bound_zero_failures"] == pytest.approx(.009936, abs=1e-6)
    assert report["cache_hit_rate"] is None  # No cache-lookup denominator was measured.
    pairs[0][0]["change_review"]["same_work_and_edition"] = False
    bad = score([row for row, _ in pairs], [pred for _, pred in pairs])
    assert bad["default_on_release_eligible"] is False
    assert bad["counts"]["wrong_work_or_edition"] == 1
    assert bad["wrong_identity_95pct_upper_bound_zero_failures"] is None
    row, prediction = _case()
    prediction["before_csl"]["publisher"] = "Press"
    fills_only = score([row], [prediction])
    assert fills_only["metrics"]["correction_precision"]["rate"] is None
    assert fills_only["release_gates"]["correction_precision_99pct"] is False


def test_audit_checks_every_step_of_repeated_field_changes():
    row, prediction = _case()
    decisions = prediction["online_enrichment"]["decisions"]
    first = copy.deepcopy(decisions[0])
    first["new_value"] = "Intermediate Press"
    decisions[0]["old_value"] = first["new_value"]
    decisions.insert(0, first)
    assert score([row], [prediction])["counts"]["audit_value_mismatches"] == 0
    decisions[1]["old_value"] = "Unrelated"
    assert score([row], [prediction])["counts"]["audit_value_mismatches"] == 1


def test_failures_abstention_harmful_overwrites_and_unreviewed_gold():
    row, prediction = _case()
    absent, _ = _case("missing", "CJK")
    prediction["before_csl"]["publisher"] = "Press"
    prediction["csl"]["publisher"] = "Bad Press"
    row["change_review"]["prediction_digest"] = candidate_digest(prediction["csl"])
    prediction["online_enrichment"]["decisions"][0]["tier"] = "T4"
    prediction["online_enrichment"]["source_repaired_fields"] = ["publisher"]
    report = score([row, absent], [prediction])
    assert report["counts"]["harmful_overwrites"] == 1
    assert report["counts"]["forbidden_overwrites"] == 1
    assert report["counts"]["source_repair_reversals"] == 1
    assert report["metrics"]["abstention"] == {"numerator": 1, "denominator": 2, "rate": .5}
    assert report["by"]["script"]["CJK"]["inputs"] == 1
    assert report["default_on_release_eligible"] is False
    row["registry_gold_origin"] = "provider_payload"
    with pytest.raises(ValueError, match="independent"):
        score([row], [prediction])


def test_reject_stale_identity_source_digest_unknown_fields_and_split_leakage():
    row, prediction = _case()
    row["change_review"]["prediction_digest"] = "stale"
    report = score([row], [prediction])
    assert report["counts"]["identity_reviewed"] == 0
    assert report["metrics"]["fill_precision"]["rate"] == 0
    prediction["source_sha256"] = "c" * 64
    with pytest.raises(ValueError, match="digest mismatch"):
        score([row], [prediction])
    prediction["source_sha256"] = row["source_sha256"]
    prediction["csl"]["volume"] = "2"
    with pytest.raises(ValueError, match="lack registry adjudication"):
        score([row], [prediction])
    other, _ = _case("other")
    other.update(work_family=row["work_family"], split="development")
    with pytest.raises(ValueError, match="work family"):
        score([row, other], [])


def test_ingest_runner_pins_baseline_and_retains_explicit_enrichment(tmp_path, monkeypatch):
    from benchmarks.citations import ingest
    commands = []
    def run(command, timeout, stdout, stderr, env):
        commands.append(command)
        stdout.write_text(json.dumps({"status": "ok", "standardized_csl_json": {}}))
        return 0, None
    monkeypatch.setattr(ingest, "run_source", run)
    manifest = tmp_path / "manifest.jsonl"
    manifest.write_text("\n".join(json.dumps(row) for row in (
        {"id": "baseline", "source_path": "source.pdf"},
        {"id": "enriched", "source_path": "source.pdf", "cli_args": ["--online-enrich"]})))
    ingest.main(str(manifest), str(tmp_path / "predictions.jsonl"))
    assert "--no-online-enrich" in commands[0]
    assert "--online-enrich" in commands[1] and "--no-online-enrich" not in commands[1]


def test_script_scores_reviewed_jsonl_and_failed_runs_stay_in_denominators(tmp_path):
    row, prediction = _case()
    gold_path, prediction_path = tmp_path / "gold.jsonl", tmp_path / "prediction.jsonl"
    gold_path.write_text(json.dumps(row) + "\n")
    prediction_path.write_text(json.dumps(prediction) + "\n")
    result = subprocess.run([sys.executable, "benchmarks/citations/enrichment.py", str(gold_path), str(prediction_path)],
                            text=True, capture_output=True, check=True)
    report = json.loads(result.stdout)
    assert report["metrics"]["recovery"]["rate"] == 1
    assert report["default_on_release_eligible"] is False
    prediction["status"] = "failed"
    failed = score([row], [prediction])
    assert failed["metrics"]["core_accuracy"]["rate"] == 0
    assert failed["metrics"]["recovery"]["denominator"] == 1
