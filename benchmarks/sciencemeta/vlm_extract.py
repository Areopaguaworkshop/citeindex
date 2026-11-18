"""Pure VLM extraction arm: pages 1-6 as images, one JSON call per record."""

import argparse
import base64
import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pymupdf
import requests

MODEL = "deepseek-v4.1-flash:1017-cloud"
PROMPT = (
    "Read these first pages of a book. Return JSON only with keys: "
    "title (string), subtitle (string or null), authors (array of strings), "
    "publisher (string or null), publisher_place (string or null), "
    "issued_year (integer or null), isbn (string or null). "
    "Use only what is printed. If a field is not printed, use null. "
    "Do not translate names or titles; copy them exactly as printed."
)


def extract(row: dict, model: str = MODEL) -> dict:
    started = time.monotonic()
    path = row["source_path"]
    try:
        images = []
        with pymupdf.open(path) as doc:
            for i in range(min(6, doc.page_count)):
                images.append(base64.b64encode(doc[i].get_pixmap(dpi=140).tobytes("png")).decode())
        resp = requests.post(
            "http://localhost:11434/api/chat",
            json={"model": model, "stream": False, "format": "json",
                  "messages": [{"role": "user", "content": PROMPT, "images": images}]},
            timeout=300,
        )
        text = resp.json()["message"]["content"].strip()
        if text.startswith("```"):
            parts = text.split("```")
            text = parts[1] if len(parts) > 1 else text
            if text.lstrip().lower().startswith("json"):
                text = text.lstrip()[4:]
        payload = json.loads(text.strip())
        csl = {
            "type": "book",
            "title": payload.get("title"),
            "author": [{"literal": a} for a in (payload.get("authors") or []) if a],
            "publisher": payload.get("publisher"),
            "publisher-place": payload.get("publisher_place"),
            "ISBN": payload.get("isbn"),
        }
        if payload.get("subtitle"):
            csl["subtitle"] = payload["subtitle"]
        if payload.get("issued_year"):
            csl["issued"] = {"date-parts": [[int(payload["issued_year"])]]}
        csl = {k: v for k, v in csl.items() if v not in (None, "", [])}
        return {"id": row["id"], "status": "ok", "seconds": round(time.monotonic() - started, 1),
                "csl": csl, "source_sha256": row.get("source_sha256"), "modality": row.get("modality"),
                "attempt": 1, "error": None, "protocol": {"engine": "vlm", "model": model,
                "pages": "1-6", "dpi": 140}}
    except Exception as exc:
        return {"id": row["id"], "status": "failed", "seconds": round(time.monotonic() - started, 1),
                "csl": {}, "error": f"{type(exc).__name__}: {exc}",
                "source_sha256": row.get("source_sha256"), "modality": row.get("modality"),
                "attempt": 1, "protocol": {"engine": "vlm", "model": model}}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--manifest", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--model", default=MODEL)
    args = ap.parse_args()

    rows = [json.loads(l) for l in open(args.manifest)]
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if out.exists():
        for line in out.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("status") == "ok":
                done.add(row["id"])
    rows = [r for r in rows if r["id"] not in done]
    print(f"{len(rows)} pending / {len(done)} already ok", flush=True)
    with out.open("a", encoding="utf-8") as fh, ThreadPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(lambda r: extract(r, args.model), rows):
            fh.write(json.dumps(result, ensure_ascii=False) + "\n")
            fh.flush()
            print(result["id"][:8], result["status"], result["seconds"], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
