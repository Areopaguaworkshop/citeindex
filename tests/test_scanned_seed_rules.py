"""Offline tests for the scanned title/credit seed rules (probe-derived, no LLM).

Each rule carries a small synthetic fixture mirroring the 12-source OCR probe
(2026-09-26): heading titles (L1 pair, medial ' The ' split, L2 continuation,
back-matter exclusion), title-page year priority, translator and editor
credits, and the Roman-numeral imprint year.
"""

from types import SimpleNamespace

from citeindex.ingestion.citation_verification import validate_block_evidence
from citeindex.ingestion.pipelines.dspy_extract import (
    _heading_title_candidates,
    _latin_person_value,
    _person_leading,
    _roman_year,
    _seed_deterministic_candidates,
    _split_person_list,
)


def _cl(*items):
    """content_list items: (page_idx, text, text_level)."""
    return [{"page_idx": page, "text": text, "text_level": level}
            for page, text, level in items]


def _blocks(content_list):
    return [{"id": f"ocr_{i}", "text": it["text"], "physical_page_index": it["page_idx"],
             "role": it.get("type", "text")}
            for i, it in enumerate(content_list) if it["text"].strip()]


def _seed(content_list, doc_type="book"):
    accepted, evidence = {}, {}
    _seed_deterministic_candidates(_blocks(content_list), [], [],
                                   {"document_type": doc_type, "selected_pages": [1]},
                                   accepted, evidence, content_list=content_list)
    return accepted


# ---------------------------------------------------------------------------
# Person-name helpers
# ---------------------------------------------------------------------------


def test_person_value_uses_last_token_as_family():
    assert _latin_person_value("Robert A. Kitchen") == {"given": "Robert A.", "family": "Kitchen"}
    assert _latin_person_value("Martien F. G. Parmentier") == {
        "given": "Martien F. G.", "family": "Parmentier"}
    assert _latin_person_value("G. GARITTE") == {"given": "G.", "family": "GARITTE"}
    assert _latin_person_value("高陈宝婵") is None
    assert _latin_person_value("Band 20") is None


def test_split_person_list_joins_two_translators():
    assert _split_person_list("Robert A. Kitchen and Martien F. G. Parmentier") == [
        {"given": "Robert A.", "family": "Kitchen"},
        {"given": "Martien F. G.", "family": "Parmentier"}]


def test_person_leading_drops_office_and_rejects_role_words():
    assert _person_leading("DR. OTTO STÄHLIN PROIESSOR AM K. MAXGYMNASIUM IN MÜNCHEN") == {
        "given": "OTTO", "family": "STÄHLIN"}
    assert _person_leading("G. GARITTE") == {"given": "G.", "family": "GARITTE"}
    assert _person_leading("VON") is None
    assert _person_leading("IM AUFTRAGE DER KIRCHENVÄTER-COMMISSION") is None
    assert _person_leading("DER KONIGL. PREUSSISCHEN AKADEMIE") is None
    assert _person_leading("HERAUSGEGEBEN") is None


# ---------------------------------------------------------------------------
# Heading title candidates
# ---------------------------------------------------------------------------


def test_heading_pair_is_title_and_subtitle():
    cands = _heading_title_candidates(_cl((0, "Saint Benedict", 1), (0, "The Man and His Work", 1)))
    assert [(c["field"], c["value"]) for c in cands] == [
        ("title", "Saint Benedict"), ("subtitle", "The Man and His Work")]


def test_heading_medial_the_splits_title_and_subtitle():
    cands = _heading_title_candidates(_cl((0, "The BOOK of STEPS The Syriac Liber Graduum", 1)))
    assert [(c["field"], c["value"]) for c in cands] == [
        ("title", "The BOOK of STEPS"), ("subtitle", "The Syriac Liber Graduum")]


def test_heading_takes_following_l2_as_subtitle():
    cands = _heading_title_candidates(_cl(
        (0, "THE LETTERS OF ST. ANTONY", 1),
        (0, "MONASTICISM AND THE MAKING OF A SAINT", 2),
        (0, "Samuel Rubenson", 2)))
    assert [(c["field"], c["value"]) for c in cands] == [
        ("title", "THE LETTERS OF ST. ANTONY"),
        ("subtitle", "MONASTICISM AND THE MAKING OF A SAINT")]


def test_heading_skips_back_matter_pages():
    # The sampler takes pages 1-10 plus the last 3; a back-matter chapter
    # heading (astudy p112, orthodox p241) must not become the title.
    cands = _heading_title_candidates(_cl(
        (0, "A STUDY OF THE DIVINE LITURGY", 1),
        (111, "APPENDIX III", 1), (112, "A Commentary on the Divine Liturgy", 1)))
    assert [c["field"] for c in cands] == ["title"]
    assert cands[0]["value"] == "A STUDY OF THE DIVINE LITURGY"


def test_heading_skips_series_frontmatter_and_garble():
    assert _heading_title_candidates(_cl((0, "Gorgias Eastern Christian Studies", 1))) == []
    assert _heading_title_candidates(_cl((0, "Preface", 1))) == []
    assert _heading_title_candidates(_cl((0, "About the Author", 1))) == []
    assert _heading_title_candidates(_cl((0, "THE DIVINE LI+URGY OF ST JON CHRSOSTOM", 1))) == []
    assert _heading_title_candidates(_cl((0, "PROOEMIUM.", 1))) == []


def test_single_heading_flags_no_subtitle():
    cands = _heading_title_candidates(_cl((0, "使徒教父著作", 1)))
    assert [(c["field"], c["value"]) for c in cands] == [("title", "使徒教父著作")]
    assert cands[0]["_no_subtitle"] is True


def test_caps_title_with_editor_verb_also_seeds_author_literal():
    cands = _heading_title_candidates(_cl(
        (0, "CLEMENS ALEXANDRINUS", 1),
        (0, "PROTREPTICUS UND PAEDAGOGUS", 1),
        (0, "HERAUSGEGEBEN", None)))
    assert ("authors", "Clemens Alexandrinus") in [(c["field"], c["value"]) for c in cands]


def test_caps_title_without_editor_verb_stays_title_only():
    # 'Saint Benedict' is a title, not an author: no editor-verb signal.
    cands = _heading_title_candidates(_cl((0, "SAINT BENEDICT", 1)))
    assert [c["field"] for c in cands] == ["title"]


# ---------------------------------------------------------------------------
# Year seeds
# ---------------------------------------------------------------------------


def test_copyright_year_prefers_copyright_prefix_over_bare_notice():
    accepted = _seed(_cl((1, "©2001 Abbaye de Bellefontaine", None),
                         (1, "Copyright© 2006 St. Bede's Publications", None)))
    assert accepted["issued"] == {"date-parts": [[2006]]}


def test_copyright_year_prefers_first_published_over_copyright():
    accepted = _seed(_cl(
        (1, "was first pub  \nlished in 1995. Copyright ©1990, 1995 Samuel Rubenson", None)))
    assert accepted["issued"] == {"date-parts": [[1995]]}


def test_roman_year_seeds_imprint_year():
    assert _roman_year("MDCCCCXLIX") == 1949
    accepted = _seed(_cl((0, "PARISIIS MDCCCCXLIX", None), (0, "MDCCCCXLIX", None)))
    assert accepted["issued"] == {"date-parts": [[1949]]}


def test_roman_numeral_is_valid_evidence_for_arabic_year():
    blocks = [{"id": "b1", "text": "PARISIIS MDCCCCXLIX", "physical_page_index": 0}]
    assert validate_block_evidence("issued", {"date-parts": [[1949]]},
                                   [{"block_id": "b1", "quote": "PARISIIS MDCCCCXLIX"}], blocks)
    # A numeral for a different year does not vouch for 1949.
    assert validate_block_evidence("issued", {"date-parts": [[2001]]},
                                   [{"block_id": "b1", "quote": "PARISIIS MDCCCCXLIX"}], blocks) is None


# ---------------------------------------------------------------------------
# Translator and editor credits
# ---------------------------------------------------------------------------


def test_translator_line_splits_two_names():
    accepted = _seed(_cl((0, "Translated, with an Introduction and Notes by Robert A. Kitchen "
                             "and Martien F. G. Parmentier", None)))
    assert accepted["translator"] == [{"given": "Robert A.", "family": "Kitchen"},
                                      {"given": "Martien F. G.", "family": "Parmentier"}]


def test_translator_bare_by_takes_next_block():
    accepted = _seed(_cl((0, "Translated by", None), (0, "Gerald Malsbary", None)))
    assert accepted["translator"] == [{"given": "Gerald", "family": "Malsbary"}]


def test_translator_cjk_colophon_keeps_deng():
    accepted = _seed(_cl((2, "使徒教父著作/(古罗马)克莱门等著；高陈宝婵等译.--北京", None)))
    assert accepted["translator"] == [{"literal": "高陈宝婵等"}]


def test_editor_bare_verb_with_von_lookback():
    accepted = _seed(_cl(
        (8, "HERAUSGEGEBEN", None),
        (8, "IM AUFTRAGE DER KIRCHENVÄTER-COMMISSION", None),
        (8, "VON", None),
        (8, "DR. OTTO STÄHLIN PROIESSOR AM K. MAXGYMNASIUM IN MÜNCHEN", None)))
    assert accepted["editor"] == [{"given": "OTTO", "family": "STÄHLIN"}]


def test_editor_bare_verb_with_next_block_name():
    accepted = _seed(_cl((0, "EDIDIT", None), (0, "G. GARITTE", None)))
    assert accepted["editor"] == [{"given": "G.", "family": "GARITTE"}]


def test_person_value_splits_glued_nobiliary_particle():
    # OCR glues the particle ('deVogüé'); the printed LoC form is 'de Vogüé'.
    assert _latin_person_value("Adalbert deVogüé") == {"given": "Adalbert de", "family": "Vogüé"}


def test_editor_chief_deng_credit():
    accepted = _seed(_cl((0, "黄锡木主编", None)))
    assert accepted["editor"] == [{"literal": "黄锡木"}]


def test_imprint_seeds_publisher_and_place():
    # bookstep: the clean imprint sits on a sampled tail page (474); the
    # title-page copies are OCR-garbled ('Gstercian Publications').
    accepted = _seed(_cl((474, "CISTERCIAN PUBLICATIONS KALAMAZOO, MICHIGAN", None)))
    assert accepted["publisher"] == "CISTERCIAN PUBLICATIONS"
    assert accepted["publisher-place"] == "KALAMAZOO, MICHIGAN"


def test_imprint_rejects_garbled_and_generic_lines():
    # Garbled publisher copy must not seed (benedict/bookstep title pages).
    assert "publisher" not in _seed(_cl((3, "císteRcíarj paBLfcatíons KALAMAZOO, MICHIGAN", None)))
    assert "publisher" not in _seed(_cl((3, "Gstercian Publications, Inc. 2004", None)))
    # 'Third Edition' is a book-type line, not a publisher.
    assert "publisher" not in _seed(_cl((1, "Third Edition, Revised", None)))
    # A 'by'-attribution line is not an imprint.
    assert "publisher" not in _seed(_cl((3, "Typography by Gale Akins at Humble Hills Press", None)))


def test_author_byline_seeds_canonical_particle_form():
    accepted = _seed(_cl((0, "by Adalbert deVogüé", None)))
    assert accepted["author"] == [{"given": "Adalbert de", "family": "Vogüé"}]


def test_author_title_line_epithet_seeds_literal():
    # aphrahat: the title line spells 'Aphrahat'; the LoC block spells
    # 'Aphraates'. The title-line form must win.
    accepted = _seed(_cl((0, "The Demonstrations of Aphrahat, the Persian Sage", None)))
    assert accepted["author"] == [{"literal": "Aphrahat, the Persian Sage"}]


def test_loc_entry_title_seed_on_library_of_congress_page():
    # astudy: title-page heading is OCR-garbled ('LI+URGY'); the catalog
    # entry line is the only clean printed title.
    accepted = _seed(_cl(
        (3, "Hostetter, Jr., William Taylor A Study of The Divine Liturgy of St John Chrysostom", None),
        (3, "LIBRARY OF CONGRESS CATALOGING-IN-PUBLICATION DATA", None)))
    assert accepted["title"] == "A Study of The Divine Liturgy of St John Chrysostom"


def test_loc_entry_title_not_seeded_without_loc_anchor():
    accepted = _seed(_cl(
        (3, "Hostetter, Jr., William Taylor A Study of The Divine Liturgy of St John Chrysostom", None)))
    assert "title" not in accepted


def test_garbled_pattern_title_is_not_filled(monkeypatch):
    """The C1 pattern layer must not pin OCR garble as a title (astudy)."""
    import json
    from citeindex.ingestion.pipelines import dspy_extract

    def locate_predict(**kwargs):
        return SimpleNamespace(
            document_type="book",
            type_evidence=[{"block_id": "ocr_0", "quote": "A STUDY OF"}],
            selected_pages=[{"page": 1, "role": "title_page",
                             "block_id": "ocr_0", "quote": "A STUDY OF"}])

    def extract_predict(**kwargs):
        # LLM rejects its own title this run (variance) -> pattern fill runs.
        return SimpleNamespace(csl={}, field_evidence={})

    predict_seq = [locate_predict, extract_predict]
    monkeypatch.setattr(dspy_extract, "get_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(dspy_extract.dspy, "Predict", lambda *args: predict_seq.pop(0))
    content = [{"page_idx": 0, "text": "A STUDY OF", "text_level": None},
               {"page_idx": 0, "text": "THE DIVINE LI+URGY OF ST JON CHRSOSTOM", "text_level": None}]
    result = dspy_extract.extract_metadata_with_dspy_priority(
        content_list=content, normalized_markdown="A STUDY OF\nTHE DIVINE LI+URGY OF ST JON CHRSOSTOM",
        doc_type="book", source_blocks=dspy_extract.build_source_blocks([(1, ["A STUDY OF"])]),
        total_pages=1)
    assert result.get("title") is None


# ---------------------------------------------------------------------------
# Scope: the digital path (content_list=None) sees no scanned rules
# ---------------------------------------------------------------------------


def test_digital_path_ignores_scanned_seeds():
    content = _cl((1, "Copyright© 2006 St. Bede's Publications", None))
    accepted, evidence = {}, {}
    _seed_deterministic_candidates(_blocks(content), [], [],
                                   {"document_type": "book", "selected_pages": [1]},
                                   accepted, evidence, content_list=None)
    assert "issued" not in accepted
