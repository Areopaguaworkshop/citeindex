import json
from pathlib import Path

import pytest

from citeindex.ingestion.citation_verification import build_quotation_locators, replay_citation_evidence
from citeindex.ingestion.master import CiteIndexIngestionOrchestrator
from citeindex.ingestion.models import PipelineResult
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


@pytest.mark.parametrize(("kind", "coordinates", "display"), [
    ("digital_pdf", {"physical_page_index": 7, "printed_page_label": "viii",
                     "page_label_source": "pdf-page-label"}, "viii"),
    ("scanned_pdf", {"physical_page_index": 12, "printed_page_label": "3",
                     "page_label_source": "ocr-sequence"}, "3"),
    ("url_article", {"section_index": 4}, "4"),
    ("media", {"segment_id": "seg1", "start_seconds": 1, "end_seconds": 2}, "00:01–00:02"),
])
def test_ingestion_persists_quotation_locator_for_each_source(tmp_path, monkeypatch, kind, coordinates, display):
    source = tmp_path / "source"
    source.write_bytes(b"fixture")
    text = "A quotation."
    block = {"id": "b1", "text": text, **coordinates}
    orchestrator = CiteIndexIngestionOrchestrator(str(tmp_path / "corpus"))
    monkeypatch.setattr(orchestrator, "detect_resource_type", lambda *_a, **_k: (kind, str(source)))
    monkeypatch.setattr(orchestrator, "route_to_pipeline", lambda *_: PipelineResult(
        "ok", "source", kind, {"type": "article", "title": "Example"},
        extra={"source_blocks": [block]},
    ))
    output = orchestrator.ingest(str(source))
    assert output["status"] == "ok", output
    sidecar = Path(output["document_path"]) / "quotation_locators.json"
    entry, = json.loads(sidecar.read_text())
    assert entry["source_id"] == output["standardized_csl_json"]["id"]
    assert entry["source_sha256"]
    assert entry["citation_item"]["locator"] == display
    report = replay_citation_evidence({}, None, kind, [block], fields=[], quotation=text, locator=entry["locator"])
    assert report["quotation"]["status"] == "extraction-match"


def test_same_pdf_block_and_node_yield_one_locator():
    text = "Quotation on the same page."
    document = {"structure": {"pages": [{
        "physical_page_index": 7, "paragraphs": [{"node_id": "node", "text": text}],
    }]}}
    blocks = [{"id": "b1", "physical_page_index": 7, "text": text}]
    entry, = build_quotation_locators("record", "digest", document, "digital_pdf", blocks)
    assert entry["locator"]["block_id"] == "b1"
    assert entry["locator"]["node_id"] == "node"
