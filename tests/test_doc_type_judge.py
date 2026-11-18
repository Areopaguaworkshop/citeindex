"""Stage 2: the deterministic document-type judge.

The judge runs before extraction and records a verdict, a confidence and the
verbatim evidence behind it. These tests pin the signal ladder: override,
thesis, book, paper, page count, default — and that a tie between book and
paper keeps book only while a book signal is printed.
"""

import pytest

from citeindex.ingestion.pipelines.doc_type_judge import judge_document_type

THESIS_EN = (
    "A thesis submitted in partial fulfillment of the requirements "
    "for the degree of Doctor of Philosophy"
)
THESIS_ZH = "博士学位论文\n本文以明清江南市镇为研究对象。"
CIP_TEXT = (
    "图书在版编目（CIP）数据\n"
    "视觉SLAM十四讲 / 高翔等著.—北京：电子工业出版社，2017.3\n"
    "ISBN 978-7-121-31104-8"
)
CJK_PRESS = "北京：电子工业出版社"
ISBN_PRESS = "ISBN 978-0-19-953209-7\nOxford University Press"
PAPER_DOI = "Abstract\nThis article studies caching in distributed stores.\nDOI: 10.1234/abcd.2020.001"
PLAIN_BODY = "The remaining leaves were copied by a later hand in the same script."


def _blocks(*texts):
    return [{"id": f"p{index}_b1", "text": text, "physical_page_index": index - 1}
            for index, text in enumerate(texts, start=1)]


def _signals(result):
    return {entry["signal"] for entry in result["evidence"]}


# --- 1. override -----------------------------------------------------------

def test_override_wins_over_every_marker():
    result = judge_document_type(_blocks(CIP_TEXT), override="paper")
    assert result["type"] == "paper"
    assert result["confidence"] == 1.0
    assert result["evidence"] == [{"block_id": "", "quote": "paper", "signal": "override"}]


def test_unknown_override_is_ignored_and_markers_decide():
    result = judge_document_type(_blocks(PAPER_DOI), override="journal")
    assert result["type"] == "paper"
    assert result["confidence"] == 0.9
    assert "override" not in _signals(result)


# --- 2. thesis -------------------------------------------------------------

def test_thesis_from_english_markers():
    result = judge_document_type(_blocks(THESIS_EN))
    assert result["type"] == "thesis"
    assert result["confidence"] == 0.9
    assert {"thesis", "thesis_submitted", "partial_fulfillment", "degree_of"} <= _signals(result)


def test_thesis_from_chinese_markers():
    result = judge_document_type(_blocks(THESIS_ZH))
    assert result["type"] == "thesis"
    assert result["confidence"] == 0.9
    assert {"cn_degree_thesis", "cn_doctor"} <= _signals(result)


def test_thesis_outranks_book_and_paper():
    result = judge_document_type(_blocks(CIP_TEXT, THESIS_EN))
    assert result["type"] == "thesis"
    assert result["confidence"] == 0.9


# --- 3. book ---------------------------------------------------------------

def test_book_from_cip():
    result = judge_document_type(_blocks(CIP_TEXT))
    assert result["type"] == "book"
    assert result["confidence"] == 0.9
    assert {"cip_cataloguing", "cip", "isbn"} <= _signals(result)


def test_book_from_chinese_publisher():
    result = judge_document_type(_blocks(CJK_PRESS))
    assert result["type"] == "book"
    assert result["confidence"] == 0.7
    assert _signals(result) == {"cn_publisher"}


def test_book_from_isbn_and_publisher_suffix():
    result = judge_document_type(_blocks(ISBN_PRESS))
    assert result["type"] == "book"
    assert result["confidence"] == 0.9
    assert {"isbn", "publisher_suffix"} <= _signals(result)


def test_book_outranks_paper_markers():
    result = judge_document_type(_blocks(ISBN_PRESS, "Abstract\nDOI: 10.1234/abcd.2020.001"))
    assert result["type"] == "book"
    assert result["confidence"] == 0.9


# --- 4. paper --------------------------------------------------------------

def test_paper_from_doi_and_abstract():
    result = judge_document_type(_blocks(PAPER_DOI))
    assert result["type"] == "paper"
    assert result["confidence"] == 0.9
    assert {"doi", "abstract"} <= _signals(result)


# --- 5. page count ---------------------------------------------------------

def test_page_count_long_document_is_book():
    result = judge_document_type(_blocks(PLAIN_BODY), page_count=300)
    assert result["type"] == "book"
    assert result["confidence"] == 0.6
    assert result["evidence"] == [{"block_id": "", "quote": "", "signal": "page_count"}]


def test_long_document_with_a_bare_doi_gets_no_length_verdict():
    # '>= 60 without a DOI' is the page-count rule; a bare DOI defeats it and
    # the run falls through to the documented default.
    result = judge_document_type(_blocks("10.1234/abcd-ef"), page_count=300)
    assert result["type"] == "book"
    assert result["confidence"] == 0.3
    assert result["evidence"][0]["signal"] == "default"


def test_page_count_short_document_with_doi_is_paper():
    result = judge_document_type(_blocks("10.1234/abcd-ef"), page_count=8)
    assert result["type"] == "paper"
    assert result["confidence"] == 0.6
    assert result["evidence"][0]["signal"] == "page_count"


def test_page_count_short_document_without_doi_is_not_paper():
    result = judge_document_type(_blocks(PLAIN_BODY), page_count=8)
    assert result["type"] == "book"
    assert result["confidence"] == 0.3


# --- tie, default, verbatim quotes, ladder ---------------------------------

def test_tie_resolves_to_book_when_a_book_signal_is_present():
    result = judge_document_type(_blocks("Published by ACM", "Keywords: caching"))
    assert result["type"] == "book"
    assert result["confidence"] == 0.7
    assert _signals(result) == {"published_by"}


def test_paper_wins_when_no_book_signal_is_present():
    result = judge_document_type(_blocks("Received: 3 March 2020", "Keywords: caching"))
    assert result["type"] == "paper"
    assert result["confidence"] == 0.9


def test_default_when_nothing_matches():
    result = judge_document_type(_blocks(PLAIN_BODY))
    assert result["type"] == "book"
    assert result["confidence"] == 0.3
    assert result["evidence"] == [{"block_id": "", "quote": "", "signal": "default"}]


@pytest.mark.parametrize("blocks", [[], None, [{"id": "p1_b1", "text": "   "}, "  "]])
def test_empty_or_textless_blocks_return_default(blocks):
    result = judge_document_type(blocks)
    assert result["type"] == "book"
    assert result["confidence"] == 0.3
    assert result["evidence"][0]["signal"] == "default"


@pytest.mark.parametrize("text", [THESIS_EN, CIP_TEXT, CJK_PRESS, ISBN_PRESS, PAPER_DOI])
def test_evidence_quotes_are_verbatim_block_substrings(text):
    blocks = _blocks(text)
    result = judge_document_type(blocks)
    by_id = {block["id"]: block["text"] for block in blocks}
    assert result["evidence"]
    for entry in result["evidence"]:
        assert entry["quote"]
        assert entry["quote"] in by_id[entry["block_id"]]


def test_confidence_ladder_matches_documented_values():
    assert judge_document_type(_blocks(CIP_TEXT), override="thesis")["confidence"] == 1.0
    assert judge_document_type(_blocks(PAPER_DOI))["confidence"] == 0.9
    assert judge_document_type(_blocks(CJK_PRESS))["confidence"] == 0.7
    assert judge_document_type(_blocks(PLAIN_BODY), page_count=300)["confidence"] == 0.6
    assert judge_document_type(_blocks(PLAIN_BODY))["confidence"] == 0.3


def test_blocks_are_not_mutated():
    blocks = _blocks(CIP_TEXT)
    snapshot = [dict(block) for block in blocks]
    judge_document_type(blocks)
    assert blocks == snapshot
