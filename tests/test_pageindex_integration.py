import json

from citeindex.ingestion.markdown_export import generate_library_markdown
from citeindex.ingestion.pipelines.digital_pdf import _annotate_document_with_pageindex
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
    assert "\n\n======page:1" in markdown
    assert "\n\n======page:2" in markdown
    assert "Origins\nAramaic Origins\nPage one body text." not in markdown
    assert "THE ARAMAIC KINGDOMS\nPage two body text." not in markdown
    assert "Page one body text." in markdown
    assert "Page two body text." in markdown


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
