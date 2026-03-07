import os
import subprocess
from datetime import datetime, timezone
from typing import Any, Dict, List

from pymediainfo import MediaInfo

from ..models import PipelineResult
from .common import (
    build_merkle_for_nodes,
    build_nodes,
    build_retrieval_index,
    make_basic_csl,
    make_source_id,
)


def _is_url(value: str) -> bool:
    return value.startswith("http://") or value.startswith("https://")


def _probe_local_media(path: str) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {"source_path": os.path.abspath(path), "title": os.path.basename(path)}
    try:
        parsed = MediaInfo.parse(path)
        tracks = parsed.tracks
        general = next((t for t in tracks if t.track_type == "General"), None)
        if general:
            metadata["title"] = general.title or metadata["title"]
            metadata["duration_ms"] = general.duration
            metadata["format"] = general.format
            metadata["performer"] = getattr(general, "performer", None)
    except Exception:
        pass
    return metadata


def _probe_url_media(url: str) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {"url": url, "title": url}
    cmd = ["yt-dlp", "--dump-single-json", "--no-warnings", url]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if proc.returncode == 0 and proc.stdout.strip():
            import json

            info = json.loads(proc.stdout)
            metadata.update(
                {
                    "title": info.get("title") or url,
                    "uploader": info.get("uploader"),
                    "duration_seconds": info.get("duration"),
                    "upload_date": info.get("upload_date"),
                    "platform": info.get("extractor_key") or info.get("extractor"),
                }
            )
    except Exception:
        pass
    return metadata


def run(media_ref: str) -> PipelineResult:
    source_id = make_source_id(media_ref)
    input_type = "url" if _is_url(media_ref) else "file"
    media_metadata = _probe_url_media(media_ref) if input_type == "url" else _probe_local_media(media_ref)

    transcript_segments: List[Dict[str, Any]] = []
    if media_metadata.get("title"):
        transcript_segments.append(
            {
                "start": 0.0,
                "end": 0.0,
                "text": media_metadata["title"],
            }
        )

    page_paragraphs = [(1, [seg["text"] for seg in transcript_segments if seg.get("text")])]
    nodes = build_nodes(source_id, page_paragraphs)
    merkle_tree = build_merkle_for_nodes(nodes)
    retrieval_index = build_retrieval_index(nodes)

    csl_extra: Dict[str, Any] = {
        "URL": media_ref if input_type == "url" else None,
        "publisher": media_metadata.get("platform"),
        "accessed": {
            "date-parts": [
                [
                    datetime.now(timezone.utc).year,
                    datetime.now(timezone.utc).month,
                    datetime.now(timezone.utc).day,
                ]
            ]
        },
    }
    csl_extra = {k: v for k, v in csl_extra.items() if v is not None}
    csl_json = make_basic_csl(source_id, media_metadata.get("title") or source_id, "motion_picture", csl_extra)

    transcript_json = {
        "source_id": source_id,
        "source_type": "media",
        "metadata": media_metadata,
        "segments": transcript_segments,
        "nodes": nodes,
    }

    return PipelineResult(
        status="ok",
        source_id=source_id,
        resource_type="media",
        csl_json=csl_json,
        transcript_json=transcript_json,
        merkle_tree=merkle_tree,
        media_metadata=media_metadata,
        retrieval_index=retrieval_index,
    )
