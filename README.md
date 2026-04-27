# CiteIndex

Ingest sources with proper citation. PDF, URL, media, Office, DJVU.

## Install

```bash
pip install -e .
```

## CLI

```bash
# Ingest a PDF
citeindex paper.pdf

# Ingest a URL
citeindex https://example.com/article

# Crawl and ingest all articles from a site
citeindex https://example.com/articles --all-url-article --crawl-depth 2

# Options
citeindex paper.pdf --llm ollama/qwen3 --type thesis --is-primary
citeindex paper.pdf --text-direction vertical --vertical-lang ch
citeindex scanned.pdf --lang auto --page-range "1-10"
```

## Python API

```python
from citeindex import ingest, IngestionConfig

# Simple
result = ingest("paper.pdf")
print(result["status"])  # "ok"

# With config
config = IngestionConfig(
    llm_model="ollama/qwen3",
    text_direction="vertical",
    is_primary=True,
)
result = ingest("paper.pdf", corpus_root="my_corpus", config=config)
```

## Supported Formats

| Format | Extension / Protocol |
|--------|----------------------|
| Digital PDF | `.pdf` (with embedded text) |
| Scanned PDF | `.pdf` (image-based, OCR applied) |
| URL Article | `http://` / `https://` |
| Media | `.mp3`, `.wav`, `.m4a`, `.mp4`, `.mkv`, `.webm` |
| Office | `.docx`, `.doc`, `.rtf`, `.odt`, `.pptx`, `.ppt`, `.odp` |
| DJVU | `.djvu` |

## Output

Each ingestion produces a corpus folder containing:

- `csl.json` — Citation metadata (CSL-JSON with `ci_*` extensions)
- `document.json` — Structured document tree (PageIndex)
- `merkle.json` — SHA-256 Merkle tree for integrity verification
- `ingestion_output.json` — Full ingestion result
- `library.md` — Human-readable citation with extracted text

## License

MIT