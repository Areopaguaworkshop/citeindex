import copy
import hashlib

from citeindex.ingestion.title_guard import looks_like_filename, reject_filename_title

SHA256 = hashlib.sha256(b"sciencemetabench-source").hexdigest()
SHA_PATH = f"/tmp/corpus/{SHA256}.pdf"


def _supporting_record() -> dict:
    return {
        "type": "book",
        "title": f"{SHA256}.pdf",
        "author": [{"family": "Doe", "given": "Jane"}],
        "_field_status": {
            "title": "source-supported",
            "author": "source-supported",
            "issued": "source-supported",
        },
        "_citation_status": "source-supported",
    }


def test_sha256_filename_title_is_rejected():
    record = _supporting_record()

    assert reject_filename_title(record, SHA_PATH) is True
    assert "title" not in record
    assert record["_field_status"]["title"] == "missing"
    assert record["_field_drops"]["title"] == {"reason": "filename_fallback"}


def test_same_sha256_filename_with_different_case_is_rejected():
    record = _supporting_record()
    record["title"] = f"{SHA256.upper()}.PDF"

    assert looks_like_filename(record["title"], SHA_PATH) is True
    assert reject_filename_title(record, SHA_PATH) is True
    assert "title" not in record


def test_legitimate_title_ending_in_pdf_is_kept():
    record = _supporting_record()
    record["title"] = "Reading the Portable Document Format.pdf"

    assert looks_like_filename(record["title"], SHA_PATH) is False
    assert reject_filename_title(record, SHA_PATH) is False
    assert record["title"] == "Reading the Portable Document Format.pdf"


def test_human_filename_equal_to_title_is_rejected():
    record = _supporting_record()
    record["title"] = "My Book.pdf"
    original = copy.deepcopy(record)

    assert reject_filename_title(record, "/home/reader/My Book.pdf") is True
    assert "title" not in record
    assert original != record


def test_filename_stem_without_extension_is_rejected():
    record = _supporting_record()
    record["title"] = "My Book"

    assert reject_filename_title(record, "/home/reader/My Book.pdf") is True
    assert "title" not in record


def test_subtitle_is_removed_with_filename_title():
    record = _supporting_record()
    record["subtitle"] = "A subtitle recovered from the same fallback"

    assert reject_filename_title(record, SHA_PATH) is True
    assert "subtitle" not in record


def test_existing_field_drops_entries_are_preserved():
    record = _supporting_record()
    record["_field_drops"] = {
        "publisher": {"reason": "not_in_source", "quote": "no imprint page"},
        "issued": {"reason": "ambiguous"},
    }

    assert reject_filename_title(record, SHA_PATH) is True
    assert record["_field_drops"]["publisher"] == {
        "reason": "not_in_source",
        "quote": "no imprint page",
    }
    assert record["_field_drops"]["issued"] == {"reason": "ambiguous"}
    assert record["_field_drops"]["title"] == {"reason": "filename_fallback"}


def test_citation_status_is_downgraded_to_incomplete():
    record = _supporting_record()
    assert record["_citation_status"] == "source-supported"

    assert reject_filename_title(record, SHA_PATH) is True
    assert record["_citation_status"] == "incomplete"


def test_second_call_is_idempotent():
    record = _supporting_record()
    assert reject_filename_title(record, SHA_PATH) is True
    after_first = copy.deepcopy(record)

    assert reject_filename_title(record, SHA_PATH) is False
    assert record == after_first
    assert record["_field_status"]["title"] == "missing"
    assert record["_field_drops"] == {"title": {"reason": "filename_fallback"}}


def test_record_without_title_is_untouched():
    record = {
        "type": "book",
        "author": [{"family": "Doe", "given": "Jane"}],
        "_field_status": {"title": "missing", "author": "source-supported"},
        "_citation_status": "incomplete",
    }
    original = copy.deepcopy(record)

    assert reject_filename_title(record, SHA_PATH) is False
    assert record == original


def test_missing_or_url_source_path_never_raises():
    record = _supporting_record()
    assert reject_filename_title(record, None) is True  # hash-shaped title still rejected

    url_record = _supporting_record()
    url_record["title"] = f"{SHA256}.pdf"
    assert reject_filename_title(url_record, "https://example.org/files/paper") is True
    assert "title" not in url_record

    assert looks_like_filename(None, None) is False
    assert looks_like_filename("", "https://example.org/") is False
