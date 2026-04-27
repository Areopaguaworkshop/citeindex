# CiteIndex

**v0.12.0** — Ingest sources with proper citation. PDF, URL, media, Office, DJVU.

Deterministic citation extraction, Merkle-verified integrity, CJK-first OCR.
Every claim is traced, verified, and cited — no hallucinations.

## Install

```bash
# Using rye (recommended)
rye sync

# Or pip
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

# Crawl and re-ingest only changed pages
citeindex https://example.com/articles --update-url-article

# Options
citeindex paper.pdf --llm ollama/qwen3 --type thesis --is-primary
citeindex paper.pdf --text-direction vertical --vertical-lang ch
citeindex scanned.pdf --lang auto --page-range "1-10"
citeindex paper.pdf --no-layout  # disable column/footnote detection
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

## Ingestion Pipelines

CiteIndex automatically detects the input type and routes to the correct pipeline:

### Digital PDF

```
PDF → GROBID (metadata) → MinerU (layout) → DSPy reconciliation
    → document structure (pages/columns/paragraphs/lines)
    → Merkle tree → store to corpus/
```

- **GROBID** extracts metadata and references deterministically
- **MinerU** performs layout analysis (columns, footnotes, tables)
- **DSPy** reconciles GROBID output with pattern extraction as fallback
- Builds section-hierarchical document structure with actual page numbers

### Scanned PDF

```
PDF → OCRmyPDF (normalize) → PaddleOCR (vertical detect) → MinerU (layout)
    → Tesseract (text) → GROBID (citations) → document structure
    → Merkle tree → store to corpus/
```

- **OCRmyPDF** normalizes and adds text layer to scanned pages
- **PaddleOCR** detects CJK vertical text layouts
- **Tesseract** provides OCR with auto-detected language
- Supports `--text-direction vertical` for traditional Chinese/Japanese

### URL Article

```
URL → Playwright/requests (fetch) → trafilatura (content)
    → Zotero (metadata) → CSL JSON → deterministic chunking
    → hashes → Merkle tree → store to corpus/
```

- **Playwright** renders JavaScript-heavy pages (fallback to **requests**)
- **trafilatura** extracts clean text with heading structure
- **Zotero** extracts citation metadata (title, authors, date, DOI)
- Discovers in-page citation guidance (若要引用 / Cite this / etc.)
- Supports batch crawling with `--all-url-article` and `--update-url-article`

### Media

```
URL/File → yt-dlp (download) → ffmpeg (audio) → WhisperX (transcription)
        → pyannote (diarization, optional) → CSL JSON
        → chunking → hashes → Merkle tree → store to corpus/
```

- **yt-dlp** downloads from YouTube, Vimeo, podcasts, etc.
- **WhisperX** transcribes with word-level timestamps
- **pyannote** speaker diarization (optional)
- Supports audio (`.mp3`, `.wav`, `.m4a`) and video (`.mp4`, `.mkv`, `.webm`)

### Office & DJVU

Office documents (`.docx`, `.doc`, `.rtf`, `.odt`, `.pptx`, `.ppt`, `.odp`) and DJVU (`.djvu`) are converted to PDF via LibreOffice/ddjvu, then routed to the digital or scanned PDF pipeline.

## Configuration Reference

| Option | CLI Flag | Default | Description |
|--------|----------|---------|-------------|
| `llm_model` | `--llm` | `ollama/qwen3` | LLM model for citation extraction |
| `text_direction` | `--text-direction`, `-td` | `horizontal` | `horizontal`, `auto`, or `vertical` |
| `vertical_lang` | `--vertical-lang` | `ch` | CJK language: `ch` (Chinese) or `japan` |
| `lang` | `--lang`, `-l` | `auto` | OCR language (auto-detect or Tesseract code) |
| `page_range` | `--page-range`, `-p` | `1-5, -3` | Pages to extract (e.g. `"1-10"`, `"1-5, -3"`) |
| `doc_type_override` | `--type`, `-t` | auto | `book`, `thesis`, `journal`, or `bookchapter` |
| `use_layout_analysis` | `--no-layout` | `True` | Disable column/footnote detection |
| `is_primary` | `--is-primary` | `False` | Line-level granularity (vs paragraph-level) |
| `use_pageindex` | `--use-pageindex` | `False` | LLM-driven section hierarchy (requires Ollama) |
| `pageindex_model` | `--pageindex-model` | `ollama/qwen3.5:cloud` | LLM for PageIndex tree building |
| `citation_style` | (API only) | `chicago-author-date` | CSL citation style for output |
| `corpus_root` | `--corpus-root` | `corpus` | Output directory for ingested artifacts |
| `schema_version` | `--schema-version` | `1.0.0` | Output schema version tag |

## Output

Each ingestion produces a corpus folder (e.g., `corpus/Author_2024_Title/`) containing:

| File | Description |
|------|-------------|
| `csl.json` | Citation metadata (CSL-JSON with `ci_*` extensions: `content_hash`, `merkle_root`, `source_type`, `ingestion_timestamp`) |
| `document.json` | Structured document tree (PageIndex) — sections, pages, paragraphs, lines |
| `merkle.json` | SHA-256 Merkle tree for integrity verification |
| `ingestion_output.json` | Full ingestion result with all pipeline outputs |
| `library.md` | Human-readable citation with extracted text and footnotes |

### Return Value

The `ingest()` function returns a dict:

```python
{
    "status": "ok",                    # "ok" or "blocked"
    "document_path": "corpus/Author_2024_Title",
    "standardized_csl_json": { ... },  # Full CSL-JSON with ci_ extensions
    "sub_pipeline_outputs": { ... },   # Raw pipeline results
    "ingestion_log_entry": { ... },     # Log entry with merkle_root
}

# On failure:
{
    "status": "blocked",
    "stage": "detect_resource_type",
    "error_code": "unsupported_input",
    "error_message": "Unsupported input: ...",
    "next_action": "Provide PDF, URL, or media file",
}
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

## License

MIT