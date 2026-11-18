"""Unit tests for the ScienceMetaBench scorer and manifest mapping.

No network, no LLM: every row is built in memory or in a tmp_path.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import manifest
import score
from score import (
    FIELDS,
    evaluated_fields,
    field_correct,
    ratio,
    render_markdown,
    wilson,
)
from score import (
    score as score_rows,
)

SHA = "544a6f5b192fe1a2a1da489dab828eb6ef06c18580210a2043f7aebae3373169"


def gold_row(csl, field_status=None, row_id="one"):
    status = {field: ("present" if csl.get(field) is not None else "outside_scope") for field in FIELDS}
    status.update(field_status or {})
    return {"id": row_id, "modality": "textbook", "source_path": f"pdf/{row_id}.pdf",
            "source_sha256": row_id, "csl": csl, "field_status": status,
            "field_evidence": {}, "gold": {"extra": {}}}


def prediction(csl, status="ok", row_id="one"):
    return {"id": row_id, "status": status, "csl": csl}


BOOK = {"type": "book", "title": "A History of South Africa", "author": [{"literal": "DOROTHEA FAIRBRIDGE"}],
        "publisher": "Cambridge University Press", "issued": {"date-parts": [[1918]]}}


# --- normalization -----------------------------------------------------------
def test_norm_folds_case_whitespace_and_identifiers():
    assert score._norm("  The   BOOK ", "title") == "the book"
    assert score._norm("978-3-16-148410-0", "ISBN") == "9783161484100"
    assert score._norm("https://doi.org/10.1/ABC", "DOI") == "10.1/abc"
    assert score._norm(["A", "B"]) != score._norm(["B", "A"])  # author order matters


def test_norm_keeps_date_parts_and_composes_titles():
    assert score._norm({"date-parts": [[1918]]}) == {"date-parts": [["1918"]]}
    assert score._composed_title({"title": "Grace for Grace", "subtitle": "The Debates"}) == \
        "Grace for Grace: The Debates"
    assert score._composed_title({"title": "A: B", "subtitle": "B"}) == "A: B"


# --- strict vs soft ----------------------------------------------------------
def test_strict_vs_soft_divergence_on_punctuation():
    row = gold_row({"type": "book", "title": "A History of South Africa."})
    strict_pred = prediction({"title": "A History of South Africa"})
    soft_pred = prediction({"title": "a history of south africa"})
    assert field_correct(row, strict_pred, "title", "strict") is False
    assert field_correct(row, strict_pred, "title", "soft") is True
    assert field_correct(row, soft_pred, "title", "strict") is False
    assert field_correct(row, soft_pred, "title", "soft") is True


def test_strict_is_already_case_and_space_insensitive():
    row = gold_row({"type": "book", "title": "Modern  Approaches to IoT"})
    assert field_correct(row, prediction({"title": "modern approaches to iot"}), "title", "strict") is True


def test_soft_flattens_name_objects_to_literals():
    row = gold_row({"type": "book", "author": [{"literal": "MIHAI Carmen"}]})
    split = prediction({"author": [{"given": "MIHAI", "family": "Carmen"}]})
    # Strict now uses canonical name tokens, so this split name is strictly
    # equal too; the old assertion expected False for strict.
    assert field_correct(row, split, "author", "strict") is True
    assert field_correct(row, split, "author", "soft") is True


# --- canonical person names --------------------------------------------------
def test_literal_equals_family_given_split():
    row = gold_row({"type": "book", "author": [{"literal": "Todd Hoff"}]})
    split = prediction({"author": [{"family": "Hoff", "given": "Todd"}]})
    assert score.canonical_names({"literal": "Todd Hoff"}) == [frozenset({"todd", "hoff"})]
    assert field_correct(row, split, "author", "strict") is True
    assert field_correct(row, split, "author", "soft") is True


def test_name_folding_ignores_case_and_diacritics():
    row = gold_row({"type": "book", "author": [{"literal": "JAY ABRAMSON"}]})
    lowercase = prediction({"author": [{"family": "Abramson", "given": "Jay"}]})
    assert field_correct(row, lowercase, "author", "strict") is True
    accent_row = gold_row({"type": "book", "author": [{"literal": "José Núñez"}]})
    plain = prediction({"author": [{"family": "Nunez", "given": "Jose"}]})
    assert field_correct(accent_row, plain, "author", "strict") is True
    assert field_correct(accent_row, plain, "author", "soft") is True


def test_name_comma_and_whitespace_splitting():
    inverted = gold_row({"type": "book", "author": [{"literal": "Hoff, Todd"}]})
    split = prediction({"author": [{"family": "Hoff", "given": "Todd"}]})
    assert field_correct(inverted, split, "author", "strict") is True
    # "Hoff, Todd" tokenizes to the same set whatever the order of the parts,
    # but a genuinely reversed literal keeps both tokens: only order-insensitive
    # within one name, not across names.
    particles = gold_row({"type": "book",
                          "author": [{"family": "Hoff", "given": "Todd",
                                      "non-dropping-particle": "van der"}]})
    literal = prediction({"author": [{"literal": "Todd van der Hoff"}]})
    assert field_correct(particles, literal, "author", "strict") is True


def test_name_with_different_surname_is_not_equal():
    row = gold_row({"type": "book", "author": [{"literal": "Todd Hoff"}]})
    other = prediction({"author": [{"family": "Hoffman", "given": "Todd"}]})
    assert field_correct(row, other, "author", "strict") is False
    assert field_correct(row, other, "author", "soft") is False
    # A subset of the tokens is not equality either.
    partial = prediction({"author": [{"literal": "Todd"}]})
    assert field_correct(row, partial, "author", "strict") is False
    assert field_correct(row, partial, "author", "soft") is False


def test_name_list_length_and_order_matter():
    two_gold = gold_row({"type": "book", "author": [{"literal": "JAY ABRAMSON"},
                                                    {"literal": "SHARON NORTH"}]})
    two_pred = prediction({"author": [{"family": "Abramson", "given": "Jay"},
                                      {"family": "North", "given": "Sharon"}]})
    assert field_correct(two_gold, two_pred, "author", "strict") is True
    assert score.canonical_names(two_gold["csl"]["author"]) != \
        score.canonical_names(list(reversed(two_gold["csl"]["author"])))
    reversed_pred = prediction({"author": [{"family": "North", "given": "Sharon"},
                                           {"family": "Abramson", "given": "Jay"}]})
    assert field_correct(two_gold, reversed_pred, "author", "strict") is False
    one_pred = prediction({"author": [{"family": "Abramson", "given": "Jay"}]})
    assert field_correct(two_gold, one_pred, "author", "strict") is False


def test_strict_record_passes_only_because_of_name_rule():
    row = gold_row({"type": "book", "title": "The Hacker Ethic",
                    "author": [{"literal": "Todd Hoff"}],
                    "publisher": "Springer", "issued": {"date-parts": [[2001]]}})
    split = prediction({"title": "The Hacker Ethic",
                        "author": [{"family": "Hoff", "given": "Todd"}],
                        "publisher": "Springer", "issued": {"date-parts": [[2001]]}})
    assert field_correct(row, split, "title", "strict") is True
    assert field_correct(row, split, "author", "strict") is True
    report = score_rows([row], {"one": split})
    assert report["metrics"]["strict_record_accuracy"]["value"] == 1.0
    assert report["per_field"]["author"]["tp"] == 1
    assert report["metrics"]["instance_recall"]["value"] == 1.0
    # The same record fails strict if the name rule is not the reason it passes:
    # a different surname still fails every mode.
    wrong = prediction({**split["csl"], "author": [{"family": "Hoffman", "given": "Todd"}]})
    assert score_rows([row], {"one": wrong})["metrics"]["strict_record_accuracy"]["value"] == 0.0


# --- adapter rules (v3) ------------------------------------------------------
def test_year_only_gold_matches_a_year_month_prediction():
    row = gold_row({"type": "book", "issued": {"date-parts": [[1918]]}})
    assert field_correct(row, prediction({"issued": {"date-parts": [[1918, 5]]}}),
                         "issued", "strict") is True
    assert field_correct(row, prediction({"issued": {"date-parts": [[1918, 5]]}}),
                         "issued", "fuzzy") is True
    report = score_rows([row], {"one": prediction({"issued": {"date-parts": [[1918, 5]]}})})
    assert report["adapter"]["issued"]["date_year_only"]["strict"] == 1


def test_precision_is_capped_at_the_gold_precision():
    # Gold printed a month; a more precise prediction is compared at the
    # month, so an extra day does not lose the field.
    row = gold_row({"type": "book", "issued": {"date-parts": [[1918, 5]]}})
    assert field_correct(row, prediction({"issued": {"date-parts": [[1918, 5, 3]]}}),
                         "issued", "strict") is True
    assert field_correct(row, prediction({"issued": {"date-parts": [[1918, 6]]}}),
                         "issued", "strict") is False
    report = score_rows([row], {"one": prediction({"issued": {"date-parts": [[1918, 5, 3]]}})})
    assert report["adapter"]["issued"]["date_precision"]["strict"] == 1


def test_precision_is_never_upgraded_to_match_a_more_precise_gold():
    year_month = gold_row({"type": "book", "issued": {"date-parts": [[1918, 5]]}})
    assert field_correct(year_month, prediction({"issued": {"date-parts": [[1918]]}}),
                         "issued", "strict") is False
    year_only = gold_row({"type": "book", "issued": {"date-parts": [[1918]]}})
    wrong_year = prediction({"issued": {"date-parts": [[1919, 1]]}})
    assert field_correct(year_only, wrong_year, "issued", "strict") is False


def test_title_is_composed_with_its_subtitle_on_both_sides():
    in_title = gold_row({"type": "book", "title": "Grace for Grace: The Debates"})
    split_pred = prediction({"title": "Grace for Grace", "subtitle": "The Debates"})
    assert field_correct(in_title, split_pred, "title", "strict") is True
    split_gold = gold_row({"type": "book", "title": "Grace for Grace",
                           "subtitle": "The Debates"})
    in_title_pred = prediction({"title": "Grace for Grace: The Debates"})
    assert field_correct(split_gold, in_title_pred, "title", "strict") is True
    assert score_rows([in_title], {"one": split_pred})["adapter"]["title"][
        "title_composed"]["strict"] == 1


def test_title_composition_still_rejects_a_dropped_subtitle():
    row = gold_row({"type": "book", "title": "Grace for Grace: The Debates"})
    assert field_correct(row, prediction({"title": "Grace for Grace"}), "title", "strict") is False


def test_gold_imprint_is_also_read_as_its_publisher_part():
    row = gold_row({"type": "book", "publisher": "White Plains, NY : Longman"})
    publisher_only = prediction({"publisher": "Longman"})
    assert field_correct(row, publisher_only, "publisher", "strict") is True
    assert field_correct(row, prediction({"publisher": "White Plains, NY : Longman"}),
                         "publisher", "strict") is True
    assert field_correct(row, prediction({"publisher": "White Plains"}),
                         "publisher", "strict") is False
    report = score_rows([row], {"one": publisher_only})
    assert report["adapter"]["publisher"]["publisher_split"]["strict"] == 1


def test_publisher_split_does_not_accept_the_place_of_a_publisher_first_imprint():
    # "Random House, Inc., New York" is a publisher followed by a place; the
    # part after the comma must not become an accepted publisher.
    row = gold_row({"type": "book", "publisher": "Random House, Inc., New York"})
    assert field_correct(row, prediction({"publisher": "New York"}), "publisher", "strict") is False
    assert field_correct(row, prediction({"publisher": "Random House, Inc."}),
                         "publisher", "strict") is False
    report = score_rows([row], {"one": prediction({"publisher": "New York"})})
    assert "publisher" not in report["adapter"]


def test_adapter_rules_rescue_a_whole_record_and_are_reported():
    row = gold_row({"type": "book", "title": "Grace for Grace: The Debates",
                    "publisher": "White Plains, NY : Longman",
                    "issued": {"date-parts": [[1918]]}})
    shaped = prediction({"title": "Grace for Grace", "subtitle": "The Debates",
                         "publisher": "Longman", "issued": {"date-parts": [[1918, 5]]}})
    report = score_rows([row], {"one": shaped})
    assert report["scorer_version"] == "sciencemeta-v3"
    assert report["metrics"]["strict_record_accuracy"]["value"] == 1.0
    assert report["adapter"] == {
        # difflib already gives "1918" vs "1918-5" a 0.8 ratio, so the date rule
        # only changes the strict and soft comparisons.
        "issued": {"date_year_only": {"strict": 1, "soft": 1, "fuzzy": 0}},
        "publisher": {"publisher_split": {"strict": 1, "soft": 1, "fuzzy": 1}},
        "title": {"title_composed": {"strict": 1, "soft": 1, "fuzzy": 1}},
    }
    markdown = render_markdown(report, "adapter")
    assert "## Adapter rules (v3)" in markdown
    assert "| issued | `date_year_only` | 1 | 1 | 0 |" in markdown


def test_a_rule_that_changes_nothing_is_not_reported():
    row = gold_row({"type": "book", "title": "A History of South Africa",
                    "publisher": "Cambridge University Press"})
    report = score_rows([row], {"one": prediction({"title": "A History of South Africa",
                                                   "publisher": "Cambridge University Press"})})
    assert report["adapter"] == {}


# --- Levenshtein -------------------------------------------------------------
def test_levenshtein_threshold_accepts_near_miss_and_rejects_other_work():
    row = gold_row({"type": "book", "title": "Modern Approaches to Machine Learning"})
    near = prediction({"title": "Modern Approaches to Machine Learnin"})
    other = prediction({"title": "Ancient Philosophy of Mind"})
    assert ratio("abcdefgh", "abcdefgz") >= score.LEVENSHTEIN_THRESHOLD
    assert field_correct(row, near, "title", "fuzzy") is True
    assert field_correct(row, other, "title", "fuzzy") is False


def test_levenshtein_uses_field_normalization_for_isbn():
    row = gold_row({"type": "book", "ISBN": "9787030336613"})
    assert field_correct(row, prediction({"ISBN": "978-7-03-033661-3"}), "ISBN", "fuzzy") is True


# --- Wilson intervals --------------------------------------------------------
def test_wilson_zero_denominator_is_undefined():
    assert wilson(0, 0) == (None, None)


def test_wilson_all_successes_and_no_successes():
    low, high = wilson(10, 10)
    assert high == 1.0
    assert 0.6 < low < 1.0
    low, high = wilson(0, 10)
    assert low == 0.0
    assert 0.0 < high < 0.5


def test_wilson_midpoint_is_symmetric_and_brackets_the_rate():
    low, high = wilson(5, 10)
    assert low < 0.5 < high
    assert abs((low + high) / 2 - 0.5) < 0.001
    assert 0.2 < low < 0.25  # 0.2366 for n=10


# --- records, instance recall, outside_scope --------------------------------
def test_outside_scope_field_is_excluded_from_every_denominator():
    row = gold_row(BOOK)  # no ISBN in gold
    assert "ISBN" not in evaluated_fields(row)
    assert row["field_status"]["ISBN"] == "outside_scope"
    assert row["field_status"]["title"] == "present"
    extra = prediction({**BOOK, "ISBN": "9783161484100"})
    assert field_correct(row, extra, "title", "strict") is True
    report = score_rows([row], {"one": extra})
    assert report["per_field"]["ISBN"]["n"] == 0
    assert report["per_field"]["ISBN"]["precision"]["ci95"] == [None, None]
    assert report["metrics"]["strict_record_accuracy"]["value"] == 1.0


def test_strict_and_soft_record_accuracy_diverge():
    row = gold_row({"type": "book", "title": "A History of South Africa."})
    report = score_rows([row], {"one": prediction({"title": "a history of south africa"})})
    assert report["metrics"]["strict_record_accuracy"]["value"] == 0.0
    assert report["metrics"]["strict_record_accuracy"]["n"] == 1
    assert report["metrics"]["soft_record_accuracy"]["value"] == 1.0
    assert report["metrics"]["levenshtein_record_accuracy"]["value"] == 1.0


def test_levenshtein_record_accuracy_rejects_unrelated_record():
    row = gold_row({"type": "book", "title": "Modern Approaches to Machine Learning"})
    report = score_rows([row], {"one": prediction({"title": "Ancient Philosophy of Mind"})})
    assert report["metrics"]["levenshtein_record_accuracy"]["value"] == 0.0


def test_instance_recall_requires_every_gold_present_field():
    rows = [
        gold_row(BOOK, row_id="one"),
        gold_row({"type": "book", "title": BOOK["title"], "author": BOOK["author"],
                  "publisher": "Oxford University Press", "issued": BOOK["issued"]}, row_id="two"),
    ]
    predictions = {
        "one": prediction(BOOK, row_id="one"),
        "two": prediction({**BOOK, "publisher": "Cambridge University Press"}, row_id="two"),
    }
    report = score_rows(rows, predictions)
    assert report["metrics"]["instance_recall"]["n"] == 2
    assert report["metrics"]["instance_recall"]["hits"] == 1
    assert report["metrics"]["instance_recall"]["value"] == 0.5


def test_missing_prediction_counts_against_precision_and_recall():
    rows = [gold_row(BOOK, row_id="one"), gold_row(BOOK, row_id="two")]
    report = score_rows(rows, {"one": prediction(BOOK, row_id="one")})
    title = report["per_field"]["title"]
    assert (title["tp"], title["fp"], title["fn"], title["n"]) == (1, 0, 1, 2)
    assert title["precision"]["value"] == 1.0
    assert title["recall"]["value"] == 0.5
    assert round(title["value"], 4) == round(2 / 3, 4)
    assert report["metrics"]["strict_record_accuracy"]["value"] == 0.5
    assert report["not_attempted"] == 1
    assert report["attempted"] == 1


def test_failed_status_is_scored_as_missing():
    row = gold_row(BOOK)
    report = score_rows([row], {"one": prediction(BOOK, status="failed")})
    assert report["per_field"]["title"]["fn"] == 1
    assert report["completed"] == 0
    assert report["failed"] == 1


def test_rows_without_evaluated_fields_are_not_scored():
    row = gold_row({"type": "book"}, field_status={field: "outside_scope" for field in FIELDS})
    report = score_rows([row], {})
    assert report["sources"] == 1
    assert report["scored_sources"] == 0
    assert report["metrics"]["strict_record_accuracy"]["n"] == 0
    assert report["metrics"]["strict_record_accuracy"]["ci95"] == [None, None]
    markdown = render_markdown(report, "empty")
    assert "n=0" in markdown
    assert "95% CI n/a" in markdown
    assert "No evaluated rows for: title, author, publisher, issued, ISBN" in markdown


# --- prediction schema -------------------------------------------------------
def test_bare_csl_objects_join_on_id_and_count_as_ok():
    indexed = score._prediction_rows([{"id": "one", "type": "book", "title": "A History of South Africa"}])
    assert indexed["one"]["status"] == "ok"
    assert indexed["one"]["csl"]["title"] == "A History of South Africa"
    report = score_rows([gold_row({"type": "book", "title": "A History of South Africa"})], indexed)
    assert report["metrics"]["strict_record_accuracy"]["value"] == 1.0


# --- reports -----------------------------------------------------------------
def test_markdown_carries_n_and_ci_on_every_metric_line():
    row = gold_row(BOOK)
    report = score_rows([row], {"one": prediction(BOOK)})
    markdown = render_markdown(report, "unit")
    metric_lines = [line for line in markdown.splitlines()
                    if line.startswith("| ") and ("accuracy" in line or "instance recall" in line)]
    assert len(metric_lines) == 4
    for line in metric_lines:
        assert " n=" in line
        assert "95% CI " in line
        assert "–" in line
    assert "outside_scope" in markdown


def test_main_writes_report_json_and_markdown(tmp_path):
    manifest_path = tmp_path / "manifest.jsonl"
    predictions_path = tmp_path / "predictions.jsonl"
    manifest_path.write_text(json.dumps(gold_row(BOOK)) + "\n", encoding="utf-8")
    predictions_path.write_text(json.dumps(prediction({"title": BOOK["title"]})) + "\n", encoding="utf-8")
    assert score.main(manifest_path, predictions_path, tmp_path / "reports", "unit-label") == 0
    report = json.loads((tmp_path / "reports" / "report.json").read_text(encoding="utf-8"))
    assert report["label"] == "unit-label"
    assert report["manifest_sha256"] == score.hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    markdown = (tmp_path / "reports" / "report.md").read_text(encoding="utf-8")
    assert "n=1" in markdown
    assert "95% CI " in markdown


# --- manifest mapping --------------------------------------------------------
ROW = {"sha256": SHA, "isbn": "978-7-03-033661-3", "title": "伏牛山药用植物志 第四卷",
       "author": "尹卫平,王忠东\n", "abstract": "  摘要 文本 ", "category": "Q949.95",
       "pub_time": "2012", "publisher": "科学出版社", "doi": "10.1/xyz", "keyword": "药用植物",
       "kind": "textbook", "batch": "20250806"}


def test_build_row_maps_gold_fields_and_hides_extras():
    row = manifest.build_row(ROW, "textbook", Path("data/pdf"))
    csl = row["csl"]
    assert row["id"] == SHA and row["source_sha256"] == SHA
    assert row["source_path"] == f"data/pdf/{SHA}.pdf"
    assert csl["type"] == "book"
    assert csl["ISBN"] == "978-7-03-033661-3"
    assert csl["title"] == "伏牛山药用植物志 第四卷"
    assert row["csl"]["author"] == [{"literal": "尹卫平"}, {"literal": "王忠东"}]
    assert csl["publisher"] == "科学出版社"
    assert csl["issued"] == {"date-parts": [[2012]]}
    for field in ("abstract", "doi", "keyword", "category"):
        assert field not in csl
    assert row["gold"]["extra"] == {"abstract": "摘要 文本", "doi": "10.1/xyz",
                                    "keyword": "药用植物", "category": "Q949.95"}
    assert row["gold"]["kind"] == "textbook" and row["gold"]["batch"] == "20250806"
    assert "type" not in row["field_status"]
    assert all(row["field_status"][field] == "present" for field in FIELDS)


def test_build_row_marks_absent_fields_outside_scope():
    row = manifest.build_row({"sha256": SHA, "title": "Only a title", "author": "", "isbn": "",
                              "publisher": "", "pub_time": "ECON", "kind": "ebook"}, "ebook")
    assert "author" not in row["csl"] and "publisher" not in row["csl"]
    assert "issued" not in row["csl"] and "ISBN" not in row["csl"]
    for field in ("author", "publisher", "issued", "ISBN"):
        assert row["field_status"][field] == "outside_scope"
    assert row["field_status"]["title"] == "present"


def test_year_accepts_four_digits_only():
    assert manifest.year("1957") == 1957
    assert manifest.year("2020-05-01") == 2020
    assert manifest.year("ECON") is None
    assert manifest.year("") is None
    assert manifest.year("57") is None


def test_isbn_keeps_a_single_identifier():
    assert manifest.isbn("9783031099540,9783031099557") == "9783031099540"
    assert manifest.isbn(" 978-3-16-148410-0 ") == "978-3-16-148410-0"


def test_main_filters_by_modality(tmp_path):
    rows_path = tmp_path / "rows.jsonl"
    ebook = {**ROW, "sha256": "a" * 64, "kind": "ebook"}
    rows_path.write_text(json.dumps(ROW) + "\n" + json.dumps(ebook) + "\n", encoding="utf-8")
    out = tmp_path / "manifest.jsonl"
    assert manifest.main(rows_path, out, "textbook") == 0
    rows = [json.loads(line) for line in out.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert [row["id"] for row in rows] == [SHA]
    assert rows[0]["modality"] == "textbook"
