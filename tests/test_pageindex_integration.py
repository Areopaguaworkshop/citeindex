import json

from citeindex.ingestion.markdown_export import generate_library_markdown
from citeindex.ingestion.pipelines.digital_pdf import _annotate_document_with_pageindex
from citeindex.ingestion.pipelines.digital_pdf import _attach_layout_footnotes
from citeindex.ingestion.pipelines.pdf_text_cleanup import clean_page_texts
from citeindex.ingestion.storage import store_corpus_artifacts


def test_digital_pdf_pageindex_headings_flow_into_document_and_library_markdown():
    document_structure = {
        "pages": [
            {
                "page_number": 1,
                "paragraphs": [
                    {
                        "paragraph_id": "p1_1",
                        "text": "Origins\nAramaic Origins\nPage one body text.",
                        "type": "text",
                    },
                ],
                "footnotes": [],
            },
            {
                "page_number": 2,
                "paragraphs": [
                    {
                        "paragraph_id": "p2_1",
                        "text": "THE ARAMAIC KINGDOMS\nPage two body text.",
                        "type": "text",
                    },
                ],
                "footnotes": [],
            },
        ],
        "section_tree": [],
    }
    ci_tree = {
        "level_1": [
            {
                "heading": "Origins",
                "page_range": "1-2",
                "children": [
                    {
                        "heading": "Aramaic Origins",
                        "page_range": "1",
                        "children": [],
                    },
                    {
                        "heading": "THE ARAMAIC KINGDOMS",
                        "page_range": "2",
                        "children": [],
                    },
                ],
            }
        ]
    }

    _annotate_document_with_pageindex(document_structure, ci_tree)

    assert document_structure["section_tree"][0]["heading"] == "Origins"
    assert document_structure["pages"][0]["paragraphs"][0]["text"] == "Origins"
    assert document_structure["pages"][0]["paragraphs"][1]["text"] == "Aramaic Origins"
    assert document_structure["pages"][1]["paragraphs"][0]["text"] == "THE ARAMAIC KINGDOMS"
    assert document_structure["pages"][0]["section_title"] == "Aramaic Origins"
    assert document_structure["pages"][1]["section_title"] == "THE ARAMAIC KINGDOMS"

    markdown = generate_library_markdown(
        csl_json={
            "title": "Origins: A Culture of Encounter and Contact",
            "author": [{"literal": "Chatonnet"}],
            "type": "article-journal",
        },
        document_json={"structure": document_structure},
        transcript_json=None,
        resource_type="digital_pdf",
    )

    assert "## Origins" in markdown
    assert "### Aramaic Origins" in markdown
    assert "### THE ARAMAIC KINGDOMS" in markdown
    assert markdown.count("======page:") >= 2
    assert markdown.index("Page one body text.") < markdown.index("======page:1")
    assert markdown.index("Page two body text.") < markdown.index("======page:2")
    assert "Origins\nAramaic Origins\nPage one body text." not in markdown
    assert "THE ARAMAIC KINGDOMS\nPage two body text." not in markdown
    assert "Page one body text." in markdown
    assert "Page two body text." in markdown


def test_digital_pdf_layout_footnotes_flow_into_library_markdown():
    document_structure = {
        "pages": [
            {
                "page_number": 1,
                "paragraphs": [
                    {
                        "paragraph_id": "p1_1",
                        "text": "Body text.",
                        "type": "text",
                    }
                ],
                "footnotes": [],
            }
        ],
        "section_tree": [],
    }
    page_layouts = [
        {
            "page_number": 1,
            "footnotes": [
                {
                    "footnote_id": "p1_fn1",
                    "text": "Footnote text.",
                }
            ],
        }
    ]

    _attach_layout_footnotes(document_structure, page_layouts)

    markdown = generate_library_markdown(
        csl_json={
            "title": "Origins: A Culture of Encounter and Contact",
            "author": [{"literal": "Chatonnet"}],
            "type": "article-journal",
        },
        document_json={"structure": document_structure},
        transcript_json=None,
        resource_type="digital_pdf",
    )

    assert "[^1]: Footnote text." in markdown


def test_store_corpus_artifacts_writes_pageindex_tree_json(tmp_path):
    doc_dir = store_corpus_artifacts(
        str(tmp_path),
        "sample_doc",
        artifacts={
            "pageindex_tree": {"level_1": [{"heading": "Origins", "children": []}]},
        },
    )

    tree_path = tmp_path / "sample_doc" / "pageindex_tree.json"
    assert doc_dir == str(tmp_path / "sample_doc")
    assert tree_path.exists()
    assert json.loads(tree_path.read_text())["level_1"][0]["heading"] == "Origins"


def test_clean_page_texts_strips_repeated_running_headers_and_page_numbers():
    page_texts = [
        "1\nOrigins\nA Culture of Encounter and Contact\nBody page one.",
        "2\nOrigins\nBody page two.",
        "Origins\n3\nBody page three.",
        "4\nOrigins\nBody page four.",
    ]

    cleaned = clean_page_texts(page_texts)

    assert cleaned[0].startswith("Origins\nA Culture of Encounter and Contact")
    assert cleaned[1] == "Body page two."
    assert cleaned[2] == "Body page three."
    assert cleaned[3] == "Body page four."


def test_collect_footnotes_for_range():
    from citeindex.ingestion.pipelines.pageindex_tree import _collect_footnotes_for_range

    page_layouts = [
        {"page_number": 1, "footnotes": [{"footnote_id": "p1_fn1", "text": "Note 1.", "marker": "1"}]},
        {"page_number": 2, "footnotes": [{"footnote_id": "p2_fn1", "text": "Note 2.", "marker": "2"}]},
        {"page_number": 3, "footnotes": []},
    ]

    # Range spanning pages 1-2
    result = _collect_footnotes_for_range(1, 2, page_layouts)
    assert len(result) == 2
    assert result[0]["footnote_id"] == "p1_fn1"
    assert result[1]["footnote_id"] == "p2_fn1"

    # Range on a single page with no footnotes
    result = _collect_footnotes_for_range(3, 3, page_layouts)
    assert result == []

    # None range (no page bounds)
    result = _collect_footnotes_for_range(None, None, page_layouts)
    assert result == []

    # None page_layouts
    result = _collect_footnotes_for_range(1, 2, None)
    assert result == []


def test_collect_footnotes_no_duplicates_on_overlapping_ranges():
    """Footnotes on boundary pages should not be duplicated across overlapping ranges."""
    from citeindex.ingestion.pipelines.pageindex_tree import _collect_footnotes_for_range

    page_layouts = [
        {"page_number": 9, "footnotes": [{"footnote_id": "p9_fn1", "text": "Note on page 9."}]},
        {"page_number": 26, "footnotes": [{"footnote_id": "p26_fn1", "text": "Note on page 26."}]},
    ]

    # Simulate two overlapping locators: 5-9 and 9-11
    assigned = set()
    fns_first = _collect_footnotes_for_range(5, 9, page_layouts, assigned)
    fns_second = _collect_footnotes_for_range(9, 11, page_layouts, assigned)

    # p9_fn1 should only appear once (assigned to the first range)
    all_ids = [fn["footnote_id"] for fn in fns_first + fns_second]
    assert all_ids.count("p9_fn1") == 1
    assert "p9_fn1" in assigned

    # Another overlapping pair: 23-26 and 26-27
    assigned2 = set()
    fns_a = _collect_footnotes_for_range(23, 26, page_layouts, assigned2)
    fns_b = _collect_footnotes_for_range(26, 27, page_layouts, assigned2)
    all_ids2 = [fn["footnote_id"] for fn in fns_a + fns_b]
    assert all_ids2.count("p26_fn1") == 1


def test_pageindex_tree_footnotes_match_page_ranges():
    """Integration test: footnotes are attached to the correct LocatorNodes by page range."""
    from citeindex.ingestion.pipelines.pageindex_tree import pageindex_to_citeindex_tree

    pi_result = {
        "structure": [
            {
                "title": "Introduction",
                "node_id": "0001",
                "start_index": 1,
                "end_index": 2,
                "summary": "Intro",
                "nodes": [
                    {
                        "title": "1.1 Background",
                        "node_id": "0002",
                        "start_index": 1,
                        "end_index": 1,
                        "summary": "Background",
                        "nodes": [],
                    },
                    {
                        "title": "1.2 Scope",
                        "node_id": "0003",
                        "start_index": 2,
                        "end_index": 2,
                        "summary": "Scope",
                        "nodes": [],
                    },
                ],
            },
            {
                "title": "Methods",
                "node_id": "0004",
                "start_index": 3,
                "end_index": 5,
                "summary": "Methods chapter",
                "nodes": [
                    {
                        "title": "2.1 Approach",
                        "node_id": "0005",
                        "start_index": 3,
                        "end_index": 5,
                        "summary": "Approach",
                        "nodes": [],
                    },
                ],
            },
        ],
    }
    csl_data = {"id": "doc1", "type": "book", "title": "Test Paper"}
    page_number_map = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5}
    page_layouts = [
        {
            "page_number": 1,
            "footnotes": [
                {"footnote_id": "p1_fn1", "text": "Ref 1.", "marker": "1"},
                {"footnote_id": "p1_fn2", "text": "Ref 2.", "marker": "2"},
            ],
        },
        {
            "page_number": 2,
            "footnotes": [
                {"footnote_id": "p2_fn1", "text": "Ref 3.", "marker": "3"},
            ],
        },
        {"page_number": 3, "footnotes": []},
        {
            "page_number": 4,
            "footnotes": [
                {"footnote_id": "p4_fn1", "text": "Ref 4.", "marker": "4"},
            ],
        },
        {"page_number": 5, "footnotes": []},
    ]

    tree = pageindex_to_citeindex_tree(
        pi_result=pi_result,
        doc_id="doc1",
        csl_data=csl_data,
        page_number_map=page_number_map,
        page_layouts=page_layouts,
    )

    # Locator for "1.1 Background" (page 1) should have 2 footnotes
    bg_locator = tree["level_1"][0]["children"][0]["children"][0]
    assert len(bg_locator["footnotes"]) == 2
    assert bg_locator["footnotes"][0]["footnote_id"] == "p1_fn1"

    # Locator for "1.2 Scope" (page 2) should have 1 footnote
    scope_locator = tree["level_1"][0]["children"][1]["children"][0]
    assert len(scope_locator["footnotes"]) == 1
    assert scope_locator["footnotes"][0]["footnote_id"] == "p2_fn1"

    # Locator for "Methods" (pages 3-5) should have 1 footnote (p4_fn1)
    methods_locator = tree["level_1"][1]["children"][0]["children"][0]
    assert len(methods_locator["footnotes"]) == 1
    assert methods_locator["footnotes"][0]["footnote_id"] == "p4_fn1"


def test_pageindex_to_citeindex_tree_without_layout_returns_empty_footnotes():
    from citeindex.ingestion.pipelines.pageindex_tree import pageindex_to_citeindex_tree

    pi_result = {
        "structure": [
            {
                "title": "Chapter 1",
                "node_id": "0001",
                "start_index": 1,
                "end_index": 1,
                "summary": "Summary",
                "nodes": [
                    {
                        "title": "Sec 1.1",
                        "node_id": "0002",
                        "start_index": 1,
                        "end_index": 1,
                        "summary": "Sub",
                        "nodes": [],
                    }
                ],
            }
        ],
    }
    csl_data = {"id": "doc1", "type": "book", "title": "Test Doc"}
    page_number_map = {0: 1}

    # No page_layouts → footnotes should be empty list
    tree = pageindex_to_citeindex_tree(
        pi_result=pi_result,
        doc_id="doc1",
        csl_data=csl_data,
        page_number_map=page_number_map,
    )

    locator = tree["level_1"][0]["children"][0]["children"][0]
    assert locator["footnotes"] == []


def test_pageindex_to_citeindex_tree_with_footnotes():
    from citeindex.ingestion.pipelines.pageindex_tree import pageindex_to_citeindex_tree

    pi_result = {
        "structure": [
            {
                "title": "Chapter 1",
                "node_id": "0001",
                "start_index": 1,
                "end_index": 3,
                "summary": "A chapter",
                "nodes": [
                    {
                        "title": "Section 1.1",
                        "node_id": "0002",
                        "start_index": 1,
                        "end_index": 2,
                        "summary": "A section",
                        "nodes": [],
                    }
                ],
            }
        ],
    }
    csl_data = {"id": "doc1", "type": "book", "title": "Test Doc"}
    page_number_map = {0: 1, 1: 2, 2: 3}
    page_layouts = [
        {"page_number": 1, "footnotes": [{"footnote_id": "p1_fn1", "text": "See Smith.", "marker": "1"}]},
        {"page_number": 2, "footnotes": [{"footnote_id": "p2_fn1", "text": "Also Jones.", "marker": "2"}]},
        {"page_number": 3, "footnotes": []},
    ]

    tree = pageindex_to_citeindex_tree(
        pi_result=pi_result,
        doc_id="doc1",
        csl_data=csl_data,
        page_number_map=page_number_map,
        page_layouts=page_layouts,
    )

    # Should have footnotes on LocatorNodes within page range
    locator = tree["level_1"][0]["children"][0]["children"][0]
    assert "footnotes" in locator
    assert len(locator["footnotes"]) == 2
    assert locator["footnotes"][0]["footnote_id"] == "p1_fn1"
    assert locator["footnotes"][1]["footnote_id"] == "p2_fn1"


def test_detect_footnotes_relaxed_thresholds():
    """Footnotes in bottom 25% with slightly smaller font (10pt in 11pt body) should be detected.
    Page numbers (short numeric text < 5 words) should NOT be detected."""
    from citeindex.ingestion.pipelines.layout import detect_footnotes

    # Simulate a page where footnotes are at 80% height with 10pt font (body is 11pt)
    page_height = 648
    blocks = [
        {"text": "Body text paragraph one.", "bbox": [72, 71, 360, 200], "font_size": 11.0},
        {"text": "Body text paragraph two.", "bbox": [72, 200, 360, 400], "font_size": 11.0},
        {"text": "Body text paragraph three.", "bbox": [72, 400, 360, 490], "font_size": 11.0},
        # Footnote at ~80% page height, 10pt font, starts with numeric marker — 5+ words
        {"text": "9 Specifically aimed at Byzantinists.", "bbox": [72, 515, 360, 540], "font_size": 10.0},
        # Page number at very bottom — only 1 word, should NOT be detected even if small font
        {"text": "25", "bbox": [200, 581, 230, 595], "font_size": 9.0},
    ]

    body, footnotes = detect_footnotes(blocks, page_height)

    assert len(footnotes) == 1
    assert footnotes[0]["text"] == "9 Specifically aimed at Byzantinists."
    # Page number "25" is too short → stays in body
    assert any(b["text"] == "25" for b in body)


def test_detect_footnotes_with_separator_line():
    """When a footnote separator line (footnoterule) is detected,
    blocks below it with small font or numeric markers are footnotes."""
    from citeindex.ingestion.pipelines.layout import detect_footnotes

    page_height = 648
    # Separator at 80% of page height (y=518)
    separator_y = 518.0
    blocks = [
        {"text": "Body text paragraph one.", "bbox": [72, 71, 360, 200], "font_size": 11.0},
        {"text": "Body text paragraph two.", "bbox": [72, 200, 360, 400], "font_size": 11.0},
        # Footnote BELOW separator, 10pt font
        {"text": "9 Specifically aimed at Byzantinists.", "bbox": [72, 523, 360, 540], "font_size": 10.0},
        # Even higher up but still below separator — 10pt with marker
        {"text": "14 In English, there are several articles.", "bbox": [72, 523, 360, 540], "font_size": 10.0},
        # Below separator but body font size and no marker — NOT a footnote
        {"text": "Some regular text in the margin.", "bbox": [72, 535, 360, 550], "font_size": 11.0},
    ]

    body, footnotes = detect_footnotes(blocks, page_height, separator_y=separator_y)

    # The 10pt footnote blocks should be detected; 11pt non-marker should not
    fn_texts = [fn["text"] for fn in footnotes]
    assert "9 Specifically aimed at Byzantinists." in fn_texts
    assert "14 In English, there are several articles." in fn_texts
    assert len(footnotes) == 2  # only the two 10pt blocks
    assert any(b["text"] == "Some regular text in the margin." for b in body)


def test_detect_footnotes_font_size_clustering():
    """Font size clustering correctly identifies small fonts as footnote-sized
    regardless of absolute sizes (8pt footnotes in 10pt body, or 10pt in 11pt body)."""
    from citeindex.ingestion.pipelines.layout import detect_footnotes

    page_height = 648

    # Academic PDF: 8pt footnotes, 10pt body (IEEE-style)
    blocks_ieee = [
        {"text": "Body text.", "bbox": [72, 71, 360, 300], "font_size": 10.0},
        {"text": "Body text 2.", "bbox": [72, 300, 360, 500], "font_size": 10.0},
        # 8pt footnote at bottom with numeric marker
        {"text": "1 See Smith et al. for details.", "bbox": [72, 545, 360, 570], "font_size": 8.0},
    ]
    _, fns_ieee = detect_footnotes(blocks_ieee, page_height)
    assert len(fns_ieee) == 1
    assert "1 See Smith" in fns_ieee[0]["text"]

    # Book PDF: 10pt footnotes, 11pt body (Springer-style)
    blocks_springer = [
        {"text": "Body text.", "bbox": [72, 71, 360, 300], "font_size": 11.0},
        {"text": "Body text 2.", "bbox": [72, 300, 360, 500], "font_size": 11.0},
        # 10pt footnote at bottom with numeric marker
        {"text": "14 In English, there are several articles.", "bbox": [72, 515, 360, 540], "font_size": 10.0},
    ]
    _, fns_springer = detect_footnotes(blocks_springer, page_height)
    assert len(fns_springer) == 1
    assert "14 In English" in fns_springer[0]["text"]


def test_detect_footnotes_rejects_page_numbers_and_headers():
    """Short text (< 5 words) at page bottom should NOT be classified as footnotes.
    This prevents false positives from page numbers and running headers/footers."""
    from citeindex.ingestion.pipelines.layout import detect_footnotes

    page_height = 648
    blocks = [
        {"text": "Body text paragraph.", "bbox": [72, 71, 360, 200], "font_size": 11.0},
        {"text": "More body text.", "bbox": [72, 200, 360, 400], "font_size": 11.0},
        # Page number at bottom — only 1 "word", small font
        {"text": "25", "bbox": [200, 581, 230, 595], "font_size": 9.0},
        # Running header at bottom — only 3 words, small font
        {"text": "A N INTRODUCTION TO SYRIAC STUDIES", "bbox": [72, 35, 360, 49], "font_size": 9.0},
    ]

    body, footnotes = detect_footnotes(blocks, page_height)

    assert len(footnotes) == 0
    # Page numbers and headers should be in body, not footnotes
    assert any(b["text"] == "25" for b in body)
    assert any("INTRODUCTION" in b["text"] for b in body)


def test_detect_footnotes_old_threshold_would_miss():
    """Verify that footnotes at 79% height with 10pt/11pt body are detected
    (these were missed by the old 85% / 85% thresholds)."""
    from citeindex.ingestion.pipelines.layout import detect_footnotes

    page_height = 648
    # Block at y0=515 = 79.5% — below old 85% threshold, above new 75% threshold
    # Font 10.0 vs median 11.0 — 10/11=90.9%, above old 85% threshold, below new 92%
    blocks = [
        {"text": "Regular body text.", "bbox": [72, 71, 360, 200], "font_size": 11.0},
        {"text": "More body text.", "bbox": [72, 200, 360, 400], "font_size": 11.0},
        {"text": "14 In English, there are several articles on this topic.", "bbox": [72, 515, 360, 540], "font_size": 10.0},
    ]

    body, footnotes = detect_footnotes(blocks, page_height)

    assert len(footnotes) == 1
    assert "14 In English" in footnotes[0]["text"]


def test_generate_library_markdown_skips_pdf_fallback_page_heading_labels():
    markdown = generate_library_markdown(
        csl_json={
            "title": "Origins: A Culture of Encounter and Contact",
            "author": [{"literal": "Chatonnet"}],
            "type": "article-journal",
        },
        document_json={
            "structure": {
                "pages": [
                    {
                        "page_number": 3,
                        "section_title": "THE ARAMAIC KINGDOMS",
                        "paragraphs": [
                            {
                                "paragraph_id": "p3_1",
                                "text": "Continuation text.",
                                "type": "text",
                            }
                        ],
                        "footnotes": [],
                    }
                ],
                "section_tree": [],
            }
        },
        transcript_json=None,
        resource_type="digital_pdf",
    )

    assert "## Page 3: THE ARAMAIC KINGDOMS" not in markdown
    assert "Continuation text." in markdown


def test_attach_layout_footnotes_removes_footnote_text_from_paragraphs():
    """When footnotes are detected by layout analysis, their text should be
    removed from body paragraphs to avoid duplication in markdown output."""
    document_structure = {
        "pages": [
            {
                "page_number": 1,
                "paragraphs": [
                    {"paragraph_id": "p1_1", "text": "Body text paragraph.", "type": "text"},
                    {"paragraph_id": "p1_2", "text": "9 Specifically aimed at Byzantinists, a summary guide.", "type": "text"},
                ],
                "footnotes": [],
            },
        ],
    }
    page_layouts = [
        {
            "page_number": 1,
            "footnotes": [
                {
                    "footnote_id": "p1_fn1",
                    "text": "9 Specifically aimed at Byzantinists, a summary guide.",
                },
            ],
        },
    ]

    _attach_layout_footnotes(document_structure, page_layouts)

    p1 = document_structure["pages"][0]
    # Footnote text should be removed from paragraphs
    para_texts = [p["text"] for p in p1["paragraphs"]]
    assert "9 Specifically aimed at Byzantinists, a summary guide." not in para_texts
    assert "Body text paragraph." in para_texts
    assert len(p1["paragraphs"]) == 1
    # Footnote should be attached
    assert p1["footnotes"][0]["footnote_id"] == "p1_fn1"


def test_attach_layout_footnotes_whitespace_tolerant_match():
    """Footnote text may differ in whitespace from paragraph text;
    the removal should handle this via normalized comparison."""
    document_structure = {
        "pages": [
            {
                "page_number": 1,
                "paragraphs": [
                    {"paragraph_id": "p1_1", "text": "Body text.", "type": "text"},
                    {"paragraph_id": "p1_2", "text": "9 Specifically aimed at Byzantinists", "type": "text"},
                ],
                "footnotes": [],
            },
        ],
    }
    # Note: footnote text has trailing space
    page_layouts = [
        {
            "page_number": 1,
            "footnotes": [
                {
                    "footnote_id": "p1_fn1",
                    "text": "9 Specifically aimed at Byzantinists ",
                },
            ],
        },
    ]

    _attach_layout_footnotes(document_structure, page_layouts)

    p1 = document_structure["pages"][0]
    para_texts = [p["text"] for p in p1["paragraphs"]]
    assert "Body text." in para_texts
    assert len(p1["paragraphs"]) == 1


# ---------------------------------------------------------------------------
# Page number detection tests
# ---------------------------------------------------------------------------


def test_detect_page_numbers_finds_candidates_in_header():
    """Page numbers in top-of-page headers should be detected."""
    from citeindex.ingestion.pipelines.layout import detect_page_numbers

    page_height = 648
    blocks = [
        # Header block with page number (top of page, y0 < 12%)
        {"text": "TOOLS 49", "bbox": [72, 35, 404, 49], "font_size": 9.0},
        # Body text (mid-page, NOT a header)
        {"text": "The Mongol period is well covered in the short book.", "bbox": [72, 200, 360, 300], "font_size": 11.0},
        # Body text 2
        {"text": "Another paragraph.", "bbox": [72, 350, 360, 400], "font_size": 11.0},
    ]

    hf_blocks, candidates = detect_page_numbers(blocks, page_height)

    assert len(hf_blocks) == 1  # "TOOLS 49" is a header
    assert 49 in candidates


def test_detect_page_numbers_finds_standalone_footer_number():
    """A standalone page number at the bottom of a page should be detected."""
    from citeindex.ingestion.pipelines.layout import detect_page_numbers

    page_height = 648
    blocks = [
        # Body text (mid-page, well outside header/footer zones)
        {"text": "Body text paragraph.", "bbox": [72, 200, 360, 400], "font_size": 11.0},
        # Footer number at bottom (y0 > 88% = 570)
        {"text": "25", "bbox": [200, 581, 230, 595], "font_size": 9.0},
    ]

    hf_blocks, candidates = detect_page_numbers(blocks, page_height)

    assert len(hf_blocks) == 1
    assert 25 in candidates


def test_detect_page_numbers_ignores_body_text():
    """Mid-page blocks (not in header/footer zone) should not be detected."""
    from citeindex.ingestion.pipelines.layout import detect_page_numbers

    page_height = 648
    blocks = [
        {"text": "Body text paragraph.", "bbox": [72, 300, 360, 400], "font_size": 11.0},
        {"text": "Another body paragraph.", "bbox": [72, 400, 360, 500], "font_size": 11.0},
    ]

    hf_blocks, candidates = detect_page_numbers(blocks, page_height)

    assert len(hf_blocks) == 0
    assert len(candidates) == 0


def test_detect_page_numbers_ignores_long_blocks():
    """Long blocks in header/footer zone are not headers/footers."""
    from citeindex.ingestion.pipelines.layout import detect_page_numbers

    page_height = 648
    blocks = [
        # Long text at top of page — not a simple header
        {"text": "This is a very long paragraph that spans multiple sentences and is clearly body text, not a header.",
         "bbox": [72, 35, 400, 70], "font_size": 11.0},
    ]

    hf_blocks, candidates = detect_page_numbers(blocks, page_height)

    assert len(hf_blocks) == 0


def test_build_page_number_map_from_layouts():
    """build_page_number_map should convert per-page candidates to a continuous offset map."""
    from citeindex.ingestion.pipelines.layout import build_page_number_map

    # Simulate 3 pages: physical 1→25, 2→26, 3→27
    page_layouts = [
        {"page_number": 1, "page_number_candidates": [25], "footnotes": [], "headers_footers": []},
        {"page_number": 2, "page_number_candidates": [26], "footnotes": [], "headers_footers": []},
        {"page_number": 3, "page_number_candidates": [27], "footnotes": [], "headers_footers": []},
    ]

    pnm = build_page_number_map(page_layouts)

    assert pnm[0] == 25
    assert pnm[1] == 26
    assert pnm[2] == 27


def test_build_page_number_map_returns_empty_when_no_candidates():
    """With no candidates, build_page_number_map returns empty dict."""
    from citeindex.ingestion.pipelines.layout import build_page_number_map

    page_layouts = [
        {"page_number": 1, "page_number_candidates": [], "footnotes": [], "headers_footers": []},
        {"page_number": 2, "page_number_candidates": [], "footnotes": [], "headers_footers": []},
    ]

    pnm = build_page_number_map(page_layouts)
    assert pnm == {}


def test_remove_header_footer_paragraphs():
    """Running header text should be removed from body paragraphs."""
    from citeindex.ingestion.pipelines.digital_pdf import _remove_header_footer_paragraphs

    document_structure = {
        "pages": [
            {
                "page_number": 1,
                "paragraphs": [
                    {"paragraph_id": "p1_1", "text": "TOOLS 49", "type": "text"},
                    {"paragraph_id": "p1_2", "text": "Body text paragraph.", "type": "text"},
                ],
                "footnotes": [],
            },
        ],
    }
    page_layouts = [
        {
            "page_number": 1,
            "headers_footers": [
                {"text": "TOOLS 49", "bbox": [72, 35, 404, 49]},
            ],
            "footnotes": [],
        },
    ]

    _remove_header_footer_paragraphs(document_structure, page_layouts)

    p1 = document_structure["pages"][0]
    para_texts = [p["text"] for p in p1["paragraphs"]]
    assert "Body text paragraph." in para_texts
    assert "TOOLS 49" not in para_texts
    assert len(p1["paragraphs"]) == 1


def test_apply_page_number_map_updates_page_numbers():
    """_apply_page_number_map should rewrite page_number fields."""
    from citeindex.ingestion.pipelines.digital_pdf import _apply_page_number_map

    document_structure = {
        "pages": [
            {"page_number": 1, "paragraphs": [], "footnotes": []},
            {"page_number": 2, "paragraphs": [], "footnotes": []},
            {"page_number": 3, "paragraphs": [], "footnotes": []},
        ]
    }
    page_number_map = {0: 25, 1: 26, 2: 27}

    _apply_page_number_map(document_structure, page_number_map)

    assert document_structure["pages"][0]["page_number"] == 25
    assert document_structure["pages"][1]["page_number"] == 26
    assert document_structure["pages"][2]["page_number"] == 27
