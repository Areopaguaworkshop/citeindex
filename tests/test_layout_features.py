import json
from pathlib import Path

import pymupdf

from citeindex.ingestion.pipelines.layout_features import extract_layout_features
from citeindex.ingestion.storage import store_corpus_artifacts


def _digital_pdf(path: Path, lines: list[tuple[str, float, str]], *, extra_pages: int = 0) -> Path:
    """Write a small text-layer PDF: one block per (text, size, font) entry."""
    document = pymupdf.open()
    page = document.new_page()
    y = 100.0
    for text, size, font in lines:
        page.insert_text((72, y), text, fontsize=size, fontname=font)
        y += size * 3
    for _ in range(extra_pages):
        extra = document.new_page()
        extra.insert_text((72, 100), "Body page", fontsize=11, fontname="helv")
    document.save(str(path))
    document.close()
    return path


def _csl() -> dict:
    return {
        "id": "abc123",
        "type": "book",
        "title": "Layout cues",
        "author": [{"family": "Doe", "given": "Jane"}],
        "issued": {"date-parts": [[2020]]},
        "content_hash": "hash-value",
    }


def _artifacts() -> dict:
    return {"csl_json": _csl(), "document_json": {"nodes": []}}


def _features() -> dict:
    return {
        "source": "pymupdf",
        "page_count": 2,
        "pages": [
            {
                "page": 1,
                "width": 595.0,
                "height": 842.0,
                "blocks": [
                    {
                        "block_index": 0,
                        "bbox": [72.0, 74.32, 217.34, 107.37],
                        "size": 24.0,
                        "median_size": 24.0,
                        "bold": True,
                        "line_count": 1,
                        "chars": 14,
                        "x_center": 0.2432,
                        "y_top": 0.0883,
                    }
                ],
            }
        ],
    }


# ---------------------------------------------------------------------------
# Digital text layer
# ---------------------------------------------------------------------------


def test_digital_blocks_report_largest_size_and_bold(tmp_path):
    pdf = _digital_pdf(
        tmp_path / "digital.pdf",
        [("Big bold title", 24, "hebo"), ("small body text", 10, "helv")],
    )

    features = extract_layout_features(str(pdf))

    assert features["source"] == "pymupdf"
    assert features["page_count"] == 1
    assert "error" not in features
    blocks = features["pages"][0]["blocks"]
    assert len(blocks) == 2
    title, body = blocks
    assert title["size"] == 24.0
    assert title["median_size"] == 24.0
    assert title["bold"] is True
    assert title["chars"] == len("Big bold title")
    assert title["line_count"] == 1
    assert body["size"] == 10.0
    assert body["bold"] is False
    assert body["block_index"] == 1
    assert set(title) == {
        "block_index",
        "bbox",
        "size",
        "median_size",
        "bold",
        "line_count",
        "chars",
        "x_center",
        "y_top",
    }


def test_normalised_coordinates_stay_inside_page(tmp_path):
    pdf = _digital_pdf(
        tmp_path / "coords.pdf",
        [("Heading", 20, "hebo"), ("Body text that sits lower", 11, "helv")],
    )

    features = extract_layout_features(str(pdf))

    blocks = features["pages"][0]["blocks"]
    assert blocks
    for block in blocks:
        assert 0.0 <= block["x_center"] <= 1.0
        assert 0.0 <= block["y_top"] <= 1.0
    assert blocks[1]["y_top"] > blocks[0]["y_top"]


def test_default_captures_every_page(tmp_path):
    pdf = _digital_pdf(tmp_path / "long.pdf", [("Body page", 11, "helv")], extra_pages=8)

    features = extract_layout_features(str(pdf))

    assert features["page_count"] == 9
    assert [page["page"] for page in features["pages"]] == list(range(1, 10))


def test_explicit_page_list_still_limits_the_capture(tmp_path):
    pdf = _digital_pdf(tmp_path / "long.pdf", [("Body page", 11, "helv")], extra_pages=8)

    features = extract_layout_features(str(pdf), pages=(1, 2, 3, 4, 5, 6))

    assert [page["page"] for page in features["pages"]] == [1, 2, 3, 4, 5, 6]


def test_two_page_document_returns_two_pages_without_error(tmp_path):
    pdf = _digital_pdf(tmp_path / "short.pdf", [("Body page", 11, "helv")], extra_pages=1)

    features = extract_layout_features(str(pdf))

    assert "error" not in features
    assert features["page_count"] == 2
    assert [page["page"] for page in features["pages"]] == [1, 2]


def test_pages_beyond_document_are_ignored(tmp_path):
    pdf = _digital_pdf(tmp_path / "short.pdf", [("Body page", 11, "helv")])

    features = extract_layout_features(str(pdf), pages=(1, 4))

    assert "error" not in features
    assert [page["page"] for page in features["pages"]] == [1]


# ---------------------------------------------------------------------------
# OCR content_list
# ---------------------------------------------------------------------------


def test_content_list_uses_bbox_height_and_skips_unusable_items(tmp_path):
    pdf = _digital_pdf(tmp_path / "ocr.pdf", [("Body page", 11, "helv")])
    content_list = [
        {"page_idx": 0, "bbox": [10, 20, 110, 60], "text": "Title line"},
        {"page_idx": 0, "text": "item without a bbox"},
        {"page_idx": 0, "bbox": [0, 0, 5], "text": "bbox with three numbers"},
        {"page_idx": 0, "bbox": [0, 0, 10, "x"], "text": "bbox with a non-number"},
        {"page_idx": 1, "bbox": [10, 20, 110, 60], "text": "other page"},
    ]

    features = extract_layout_features(str(pdf), pages=(1,), content_list=content_list)

    assert features["source"] == "mineru"
    blocks = features["pages"][0]["blocks"]
    assert len(blocks) == 1
    assert blocks[0]["size"] == 40.0
    assert blocks[0]["median_size"] == 40.0
    assert blocks[0]["chars"] == len("Title line")
    assert 0.0 <= blocks[0]["x_center"] <= 1.0
    assert 0.0 <= blocks[0]["y_top"] <= 1.0


# ---------------------------------------------------------------------------
# Failure handling
# ---------------------------------------------------------------------------


def test_missing_file_returns_error_instead_of_raising(tmp_path):
    features = extract_layout_features(str(tmp_path / "does-not-exist.pdf"))

    assert features["pages"] == []
    assert features["error"]
    assert features["source"] == "pymupdf"


def test_unreadable_pdf_returns_error_instead_of_raising(tmp_path):
    broken = tmp_path / "broken.pdf"
    broken.write_bytes(b"this is not a pdf")

    features = extract_layout_features(str(broken))

    assert features["pages"] == []
    assert "error" in features


# ---------------------------------------------------------------------------
# Sidecar persistence
# ---------------------------------------------------------------------------


def test_store_writes_layout_json_when_features_given(tmp_path):
    doc_dir = Path(
        store_corpus_artifacts(
            str(tmp_path),
            "doc",
            {**_artifacts(), "layout_features": _features()},
        )
    )

    payload = json.loads((doc_dir / "layout.json").read_text(encoding="utf-8"))
    assert payload == _features()


def test_store_writes_no_layout_json_without_payload(tmp_path):
    doc_dir = Path(store_corpus_artifacts(str(tmp_path), "doc", _artifacts()))

    assert not (doc_dir / "layout.json").exists()


def test_existing_artifacts_and_csl_bytes_are_unchanged_by_layout(tmp_path):
    with_root = tmp_path / "with"
    without_root = tmp_path / "without"
    with_root.mkdir()
    without_root.mkdir()
    with_layout = Path(
        store_corpus_artifacts(str(with_root), "doc", {**_artifacts(), "layout_features": _features()})
    )
    without_layout = Path(store_corpus_artifacts(str(without_root), "doc", _artifacts()))

    assert (with_layout / "csl.json").read_bytes() == (without_layout / "csl.json").read_bytes()
    assert (with_layout / "csl-export.json").read_bytes() == (without_layout / "csl-export.json").read_bytes()
    with_names = {path.name for path in with_layout.iterdir()}
    without_names = {path.name for path in without_layout.iterdir()}
    assert with_names - {"layout.json"} == without_names


def test_reingestion_without_payload_removes_stale_layout_json(tmp_path):
    doc_dir = Path(store_corpus_artifacts(str(tmp_path), "doc", {**_artifacts(), "layout_features": _features()}))
    csl_before = (doc_dir / "csl.json").read_bytes()
    assert (doc_dir / "layout.json").exists()

    store_corpus_artifacts(str(tmp_path), "doc", _artifacts())

    assert not (doc_dir / "layout.json").exists()
    assert (doc_dir / "csl.json").read_bytes() == csl_before
