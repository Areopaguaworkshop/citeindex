"""Character error rate (CER) and word error rate (WER) for the OCR/scanned path.

LIMITATION, stated plainly: this repository holds **no gold OCR transcripts**. There is
nothing here to compare a scanned page against at the character level, so CER cannot be
computed from repo data alone. It needs paired strings supplied by a caller: the exact same
passage as (a) human-transcribed gold text and (b) the OCR output for the same page region.
A field-level CER -- for example a recovered title versus the catalogued title -- is only a
*proxy*. It mixes OCR error with extraction error (wrong page chosen, wrong span, wrong
composed title/subtitle split), so it is an upper bound on OCR error, not a measurement of it.

Usage:
    python cer.py --gold gold.jsonl --pred pred.jsonl --key title --out reports/cer-title.md

Both inputs are JSONL with a shared ``id``; ``--key`` names the text field in each row. The
report is written as ``.md`` next to ``.json``. Exit status is 2 when ids do not match, and the
unmatched ids are listed.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import unicodedata
from dataclasses import asdict, dataclass
from pathlib import Path

Z_95 = 1.959964


def normalize(text: str | None) -> str:
    """NFC-normalize, collapse all whitespace runs to one space, and strip.

    Case is deliberately preserved: CER is case sensitive by default, and folding case here
    would silently forgive real OCR errors such as ``m`` -> ``rn`` in a heading.
    """
    if not text:
        return ""
    return " ".join(unicodedata.normalize("NFC", text).split())


def levenshtein(a: str, b: str) -> int:
    """Character-level Levenshtein distance (insert/delete/substitute cost 1), two-row DP."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, 1):
        current = [i]
        for j, char_b in enumerate(b, 1):
            current.append(
                min(
                    previous[j] + 1,  # deletion from a
                    current[j - 1] + 1,  # insertion into a
                    previous[j - 1] + (char_a != char_b),  # match or substitution
                )
            )
        previous = current
    return previous[-1]


def cer(gold: str, pred: str) -> float:
    """Edits / len(gold). Empty gold scores 1.0 against non-empty output, 0.0 against empty."""
    gold = normalize(gold)
    pred = normalize(pred)
    if not gold:
        return 0.0 if not pred else 1.0
    return levenshtein(gold, pred) / len(gold)


def wer(gold: str, pred: str) -> float:
    """Word-level equivalent of :func:`cer`, with the same empty-gold convention."""
    gold_words = normalize(gold).split()
    pred_words = normalize(pred).split()
    if not gold_words:
        return 0.0 if not pred_words else 1.0
    return levenshtein_sequences(gold_words, pred_words) / len(gold_words)


def levenshtein_sequences(a: list[str], b: list[str]) -> int:
    """Levenshtein distance over sequences of tokens (shared by :func:`wer`)."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    previous = list(range(len(b) + 1))
    for i, token_a in enumerate(a, 1):
        current = [i]
        for j, token_b in enumerate(b, 1):
            current.append(
                min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (token_a != token_b))
            )
        previous = current
    return previous[-1]


def wilson(k: int, n: int, z: float = Z_95) -> tuple[float, float]:
    """Wilson score interval for k successes out of n trials (no continuity correction).

    Returns (low, high) clamped to [0, 1]. For n == 0 the interval is (0.0, 1.0): with no
    observations the honest statement is that we know nothing.
    """
    if n <= 0:
        return (0.0, 1.0)
    if not 0 <= k <= n:
        raise ValueError(f"k={k} out of range for n={n}")
    p = k / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    margin = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denominator
    # The boundary cases are exactly 0 and 1 in the closed form; pin them so float noise
    # cannot report an interval of 0.2366-0.9999999999999999.
    return (0.0 if k == 0 else max(0.0, centre - margin), 1.0 if k == n else min(1.0, centre + margin))


@dataclass(frozen=True)
class ItemScore:
    """Per-item comparison of one gold string against one predicted string."""

    id: str
    gold: str
    pred: str
    edits: int
    gold_chars: int
    cer: float
    word_edits: int
    gold_words: int
    wer: float
    at_or_under_threshold: bool


def score_item(item_id: str, gold: str, pred: str, threshold: float = 0.10) -> ItemScore:
    gold_norm = normalize(gold)
    pred_norm = normalize(pred)
    edits = levenshtein(gold_norm, pred_norm)
    gold_words = gold_norm.split()
    word_edits = levenshtein_sequences(gold_words, pred_norm.split())
    item_cer = cer(gold_norm, pred_norm)
    item_wer = wer(gold_norm, pred_norm)
    return ItemScore(
        id=item_id,
        gold=gold_norm,
        pred=pred_norm,
        edits=edits,
        gold_chars=len(gold_norm),
        cer=item_cer,
        word_edits=word_edits,
        gold_words=len(gold_words),
        wer=item_wer,
        at_or_under_threshold=item_cer <= threshold,
    )


def _weighted(total_edits: int, total_gold: int, any_output: bool) -> float:
    """Total edits / total gold units, defined for the degenerate all-empty-gold case."""
    if total_gold == 0:
        return 1.0 if any_output else 0.0
    return total_edits / total_gold


def aggregate(items: list[ItemScore], threshold: float = 0.10) -> dict:
    """Summarise per-item scores: mean, median, weighted CER, and a Wilson interval.

    Mean and median CER have no interval here. They are means of ratios, not proportions, so
    quoting a binomial interval for them would be fabricated precision. Only the fraction of
    items at or under ``threshold`` is a proportion and carries a Wilson interval.
    """
    n = len(items)
    total_edits = sum(item.edits for item in items)
    total_gold_chars = sum(item.gold_chars for item in items)
    total_word_edits = sum(item.word_edits for item in items)
    total_gold_words = sum(item.gold_words for item in items)
    passing = sum(1 for item in items if item.at_or_under_threshold)
    low, high = wilson(passing, n)

    cers = [item.cer for item in items]
    wers = [item.wer for item in items]
    return {
        "n": n,
        "threshold": threshold,
        "mean_cer": statistics.fmean(cers) if cers else 0.0,
        "median_cer": statistics.median(cers) if cers else 0.0,
        "weighted_cer": _weighted(total_edits, total_gold_chars, any(item.pred for item in items)),
        "total_edits": total_edits,
        "total_gold_chars": total_gold_chars,
        "mean_wer": statistics.fmean(wers) if wers else 0.0,
        "median_wer": statistics.median(wers) if wers else 0.0,
        "weighted_wer": _weighted(total_word_edits, total_gold_words, any(item.pred for item in items)),
        "total_word_edits": total_word_edits,
        "total_gold_words": total_gold_words,
        "at_or_under_threshold": passing,
        "fraction_at_or_under_threshold": (passing / n) if n else 0.0,
        "wilson_low": low,
        "wilson_high": high,
    }


def _field(row: dict, key: str):
    """Read ``key`` from a row, following dots for nested fields such as ``csl.title``."""
    value = row
    for part in key.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def load_jsonl(path: Path, key: str, id_key: str = "id") -> dict[str, str]:
    """Read JSONL into an id -> text mapping, rejecting duplicate ids."""
    rows: dict[str, str] = {}
    with path.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            item_id = _field(row, id_key)
            if item_id is None:
                raise ValueError(f"{path}:{number}: missing id field {id_key!r}")
            item_id = str(item_id)
            if item_id in rows:
                raise ValueError(f"{path}:{number}: duplicate id {item_id!r}")
            value = _field(row, key)
            rows[item_id] = value if isinstance(value, str) else ("" if value is None else str(value))
    return rows


def compare(gold: dict[str, str], pred: dict[str, str], threshold: float = 0.10) -> tuple[list[ItemScore], dict]:
    """Pair gold and predictions on shared ids and aggregate the per-item scores."""
    items = [
        score_item(item_id, text, pred[item_id], threshold)
        for item_id, text in gold.items()
        if item_id in pred
    ]
    return items, aggregate(items, threshold)


def _fmt(value: float) -> str:
    return f"{value:.4f}"


def _count(count: int, singular: str) -> str:
    return f"{count} {singular}" if count == 1 else f"{count} {singular}s"


def _ci(low: float, high: float) -> str:
    return f"95% CI {_fmt(low)}-{_fmt(high)}"


def render_markdown(report: dict, gold_path: Path, pred_path: Path, key: str, threshold: float) -> str:
    items = report["items"]
    lines = [
        f"# OCR character error rate - `{key}`",
        "",
        f"- gold: `{gold_path}`",
        f"- pred: `{pred_path}`",
        f"- items joined on `id`: n={report['n']}",
        "",
        "CER = character edits / gold characters, after NFC normalization, whitespace collapse,",
        "and stripping. Case is not folded. Characters are Unicode code points, not grapheme",
        "clusters. This is a field-level proxy: it mixes OCR error with extraction error.",
        "",
        "| metric | value |",
        "| --- | --- |",
        f"| mean CER | {_fmt(report['mean_cer'])} (n={report['n']}; no interval given for a mean) |",
        f"| median CER | {_fmt(report['median_cer'])} (n={report['n']}; no interval given for a median) |",
        (
            f"| weighted CER (total edits / total gold chars) | {_fmt(report['weighted_cer'])} "
            f"({_count(report['total_edits'], 'edit')} / {_count(report['total_gold_chars'], 'gold char')}, "
            f"n={report['n']}) |"
        ),
        (
            f"| items at or under threshold {_fmt(threshold)} | "
            f"{report['at_or_under_threshold']}/{report['n']} = "
            f"{_fmt(report['fraction_at_or_under_threshold'])} "
            f"(n={report['n']}, {_ci(report['wilson_low'], report['wilson_high'])}) |"
        ),
        f"| mean WER | {_fmt(report['mean_wer'])} (n={report['n']}; no interval given for a mean) |",
        (
            f"| weighted WER (total word edits / total gold words) | {_fmt(report['weighted_wer'])} "
            f"({_count(report['total_word_edits'], 'edit')} / {_count(report['total_gold_words'], 'gold word')}, "
            f"n={report['n']}) |"
        ),
        "",
        "## Per item",
        "",
        "| id | CER | edits | gold chars | under threshold |",
        "| --- | --- | --- | --- | --- |",
    ]
    for item in items:
        lines.append(
            f"| {item['id']} | {_fmt(item['cer'])} | {item['edits']} | {item['gold_chars']} | "
            f"{'yes' if item['at_or_under_threshold'] else 'no'} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gold", required=True, type=Path, help="gold JSONL with an id and the text field")
    parser.add_argument("--pred", required=True, type=Path, help="prediction JSONL with the same ids")
    parser.add_argument("--key", default="title", help="text field to compare (supports dots, e.g. csl.title)")
    parser.add_argument("--id-key", default="id", help="id field name (default: id)")
    parser.add_argument("--out", required=True, type=Path, help="markdown report path; .json sibling is written too")
    parser.add_argument(
        "--threshold", type=float, default=0.10, help="CER at or under which an item counts as a pass (default: 0.10)"
    )
    args = parser.parse_args(argv)

    gold = load_jsonl(args.gold, args.key, args.id_key)
    pred = load_jsonl(args.pred, args.key, args.id_key)

    missing_pred = sorted(set(gold) - set(pred))
    missing_gold = sorted(set(pred) - set(gold))
    if missing_pred or missing_gold:
        if missing_pred:
            print(f"unmatched ids: {len(missing_pred)} in gold without a prediction:", file=sys.stderr)
            for item_id in missing_pred:
                print(f"  {item_id}", file=sys.stderr)
        if missing_gold:
            print(f"unmatched ids: {len(missing_gold)} predicted without gold:", file=sys.stderr)
            for item_id in missing_gold:
                print(f"  {item_id}", file=sys.stderr)
        print("refusing to score a partial join; fix the ids or remove the rows", file=sys.stderr)
        return 2

    items, summary = compare(gold, pred, args.threshold)
    if not items:
        print("no items to score: both inputs are empty", file=sys.stderr)
        return 2

    payload = {
        "gold": str(args.gold),
        "pred": str(args.pred),
        "key": args.key,
        "id_key": args.id_key,
        **summary,
        "items": [asdict(item) for item in items],
    }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    json_path = args.out.with_suffix(".json")
    args.out.write_text(
        render_markdown(payload, args.gold, args.pred, args.key, args.threshold), encoding="utf-8"
    )
    json_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print(
        f"n={summary['n']}  mean CER {_fmt(summary['mean_cer'])}  "
        f"median CER {_fmt(summary['median_cer'])}  weighted CER {_fmt(summary['weighted_cer'])}"
    )
    print(
        f"at or under {_fmt(args.threshold)}: {summary['at_or_under_threshold']}/{summary['n']} "
        f"(n={summary['n']}, {_ci(summary['wilson_low'], summary['wilson_high'])})"
    )
    print(f"report: {args.out}")
    print(f"report: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
