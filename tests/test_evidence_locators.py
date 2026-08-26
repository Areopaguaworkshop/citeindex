import json
from pathlib import Path

from citeindex.ingestion.pipelines.common import attach_evidence_locators, build_nodes


def test_scanned_cjk_paragraph_keeps_stable_evidence_locator():
    text = "王明，《中文书名》，第12页。"
    nodes = build_nodes("scan", [(12, [text])])
    structure = {
        "pages": [{
            "page_number": 12,
            "page_idx": 4,
            "paragraphs": [{"paragraph_id": "p12_para1", "text": text, "bbox": [10, 20, 30, 40]}],
        }]
    }

    attach_evidence_locators(structure, nodes)

    paragraph = structure["pages"][0]["paragraphs"][0]
    assert paragraph["node_id"] == nodes[0]["node_id"]
    assert structure["pages"][0]["physical_page_index"] == 4
    assert paragraph["char_start"] == 0
    assert paragraph["char_end"] == len(text)
    assert paragraph["bbox"] == [10, 20, 30, 40]


def test_scanned_cjk_fixture_has_stable_locator():
    fixture = json.loads((Path(__file__).parent / "fixtures/citation_verification/scanned_cjk.json").read_text())
    paragraph = fixture["structure"]["pages"][0]["paragraphs"][0]
    assert paragraph["node_id"] == "scan:p1"
    assert fixture["structure"]["pages"][0]["physical_page_index"] == 0
    assert paragraph["bbox"] == [10, 20, 200, 60]
