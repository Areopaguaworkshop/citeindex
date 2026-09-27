"""Composed-title scoring: correct title/subtitle splits must not count as title errors."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from run import _composed_title, _score_values, score


def _gold_row(csl: dict, field_status: dict | None = None) -> dict:
    return {"id": "one", "modality": "digital_pdf", "csl": csl,
            "field_status": field_status or {"title": "present"}, "field_evidence": {}}


def _prediction(csl: dict) -> dict:
    return {"id": "one", "status": "ok", "csl": csl, "source_blocks": []}


def test_composed_title_basic():
    assert _composed_title({"title": "A", "subtitle": "B"}) == "A: B"


def test_composed_title_subtitle_already_in_title():
    assert _composed_title({"title": "A: B", "subtitle": "B"}) == "A: B"
    assert _composed_title({"title": "Grace for Grace: The Debates", "subtitle": "The Debates"}) == \
        "Grace for Grace: The Debates"


def test_composed_title_missing_fields():
    assert _composed_title({"title": "A"}) == "A"
    assert _composed_title({"title": "A", "subtitle": None}) == "A"
    assert _composed_title({"title": "A", "subtitle": "  "}) == "A"
    assert _composed_title({}) == ""
    assert _composed_title({"subtitle": "B"}) == ""


def test_correct_split_scores_tp_on_title():
    gold = {"one": _gold_row({"type": "book", "title": "Long: Title"})}
    predictions = {"one": _prediction({"type": "book", "title": "Long", "subtitle": "Title"})}
    result = _score_values(gold, predictions, "title")
    assert (result["tp"], result["fp"], result["fn"]) == (1, 0, 0)


def test_correct_split_not_recorded_as_title_difference():
    report = score({"one": _gold_row({"type": "book", "title": "Long: Title", "author": [{"literal": "A"}],
                                     "issued": {"date-parts": [[2020]]}})},
                   {"one": _prediction({"type": "book", "title": "Long", "subtitle": "Title",
                                        "author": [{"literal": "A"}], "issued": {"date-parts": [[2020]]}})},
                   [])
    assert "title" not in report["field_errors"]


def test_correct_split_counts_as_exact_record_match():
    gold = {"one": _gold_row({"type": "book", "title": "Long: Title", "author": [{"literal": "A"}],
                              "issued": {"date-parts": [[2020]]}})}
    predictions = {"one": _prediction({"type": "book", "title": "Long", "subtitle": "Title",
                                       "author": [{"literal": "A"}], "issued": {"date-parts": [[2020]]}})}
    report = score(gold, predictions, [])
    assert report["exact_record_accuracy"] == 1.0
    assert report["equivalent_record_accuracy"] == 1.0


def test_subtitle_covered_by_composition_not_a_field_difference():
    # gold annotates subtitle as absent; prediction splits it out of the title
    gold = {"one": _gold_row({"type": "book", "title": "Long: Title", "author": [{"literal": "A"}],
                              "issued": {"date-parts": [[2020]]}},
                             field_status={"title": "present", "subtitle": "absent"})}
    predictions = {"one": _prediction({"type": "book", "title": "Long", "subtitle": "Title",
                                       "author": [{"literal": "A"}], "issued": {"date-parts": [[2020]]}})}
    report = score(gold, predictions, [])
    assert report["exact_record_accuracy"] == 1.0
    # subtitle fp on the per-field path is expected and acceptable
    assert report["fields"]["subtitle"]["fp"] == 1


def test_genuinely_different_title_still_differs():
    gold = {"one": _gold_row({"type": "book", "title": "Long: Title"})}
    predictions = {"one": _prediction({"type": "book", "title": "Something Else"})}
    result = _score_values(gold, predictions, "title")
    assert (result["tp"], result["fp"], result["fn"]) == (0, 1, 1)
    report = score(gold, predictions, [])
    assert report["field_errors"]["title"]["different"] == 1
    assert report["exact_record_accuracy"] == 0.0


# ---------------------------------------------------------------------------
# Core-requirement metric + equivalence conventions (two-type plan, 2026-09-26)
# ---------------------------------------------------------------------------

from run import _core_fields, _equivalent  # noqa: E402


def _book_gold(**status):
    return _gold_row({"type": "book", "title": "T", "author": [{"literal": "A"}],
                      "publisher": "P", "issued": {"date-parts": [[2020]]}}, status or None)


def test_core_fields_book_category():
    row = _book_gold()
    assert _core_fields(row) == {"type", "title", "author", "publisher", "issued"}


def test_core_record_accuracy_counts():
    ok = score({"one": _book_gold()}, {"one": _prediction(
        {"type": "book", "title": "T", "author": [{"literal": "A"}],
         "publisher": "P", "issued": {"date-parts": [[2020]]}})}, [])
    assert ok["core_record_accuracy"] == 1.0
    assert ok["core_records"] == {"one": True}
    missing_publisher = score({"one": _book_gold()}, {"one": _prediction(
        {"type": "book", "title": "T", "author": [{"literal": "A"}],
         "issued": {"date-parts": [[2020]]}})}, [])
    assert missing_publisher["core_record_accuracy"] == 0.0


def test_core_fields_exclude_outside_scope():
    row = _gold_row({"type": "article-journal", "title": "T", "author": [{"literal": "A"}],
                     "container-title": "J", "volume": 78, "issue": 3, "page": "23-35"},
                    {"volume": "outside_scope", "issue": "outside_scope", "page": "outside_scope"})
    assert _core_fields(row) == {"type", "title", "author", "container-title"}


def test_publisher_equivalence_conventions():
    row = _gold_row({"type": "book", "publisher": "The Catholic University of America Press"})
    leading_the = _prediction({"type": "book", "publisher": "Catholic University of America Press"})
    assert _equivalent(row, leading_the, "publisher")
    joint = _gold_row({"type": "book",
                       "publisher": "Cornell University Press; Northern Illinois University Press"})
    and_join = _prediction({"type": "book",
                            "publisher": "Cornell University Press and Northern Illinois University Press"})
    assert _equivalent(joint, and_join, "publisher")
    assert not _equivalent(joint, _prediction({"type": "book", "publisher": "Cornell University Press"}), "publisher")


def test_author_saint_prefix_equivalence():
    row = _gold_row({"type": "book", "author": [{"literal": "Saint Ambrose"}]})
    pred = _prediction({"type": "book", "author": [{"literal": "Ambrose"}]})
    assert _equivalent(row, pred, "author")


def test_quote_glyph_equivalence():
    row = _gold_row({"type": "article-journal", "title": "The “Nestorian” Church: A Lamentable Misnomer"})
    pred = _prediction({"type": "article-journal", "title": "The 'Nestorian' Church: A Lamentable Misnomer"})
    assert _equivalent(row, pred, "title")