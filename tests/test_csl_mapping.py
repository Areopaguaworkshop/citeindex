"""Cataloguing semantics: MARC-shaped values recorded in CSL's own fields.

Pure, offline unit tests for ``normalize_cataloguing_fields`` and its helpers.
"""
from citeindex.ingestion.csl import (
    normalize_cataloguing_fields,
    publisher_like,
    split_publisher,
    split_title,
)


def test_colon_imprint_splits_into_place_and_publisher():
    csl = {"type": "book", "publisher": "Cambridge, Mass. : Harvard University Press"}
    result = normalize_cataloguing_fields(csl)
    assert result["publisher-place"] == "Cambridge, Mass."
    assert result["publisher"] == "Harvard University Press"


def test_comma_imprint_splits_only_when_the_tail_is_publisher_like():
    result = normalize_cataloguing_fields(
        {"type": "book", "publisher": "New York, Oxford University Press"})
    assert (result["publisher-place"], result["publisher"]) == ("New York", "Oxford University Press")


def test_publisher_without_a_marker_is_not_mangled():
    # The publisher comes first here, so the part after the last comma is a
    # place, not a publisher: splitting would swap the two.
    csl = {"type": "book", "publisher": "Random House, Inc., New York"}
    result = normalize_cataloguing_fields(csl)
    assert result["publisher"] == "Random House, Inc., New York"
    assert "publisher-place" not in result
    assert split_publisher("Random House, Inc., New York") is None
    assert split_publisher("Longman") is None
    assert publisher_like("Harvard University Press")
    assert publisher_like("科学出版社")
    assert not publisher_like("New York")


def test_existing_publisher_place_is_never_overwritten():
    csl = {"type": "book", "publisher": "Longman : Longman",
           "publisher-place": "Harlow"}
    result = normalize_cataloguing_fields(csl)
    assert (result["publisher-place"], result["publisher"]) == ("Harlow", "Longman : Longman")


def test_cjk_imprint_splits_on_the_full_width_colon():
    result = normalize_cataloguing_fields({"type": "book", "publisher": "北京:科学出版社"})
    assert (result["publisher-place"], result["publisher"]) == ("北京", "科学出版社")


def test_title_splits_into_title_and_subtitle_with_the_full_string_kept():
    full = "Grace for Grace: The Debates"
    result = normalize_cataloguing_fields({"type": "book", "title": full})
    assert (result["title"], result["subtitle"]) == ("Grace for Grace", "The Debates")
    assert result["_title_full"] == full
    assert f"{result['title']}: {result['subtitle']}" == result["_title_full"]


def test_title_cjk_colon_and_short_tails():
    result = normalize_cataloguing_fields({"type": "book", "title": "伏牛山药用植物志：第四卷"})
    assert (result["title"], result["subtitle"]) == ("伏牛山药用植物志", "第四卷")
    # Fewer than three characters after the colon, or no space after an ASCII
    # colon, is not a subtitle.
    assert split_title("Star Wars: A") is None
    assert split_title("Re:Thinking") is None


def test_existing_subtitle_is_kept_and_never_re_split():
    csl = {"type": "book", "title": "Grace for Grace", "subtitle": "The Debates"}
    result = normalize_cataloguing_fields(csl)
    assert (result["title"], result["subtitle"]) == ("Grace for Grace", "The Debates")
    assert "_title_full" not in result


def test_mapping_is_idempotent():
    csl = {"type": "book", "title": "Grace for Grace: The Debates",
           "publisher": "Cambridge, Mass. : Harvard University Press",
           "issued": {"date-parts": [[1932, 5]]}}
    once = normalize_cataloguing_fields(csl)
    twice = normalize_cataloguing_fields(once)
    assert twice == once
    assert (twice["publisher-place"], twice["publisher"]) == (
        "Cambridge, Mass.", "Harvard University Press")


def test_issued_keeps_exactly_the_printed_precision():
    for parts in ([[1932]], [[1932, 5]], [[1932, 5, 6]]):
        result = normalize_cataloguing_fields({"type": "book", "issued": {"date-parts": parts}})
        assert result["issued"] == {"date-parts": parts}
    # Never truncate a month that was printed, never add one that was not.
    year_only = normalize_cataloguing_fields({"type": "book", "issued": {"date-parts": [[1932]]}})
    assert year_only["issued"]["date-parts"] == [[1932]]
    month = normalize_cataloguing_fields({"type": "book", "issued": {"date-parts": [[1932, 5]]}})
    assert month["issued"]["date-parts"] == [[1932, 5]]


def test_input_dict_is_not_mutated():
    csl = {"type": "book", "title": "Grace for Grace: The Debates",
           "publisher": "New York : Longman", "issued": {"date-parts": [[1932, 5]]}}
    snapshot = {"type": "book", "title": "Grace for Grace: The Debates",
                "publisher": "New York : Longman", "issued": {"date-parts": [[1932, 5]]}}
    normalize_cataloguing_fields(csl)
    assert csl == snapshot
    assert csl["issued"]["date-parts"][0] is not snapshot["issued"]["date-parts"][0]


def test_mapping_runs_inside_standardize_csl_json(tmp_path):
    from citeindex.ingestion.master import CiteIndexIngestionOrchestrator

    orchestrator = CiteIndexIngestionOrchestrator(str(tmp_path / "corpus"))
    csl = {"type": "book", "title": "Grace for Grace: The Debates",
           "publisher": "New York : Longman", "issued": {"date-parts": [[1932, 5]]}}
    standardized = orchestrator.standardize_csl_json(csl, {"root": "r"}, "digital_pdf")
    assert (standardized["title"], standardized["subtitle"]) == ("Grace for Grace", "The Debates")
    assert (standardized["publisher-place"], standardized["publisher"]) == ("New York", "Longman")
    assert standardized["issued"] == {"date-parts": [[1932, 5]]}
    # The digest describes the CSL that is persisted, not its pre-mapping form.
    from citeindex.ingestion.deterministic import hash_payload

    assert standardized["content_hash"] == hash_payload(
        {"csl": {key: value for key, value in standardized.items()
                 if key not in {"id", "content_hash", "merkle_root", "source_type",
                                "ingestion_timestamp"}},
         "merkle_root": "r", "source_type": "digital_pdf"})
