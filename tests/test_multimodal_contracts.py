import hashlib
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from citeindex.ingestion.csl import export_csl, evaluation_fields, valid_host_value
from citeindex.ingestion.citation_verification import validate_block_evidence
from citeindex.ingestion.models import IngestionConfig
from citeindex.ingestion.pipelines import multimodal_metadata as contracts
from citeindex.ingestion.pipelines import url_article, media
from citeindex.ingestion.master import CiteIndexIngestionOrchestrator
from citeindex.ingestion.storage import write_json


def fake_predictor(monkeypatch, make_result):
    calls = []
    monkeypatch.setattr(contracts, "get_llm_model", lambda *a, **k: None)
    def predictor(signature):
        def predict(**kwargs):
            calls.append(signature)
            return make_result(kwargs["source_blocks"])
        return predict
    monkeypatch.setattr(contracts.dspy, "Predict", predictor)
    return calls


def supported(blocks, field, value, quote):
    block = next(b for b in blocks if quote in b["text"])
    return validate_block_evidence(field, value, {"block_id": block["id"], "quote": quote}, blocks)


def test_dates_roles_and_export():
    assert valid_host_value("host", [{"literal": "张三"}])
    assert valid_host_value("accessed", {"date-parts": [[2024, 2, 29]]})
    assert not valid_host_value("issued", {"date-parts": [[2025, 2, 29]]})
    assert not valid_host_value("interviewer", "Jane")
    record = {"id": "one", "type": "broadcast", "title": "Episode", "subtitle": "A conversation",
              "host": [{"literal": "张三"}], "dimensions": "40:00", "_field_evidence": {"title": {}},
              "content_hash": "abc", "issued": {"date-parts": [[2024, 5, 6]]}}
    exported = export_csl(record)
    assert exported["title"] == "Episode: A conversation"
    assert exported["host"] == record["host"]
    assert exported["custom"]["content_hash"] == "abc"
    assert "subtitle" not in exported and "_field_evidence" not in exported
    assert record["title"] == "Episode"  # no mutation
    assert "ISBN" not in evaluation_fields(record, "media")
    assert {"host", "number", "dimensions"} <= evaluation_fields(record, "media")
    assert "DOI" in evaluation_fields({"type": "article-journal"}, "url_article")


def test_web_signature_retains_subtype_and_full_date(monkeypatch):
    html = '<meta property="article:published_time" content="2025-05-06"><h1>My article</h1><p>By Jane Doe. Journal of Tests.</p>'
    monkeypatch.setattr(url_article, "_fetch_html", lambda url: html)
    monkeypatch.setattr(url_article, "_extract_markdown", lambda html: "My article\n\nBy Jane Doe.")
    monkeypatch.setattr(url_article, "_extract_metadata", lambda *a: {"title": "My article", "date": "2025-05-06", "type": "article-journal"})
    def response(blocks):
        values = {"type": "article-journal", "title": "My article", "author": [{"literal": "Jane Doe"}],
                  "issued": {"date-parts": [[2025, 5, 6]]}, "URL": "https://evil.invalid"}
        quotes = {"type": "Journal of Tests", "title": "My article", "author": "Jane Doe", "issued": "2025-05-06"}
        return SimpleNamespace(csl=values, field_evidence={field: {"block_id": next(b["id"] for b in blocks if quote in b["text"]), "quote": quote} for field, quote in quotes.items()})
    calls = fake_predictor(monkeypatch, response)
    result = url_article.run("https://example.org/article", IngestionConfig(use_pageindex=False))
    try:
        assert result.csl_json["type"] == "article-journal"
        assert result.csl_json["issued"] == {"date-parts": [[2025, 5, 6]]}
        assert result.csl_json["URL"] == "https://example.org/article"
        assert calls == [contracts.ExtractWebMetadata]
        assert result.extra["source_blocks"]
    finally:
        Path(result.extra["source_snapshot_path"]).unlink()
    assert url_article._parse_date_string("2025-05") == {"date-parts": [[2025, 5]]}
    assert url_article._parse_date_string("May 2025") == {"date-parts": [[2025, 5]]}


def test_modification_date_is_not_publication_date():
    blocks = contracts.web_source_blocks('<meta property="article:modified_time" content="2025-05-06">')
    assert supported(blocks, "issued", {"date-parts": [[2025, 5, 6]]}, "2025-05-06") is None


def test_media_roles_and_timestamp_evidence(monkeypatch):
    blocks = contracts.metadata_blocks({"uploader": "Channel", "platform": "YouTube", "description": "Interview with Alice. Interviewer Bob. Podcast Test."})
    blocks += contracts.transcript_blocks([{"start": 12.5, "end": 20.25, "text": "My name is Alice."}])
    assert supported(blocks, "author", [{"literal": "Channel"}], "Channel") is None
    assert supported(blocks, "publisher", "YouTube", "YouTube") is None
    evidence = supported(blocks, "author", [{"literal": "Alice"}], "Alice")
    assert evidence
    transcript = next(b for b in blocks if "segment_id" in b)
    evidence = validate_block_evidence("author", [{"literal": "Alice"}], {"block_id": transcript["id"], "quote": "Alice"}, blocks)
    assert evidence["locator"]["start_seconds"] == 12.5
    assert "physical_page_index" not in evidence["locator"]
    def response(blocks):
        return SimpleNamespace(csl={"type": "interview", "interviewer": [{"literal": "Bob"}]},
            field_evidence={"type": {"block_id": "metadata:description", "quote": "Interview with Alice"},
                            "interviewer": {"block_id": "metadata:description", "quote": "Interviewer Bob"}})
    calls = fake_predictor(monkeypatch, response)
    result = contracts.extract_multimodal_metadata("media", blocks, IngestionConfig())
    assert result["type"] == "interview" and result["interviewer"] == [{"literal": "Bob"}]
    assert calls == [contracts.ExtractMediaMetadata]


def test_failed_transcription_does_not_invent_a_quote(tmp_path, monkeypatch):
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"fixture")
    monkeypatch.setattr(media, "_probe_local_media", lambda p: {"title": "Lecture", "uploader": "Not an author", "duration_ms": 60000})
    monkeypatch.setattr(media, "_extract_audio", lambda p: None)
    monkeypatch.setattr(media, "extract_multimodal_metadata", lambda *a: {})
    result = media.run(str(path), IngestionConfig())
    assert result.csl_json["type"] == "document"
    assert "author" not in result.csl_json and "publisher" not in result.csl_json
    assert result.csl_json["dimensions"] == "01:00"
    assert result.transcript_json["segments"] == []
    assert result.extra["quotation_locators"] == []
    assert result.extra["transcription_status"] == "unavailable"


def test_snapshot_hash_and_invalid_timestamps(tmp_path):
    metadata = {"description": "主持人：张三", "upload_date": "20250506"}
    blocks = contracts.metadata_blocks(metadata)
    path = tmp_path / "media_metadata.json"
    write_json(str(path), metadata)
    assert blocks[0]["source_digest"] == hashlib.sha256(path.read_bytes()).hexdigest()
    assert contracts.transcript_blocks([{"start": 0, "end": 0, "text": "fake"},
                                        {"start": float("nan"), "end": 2, "text": "fake"}]) == []
    assert supported(blocks, "issued", {"date-parts": [[2025, 5, 6]]}, "20250506")


def test_media_repair_and_finalization(tmp_path, monkeypatch):
    path = tmp_path / "audio.mp3"
    path.write_bytes(b"fixture")
    monkeypatch.setattr(media, "_probe_local_media", lambda p: {"title": "Conversation", "description": "Host Alice"})
    monkeypatch.setattr(media, "_extract_audio", lambda p: None)
    monkeypatch.setattr(media, "extract_multimodal_metadata", lambda *a: {"type": "broadcast"})
    result = media.run(str(path))
    quote = supported(result.extra["source_blocks"], "host", [{"literal": "Alice"}], "Host Alice")
    proposal = {"source_sha256": hashlib.sha256(path.read_bytes()).hexdigest(), "proposals": [{
        "status": "proposed", "field": "host", "old_value": None, "proposed_value": [{"literal": "Alice"}],
        "quote": "Host Alice", "locator": quote["locator"], "reason": "Explicit host credit"}]}
    proposal_path = tmp_path / "proposal.json"
    proposal_path.write_text(json.dumps(proposal))
    orchestrator = CiteIndexIngestionOrchestrator(str(tmp_path / "corpus"))
    monkeypatch.setattr(orchestrator, "route_to_pipeline", lambda *a: result)
    output = orchestrator.ingest(str(path), IngestionConfig(repair_proposal=str(proposal_path)))
    assert output["status"] == "ok", output
    directory = Path(output["document_path"])
    exported = json.loads((directory / "csl-export.json").read_text())[0]
    assert exported["host"] == [{"literal": "Alice"}]
    assert exported["id"] == output["standardized_csl_json"]["id"]
    assert (directory / "source_blocks.json").is_file()
    proposal["proposals"][0]["locator"]["source_digest"] = "forged"
    proposal_path.write_text(json.dumps(proposal))
    with pytest.raises(ValueError):
        orchestrator._apply_repair_proposal({"title": "Conversation"}, str(proposal_path), str(path), result)


def test_benchmark_media_profile_and_forged_timestamp(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "benchmarks/citations"))
    from run import score
    blocks = contracts.transcript_blocks([{"start": 12, "end": 20, "text": "Alice gives this lecture."}])
    evidence = supported(blocks, "author", [{"literal": "Alice"}], "Alice")
    csl = {"type": "speech", "title": "Lecture", "author": [{"literal": "Alice"}]}
    gold = {"one": {"modality": "media", "csl": csl, "field_evidence": {"author": evidence}}}
    prediction = {"id": "one", "status": "ok", "csl": {**csl, "_field_evidence": {"author": evidence}}, "source_blocks": blocks}
    report = score(gold, {"one": prediction}, [prediction])
    assert report["fields"]["ISBN"]["evaluated"] == 0
    assert report["evidence"]["matches_reviewed_attribution"] == 1
    prediction["csl"]["_field_evidence"]["author"] = {**evidence, "locator": {**evidence["locator"], "start_seconds": 13}}
    assert score(gold, {"one": prediction}, [prediction])["evidence"]["invalid_span"] == 1


def test_wenbi_adapter_preserves_timestamps_speakers_and_provenance(monkeypatch):
    package, asr = ModuleType("wenbi"), ModuleType("wenbi.asr")
    package.__path__ = []
    def transcribe(path, **kwargs):
        assert kwargs["asr_provider"] == "funasr"
        assert kwargs["enable_speakers"] is True
        return {"provider": "funasr", "segments": [
            {"start": 0.125, "end": 2.75, "text": " first ", "spk": "SPEAKER_00"},
            {"start": 2.5, "end": 4.25, "text": "overlap", "speaker": "SPEAKER_01"},
            {"start": 4.0, "end": 4.0, "text": "invalid"},
            {"start": 9.0, "end": 11.0, "text": "past duration"},
        ]}
    asr.transcribe_with_engine = transcribe
    monkeypatch.setitem(sys.modules, "wenbi", package)
    monkeypatch.setitem(sys.modules, "wenbi.asr", asr)
    monkeypatch.setattr(media, "version", lambda name: "1.2.3")
    segments, provenance = media._transcribe_wenbi(
        "audio.wav", IngestionConfig(media_asr_backend="wenbi", wenbi_asr_provider="funasr", wenbi_speaker_labels=True), 10.0
    )
    assert segments == [
        {"start": 0.125, "end": 2.75, "text": "first", "speaker": "SPEAKER_00"},
        {"start": 2.5, "end": 4.25, "text": "overlap", "speaker": "SPEAKER_01"},
    ]
    assert provenance == {"adapter": "wenbi", "provider": "funasr", "model": None,
                          "version": "1.2.3", "speaker_labels_requested": True}


def test_media_run_with_wenbi_retains_numeric_evidence_times(tmp_path, monkeypatch):
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"fixture")
    audio = tmp_path / "audio.wav"
    audio.write_bytes(b"audio")
    monkeypatch.setattr(media, "_probe_local_media", lambda p: {"title": "Clip", "duration_ms": 5000, "medium": "video"})
    monkeypatch.setattr(media, "_extract_audio", lambda p: str(audio))
    monkeypatch.setattr(media, "_transcribe_wenbi", lambda *a: (
        [{"start": 1.125, "end": 3.875, "text": "Exact quotation", "speaker": "S0"}],
        {"adapter": "wenbi", "provider": "whisper", "model": "tiny", "version": "test"},
    ))
    monkeypatch.setattr(media, "extract_multimodal_metadata", lambda *a: {})
    result = media.run(str(path), IngestionConfig(media_asr_backend="wenbi", wenbi_asr_provider="whisper"))
    assert result.transcript_json["segments"][0]["start"] == 1.125
    assert result.transcript_json["speaker_segments"] == [{"start": 1.125, "end": 3.875, "speaker": "S0"}]
    block = next(b for b in result.extra["source_blocks"] if "segment_id" in b)
    assert (block["start_seconds"], block["end_seconds"]) == (1.125, 3.875)
    assert result.transcript_json["asr"]["adapter"] == "wenbi"


def test_wenbi_isolated_environment_adapter(tmp_path, monkeypatch):
    interpreter = tmp_path / "python"
    interpreter.write_text("fixture")
    interpreter.chmod(0o755)
    payload = {"segments": [{"start": 1.25, "end": 2.5, "text": "quote"}], "provider": "funasr"}
    def run(command, **kwargs):
        assert command[0] == str(interpreter)
        assert command[-3:] == ["funasr", "0", "large-v3-turbo"]
        return SimpleNamespace(returncode=0, stdout="diagnostic\n" + json.dumps(payload), stderr="")
    monkeypatch.setattr(media.subprocess, "run", run)
    segments, provenance = media._transcribe_wenbi(
        "audio.wav", IngestionConfig(media_asr_backend="wenbi", wenbi_python=str(interpreter)), 3.0
    )
    assert segments == [{"start": 1.25, "end": 2.5, "text": "quote"}]
    assert provenance["version"] == "isolated-environment"
