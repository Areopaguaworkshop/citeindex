"""Unit tests for HTML metadata extraction and evidence linking in the url_article pipeline."""

from pathlib import Path

from citeindex.ingestion.models import IngestionConfig
from citeindex.ingestion.pipelines import url_article


def test_format_author_dict():
    """Verify author string and dict parsing into CSL format."""
    assert url_article._format_author_dict("Fowler, Austin G.") == {"family": "Fowler", "given": "Austin G."}
    assert url_article._format_author_dict("Austin G. Fowler") == {"family": "Fowler", "given": "Austin G."}
    assert url_article._format_author_dict("王小明") == {"literal": "王小明"}
    assert url_article._format_author_dict({"family": "Smith", "given": "John"}) == {"family": "Smith", "given": "John"}
    assert url_article._format_author_dict({"name": "Matteo Mariantoni"}) == {"family": "Mariantoni", "given": "Matteo"}
    assert url_article._format_author_dict("") is None


def test_extract_highwire_press_metadata():
    """Verify extraction of Highwire Press (Google Scholar) academic metadata."""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="citation_title" content="Quantum Computational Supremacy Using a Programmable Superconducting Processor">
        <meta name="citation_author" content="Arute, Frank">
        <meta name="citation_author" content="Arya, Kunal">
        <meta name="citation_author" content="Martinis, John M.">
        <meta name="citation_publication_date" content="2019/10/23">
        <meta name="citation_journal_title" content="Nature">
        <meta name="citation_volume" content="574">
        <meta name="citation_issue" content="7779">
        <meta name="citation_firstpage" content="505">
        <meta name="citation_lastpage" content="510">
        <meta name="citation_doi" content="10.1038/s41586-019-1666-5">
        <meta name="citation_publisher" content="Nature Publishing Group">
        <meta name="citation_issn" content="0028-0836">
    </head>
    <body><p>Article content</p></body>
    </html>
    """
    meta = url_article._extract_html_head_metadata(html)
    assert meta["title"] == "Quantum Computational Supremacy Using a Programmable Superconducting Processor"
    assert len(meta["author"]) == 3
    assert meta["author"][0] == {"family": "Arute", "given": "Frank"}
    assert meta["author"][2] == {"family": "Martinis", "given": "John M."}
    assert meta["date"] == "2019/10/23"
    assert meta["container-title"] == "Nature"
    assert meta["volume"] == "574"
    assert meta["issue"] == "7779"
    assert meta["page"] == "505-510"
    assert meta["DOI"] == "10.1038/s41586-019-1666-5"
    assert meta["publisher"] == "Nature Publishing Group"
    assert meta["ISSN"] == "0028-0836"
    assert meta["type"] == "article-journal"


def test_extract_highwire_conference_metadata():
    """citation_conference_title marks a conference paper, not a journal article."""
    html = """
    <html><head>
        <meta name="citation_title" content="Attention Is All You Need">
        <meta name="citation_conference_title" content="Advances in Neural Information Processing Systems">
        <meta name="citation_author" content="Vaswani, Ashish">
        <meta name="citation_publication_date" content="2017/12/04">
    </head><body></body></html>
    """
    meta = url_article._extract_html_head_metadata(html)
    assert meta["type"] == "paper-conference"
    assert meta["container-title"] == "Advances in Neural Information Processing Systems"


def test_extract_dublin_core_and_opengraph_metadata():
    """Verify fallback to Dublin Core and OpenGraph tags."""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="dc.title" content="Open Access in the 21st Century">
        <meta name="dc.creator" content="Suber, Peter">
        <meta name="dc.date" content="2012-05-15">
        <meta name="dc.publisher" content="MIT Press">
        <meta name="dc.identifier" content="doi:10.7551/mitpress/9286.001.0001">
        <meta property="og:site_name" content="MIT Press Direct">
    </head>
    <body><p>Body</p></body>
    </html>
    """
    meta = url_article._extract_html_head_metadata(html)
    assert meta["title"] == "Open Access in the 21st Century"
    assert meta["author"] == [{"family": "Suber", "given": "Peter"}]
    assert meta["date"] == "2012-05-15"
    assert meta["publisher"] == "MIT Press"
    assert meta["DOI"] == "10.7551/mitpress/9286.001.0001"
    assert meta["container-title"] == "MIT Press Direct"


def test_extract_json_ld_metadata():
    """Verify extraction of schema.org JSON-LD scripts."""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <script type="application/ld+json">
        {
            "@context": "https://schema.org",
            "@type": "ScholarlyArticle",
            "headline": "Deep Residual Learning for Image Recognition",
            "author": [
                {"@type": "Person", "name": "Kaiming He"},
                {"@type": "Person", "name": "Xiangyu Zhang"}
            ],
            "datePublished": "2015-12-10",
            "publisher": {"@type": "Organization", "name": "IEEE"},
            "isPartOf": {"@type": "PublicationIssue", "name": "CVPR 2016"},
            "doi": "10.1109/CVPR.2016.90"
        }
        </script>
    </head>
    <body><p>Content</p></body>
    </html>
    """
    meta = url_article._extract_html_head_metadata(html)
    assert meta["title"] == "Deep Residual Learning for Image Recognition"
    assert len(meta["author"]) == 2
    assert meta["author"][0] == {"family": "He", "given": "Kaiming"}
    assert meta["date"] == "2015-12-10"
    assert meta["publisher"] == "IEEE"
    assert meta["container-title"] == "CVPR 2016"
    assert meta["DOI"] == "10.1109/CVPR.2016.90"
    assert meta["type"] == "article-journal"


def test_extract_json_ld_prefers_name_over_description_headline():
    """A schema.org `name` is the work title; `headline` may hold a short description."""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta property="og:title" content="Digital object identifier - Wikipedia">
        <script type="application/ld+json">
        {"@context": "https://schema.org", "@type": "Article",
         "name": "Digital object identifier",
         "headline": "ISO standard unique string identifier for a digital object",
         "author": {"@type": "Organization", "name": "Contributors to Wikimedia projects"},
         "datePublished": "2004-01-06T23:54:28Z"}
        </script>
    </head>
    <body><p>Body</p></body>
    </html>
    """
    meta = url_article._extract_html_head_metadata(html)
    assert meta["title"] == "Digital object identifier - Wikipedia"
    assert meta["author"] == [{"family": "projects", "given": "Contributors to Wikimedia"}]


def test_parse_gbt_7714_export_strings():
    """CNKI/Wanfang reference exporters emit GB/T 7714 strings without a 若要引用 preamble."""
    journal = url_article._parse_citation_string(
        "张三, 李四. 人工智能时代的引用索引[J]. 中国图书馆学报, 2020, 46(3): 45-67."
    )
    assert journal["author"] == [{"literal": "张三"}, {"literal": "李四"}]
    assert journal["title"] == "人工智能时代的引用索引"
    assert journal["container-title"] == "中国图书馆学报"
    assert journal["volume"] == "46"
    assert journal["issue"] == "3"
    assert journal["page"] == "45-67"
    assert journal["issued"] == {"date-parts": [[2020]]}
    assert journal["type"] == "article-journal"

    thesis = url_article._parse_citation_string(
        "张三. 数字对象标识符研究[D]. 北京: 北京大学, 2021."
    )
    assert thesis["type"] == "thesis"
    assert thesis["publisher-place"] == "北京"
    assert thesis["publisher"] == "北京大学"

    book = url_article._parse_citation_string(
        "李明. 信息检索原理[M]. 上海: 复旦大学出版社, 2019: 88-90."
    )
    assert book["type"] == "book"
    assert book["page"] == "88-90"

    conference = url_article._parse_citation_string(
        "VASWANI A, SHAZEER N. Attention is all you need[C]//Advances in Neural Information Processing Systems. 2017: 5998-6008."
    )
    assert conference["type"] == "paper-conference"
    assert conference["author"] == [{"family": "Vaswani", "given": "A"},
                                    {"family": "Shazeer", "given": "N"}]
    assert conference["container-title"] == "Advances in Neural Information Processing Systems"
    assert conference["page"] == "5998-6008"


def test_find_gbt_guidance_without_ruoyao_marker():
    citation = "张三, 李四. 人工智能时代的引用索引[J]. 中国图书馆学报, 2020, 46(3): 45-67."
    for text in (f"引用本文：{citation}", f"引用此文: {citation}"):
        assert url_article._find_citation_guidance(text) == citation


def test_unlabeled_gbt_reference_list_self_citation():
    """Only the reference line whose title matches the page is adopted."""
    text = (
        "参考文献\n"
        "李四. 另一篇论文[J]. 图书馆论坛, 2019, 39(2): 1-9.\n"
        "张三, 李四. 人工智能时代的引用索引[J]. 中国图书馆学报, 2020, 46(3): 45-67.\n"
    )
    found = url_article._find_unlabeled_gbt_self_citation(text, "人工智能时代的引用索引")
    assert found is not None
    raw, parsed = found
    assert parsed["title"] == "人工智能时代的引用索引"
    assert parsed["volume"] == "46" and parsed["issue"] == "3" and parsed["page"] == "45-67"
    assert raw.startswith("张三, 李四.")

    # Reference lists about other works must not be used as this page's citation.
    assert url_article._find_unlabeled_gbt_self_citation(text, "完全不同的一篇文章") is None


def test_run_falls_back_to_unlabeled_gbt_self_citation(monkeypatch):
    page_title = "人工智能时代的引用索引"
    citation = "张三, 李四. 人工智能时代的引用索引[J]. 中国图书馆学报, 2020, 46(3): 45-67."
    markdown = f"参考文献\n李四. 另一篇论文[J]. 图书馆论坛, 2019, 39(2): 1-9.\n{citation}"
    html = f"<h1>{page_title}</h1><p>参考文献</p><p>{citation}</p>"
    monkeypatch.setattr(url_article, "_fetch_html", lambda url: html)
    monkeypatch.setattr(url_article, "_extract_markdown", lambda h: markdown)
    monkeypatch.setattr(url_article, "_extract_metadata", lambda *a: {"title": page_title})
    monkeypatch.setattr(url_article, "extract_multimodal_metadata", lambda *a: {})

    res = url_article.run("https://kns.cnki.net/kcms2/article/abstract?v=reference-list", IngestionConfig(use_pageindex=False))
    try:
        csl = res.csl_json
        assert csl["title"] == page_title
        assert csl["container-title"] == "中国图书馆学报"
        assert csl["volume"] == "46" and csl["issue"] == "3" and csl["page"] == "45-67"
        assert csl["_citation_source"] == "in_page_gbt_reference"
        assert csl["_field_status"]["title"] == "source-supported"
        assert csl["_field_status"]["volume"] == "source-supported"
        assert csl["_citation_status"] == "source-supported"
    finally:
        Path(res.extra["source_snapshot_path"]).unlink(missing_ok=True)


def test_gcdfl_ctcfol_guidance_patterns():
    """Copyright-policy preambles used by gcdfl.org / ctcfol.org."""
    lydia = (
        "若要引用本文，请参考版权申明\n\n"
        "版权声明：若要转载或引用此文，请用以下格式："
        "Lydia博士《圣经中的道成肉身与圣诞》，阿甲整理，"
        "（伦敦：光从东方来，2026年11月13日讲座），附上网页+引用日期。"
    )
    citation = url_article._find_citation_guidance(lydia)
    assert citation.startswith("Lydia博士")
    parsed = url_article._parse_citation_string(citation)
    assert parsed["title"] == "圣经中的道成肉身与圣诞"
    assert parsed["author"] == [{"literal": "Lydia博士"}]
    assert parsed["publisher"] == "光从东方来"
    assert parsed["publisher-place"] == "伦敦"
    assert parsed["issued"] == {"date-parts": [[2026, 11, 13]]}

    ctcfol = (
        "版权申明：若要引用，请采用以下格式："
        "S. Brock,《叙利亚「教会传统」研究导论1》，袁永甲中译"
        "（伦敦：教父原文中译计划，2026年4月22日），某年某月某日引用，本文网址。"
    )
    parsed = url_article._parse_citation_string(url_article._find_citation_guidance(ctcfol))
    assert parsed["title"] == "叙利亚「教会传统」研究导论1"
    assert parsed["author"] == [{"literal": "S. Brock"}]
    assert parsed["translator"] == [{"literal": "袁永甲"}]
    assert parsed["issued"] == {"date-parts": [[2026, 4, 22]]}

    # A policy pointer heading is not a citation string.
    assert url_article._find_citation_guidance("若要引用本文，请参考版权申明") is None


def test_zero_padded_chinese_date_is_source_supported(monkeypatch):
    citation = (
        "若要引用本文，ephremyuan，《神父马克西姆：认信者圣马克西姆的生平与教导》"
        "（伦敦：光从东方来，2026年08月19日），本网页网址，引用日期。"
    )
    monkeypatch.setattr(url_article, "_fetch_html", lambda url: f"<p>{citation}</p>")
    monkeypatch.setattr(url_article, "_extract_markdown", lambda h: citation)
    monkeypatch.setattr(url_article, "_extract_metadata", lambda *a: {})
    monkeypatch.setattr(url_article, "extract_multimodal_metadata", lambda *a: {})

    res = url_article.run("https://www.gcdfl.org/posts/example/", IngestionConfig(use_pageindex=False))
    try:
        assert res.csl_json["issued"] == {"date-parts": [[2026, 8, 19]]}
        assert res.csl_json["_field_status"]["issued"] == "source-supported"
        assert res.csl_json["_citation_status"] == "source-supported"
    finally:
        Path(res.extra["source_snapshot_path"]).unlink(missing_ok=True)


def test_cnki_guidance_run_binds_page_and_type(monkeypatch):
    """A CNKI/Wanfang-style page carries no Highwire tags; GB/T guidance is authoritative."""
    citation = "张三, 李四. 人工智能时代的引用索引[J]. 中国图书馆学报, 2020, 46(3): 45-67."
    html = f"<h1>人工智能时代的引用索引</h1><p>引用本文：{citation}</p>"
    monkeypatch.setattr(url_article, "_fetch_html", lambda url: html)
    monkeypatch.setattr(url_article, "_extract_markdown", lambda h: f"引用本文：{citation}")
    monkeypatch.setattr(url_article, "_extract_metadata", lambda *a: {})
    monkeypatch.setattr(url_article, "extract_multimodal_metadata", lambda *a: {})

    res = url_article.run("https://kns.cnki.net/kcms2/article/abstract?v=example", IngestionConfig(use_pageindex=False))
    try:
        csl = res.csl_json
        assert csl["type"] == "article-journal"
        assert csl["title"] == "人工智能时代的引用索引"
        assert csl["author"] == [{"literal": "张三"}, {"literal": "李四"}]
        assert csl["container-title"] == "中国图书馆学报"
        assert csl["volume"] == "46"
        assert csl["issue"] == "3"
        assert csl["page"] == "45-67"
        assert csl["issued"] == {"date-parts": [[2020]]}
        for field in ("title", "author", "volume", "issue", "page", "type", "container-title"):
            assert csl["_field_status"][field] == "source-supported", field
        assert csl["_citation_status"] == "source-supported"
    finally:
        Path(res.extra["source_snapshot_path"]).unlink(missing_ok=True)


def test_url_article_run_binds_evidence_and_snapshot_digest(monkeypatch):
    """Verify that url_article.run() binds evidence to source blocks and attaches snapshot digest."""
    html = """
    <!DOCTYPE html>
    <html>
    <head>
        <meta name="citation_title" content="Quantum Error Correction in Surface Codes">
        <meta name="citation_author" content="Fowler, Austin G.">
        <meta name="citation_publication_date" content="2012/10/01">
        <meta name="citation_journal_title" content="Physical Review A">
        <meta name="citation_doi" content="10.1103/PhysRevA.86.032324">
        <meta name="citation_volume" content="86">
        <meta name="citation_issue" content="3">
        <meta name="citation_firstpage" content="505">
        <meta name="citation_lastpage" content="510">
    </head>
    <body>
        <h1>Quantum Error Correction in Surface Codes</h1>
        <p>By Austin G. Fowler. Physical Review A.</p>
    </body>
    </html>
    """
    monkeypatch.setattr(url_article, "_fetch_html", lambda url: html)
    monkeypatch.setattr(url_article, "_extract_markdown", lambda h: "# Section 1\n\nContent paragraph here.")

    # Mock extract_multimodal_metadata to return empty (simulating offline/no-LLM mode)
    monkeypatch.setattr(url_article, "extract_multimodal_metadata", lambda *a: {})

    res = url_article.run("https://journals.aps.org/pra/abstract/10.1103/PhysRevA.86.032324", IngestionConfig(use_pageindex=False))
    try:
        assert res.status == "ok"
        csl = res.csl_json
        assert csl["title"] == "Quantum Error Correction in Surface Codes"
        assert csl["DOI"] == "10.1103/PhysRevA.86.032324"
        assert csl["container-title"] == "Physical Review A"
        assert csl["issued"] == {"date-parts": [[2012, 10, 1]]}
        assert csl["type"] == "article-journal"

        # Evidence must be anchored to source blocks
        evidence = csl.get("_field_evidence", {})
        assert "title" in evidence
        assert evidence["title"]["quote"] == "Quantum Error Correction in Surface Codes"
        assert "DOI" in evidence
        assert evidence["DOI"]["quote"] == "10.1103/PhysRevA.86.032324"

        # Field status must be source-supported
        assert csl["_field_status"]["title"] == "source-supported"
        assert csl["_field_status"]["DOI"] == "source-supported"
        assert csl["_field_status"]["volume"] == "source-supported"
        assert csl["_field_status"]["issue"] == "source-supported"
        assert csl["page"] == "505-510"
        assert csl["_field_status"]["page"] == "source-supported"
        assert csl["_field_status"]["type"] == "source-supported"
        assert evidence["volume"]["quote"] == "86"
        assert evidence["issue"]["quote"] == "3"
        assert evidence["page"]["quote"] == "505 510"

        from citeindex.ingestion.citation_verification import validate_block_evidence
        assert validate_block_evidence("page", csl["page"], evidence["page"], res.extra["source_blocks"])
        assert validate_block_evidence("type", csl["type"], evidence["type"], res.extra["source_blocks"])
        assert csl["_citation_status"] == "source-supported"

        # Snapshot digest must be attached
        assert "source_snapshot_digest" in res.extra
        assert len(res.extra["source_snapshot_digest"]) == 64
    finally:
        if res.extra.get("source_snapshot_path"):
            Path(res.extra["source_snapshot_path"]).unlink(missing_ok=True)
