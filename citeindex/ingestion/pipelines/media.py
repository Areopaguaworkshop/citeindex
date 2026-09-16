import json
import logging
import os
import math
import subprocess
import tempfile
from importlib.metadata import PackageNotFoundError, version
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from ..models import IngestionConfig, PipelineResult
from ..csl import evaluation_fields
from ..markdown_export import _format_timestamp
from .multimodal_metadata import extract_multimodal_metadata, metadata_blocks, transcript_blocks
from ..url_security import validate_public_url
from .common import (
    build_merkle_for_nodes,
    build_nodes,
    build_retrieval_index,
    make_basic_csl,
    make_source_id,
)

logger = logging.getLogger(__name__)


def _is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


# ---------------------------------------------------------------------------
# Media source probing
# ---------------------------------------------------------------------------

def _probe_local_media(path: str) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {"source_path": os.path.abspath(path), "title": os.path.basename(path)}
    try:
        from pymediainfo import MediaInfo

        parsed = MediaInfo.parse(path)
        tracks = parsed.tracks
        general = next((t for t in tracks if t.track_type == "General"), None)
        if general:
            metadata["title"] = general.title or metadata["title"]
            metadata["duration_ms"] = general.duration
            metadata["format"] = general.format
            metadata["performer"] = getattr(general, "performer", None)
            metadata["description"] = getattr(general, "description", None)
            metadata["medium"] = "video" if any(t.track_type == "Video" for t in tracks) else "audio"
    except Exception:
        logger.warning("pymediainfo unavailable or failed", exc_info=True)
    return metadata


def _probe_url_media(url: str) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {"url": url, "title": url}
    cmd = ["yt-dlp", "--dump-single-json", "--no-warnings", url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60, start_new_session=True)
        if proc.returncode == 0 and proc.stdout.strip():
            info = json.loads(proc.stdout)
            metadata.update(
                {
                    "title": info.get("title") or url,
                    "uploader": info.get("uploader"),
                    "description": info.get("description"),
                    "series": info.get("series"),
                    "episode_number": info.get("episode_number"),
                    "season_number": info.get("season_number"),
                    "creator": info.get("creator"),
                    "release_date": info.get("release_date"),
                    "medium": "audio" if info.get("vcodec") == "none" else "video" if info.get("vcodec") else None,
                    "duration_seconds": info.get("duration"),
                    "upload_date": info.get("upload_date"),
                    "platform": info.get("extractor_key") or info.get("extractor"),
                }
            )
    except Exception:
        logger.warning("yt-dlp metadata probe failed", exc_info=True)
    return metadata


# ---------------------------------------------------------------------------
# Media download (URL → file)
# ---------------------------------------------------------------------------

def _download_media(url: str) -> Optional[str]:
    """Download media from URL using yt-dlp. Returns path to downloaded file."""
    dst: Optional[str] = None
    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            output_template = os.path.join(tmpdir, "%(title)s.%(ext)s")
            cmd = [
                "yt-dlp",
                "-x",  # extract audio
                "--audio-format", "wav",
                "-o", output_template,
                url,
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, start_new_session=True)
            if proc.returncode != 0:
                logger.warning("yt-dlp download failed: %s", proc.stderr[:200])
                return None

            # Find the output file
            for f in os.listdir(tmpdir):
                src = os.path.join(tmpdir, f)
                suffix = os.path.splitext(f)[1]
                fd, dst = tempfile.mkstemp(prefix="citeindex_media_", suffix=suffix)
                os.close(fd)
                import shutil
                shutil.copy2(src, dst)
                return dst
    except Exception:
        if dst and os.path.exists(dst):
            os.remove(dst)
        logger.warning("Media download failed", exc_info=True)
    return None


# ---------------------------------------------------------------------------
# Audio extraction with ffmpeg
# ---------------------------------------------------------------------------

def _extract_audio(media_path: str) -> Optional[str]:
    """Extract audio track from media file using ffmpeg. Returns path to WAV."""
    output_path: Optional[str] = None
    try:
        fd, output_path = tempfile.mkstemp(prefix="citeindex_audio_", suffix=".wav")
        os.close(fd)
        cmd = [
            "ffmpeg", "-y",
            "-i", media_path,
            "-vn",  # no video
            "-acodec", "pcm_s16le",
            "-ar", "16000",  # 16kHz for WhisperX
            "-ac", "1",  # mono
            output_path,
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300, start_new_session=True)
        if proc.returncode == 0 and os.path.exists(output_path):
            logger.info("Audio extracted to %s", output_path)
            return output_path
        logger.warning("ffmpeg audio extraction failed: %s", proc.stderr[:200])
    except Exception:
        logger.warning("ffmpeg not available or failed", exc_info=True)
    if output_path and os.path.exists(output_path):
        os.remove(output_path)
    return None


# ---------------------------------------------------------------------------
# Transcription with WhisperX
# ---------------------------------------------------------------------------

def _transcribe_whisperx(audio_path: str) -> List[Dict[str, Any]]:
    """Generate timestamped transcript using WhisperX."""
    try:
        import whisperx

        device = "cpu"
        model = whisperx.load_model("base", device=device, compute_type="int8")
        audio = whisperx.load_audio(audio_path)
        result = model.transcribe(audio)

        # Word-level alignment
        align_model, align_metadata = whisperx.load_align_model(
            language_code=result.get("language", "en"), device=device
        )
        aligned = whisperx.align(
            result["segments"], align_model, align_metadata, audio, device
        )

        segments: List[Dict[str, Any]] = []
        for seg in aligned.get("segments", result.get("segments", [])):
            segments.append({
                "start": seg.get("start", 0.0),
                "end": seg.get("end", 0.0),
                "text": seg.get("text", ""),
            })

        logger.info("WhisperX transcription produced %d segments", len(segments))
        return segments

    except ImportError:
        logger.info("whisperx not installed, skipping transcription")
        return []
    except Exception:
        logger.warning("WhisperX transcription failed", exc_info=True)
        return []


def _normalize_segments(segments: Any, duration: float | None = None) -> List[Dict[str, Any]]:
    """Keep source-relative times/text and reject malformed ASR output."""
    normalized = []
    for raw in segments if isinstance(segments, list) else []:
        if not isinstance(raw, dict):
            continue
        start, end, text = raw.get("start"), raw.get("end"), raw.get("text")
        if (type(start) not in (int, float) or type(end) not in (int, float)
                or not math.isfinite(start) or not math.isfinite(end)
                or start < 0 or start >= end or not isinstance(text, str) or not text.strip()
                or (duration is not None and end > duration + 0.5)):
            continue
        segment = {"start": float(start), "end": float(end), "text": text.strip()}
        speaker = raw.get("spk", raw.get("speaker"))
        if isinstance(speaker, str) and speaker.strip():
            segment["speaker"] = speaker.strip()
        normalized.append(segment)
    return normalized


def _transcribe_wenbi(audio_path: str, config: IngestionConfig, duration: float | None) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Use only Wenbi's raw ASR boundary; never rewrite, translate, or auto-route."""
    if config.wenbi_asr_provider not in {"funasr", "whisper", "gladia"}:
        raise ValueError("Wenbi ASR provider must be explicit: funasr, whisper, or gladia")
    if config.wenbi_python:
        if not os.path.isfile(config.wenbi_python) or not os.access(config.wenbi_python, os.X_OK):
            raise RuntimeError("--wenbi-python must name an executable interpreter")
        script = (
            "import json,sys; from wenbi.asr import transcribe_with_engine; "
            "r=transcribe_with_engine(sys.argv[1],asr_provider=sys.argv[2],"
            "enable_speakers=sys.argv[3]=='1',gladia_speaker_labels=sys.argv[3]=='1',"
            "whisper_model=sys.argv[4]); "
            "print(json.dumps({'segments':r.get('segments',[]),'provider':r.get('provider')},ensure_ascii=False))"
        )
        process = subprocess.run(
            [config.wenbi_python, "-c", script, audio_path, config.wenbi_asr_provider,
             "1" if config.wenbi_speaker_labels else "0", config.wenbi_whisper_model],
            capture_output=True, text=True, start_new_session=True,
        )
        if process.returncode:
            raise RuntimeError(f"Wenbi ASR failed: {process.stderr[-1000:]}")
        try:
            result = json.loads(process.stdout.strip().splitlines()[-1])
        except (IndexError, json.JSONDecodeError) as exc:
            raise RuntimeError("Wenbi ASR returned invalid JSON") from exc
    else:
        try:
            from wenbi.asr import transcribe_with_engine
        except ImportError as exc:
            raise RuntimeError(
                "Wenbi ASR requested but package is unavailable; install Wenbi or pass --wenbi-python"
            ) from exc
        result = transcribe_with_engine(
            audio_path,
            asr_provider=config.wenbi_asr_provider,
            enable_speakers=config.wenbi_speaker_labels,
            gladia_speaker_labels=config.wenbi_speaker_labels,
            whisper_model=config.wenbi_whisper_model,
        )
    if not isinstance(result, dict):
        raise RuntimeError("Wenbi ASR returned an invalid result")
    segments = _normalize_segments(result.get("segments"), duration)
    try:
        package_version = version("wenbi") if not config.wenbi_python else "isolated-environment"
    except PackageNotFoundError:
        package_version = "unknown"
    provenance = {
        "adapter": "wenbi",
        "provider": result.get("provider") or config.wenbi_asr_provider,
        "model": config.wenbi_whisper_model if config.wenbi_asr_provider == "whisper" else None,
        "version": package_version,
        "speaker_labels_requested": config.wenbi_speaker_labels,
    }
    return segments, provenance


# ---------------------------------------------------------------------------
# Speaker diarization with pyannote (optional)
# ---------------------------------------------------------------------------

def _diarize_speakers(audio_path: str) -> List[Dict[str, Any]]:
    """Identify speaker segments using pyannote.audio."""
    try:
        from pyannote.audio import Pipeline

        pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1")
        diarization = pipeline(audio_path)

        speaker_segments: List[Dict[str, Any]] = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            speaker_segments.append({
                "start": turn.start,
                "end": turn.end,
                "speaker": speaker,
            })

        logger.info("Pyannote diarization found %d speaker turns", len(speaker_segments))
        return speaker_segments

    except ImportError:
        logger.info("pyannote not installed, skipping diarization")
        return []
    except Exception:
        logger.warning("Pyannote diarization failed", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Pipeline entry point
# ---------------------------------------------------------------------------

def run(media_ref: str, config: Optional[IngestionConfig] = None) -> PipelineResult:
    cfg = config or IngestionConfig()
    if _is_url(media_ref):
        validate_public_url(media_ref)
    source_id = make_source_id(media_ref)
    input_type = "url" if _is_url(media_ref) else "file"

    # Step 1: Probe metadata
    media_metadata = _probe_url_media(media_ref) if input_type == "url" else _probe_local_media(media_ref)
    duration = media_metadata.get("duration_seconds")
    if duration is None and media_metadata.get("duration_ms") is not None:
        try:
            duration = float(media_metadata["duration_ms"]) / 1000
        except (TypeError, ValueError):
            duration = None
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
        duration = None

    # Step 2: Resolve media file path
    media_file_path: Optional[str] = None
    downloaded = False

    if input_type == "file":
        media_file_path = os.path.abspath(media_ref)
    else:
        media_file_path = _download_media(media_ref)
        downloaded = True

    # Step 3: Extract audio
    audio_path: Optional[str] = None
    if media_file_path and os.path.exists(media_file_path):
        audio_path = _extract_audio(media_file_path)

    # Step 4: Transcribe
    transcript_segments: List[Dict[str, Any]] = []
    asr_provenance: Dict[str, Any] = {"adapter": cfg.media_asr_backend}
    if audio_path and os.path.exists(audio_path):
        try:
            if cfg.media_asr_backend == "wenbi":
                transcript_segments, asr_provenance = _transcribe_wenbi(audio_path, cfg, duration)
            elif cfg.media_asr_backend == "whisperx":
                transcript_segments = _normalize_segments(_transcribe_whisperx(audio_path), duration)
                asr_provenance.update(provider="whisperx", model="base", device="cpu", compute_type="int8")
            else:
                raise ValueError(f"unknown media ASR backend: {cfg.media_asr_backend}")
        except Exception:
            try:
                os.remove(audio_path)
            except OSError:
                pass
            raise

    # Step 5: Speaker diarization (optional)
    speaker_segments: List[Dict[str, Any]] = []
    if cfg.media_asr_backend == "wenbi":
        speaker_segments = [
            {"start": segment["start"], "end": segment["end"], "speaker": segment["speaker"]}
            for segment in transcript_segments if "speaker" in segment
        ]
    elif audio_path and os.path.exists(audio_path) and transcript_segments:
        speaker_segments = _diarize_speakers(audio_path)

    # An unavailable transcript stays empty; a title is not a spoken quotation.

    # Build nodes from transcript
    page_paragraphs = [(1, [seg["text"] for seg in transcript_segments if seg.get("text")])]
    nodes = build_nodes(source_id, page_paragraphs)
    merkle_tree = build_merkle_for_nodes(nodes)
    retrieval_index = build_retrieval_index(nodes)

    # Retain observed probe fields separately from inferred contributor identities.
    now = datetime.now(timezone.utc)
    csl_extra: Dict[str, Any] = {}
    if input_type == "url":
        csl_extra.update(URL=media_ref, accessed={"date-parts": [[now.year, now.month, now.day]]})
    if duration is not None:
        csl_extra["dimensions"] = _format_timestamp(duration)
    medium = media_metadata.get("medium")
    if medium not in {"audio", "video"} and input_type == "file":
        medium = "audio" if os.path.splitext(media_ref)[1].lower() in {".mp3", ".wav", ".m4a"} else "video"
    if medium in {"audio", "video"}:
        csl_extra["medium"] = medium
    # The timestamped transcript and probe JSON are different kinds of evidence.
    source_blocks = metadata_blocks(media_metadata) + transcript_blocks(transcript_segments)
    retrieval_metadata = {"URL": media_ref, "accessed": now.date().isoformat()} if input_type == "url" else {}
    source_blocks += metadata_blocks(retrieval_metadata, "retrieval_metadata.json")
    extracted = extract_multimodal_metadata("media", source_blocks, cfg)
    csl_extra.update(extracted)
    title = csl_extra.pop("title", None) or media_metadata.get("title") or source_id
    kind = csl_extra.pop("type", None) or "document"
    csl_json = make_basic_csl(source_id, title, kind, csl_extra)
    csl_json["_field_status"] = {
        field: ("source-supported" if field in extracted.get("_field_evidence", {}) else
                "unverified" if csl_json.get(field) is not None else "missing")
        for field in evaluation_fields(csl_json, "media")
    }
    for field in ("URL", "accessed", "dimensions", "medium"):
        if field in csl_json:
            csl_json["_field_status"][field] = "observed"

    transcript_json = {
        "source_id": source_id,
        "source_type": "media",
        "metadata": media_metadata,
        "segments": transcript_segments,
        "speaker_segments": speaker_segments,
        "asr": asr_provenance,
        "nodes": nodes,
    }

    extra: Dict[str, Any] = {
        "source_blocks": source_blocks,
        "source_path": media_file_path,
        "retrieval_metadata": retrieval_metadata,
        "transcription_status": "available" if transcript_segments else "unavailable",
        "transcription_provenance": asr_provenance,
        "quotation_locators": [
            {"segment_id": b["segment_id"], "quote": b["text"],
             "citation_item": {"id": source_id, "label": "timestamp",
                               "locator": f'{_format_timestamp(b["start_seconds"])}–{_format_timestamp(b["end_seconds"])}'}}
            for b in source_blocks if "segment_id" in b
        ],
    }
    if input_type == "url" and media_file_path and os.path.exists(media_file_path):
        extra["source_snapshot_path"] = media_file_path
        extra["cleanup_source_snapshot"] = True

    # Cleanup temp files
    if audio_path and os.path.exists(audio_path):
        try:
            os.remove(audio_path)
        except OSError:
            pass
    if downloaded and media_file_path and os.path.exists(media_file_path) and not extra.get("source_snapshot_path"):
        try:
            os.remove(media_file_path)
        except OSError:
            pass

    return PipelineResult(
        status="ok",
        source_id=source_id,
        resource_type="media",
        csl_json=csl_json,
        transcript_json=transcript_json,
        merkle_tree=merkle_tree,
        media_metadata=media_metadata,
        retrieval_index=retrieval_index,
        extra=extra,
    )
