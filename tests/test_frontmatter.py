"""Phase 1 frontmatter module tests (plan v2 §3.1, offline, no LLM)."""

from citeindex.ingestion.pipelines.frontmatter import (
    load_pdf_pages,
    parse_cip,
    parse_loc_cip,
    rank_candidate_pages,
    repair_ocr_numerals,
    scan_pages,
    validate_isbn13,
)


def _fields(result: list[dict], field: str) -> list:
    return [c["value"] for c in result if c["field"] == field]


# ---------------------------------------------------------------------------
# 1. GB/T §8.3 template example
# ---------------------------------------------------------------------------

GB_T83 = """图书在版编目（CIP）数据
视觉SLAM十四讲：从理论到实践 / 高翔等著.—北京：电子工业出版社，2017.3
ISBN 978-7-121-31104-8
Ⅰ.①视… Ⅱ.①高… Ⅲ.①人工智能－视觉跟踪－研究 Ⅳ.①TP18
中国版本图书馆CIP数据核字（2017）第053910号
"""


def test_gbt_83_template():
    r = parse_cip(GB_T83)
    assert r, "parser must find the CIP block"
    assert _fields(r, "title") == ["视觉SLAM十四讲"]
    assert _fields(r, "subtitle") == ["从理论到实践"]
    assert "高翔等" in _fields(r, "authors")
    assert _fields(r, "place") == ["北京"]
    assert _fields(r, "publisher") == ["电子工业出版社"]
    assert _fields(r, "year") == [2017]
    isbns = _fields(r, "ISBN")
    assert isbns and validate_isbn13(isbns[0])
    assert not _fields(r, "series"), "no series in this record"
    assert all(c["confidence"] >= 0.99 for c in r)
    # quote is the verbatim main line
    assert "视觉SLAM十四讲：从理论到实践 / 高翔等著" in r[0]["quote"]


# ---------------------------------------------------------------------------
# 2. Series + fullwidth variants
# ---------------------------------------------------------------------------

SERIES_CIP = """图书在版编目（CIP）数据
设计心理学／（美）诺曼著；张磊译．—北京：中信出版社，2016.6
（设计学文库；05）
ISBN 978-7-5086-5913-1
中国版本图书馆CIP数据核字（2016）第123456号
"""


def test_series_and_fullwidth_variants():
    r = parse_cip(SERIES_CIP)
    assert r
    assert _fields(r, "title") == ["设计心理学"]
    assert _fields(r, "series") == ["设计学文库"]
    assert "诺曼" in _fields(r, "authors")
    assert "张磊" in _fields(r, "translator"), "译 role belongs in translator"
    assert _fields(r, "place") == ["北京"]
    assert _fields(r, "publisher") == ["中信出版社"]
    assert _fields(r, "year") == [2016]


DASH_VARIANT = """图书在版编目(CIP)数据
城市的胜利／张三著.--北京：人民出版社，2019.1
ISBN 978-7-01-012345-6
"""


def test_dash_variants():
    r = parse_cip(DASH_VARIANT)
    assert r
    assert _fields(r, "title") == ["城市的胜利"]
    assert _fields(r, "place") == ["北京"]
    assert _fields(r, "publisher") == ["人民出版社"]
    assert _fields(r, "year") == [2019]


# ---------------------------------------------------------------------------
# 3. 许倬云 record (halfwidth ., no colon after place)
# ---------------------------------------------------------------------------

XUZUOYUN = """图书在版编目(CIP)数据
中国文化的精神/许倬云著.-北京.九州出版社,2018.10
ISBN 978-7-5108-6101-7
Ⅰ.①中...Ⅱ.①许...Ⅲ.①中华文化－研究Ⅳ.①K203
中国版本图书馆CIP数据核字(2018)第228824号
"""


def test_no_colon_place_variant():
    r = parse_cip(XUZUOYUN)
    assert r
    assert _fields(r, "title") == ["中国文化的精神"]
    assert "许倬云" in _fields(r, "authors")
    assert _fields(r, "place") == ["北京"]
    assert _fields(r, "publisher") == ["九州出版社"]
    assert _fields(r, "year") == [2018]
    assert validate_isbn13(_fields(r, "ISBN")[0])


# ---------------------------------------------------------------------------
# 4. LoC RDA block (Juska example)
# ---------------------------------------------------------------------------

LOC_RDA = """Library of Congress Cataloging-in-Publication Data
Names: Juska, Jane. | Austen, Jane, 1775-1817. Pride and prejudice.
Title: Mrs. Bennet has her say / Jane Juska.
Description: Berkley trade paperback edition. | New York : Berkley Books, 2015.
Identifiers: LCCN 2014048297 | ISBN 9780425278437 (softcover)
Subjects: Austen, Jane, 1775-1817. Pride and prejudice.
Classification: LCC PR4034 .J87 2015 | DDC 813/.54—dc23
"""


def test_loc_rda_block():
    r = parse_loc_cip(LOC_RDA)
    assert r, "LoC anchor present, must parse"
    assert _fields(r, "title") == ["Mrs. Bennet has her say"]
    author = _fields(r, "author")[0]
    assert author["family"] == "Juska"
    assert author["given"] == "Jane"
    assert _fields(r, "place") == ["New York"]
    assert _fields(r, "publisher") == ["Berkley Books"]
    assert _fields(r, "year") == [2015]
    assert validate_isbn13(_fields(r, "ISBN")[0])
    assert _fields(r, "LCCN") == ["2014048297"]
    assert all(c["confidence"] == 0.98 for c in r)


def test_loc_modern_spelling_and_data_alone():
    # Modern spelling without "Data", and "Cataloging-in-Publication Data" alone.
    modern = """Names: Juska, Jane.
Title: Mrs. Bennet has her say / Jane Juska.
Description: New York : Berkley Books, 2015."""
    # No anchor at all → []
    assert parse_loc_cip(modern) == []
    anchored = "Cataloging-in-Publication Data\n" + modern
    r = parse_loc_cip(anchored)
    assert r and _fields(r, "title") == ["Mrs. Bennet has her say"]


# ---------------------------------------------------------------------------
# 5. ISBN checksum
# ---------------------------------------------------------------------------


def test_isbn13_checksum():
    assert validate_isbn13("978-7-121-31104-8")
    assert validate_isbn13("9780425278437")
    assert validate_isbn13("9787510861017")
    # Deliberately invalid check digit.
    assert not validate_isbn13("978-7-121-31104-9")
    # 977 prefix gate.
    assert not validate_isbn13("9771234567890")
    # Wrong length / garbage.
    assert not validate_isbn13("978-7-121")
    assert not validate_isbn13("")
    assert not validate_isbn13("978-7-121-31104-8x")


# ---------------------------------------------------------------------------
# 6. scan_pages on a 30-page fake document
# ---------------------------------------------------------------------------


def _fake_pages():
    pages = []
    for i in range(1, 31):
        text = f"Chapter {i} body text. Some ordinary prose here, paragraph {i}.\n"
        if i == 3:
            text += GB_T83
        if i == 28:
            text += "See Smith 2020, ISBN 978-0-19-953566-9 for a survey of the field.\n"
        if i in (5, 11, 17, 23, 29):
            text += "Crack by RAOGY.\n"
        pages.append({"page": i, "text": text})
    return pages


def test_scan_pages_ranking_and_watermark():
    ranked = scan_pages(_fake_pages())
    assert ranked[0]["page"] == 3, "CIP page must rank first"
    assert ranked[0]["score"] >= 20  # header(10) + cert(10) + ISBN(5)
    assert "978-7-121-31104-8" in ranked[0]["isbn_candidates"]

    by_page = {r["page"]: r for r in ranked}
    assert by_page[28]["repeated_block"] is False
    assert by_page[28]["score"] >= 5  # ISBN marker
    for watermark_page in (5, 11, 17, 23, 29):
        assert by_page[watermark_page]["repeated_block"] is True
        assert by_page[watermark_page]["markers"] == []
        assert by_page[watermark_page]["score"] == 0


# ---------------------------------------------------------------------------
# 7. rank_candidate_pages neighbors
# ---------------------------------------------------------------------------


def test_rank_candidate_pages_neighbors():
    pages = _fake_pages()
    ranked = rank_candidate_pages(pages, top_k=8)
    assert 3 in ranked
    assert 2 in ranked and 4 in ranked, "neighbors of best page included"
    assert len(ranked) <= 8
    assert ranked == sorted(ranked)


# ---------------------------------------------------------------------------
# 8. No false positives on plain body text
# ---------------------------------------------------------------------------


def test_parse_cip_plain_body_text():
    body = """This chapter surveys the history of the region.
The evidence presented in section 3 supports the main claim.
Further work is needed to confirm these findings."""
    assert parse_cip(body) == []


def test_parse_cip_header_without_body():
    # Header present but no data line → no fabricated fields.
    assert parse_cip("图书在版编目（CIP）数据\n版权所有\n") == []


def test_cip_spanning_two_pages():
    page_a = "图书在版编目（CIP）数据\n"
    page_b = "视觉SLAM十四讲：从理论到实践 / 高翔等著.—北京：电子工业出版社，2017.3\nISBN 978-7-121-31104-8\n"
    r = parse_cip(page_a + "\n" + page_b)
    assert _fields(r, "title") == ["视觉SLAM十四讲"]
    assert _fields(r, "publisher") == ["电子工业出版社"]


# ---------------------------------------------------------------------------
# 9. OCR-garbled anchored CIP (augustin 论三位一体, 2005)
# ---------------------------------------------------------------------------

AUGUSTIN_CIP = """ffl书在版编目（C I P ) 数据
论
二位一体/ ( 古罗
马
）奥
古斯丁（Augustine, A . ) 著I
周伟驰译•一上海：上海人民出版社，2005
(世:纪人文系列丛书）
中国
版本图
书
馆CIP数
据核字（2 0 0 4 ) 第0739】】号
"""


def test_ocr_garbled_cip_recovers_imprint_fields():
    r = parse_cip(AUGUSTIN_CIP)
    assert _fields(r, "publisher") == ["上海人民出版社"]
    assert _fields(r, "place") == ["上海"]
    assert _fields(r, "year") == [2005]
    assert _fields(r, "translator") == ["周伟驰"], "译 role recovered under OCR garble"
    assert "奥古斯丁" in _fields(r, "authors")
    assert all(c["confidence"] >= 0.99 for c in r)
    # A fuzzy block's garbled title/series must never seed.
    assert not _fields(r, "title")
    assert not _fields(r, "series")


def test_repair_ocr_numerals():
    assert repair_ocr_numerals("Age of Transition (7th—g9th Century)") == "Age of Transition (7th—9th Century)"
    assert repair_ocr_numerals("7th-9th Century") == "7th-9th Century"
    # Real words and identifiers with digits must not be mangled.
    assert repair_ocr_numerals("Windows x86 Edition") == "Windows x86 Edition"
    assert repair_ocr_numerals("COVID-19 in 3rd wave") == "COVID-19 in 3rd wave"
    assert repair_ocr_numerals("") == ""