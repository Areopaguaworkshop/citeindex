"""Phase 3: closed zone taxonomy and explicit seed-vs-model arbitration.

Change 1 gives every window block exactly one label from a closed set, and
orders the prompt by zone instead of free-text page roles. Change 2 makes the
seed-wins rule auditable in ``_field_conflicts`` and keeps ``running_head`` /
``contents`` blocks from supplying a title or publisher while another zone can.
"""

from types import SimpleNamespace

from citeindex.ingestion.pipelines import dspy_extract
from citeindex.ingestion.pipelines.zones import (
    DEGENERATE_ZONES,
    ZONES,
    evidence_zone,
    has_zone_candidate,
    label_zones,
    rank_zones,
)

TITLE = "The Letters of St. Antony"
AUTHOR = "Samuel Rubenson"
RUNNING_HEAD = "THE LETTERS OF ST. ANTONY"
BODY = "This is a long body paragraph about monasticism in Egypt and its manuscripts."

CIP_TEXT = (
    "图书在版编目（CIP）数据\n"
    "视觉SLAM十四讲：从理论到实践 / 高翔等著.—北京：电子工业出版社，2017.3\n"
    "ISBN 978-7-121-31104-8\n"
    "中国版本图书馆CIP数据核字（2017）第053910号"
)


def _blocks(pages):
    """pages: iterable of (page_number, [texts]) on distinct pages."""
    return [{"id": f"p{page}_b{position}", "text": text, "physical_page_index": page - 1,
             "role": "body"}
            for page, texts in pages
            for position, text in enumerate(texts, start=1)]


def _zones(blocks):
    return {block["id"]: block["zone"] for block in label_zones(blocks, page_count=20)}


def _run(monkeypatch, blocks, csl=None, field_evidence=None, doc_type="book"):
    """Run the locate+extract cascade with a stubbed LLM.

    ``csl``/``field_evidence`` may be one dict or a per-attempt list.
    """
    values = csl if isinstance(csl, list) else [csl or {}]
    citations = field_evidence if isinstance(field_evidence, list) else [field_evidence or {}]
    attempts = {"n": 0}
    first = next(block for block in blocks if block["text"].strip())
    quote = first["text"].splitlines()[0]

    def locate_predict(**kwargs):
        return SimpleNamespace(
            document_type="book",
            type_evidence=[{"block_id": first["id"], "quote": quote}],
            selected_pages=[{"page": first["physical_page_index"] + 1, "role": "title",
                             "block_id": first["id"], "quote": quote}])

    def extract_predict(**kwargs):
        position = min(attempts["n"], len(values) - 1)
        attempts["n"] += 1
        return SimpleNamespace(csl=dict(values[position]),
                               field_evidence=dict(citations[min(position, len(citations) - 1)]))

    queue = [locate_predict, extract_predict, extract_predict, extract_predict]
    monkeypatch.setattr(dspy_extract, "get_llm_model", lambda *a, **k: None)
    monkeypatch.setattr(dspy_extract.dspy, "Predict", lambda *args: queue.pop(0))
    return dspy_extract._run_dspy_extraction(
        "", doc_type, source_blocks=blocks, total_pages=max(b["physical_page_index"] + 1 for b in blocks))


# ---------------------------------------------------------------------------
# Change 1: closed taxonomy
# ---------------------------------------------------------------------------


def test_every_block_gets_a_zone_from_the_closed_set():
    blocks = _blocks([
        (1, [RUNNING_HEAD]),
        (2, [TITLE, AUTHOR]),
        (3, ["Contents", "Introduction ........ 1", "Chapter One .......... 12"]),
        (4, [CIP_TEXT, "ISBN 978-7-121-31104-8"]),
        (5, ["Copyright © 2006 St. Bede's Publications"]),
        (6, ["北京：商务印书馆"]),
        (7, ["Oxford University Press"]),
        (8, [BODY]),
    ])
    labelled = label_zones(blocks, page_count=8)
    assert len(labelled) == len(blocks)
    assert {block["zone"] for block in labelled} <= set(ZONES)
    assert all(isinstance(block["zone"], str) for block in labelled)
    # The input dicts are copied, never mutated, and ``role`` survives.
    assert all("zone" not in block for block in blocks)
    assert all(block["role"] == "body" for block in labelled)


def test_cip_page_labelled_from_cjk_cataloguing_marker():
    blocks = _blocks([(2, ["图书在版编目（CIP）数据\n视觉SLAM十四讲 / 高翔等著"])])
    assert _zones(blocks) == {"p2_b1": "cip"}


def test_cip_page_keeps_its_isbn_line_in_the_cip_zone():
    blocks = _blocks([(2, [CIP_TEXT, "ISBN 978-7-121-31104-8"])])
    assert set(_zones(blocks).values()) == {"cip"}


def test_copyright_page_labelled_from_copyright_sign():
    blocks = _blocks([(2, ["Copyright © 2006 St. Bede's Publications"])])
    assert _zones(blocks) == {"p2_b1": "copyright"}


def test_copyright_page_labelled_from_cjk_all_rights_notice():
    blocks = _blocks([(3, ["版权所有 ©2001 某某出版社"])])
    assert _zones(blocks) == {"p3_b1": "copyright"}


def test_imprint_labelled_from_cjk_place_and_publisher():
    blocks = _blocks([(3, ["北京：商务印书馆"])])
    assert _zones(blocks) == {"p3_b1": "imprint"}


def test_imprint_labelled_from_latin_press_name():
    blocks = _blocks([(3, ["Oxford University Press"])])
    assert _zones(blocks) == {"p3_b1": "imprint"}


def test_contents_page_labelled_from_heading_and_dot_leaders():
    blocks = _blocks([
        (3, ["Contents"]),
        (4, ["Introduction ........ 1", "Chapter One .......... 12"]),
    ])
    assert _zones(blocks) == {"p3_b1": "contents", "p4_b1": "contents", "p4_b2": "contents"}


def test_running_head_repeated_on_three_pages():
    blocks = _blocks([(1, [TITLE, AUTHOR])] +
                     [(page, [RUNNING_HEAD, BODY]) for page in (2, 3, 4)])
    zones = _zones(blocks)
    assert zones["p1_b1"] == "title"          # the printed title keeps its label
    for page in (2, 3, 4):
        assert zones[f"p{page}_b1"] == "running_head", page


def test_cover_and_title_page_on_page_one():
    blocks = _blocks([
        (1, [RUNNING_HEAD]),
        (2, [TITLE, AUTHOR]),
        (3, [BODY]),
    ])
    zones = _zones(blocks)
    assert zones["p1_b1"] == "cover"
    assert zones["p2_b1"] == "title"
    assert zones["p2_b2"] == "statement_of_responsibility"


def test_title_page_with_two_short_lines_is_not_a_cover():
    blocks = _blocks([(1, [RUNNING_HEAD, AUTHOR])])
    assert _zones(blocks) == {"p1_b1": "title", "p1_b2": "statement_of_responsibility"}


def test_body_is_the_default_zone():
    blocks = _blocks([(5, [BODY])])
    assert _zones(blocks) == {"p5_b1": "body"}


def test_rank_zones_puts_cip_before_body():
    ranked = rank_zones([
        {"id": "body", "zone": "body"},
        {"id": "cip", "zone": "cip"},
    ])
    assert [block["id"] for block in ranked] == ["cip", "body"]


def test_rank_zones_puts_furniture_last_and_keeps_stable_order():
    ranked = rank_zones([
        {"id": "contents", "zone": "contents"},
        {"id": "running", "zone": "running_head"},
        {"id": "body1", "zone": "body"},
        {"id": "imprint", "zone": "imprint"},
        {"id": "cover", "zone": "cover"},
        {"id": "body2", "zone": "body"},
        {"id": "title", "zone": "title"},
    ])
    assert [block["id"] for block in ranked] == [
        "title", "imprint", "cover", "body1", "body2", "running", "contents"]


# ---------------------------------------------------------------------------
# Change 2: degenerate zones may not supply a title or publisher
# ---------------------------------------------------------------------------


def test_running_head_title_is_refused_when_a_title_page_exists(monkeypatch):
    # The copyright line keeps page 2 in the marker-ranked prompt shortlist, so
    # the repeated title line is actually handed to the model.
    blocks = _blocks([
        (1, [TITLE, AUTHOR]),
        (2, ["Copyright © 2006 St. Bede's Publications", RUNNING_HEAD, BODY]),
        (3, [RUNNING_HEAD, BODY]),
        (4, [RUNNING_HEAD, BODY]),
    ])
    result = _run(monkeypatch, blocks,
                  csl=[{"title": RUNNING_HEAD}, {}],
                  field_evidence=[{"title": {"block_id": "p2_b2", "quote": RUNNING_HEAD}}, {}])
    assert result.get("title") != RUNNING_HEAD
    assert result["_field_drops"]["title"]["reason"] == "wrong_role"
    assert "title" not in result.get("_field_conflicts", {})


def test_running_head_wins_title_when_it_is_the_only_candidate(monkeypatch):
    blocks = _blocks([
        (1, [BODY]),
        (2, ["Copyright © 2006 St. Bede's Publications", RUNNING_HEAD, BODY]),
        (3, [RUNNING_HEAD, BODY]),
        (4, [RUNNING_HEAD, BODY]),
    ])
    assert not has_zone_candidate("title", _zones(blocks))
    result = _run(monkeypatch, blocks,
                  csl={"title": RUNNING_HEAD},
                  field_evidence={"title": {"block_id": "p2_b2", "quote": RUNNING_HEAD}})
    assert result["title"] == RUNNING_HEAD
    assert result["_field_conflicts"]["title"]["fallback_zone"] == "running_head"


def test_seed_from_running_head_loses_to_an_imprint_zone():
    blocks = ([{"id": f"p{page}_b1", "text": "Cambridge University Press",
                "physical_page_index": page - 1} for page in (2, 3)] +
              [{"id": "p5_b1", "text": "Oxford University Press", "physical_page_index": 4}])
    accepted, evidence = {}, {}
    dspy_extract._seed_deterministic_candidates(
        blocks, [], [], {"document_type": "book"}, accepted, evidence,
        zones={"p2_b1": "running_head", "p3_b1": "running_head", "p5_b1": "imprint"})
    assert accepted["publisher"] == "Oxford University Press"
    # Only the imprint block can vouch for the value.
    assert evidence["publisher"]["spans"][0]["block_id"] == "p5_b1"


def test_seed_from_running_head_allowed_when_no_other_zone_can():
    blocks = [{"id": f"p{page}_b1", "text": "Cambridge University Press",
               "physical_page_index": page - 1} for page in (2, 3)]
    accepted, evidence = {}, {}
    dspy_extract._seed_deterministic_candidates(
        blocks, [], [], {"document_type": "book"}, accepted, evidence,
        zones={"p2_b1": "running_head", "p3_b1": "running_head"})
    assert accepted["publisher"] == "Cambridge University Press"


# ---------------------------------------------------------------------------
# Change 2: explicit seed-vs-model arbitration
# ---------------------------------------------------------------------------


def _cip_blocks():
    return dspy_extract.build_source_blocks([
        (1, ["视觉SLAM十四讲：从理论到实践", "高翔等著"]), (2, [CIP_TEXT])])


def test_seed_wins_and_both_values_are_recorded(monkeypatch):
    result = _run(monkeypatch, _cip_blocks(),
                  csl={"title": "视觉SLAM：从理论到实践"}, field_evidence={})
    assert result["title"] == "视觉SLAM十四讲"
    assert result["_field_conflicts"]["title"] == {
        "seed": "视觉SLAM十四讲",
        "model": "视觉SLAM：从理论到实践",
        "chosen": "seed",
    }


def test_agreement_records_no_conflict(monkeypatch):
    result = _run(monkeypatch, _cip_blocks(),
                  csl={"title": "视觉SLAM十四讲"}, field_evidence={})
    assert result["title"] == "视觉SLAM十四讲"
    assert "title" not in result.get("_field_conflicts", {})
    assert "title" not in {c["field"] for c in result.get("_metadata_conflicts", [])}


def test_model_only_title_is_kept(monkeypatch):
    blocks = _blocks([(1, [TITLE, AUTHOR])])
    result = _run(monkeypatch, blocks,
                  csl={"title": TITLE},
                  field_evidence={"title": {"block_id": "p1_b1", "quote": TITLE}})
    assert result["title"] == TITLE
    assert "title" not in result.get("_field_conflicts", {})


# ---------------------------------------------------------------------------
# Helpers used by the arbitration rules
# ---------------------------------------------------------------------------


def test_degenerate_zones_are_a_subset_of_the_closed_set():
    assert set(DEGENERATE_ZONES) <= set(ZONES)
    assert evidence_zone({"block_id": "x"}, {"x": "contents"}) == "contents"
    assert evidence_zone({"spans": [{"block_id": "x"}, {"block_id": "y"}]},
                        {"x": "running_head", "y": "running_head"}) == "running_head"
    assert evidence_zone({"spans": [{"block_id": "x"}, {"block_id": "y"}]},
                        {"x": "running_head", "y": "body"}) is None


def test_non_string_block_text_does_not_crash_labels():
    # Some OCR payloads carry structured text; labelling must not call .strip() on it.
    blocks = [
        {"id": "a", "text": ["line"], "physical_page_index": 0},
        {"id": "b", "text": "CONTENTS", "physical_page_index": 1},
    ]
    labelled = label_zones(blocks)
    assert [b["zone"] for b in labelled]
    assert labelled[0]["text"] == ""
