"""Tests for the OCR CER/WER benchmark. All data is built inline; no fixtures."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import cer as cer_module  # noqa: E402
from cer import ItemScore, aggregate, cer, levenshtein, normalize, score_item, wer, wilson  # noqa: E402


def test_identical_strings_score_zero():
    assert cer("Grace for Grace", "Grace for Grace") == 0.0
    assert levenshtein("abc", "abc") == 0
    assert wer("a b c", "a b c") == 0.0


def test_insertion_is_charged_to_gold_length():
    # pred has one extra character: 1 edit / 3 gold characters.
    assert levenshtein("abc", "abcd") == 1
    assert cer("abc", "abcd") == 1 / 3


def test_deletion_is_charged_to_gold_length():
    assert levenshtein("abcd", "abc") == 1
    assert cer("abcd", "abc") == 0.25


def test_substitution():
    assert levenshtein("abc", "abd") == 1
    assert cer("abc", "abd") == 1 / 3
    # Two edits in a six-character gold string: `n` -> `m` substituted, and `r` dropped.
    assert cer("modern", "modem") == 2 / 6


def test_empty_gold():
    assert cer("", "") == 0.0
    assert cer("", "anything") == 1.0
    assert wer("", "") == 0.0
    assert wer("   ", "recovered title") == 1.0


def test_unicode_normalization():
    decomposed = "e\u0301cole"  # e + combining acute, not NFC
    assert normalize(decomposed) == "\u00e9cole"
    assert cer(decomposed, "\u00e9cole") == 0.0
    assert cer("\u00e9cole", "ecole") == 1 / 5
    # Whitespace collapse and strip, without case folding.
    assert normalize("  Grace \n\t for   Grace  ") == "Grace for Grace"
    assert cer("Grace", "grace") == 1 / 5


def test_weighted_versus_mean_divergence():
    items = [
        score_item("long-clean", "abcdefghij", "abcdefghij"),  # CER 0.0 over 10 chars
        score_item("short-bad", "ab", "xy"),  # CER 1.0 over 2 chars
    ]
    summary = aggregate(items)
    assert summary["n"] == 2
    assert summary["mean_cer"] == 0.5
    assert summary["median_cer"] == 0.5
    # 2 total edits / 12 gold characters, far below the mean: the mean over-weights
    # the short row because each item counts equally regardless of length.
    assert summary["weighted_cer"] == 2 / 12
    assert summary["weighted_cer"] < summary["mean_cer"]


def test_wilson_bounds():
    low, high = wilson(5, 10)
    assert abs(low - 0.2366) < 1e-3
    assert abs(high - 0.7634) < 1e-3
    # Degenerate proportions stay inside [0, 1] and bracket p.
    assert wilson(0, 10)[0] == 0.0
    assert 0.0 < wilson(0, 10)[1] < 0.35
    assert wilson(10, 10)[1] == 1.0
    assert 0.65 < wilson(10, 10)[0] < 1.0
    for k in range(0, 11):
        low, high = wilson(k, 10)
        assert 0.0 <= low <= k / 10 <= high <= 1.0
    # No observations: an honest maximally wide interval, not a fabricated point.
    assert wilson(0, 0) == (0.0, 1.0)


def test_threshold_fraction_carries_wilson_interval():
    items = [
        score_item("a", "abcdefghij", "abcdefghij"),  # CER 0.0, passes
        score_item("b", "abcdefghij", "abcdefghij"),  # CER 0.0, passes
        score_item("c", "abcd", "wxyz"),  # CER 1.0, fails
    ]
    summary = aggregate(items, threshold=0.10)
    assert summary["at_or_under_threshold"] == 2
    assert summary["fraction_at_or_under_threshold"] == 2 / 3
    assert summary["wilson_low"] == wilson(2, 3)[0]
    assert summary["wilson_high"] == wilson(2, 3)[1]


def test_unmatched_ids_fail_nonzero(tmp_path, capsys):
    gold_path = tmp_path / "gold.jsonl"
    pred_path = tmp_path / "pred.jsonl"
    out_path = tmp_path / "reports" / "cer-title.md"
    gold_rows = [
        {"id": "one", "title": "Grace for Grace"},
        {"id": "two", "title": "The Debates"},
    ]
    pred_rows = [
        {"id": "one", "title": "Grace for Grace"},
        {"id": "three", "title": "Somewhere Else"},
    ]
    gold_path.write_text("\n".join(json.dumps(row) for row in gold_rows) + "\n", encoding="utf-8")
    pred_path.write_text("\n".join(json.dumps(row) for row in pred_rows) + "\n", encoding="utf-8")

    code = cer_module.main(
        ["--gold", str(gold_path), "--pred", str(pred_path), "--key", "title", "--out", str(out_path)]
    )
    captured = capsys.readouterr()

    assert code != 0
    assert "two" in captured.err  # in gold, no prediction
    assert "three" in captured.err  # predicted, no gold
    assert "one" not in captured.err  # matched ids are not reported as unmatched
    assert not out_path.exists()


def test_cli_writes_markdown_and_json(tmp_path):
    gold_path = tmp_path / "gold.jsonl"
    pred_path = tmp_path / "pred.jsonl"
    out_path = tmp_path / "reports" / "cer-title.md"
    rows = [
        {"id": "one", "title": "Grace for Grace"},
        {"id": "two", "title": "The Debates"},
    ]
    preds = [
        {"id": "one", "title": "Grace for Grace"},
        {"id": "two", "title": "The Debate"},
    ]
    gold_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    pred_path.write_text("\n".join(json.dumps(row) for row in preds) + "\n", encoding="utf-8")

    code = cer_module.main(
        ["--gold", str(gold_path), "--pred", str(pred_path), "--key", "title", "--out", str(out_path)]
    )

    assert code == 0
    assert out_path.exists()
    json_path = out_path.with_suffix(".json")
    assert json_path.exists()

    markdown = out_path.read_text(encoding="utf-8")
    assert "n=2" in markdown
    assert "95% CI" in markdown
    assert "no interval given for a mean" in markdown

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["n"] == 2
    assert payload["total_gold_chars"] == len("Grace for Grace") + len("The Debates")
    assert payload["mean_cer"] == 0.5 * (1 / len("The Debates"))
    assert payload["wilson_low"] <= payload["fraction_at_or_under_threshold"] <= payload["wilson_high"]
    assert isinstance(payload["items"][0], dict)


def test_aggregate_reports_no_interval_for_means():
    item = score_item("one", "abc", "abd")
    assert isinstance(item, ItemScore)
    summary = aggregate([item])
    assert summary["n"] == 1
    # Only proportions get interval keys; mean CER is reported as a bare value plus n.
    assert set(summary) >= {"mean_cer", "median_cer", "weighted_cer", "n", "wilson_low", "wilson_high"}
    assert not any("mean_cer_low" in key or "median_cer_high" in key for key in summary)
