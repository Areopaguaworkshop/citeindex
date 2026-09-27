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
from citeindex.ingestion.pipelines import url_article, media, mineru
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


def test_web_selection_keeps_late_byline(monkeypatch):
    blocks = contracts.web_source_blocks("<h1>Antioch</h1>" + "<p>Navigation text.</p>" * 600 + "<p>(ca. 420) by Hidemi Takahashi</p>")
    def response(selected):
        assert sum(len(block["text"]) for block in selected) <= 12000
        assert any("by Hidemi Takahashi" in block["text"] for block in selected)
        return SimpleNamespace(csl={}, field_evidence={})
    fake_predictor(monkeypatch, response)
    result = contracts.extract_multimodal_metadata("web", blocks, IngestionConfig())
    assert result["author"] == [{"literal": "Hidemi Takahashi"}]


def test_web_site_tagline_uses_short_name(monkeypatch):
    blocks = contracts.web_source_blocks('<meta name="description" content="Example Journal — Articles">'
                                         '<p>Example Journal</p>')
    def response(selected):
        long = next(b for b in selected if "Example Journal — Articles" in b["text"])
        short = next(b for b in selected if b["text"] == "Example Journal")
        return SimpleNamespace(csl={"publisher": "Example Journal — Articles", "container-title": "Example Journal"},
                               field_evidence={"publisher": {"block_id": long["id"], "quote": "Example Journal — Articles"},
                                               "container-title": {"block_id": short["id"], "quote": "Example Journal"}})
    fake_predictor(monkeypatch, response)
    result = contracts.extract_multimodal_metadata("web", blocks, IngestionConfig())
    assert result["publisher"] == "Example Journal"


def test_meta_refresh_fetches_article_without_losing_redirect_snapshot(monkeypatch):
    old = 'https://8.8.8.8/old'
    target = 'https://8.8.8.8/new'
    pages = {old: '<meta http-equiv="refresh" content="0;url=/new"><title>Redirecting...</title>',
             target: '<h1>Article title</h1><p>Real article text.</p>'}
    monkeypatch.setattr(url_article, "_fetch_html", pages.__getitem__)
    final_url, html, chain = url_article._fetch_article(old)
    assert final_url == target and "Real article" in html
    assert len(chain) == 1 and chain[0]["url"] == old and chain[0]["target_url"] == target
    assert chain[0]["html"] == pages[old]


def test_exact_spans_can_support_names_across_blocks():
    blocks = [{"id": "p1_b1", "text": "Alice Doe", "physical_page_index": 0},
              {"id": "p1_b2", "text": "Bob Roe", "physical_page_index": 0}]
    names = [{"literal": "Alice Doe"}, {"literal": "Bob Roe"}]
    spans = [{"block_id": "p1_b1", "quote": "Alice Doe"}, {"block_id": "p1_b2", "quote": "Bob Roe"}]
    assert len(validate_block_evidence("author", names, {"spans": spans}, blocks)["spans"]) == 2
    assert validate_block_evidence("author", names, {"spans": spans[:1]}, blocks) is None
    assert validate_block_evidence("author", names, {"spans": [*spans[:1], {"block_id": "p1_b2", "quote": "Invented"}]}, blocks) is None


def test_missing_asr_keeps_media_metadata_without_transcript(tmp_path, monkeypatch):
    source = tmp_path / "unknown.mp4"
    source.write_bytes(b"placeholder")
    monkeypatch.setattr(media, "find_spec", lambda package: None)
    monkeypatch.setattr(media, "_probe_local_media", lambda path: {"filename": source.name, "source_path": str(source)})
    monkeypatch.setattr(media, "_extract_audio", lambda path: pytest.fail("audio should not be extracted"))
    monkeypatch.setattr(media, "extract_multimodal_metadata", lambda *args: {})
    result = media.run(str(source), IngestionConfig())
    assert result.status == "ok" and result.extra["transcription_status"] == "unavailable"
    assert result.transcript_json["segments"] == []
    assert result.csl_json["_citation_status"] == "incomplete"
    assert result.csl_json["_field_status"]["title"] == "provisional-filename"


def test_mineru_image_export_respects_page_sample(tmp_path, monkeypatch):
    visited = []
    class Page:
        def get_images(self, full=False):
            return []
    class Doc:
        page_count = 100
        def __getitem__(self, index):
            visited.append(index)
            return Page()
        def close(self):
            pass
    monkeypatch.setattr(mineru.fitz, "open", lambda path: Doc())
    assert mineru.extract_pdf_images("sample.pdf", str(tmp_path), "sample", page_indices=[0, 1, 90, 99]) == []
    assert visited == [0, 1, 90, 99]


def test_benchmark_ocr_reads_only_citation_pages_and_keeps_physical_indices(tmp_path, monkeypatch):
    class Doc:
        page_count = 100
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setattr(mineru.fitz, "open", lambda path: Doc())
    calls = []
    def run_mineru(path, **kwargs):
        calls.append((kwargs["start_page"], kwargs["end_page"]))
        return {"content_list": [{"page_idx": 0, "text": "page"}],
                "middle_json": [], "markdown": "page"}
    monkeypatch.setattr(mineru, "run_mineru", run_mineru)
    result = mineru.run_mineru_chunked(
        "source.pdf", str(tmp_path), chunk_pages="auto", benchmark_page_range="1-5, -3")
    assert calls == [(0, 4), (97, 99)]
    assert [item["page_idx"] for item in result["content_list"]] == [0, 97]


@pytest.mark.parametrize("guidance,title", [
    ("若要引用本文，袁永甲，《题名》（伦敦：光从东方来，2026年08月19日），本网页网址，引用日期。", "题名"),
    ("Cite this article: Jane Doe, 'Example Article,' Example Journal, May 6, 2025.", "Example Article"),
])
def test_web_guidance_and_jsonld_survive_text_budget(monkeypatch, guidance, title):
    html = ('<script type="application/ld+json">'
            '{"@type":"https://schema.org/Article","author":{"name":"Jane Doe"},'
            '"datePublished":"2025-05-06","dateModified":"2025-06-01"}'
            '</script><p>' + 'body ' * 3000 + '</p><p>' + guidance + '</p>')
    blocks = contracts.web_source_blocks(html)
    assert any(b.get("metadata_key") == "jsonld:author" for b in blocks)
    assert any(b.get("metadata_key") == "jsonld:datePublished" for b in blocks)
    assert not any("2025-06-01" in b["text"] for b in blocks)

    def response(selected):
        assert sum(len(b["text"]) for b in selected) <= 12000
        block = next(b for b in selected if guidance in b["text"])
        return SimpleNamespace(csl={"title": title}, field_evidence={
            "title": {"block_id": block["id"], "quote": title}})

    calls = fake_predictor(monkeypatch, response)
    result = contracts.extract_multimodal_metadata("web", blocks, IngestionConfig())
    assert result["title"] == title
    assert calls == [contracts.ExtractWebMetadata]


def test_chinese_page_citation_overrides_conflicting_model(monkeypatch):
    guidance = "若要引用本文，ephremyuan，《神父马克西姆：认信者圣马克西姆的生平与教导》（伦敦：光从东方来，2026年08月19日），本网页网址，引用日期。"
    monkeypatch.setattr(url_article, "_fetch_html", lambda url: f"<p>{guidance}</p>")
    monkeypatch.setattr(url_article, "_extract_markdown", lambda html: guidance)
    monkeypatch.setattr(url_article, "_extract_metadata", lambda *a: {"title": "Wrong metadata title"})
    monkeypatch.setattr(url_article, "extract_multimodal_metadata", lambda *a: {
        "title": "Wrong model title", "author": [{"literal": "Wrong author"}],
        "issued": {"date-parts": [[2025]]}, "_field_evidence": {"title": {"quote": "Wrong model title"}},
    })
    result = url_article.run("https://example.org/post", IngestionConfig(use_pageindex=False))
    try:
        csl = result.csl_json
        assert csl["title"] == "神父马克西姆：认信者圣马克西姆的生平与教导"
        assert csl["author"] == [{"literal": "ephremyuan"}]
        assert csl["issued"] == {"date-parts": [[2026, 8, 19]]}
        assert csl["publisher"] == "光从东方来" and csl["publisher-place"] == "伦敦"
        assert csl["_field_evidence"]["title"]["quote"] == guidance
        assert csl["_field_status"]["title"] == "source-supported"
    finally:
        Path(result.extra["source_snapshot_path"]).unlink()


def test_abbreviated_guidance_author_keeps_supported_full_byline(monkeypatch):
    guidance = "若要转载请参考如下格式：托伦斯 Torrance 《缩放文本：天梯约翰著作中书籍地位的模糊性》，Albert Sun 中译 （伦敦：教父原文中译计划，2024年1月22日），引用日期，此文链接。"
    html = f"<p>Alexis Torrance</p><p>{guidance}</p>"
    monkeypatch.setattr(url_article, "_fetch_html", lambda url: html)
    monkeypatch.setattr(url_article, "_extract_markdown", lambda html: guidance)
    monkeypatch.setattr(url_article, "_extract_metadata", lambda *a: {"title": "Wrong title"})
    def extracted(kind, blocks, config):
        block = next(b for b in blocks if b["text"] == "Alexis Torrance")
        return {"author": [{"family": "Torrance", "given": "Alexis"}],
                "_field_evidence": {"author": {"block_id": block["id"], "quote": "Alexis Torrance"}}}
    monkeypatch.setattr(url_article, "extract_multimodal_metadata", extracted)
    result = url_article.run("https://example.org/post", IngestionConfig(use_pageindex=False))
    try:
        assert result.csl_json["author"] == [{"family": "Torrance", "given": "Alexis"}]
        assert result.csl_json["title"] == "缩放文本：天梯约翰著作中书籍地位的模糊性"
    finally:
        Path(result.extra["source_snapshot_path"]).unlink()


def test_observed_gcdfl_and_ctcfol_guidance_forms():
    ctcfol = "若要转载请参考如下格式：托伦斯 Torrance 《缩放文本：天梯约翰著作中书籍地位的模糊性》，Albert Sun 中译 （伦敦：教父原文中译计划，2024年1月22日），引用日期，此文链接。"
    parsed_ctcfol = url_article._parse_citation_string(url_article._find_citation_guidance(ctcfol))
    assert parsed_ctcfol["title"] == "缩放文本：天梯约翰著作中书籍地位的模糊性"
    assert parsed_ctcfol["translator"] == [{"literal": "Albert Sun"}]
    # Source wording: gcdfl.org/posts/ajia/2025-10-22-syriac1-origin/
    series = "若要引用本文，袁永甲，《叙利亚教会的起源》，教会历史第二季之叙利亚传统第一课（伦敦：光从东方来，2025年10月22日），本网页网址，引用日期。"
    assert url_article._parse_citation_string(url_article._find_citation_guidance(series))["collection-title"] == "教会历史第二季之叙利亚传统第一课"

    # Source wording: gcdfl.org/posts/lectures/2024-05-03-liquan1-liuxiaofeng/
    lecture = "若要引用本文，请参考以下格式：李泉《受难英雄的盼望——再思刘小枫的超越基督论讲座》，2024年5月3日（伦敦：光从东方来），本页网址，引用讲座的具体时段，引用日期。"
    assert url_article._find_citation_guidance(lecture).startswith("李泉《受难英雄")

    # Source wording: gcdfl.org/categories/基督信仰/ (article excerpt)
    copyright_guidance = "版权声明：若要转载或引用此文，请用以下格式：Lydia博士《圣经中的立约与基督徒的生命》，（伦敦：光从东方来，2026年03月13日网上讲座），附上网页+引用日期。"
    assert url_article._find_citation_guidance(copyright_guidance).startswith("Lydia博士")

    # Source wording: ctcfol.org/posts/academic/1-brock-2017-introduction-syriac-studies-11-34_draft
    translation = "版权申明：若要引用，请采用以下格式：S. Brock,《叙利亚「教会传统」研究导论1》，袁永甲中译（伦敦：教父原文中译计划，2026年4月22日），某年某月某日引用，本文网址。"
    parsed = url_article._parse_citation_string(url_article._find_citation_guidance(translation))
    assert parsed["author"] == [{"literal": "S. Brock"}]
    assert parsed["translator"] == [{"literal": "袁永甲"}]
    assert parsed["issued"] == {"date-parts": [[2026, 4, 22]]}
    assert "collection-title" not in parsed

    # Source wording: ctcfol.org/posts/philokalia/philimon
    next_line = "版权申明：若要引用此文，请按以下格式\n\n袁永甲译，《阿爸腓利门传记》in《爱神集导读版》（伦敦：教父原文中译计划，2024年12月08日），本页网址，引用日期。"
    citation = url_article._find_citation_guidance(next_line)
    assert citation.startswith("袁永甲译")
    parsed = url_article._parse_citation_string(citation)
    assert parsed["translator"] == [{"literal": "袁永甲"}]
    assert parsed["title"] == "阿爸腓利门传记"
    assert parsed["container-title"] == "爱神集导读版"
    assert "author" not in parsed

    # Source wording: ctcfol.org/posts/academic/神父马克西姆被提狂喜与神圣空间
    translated_work = "若参考了这篇中译，请注明引用格式如下：神父马克西姆康斯坦斯，《被提，狂喜与神圣空间的建构》，关家胜译（伦敦：教父原文中译计划，2023年7月22日），引用日期，附上本中译链接。"
    parsed = url_article._parse_citation_string(url_article._find_citation_guidance(translated_work))
    assert parsed["author"] == [{"literal": "神父马克西姆康斯坦斯"}]
    assert parsed["translator"] == [{"literal": "关家胜"}]


@pytest.mark.parametrize("heading,intro,citation,title", [
    ("Cite this article", "", "Seavill, P.W. Alkaline earth metal-based perovskite ferroelectrics. Nat. Synth 4, 1020 (2025).", "Alkaline earth metal-based perovskite ferroelectrics"),
    ("Cite this work", "This article can be cited as:", "Max Roser (2019) - “We won the Lovie Award!” Published online at OurWorldinData.org.", "We won the Lovie Award!"),
])
def test_english_citation_after_heading_survives_budget(monkeypatch, heading, intro, citation, title):
    # Source wording: nature.com/articles/s44160-025-00881-w and ourworldindata.org/we-won-the-lovie-award
    html = "<p>" + "body " * 3000 + "</p><h2>" + heading + "</h2><p>" + intro + "</p><p>" + citation + "</p>"
    blocks = contracts.web_source_blocks(html)

    def response(selected):
        block = next(b for b in selected if citation in b["text"])
        return SimpleNamespace(csl={"title": title}, field_evidence={
            "title": {"block_id": block["id"], "quote": title}})

    calls = fake_predictor(monkeypatch, response)
    assert contracts.extract_multimodal_metadata("web", blocks, IngestionConfig())["title"] == title
    if intro:
        assert url_article._find_citation_guidance(f"{heading}\n\n{intro}\n\n`{citation}`") == citation
    assert calls == [contracts.ExtractWebMetadata]


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
