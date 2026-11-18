"""Unit tests for the ScienceMetaBench oracle audit.

No network and no external binary: every prediction, gold row and TEI document
is built in memory or in a tmp_path.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import oracle

FIELDS = oracle.FIELDS

TEI = """<?xml version="1.0" encoding="UTF-8"?>
<TEI xmlns="http://www.tei-c.org/ns/1.0">
  <teiHeader>
    <fileDesc>
      <titleStmt><title>Grace for Grace: The Debates</title></titleStmt>
      <sourceDesc>
        <biblStruct>
          <analytic>
            <author>
              <persName><forename>John</forename><surname>Smith</surname></persName>
            </author>
          </analytic>
          <monogr>
            <title>Monograph Title Must Lose</title>
            <imprint>
              <pubPublisher>Cambridge University Press</pubPublisher>
              <date when="2016-06-01"/>
            </imprint>
            <idno type="ISBN">978-3-16-148410-0</idno>
          </monogr>
        </biblStruct>
      </sourceDesc>
    </fileDesc>
  </teiHeader>
  <text/>
</TEI>
"""


def prediction(csl, row_id="one", status="ok"):
    return {"id": row_id, "status": status, "csl": csl}


def predictions(*rows):
    return {row["id"]: row for row in rows}


def gold_row(csl, row_id="one"):
    status = {field: ("present" if csl.get(field) is not None else "outside_scope")
              for field in FIELDS}
    return {"id": row_id, "csl": csl, "field_status": status}


def write_jsonl(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def counts(result, field):
    return result["per_field"][field]


# --- agreement and intervals -------------------------------------------------
def test_agreement_counts_and_wilson_interval():
    a = predictions(prediction({"title": "Same Title"}),
                    prediction({"title": "Alpha"}, row_id="two"),
                    prediction({"title": "Beta"}, row_id="three"))
    b = predictions(prediction({"title": "same   title"}),
                    prediction({"title": "Alpha"}, row_id="two"),
                    prediction({"title": "Gamma"}, row_id="three"))
    title = counts(oracle.audit(a, b, ["one", "two", "three"]), "title")
    assert title["n"] == 3
    assert title["agreements"] == 2
    assert title["rate"] == 2 / 3
    low, high = title["ci95"]
    assert low is not None and high is not None
    assert low < title["rate"] < high


def test_zero_denominator_has_no_invented_rate():
    title = counts(oracle.audit({}, {}, []), "title")
    assert title["n"] == 0
    assert title["rate"] is None
    assert title["ci95"] == [None, None]


def test_a_only_b_only_and_both_absent():
    a = predictions(prediction({"title": "Alpha", "author": [{"literal": "A"}], "ISBN": "1"}))
    b = predictions(prediction({"title": "Alpha", "publisher": "P"}))
    result = oracle.audit(a, b, ["one"])
    assert counts(result, "title")["n"] == 1
    assert counts(result, "author")["a_only"] == 1
    assert counts(result, "publisher")["b_only"] == 1
    assert counts(result, "issued")["both_absent"] == 1


def test_failed_status_supplies_no_fields():
    a = predictions(prediction({"title": "Alpha"}, status="failed"))
    b = predictions(prediction({"title": "Alpha"}))
    result = oracle.audit(a, b, ["one"])
    assert counts(result, "title")["b_only"] == 1
    assert counts(result, "title")["n"] == 0


# --- gold-relative counts ----------------------------------------------------
def test_both_wrong_needs_a_manifest():
    a = predictions(prediction({"title": "Alpha"}))
    b = predictions(prediction({"title": "Beta"}))
    assert counts(oracle.audit(a, b, ["one"]), "title")["gold"] is None
    result = oracle.audit(a, b, ["one"], {"one": gold_row({"title": "Gamma"})})
    gold = counts(result, "title")["gold"]
    assert gold["judged"] == 1
    assert gold["both_wrong"] == 1
    assert gold["a_right_b_wrong"] == 0
    assert gold["a_wrong_b_right"] == 0


def test_gold_relative_side_wins():
    a = predictions(prediction({"title": "Alpha"}))
    b = predictions(prediction({"title": "Beta"}))
    result = oracle.audit(a, b, ["one"], {"one": gold_row({"title": "Alpha"})})
    gold = counts(result, "title")["gold"]
    assert gold["a_right_b_wrong"] == 1
    assert counts(result, "title")["gold"]["both_wrong"] == 0


# --- pattern detectors -------------------------------------------------------
def test_publisher_in_author_detector():
    a = predictions(prediction({"publisher": "Longman", "author": [{"literal": "Smith"}]}))
    b = predictions(prediction({"publisher": "Longman Group", "author": [{"literal": "Longman, Smith"}]}))
    pattern = oracle.audit(a, b, ["one"])["patterns"]["publisher_in_author"]
    assert pattern["count"] == 1
    assert pattern["example"]["id"] == "one"


def test_title_prefix_detector():
    a = predictions(prediction({"title": "A History"}))
    b = predictions(prediction({"title": "A History of South Africa"}))
    pattern = oracle.audit(a, b, ["one"])["patterns"]["title_prefix"]
    assert pattern["count"] == 1
    assert pattern["example"]["id"] == "one"


def test_date_precision_detector():
    a = predictions(prediction({"issued": {"date-parts": [[2016, 6, 1]]}}))
    b = predictions(prediction({"issued": {"date-parts": [[2016]]}}))
    result = oracle.audit(a, b, ["one"])
    assert counts(result, "issued")["n"] == 1
    assert result["patterns"]["date_precision"]["count"] == 1
    assert result["patterns"]["date_precision"]["example"]["id"] == "one"


def test_name_order_detector():
    a = predictions(prediction({"author": [{"literal": "Smith, John"}]}))
    b = predictions(prediction({"author": [{"literal": "John Smith"}]}))
    pattern = oracle.audit(a, b, ["one"])["patterns"]["name_order"]
    assert pattern["count"] == 1
    assert pattern["example"]["id"] == "one"


def test_subtitle_split_detector():
    a = predictions(prediction({"title": "Grace for Grace: The Debates"}))
    b = predictions(prediction({"title": "Grace for Grace"}))
    patterns = oracle.audit(a, b, ["one"])["patterns"]
    assert patterns["subtitle_split"]["count"] == 1
    assert patterns["subtitle_split"]["example"]["id"] == "one"
    # Documented overlap: a subtitle split is also a title prefix.
    assert patterns["title_prefix"]["count"] == 1


# --- Mode B ------------------------------------------------------------------
def test_parse_tei_header_from_inline_xml():
    csl = oracle.parse_tei(TEI)
    assert csl["title"] == "Grace for Grace: The Debates"
    assert csl["author"] == [{"given": "John", "family": "Smith"}]
    assert csl["publisher"] == "Cambridge University Press"
    assert csl["issued"] == {"date-parts": [[2016, 6, 1]]}
    assert csl["ISBN"] == "978-3-16-148410-0"


def test_mode_b_compares_predictions_against_tei_directory(tmp_path):
    tei_dir = tmp_path / "tei"
    tei_dir.mkdir()
    (tei_dir / "one.tei.xml").write_text(TEI, encoding="utf-8")
    predictions_a = tmp_path / "a.jsonl"
    write_jsonl(predictions_a, [prediction(oracle.parse_tei(TEI))])
    out_dir = tmp_path / "out"
    assert oracle.run(predictions_a=predictions_a, tei_dir=tei_dir,
                      out_dir=out_dir, label="tei") == 0
    report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    assert report["mode"] == "tei"
    assert report["coverage"]["compared_ids"] == 1
    for field in ("title", "author", "publisher", "issued", "ISBN"):
        assert report["per_field"][field]["n"] == 1
        assert report["per_field"][field]["agreements"] == 1
    markdown = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "B - GROBID TEI" in markdown
    assert "n=1" in markdown
    assert "95% CI" in markdown


# --- Mode A integration ------------------------------------------------------
def test_mode_a_writes_report_json_and_markdown(tmp_path):
    predictions_a = tmp_path / "a.jsonl"
    predictions_b = tmp_path / "b.jsonl"
    manifest = tmp_path / "manifest.jsonl"
    write_jsonl(predictions_a, [prediction({"title": "Alpha"}),
                                prediction({"title": "Beta"}, row_id="two")])
    write_jsonl(predictions_b, [prediction({"title": "Alpha"}),
                                prediction({"title": "Gamma"}, row_id="two")])
    write_jsonl(manifest, [gold_row({"title": "Alpha"}),
                           gold_row({"title": "Beta"}, row_id="two")])
    out_dir = tmp_path / "out"
    assert oracle.run(predictions_a=predictions_a, predictions_b=predictions_b,
                      manifest=manifest, out_dir=out_dir, label="a vs b") == 0
    report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    title = report["per_field"]["title"]
    assert (title["n"], title["agreements"], title["rate"]) == (2, 1, 0.5)
    assert title["gold"]["a_right_b_wrong"] == 1
    markdown = (out_dir / "report.md").read_text(encoding="utf-8")
    assert "a vs b" in markdown
    assert "a right / b wrong" in markdown
    assert "95% CI" in markdown


def test_trailing_partial_prediction_line_is_skipped(tmp_path):
    predictions_a = tmp_path / "a.jsonl"
    predictions_a.write_text(
        json.dumps(prediction({"title": "Alpha"})) + "\n"
        + '{"id": "two", "status": "ok", "csl": {"ti', encoding="utf-8")
    predictions_b = tmp_path / "b.jsonl"
    write_jsonl(predictions_b, [prediction({"title": "Alpha"})])
    out_dir = tmp_path / "out"
    assert oracle.run(predictions_a=predictions_a, predictions_b=predictions_b,
                      out_dir=out_dir, label="partial") == 0
    report = json.loads((out_dir / "report.json").read_text(encoding="utf-8"))
    assert report["coverage"]["unreadable_lines"]["a"] == 1
    assert report["coverage"]["compared_ids"] == 1


# --- unusable inputs ---------------------------------------------------------
def test_exit_code_2_and_one_line_when_no_oracle_is_supplied(tmp_path, capsys):
    predictions_a = tmp_path / "a.jsonl"
    write_jsonl(predictions_a, [prediction({"title": "Alpha"})])
    out_dir = tmp_path / "out"
    assert oracle.run(predictions_a=predictions_a, out_dir=out_dir) == 2
    error = capsys.readouterr().err.strip()
    assert error.startswith("oracle:")
    assert "Mode A" in error
    assert "Mode B" in error
    assert len(error.splitlines()) == 1
    assert not out_dir.exists()


def test_exit_code_2_when_tei_directory_is_empty(tmp_path, capsys):
    predictions_a = tmp_path / "a.jsonl"
    write_jsonl(predictions_a, [prediction({"title": "Alpha"})])
    tei_dir = tmp_path / "tei"
    tei_dir.mkdir()
    out_dir = tmp_path / "out"
    assert oracle.run(predictions_a=predictions_a, tei_dir=tei_dir, out_dir=out_dir) == 2
    error = capsys.readouterr().err.strip()
    assert "TEI" in error
    assert len(error.splitlines()) == 1
    assert not out_dir.exists()


def test_exit_code_2_when_both_oracle_sources_are_given(tmp_path, capsys):
    predictions_a = tmp_path / "a.jsonl"
    write_jsonl(predictions_a, [prediction({"title": "Alpha"})])
    predictions_b = tmp_path / "b.jsonl"
    write_jsonl(predictions_b, [prediction({"title": "Alpha"})])
    assert oracle.run(predictions_a=predictions_a, predictions_b=predictions_b,
                      tei_dir=tmp_path, out_dir=tmp_path / "out") == 2
    error = capsys.readouterr().err.strip()
    assert "Mode A" in error and "Mode B" in error
