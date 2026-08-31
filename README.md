# CiteIndex

CiteIndex, ingest sources with proper citation. PDF, URL, media, Office, DJVU.

Evidence-backed citation metadata, Merkle-verified integrity, CJK-first OCR.
Optional verification traces accepted metadata corrections to source evidence.

[![PyPI Downloads](https://static.pepy.tech/personalized-badge/citeindex?period=total&units=INTERNATIONAL_SYSTEM&left_color=BLACK&right_color=RED&left_text=downloads)](https://pepy.tech/projects/citeindex)

## Install

```bash
# Using uv (recommended)
uv pip install citeindex

# Or pip
pip install citeindex
```

## CLI

```bash
# Ingest a PDF
citeindex paper.pdf

# Ingest a scanned PDF with the default MinerU backend
citeindex scanned.pdf --ocr-engine mineru

# Use the optional GLM-OCR backend via local Ollama
citeindex scanned.pdf --ocr-engine glm-ocr --ocr-model glm-ocr:latest

# Ingest a URL
citeindex https://example.com/article

# Crawl and ingest all articles from a site
citeindex https://example.com/articles --all-url-article --crawl-depth 2

# Crawl and re-ingest only changed pages
citeindex https://example.com/articles --update-url-article

# Options
citeindex paper.pdf --llm ollama/qwen3 --type thesis --is-primary
citeindex paper.pdf --text-direction vertical --vertical-lang ch
citeindex scanned.pdf --ocr-engine mineru --lang auto --page-range "1-10"
citeindex paper.pdf --no-layout  # disable column/footnote detection
citeindex paper.pdf --force-ocr  # override PDF classification
citeindex -q paper.pdf           # disable default debug logging

# Evidence-backed citation verification (Crossref exact DOI lookup)
citeindex paper.pdf --verify-citations --registry-contact-email you@example.org

# Ask a stronger provider-qualified model only to resolve remaining conflicts
citeindex paper.pdf --verify-citations --citation-verifier-model openai/gpt-5
```

Verification is opt-in. Crossref receives only an exact DOI, never document
text. A model correction is accepted only when it supplies a matching source
quote and stable locator; unavailable models leave the record unchanged for
review. Use `--no-crossref` to disable registry lookup or
`--offline-verification` to disable registry and verifier-model requests.

An independent harness audit is available only through an explicit Codex,
Claude Code, OpenCode, or Pi skill/command; running `citeindex` directly does
not auto-trigger one.

## Python API

```python
from citeindex import ingest, IngestionConfig, IngestionFailure, PipelineResult

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

## Code Structure

- `citeindex/cli.py` maps CLI options into `IngestionConfig`.
- `citeindex/ingestion/master.py` detects input types, routes pipelines, verifies metadata, and persists results.
- `citeindex/ingestion/pipelines/` contains the digital PDF, scanned PDF, URL, media, GROBID, PageIndex, and metadata-extraction implementations.
- `citeindex/ingestion/storage.py` and `markdown_export.py` write corpus artifacts and companion library Markdown.
- `.agents/`, `.claude/`, `.codex/`, `.opencode/`, and `.pi/` expose the optional citation-evidence audit to supported agent harnesses.

## Ingestion Pipelines

CiteIndex automatically detects the input type and routes to the correct pipeline:

### Digital PDF

```
PDF → PyMuPDF4LLM layout/text (PyMuPDF fallback) + image extraction
    → page-paragraph document structure → GROBID references
    → PageIndex tree (default) → GROBID metadata / DSPy fallback
    → Merkle tree → section_tree + heading injection
    → store document.json and library Markdown
```

- **PyMuPDF4LLM** performs layout-aware extraction when enabled; raw **PyMuPDF** is the fallback
- **GROBID** extracts metadata and references when its service is available
- **DSPy** extracts citation metadata only when GROBID metadata is unavailable
- Builds page-based document structure and augments it with PageIndex section headings
- **PageIndex** builds LLM-driven section hierarchy, persists it to corpus, and feeds library markdown headings

### Scanned PDF

```
PDF → scanned backend selector
    → MinerU (default) OR GLM-OCR + PaddleOCR LayoutDetection
    → normalized content_list / markdown / extracted figures
    → pattern extraction with DSPy-priority metadata
    → document structure → Merkle tree → PageIndex tree (default)
    → store only CiteIndex-native artifacts to corpus/
```

- **MinerU** is the default scanned backend
- **GLM-OCR** is an optional backend that runs through local **Ollama** using the native `/api/generate` endpoint
- **PaddleOCR LayoutDetection** (`PP-DocLayout_plus-L`) supplies external region proposals for GLM-OCR from the start
- Scanned PDFs do **not** use GROBID; metadata is extracted from structured backend output via DSPy-backed extraction
- DSPy is allowed to overwrite pattern-extracted metadata fields for scanned documents
- **PageIndex** runs by default for scanned PDFs, just like digital PDFs
- Only extracted figures / illustrations are exported into the corpus `images/` folder; raw backend artifacts are not preserved
- Supports `--ocr-engine mineru` or `--ocr-engine glm-ocr`

### Scanned PDF Backend Selection

Use the scanned backend flags only for image-based PDFs:

```bash
# Default scanned backend
citeindex scanned.pdf --ocr-engine mineru

# Local GLM-OCR through Ollama
citeindex scanned.pdf --ocr-engine glm-ocr --ocr-model glm-ocr:latest

# Custom Ollama host
citeindex scanned.pdf --ocr-engine glm-ocr --ollama-host http://localhost:11434
```

- `mineru` is the default and recommended general-purpose backend
- `glm-ocr` requires a local Ollama model plus PaddleOCR layout-detection dependencies
- `--mineru-backend` is forwarded directly to the MinerU CLI backend selector

### URL Article

```
URL → Playwright/requests (fetch) → trafilatura/readability (content)
    → Zotero (metadata) → in-page citation guidance (regex → DSPy fallback)
    → section-hierarchical paragraphs → PageIndex tree (optional)
    → hashes → Merkle tree → store to corpus/
```

- **Playwright** renders JavaScript-heavy pages (fallback to **requests**)
- **trafilatura** extracts clean markdown with heading structure (fallback to **readability-lxml**)
- **Zotero** extracts citation metadata via translation-server (title, authors, date, DOI)
- Discovers in-page citation guidance: 若要引用 / 引用格式 / Cite this / Zitierweise / Pour citer
- Parses citation strings with regex first, DSPy fallback for unparseable formats
- Citation guidance overrides Zotero/trafilatura metadata (more authoritative)
- Supports batch crawling with `--all-url-article` and `--update-url-article`

### Media

```
Recognized media URL/local file → yt-dlp or local copy → ffmpeg (audio) → WhisperX (transcription)
        → pyannote (diarization, optional) → CSL JSON
        → chunking → hashes → Merkle tree → store to corpus/
```

- **yt-dlp** downloads from YouTube, Vimeo, podcasts, etc.
- **WhisperX** transcribes with word-level timestamps
- **pyannote** speaker diarization (optional)
- Supports audio (`.mp3`, `.wav`, `.m4a`) and video (`.mp4`, `.mkv`, `.webm`)
- Remote media routing recognizes YouTube, Vimeo, podcast, SoundCloud, and `youtu.be` hosts; other URLs use the article pipeline

### Office & DJVU

Office documents (`.docx`, `.doc`, `.rtf`, `.odt`, `.pptx`, `.ppt`, `.odp`) are converted to PDF via **LibreOffice**, and DJVU (`.djvu`) via **ddjvu**, then routed to the digital or scanned PDF pipeline. Default DJVU conversion is limited to the first 10 pages.

### Citation Enrichment Cascade

For digital PDF inputs, CiteIndex enriches metadata through a priority cascade:

1. **GROBID** — deterministic metadata + references (primary)
2. **LLM extraction** — DSPy-based citation parsing (fallback)
3. **PDF metadata** — basic file metadata only (last resort)

Scanned PDFs do not use GROBID. Their structured OCR output is parsed with
document-specific patterns, then DSPy values take priority when available.

With `--verify-citations`, the finalized draft follows this additional
evidence-first stage before CSL IDs, hashes, filenames, and Markdown are
written:

```
candidate CSL → DOI extraction → exact Crossref lookup
              → source quote + page/paragraph or URL snapshot evidence
              → deterministic reconciliation → optional conflict model
              → standardized CSL + verification report
```

Registry-only values are never applied automatically. They must also appear in
the original source evidence; otherwise CiteIndex records `needs_review`.

## Configuration Reference

| Option | CLI Flag | Default | Description |
|--------|----------|---------|-------------|
| `llm_model` | `--llm` | `ollama/glm-5.3-flash:cloud` | LLM model (`ollama/name` or `gemini/name`) |
| `ocr_engine` | `--ocr-engine` | `mineru` | Scanned PDF OCR backend: `mineru` or `glm-ocr` |
| `ocr_model` | `--ocr-model` | `glm-ocr:latest` | Ollama model name used by model-backed OCR engines such as GLM-OCR |
| `ollama_host` | `--ollama-host` | `http://localhost:11434` | Ollama base URL for GLM-OCR requests |
| `mineru_backend` | `--mineru-backend` | `pipeline` | Backend value forwarded to the MinerU CLI |
| `mineru_timeout` | `--mineru-timeout` | `3600` | MinerU subprocess timeout in seconds, up to `3600` |
| `mineru_chunk_pages` | `--mineru-chunk-pages` | `auto` | Split large PDFs into adaptive MinerU chunks; use a page count to override or `0` to disable |
| `text_direction` | `--text-direction`, `-td` | `horizontal` | `horizontal`, `auto`, or `vertical` |
| `vertical_lang` | `--vertical-lang` | `ch` | CJK language: `ch` (Chinese) or `japan` |
| `lang` | `--lang`, `-l` | `auto` | OCR language (auto-detect or Tesseract code) |
| `page_range` | `--page-range`, `-p` | `1-5, -3` | Pages to extract (e.g. `"1-10"`, `"1-5, -3"`) |
| `doc_type_override` | `--type`, `-t` | auto | `book`, `thesis`, `journal`, or `bookchapter` |
| `use_layout_analysis` | `--no-layout` | `True` | Disable column/footnote detection |
| `is_primary` | `--is-primary` | `False` | Line-level granularity (vs paragraph-level) |
| `use_pageindex` | `--no-pageindex` | `True` | PageIndex hierarchy is enabled by default; pass `--no-pageindex` to disable it |
| `pageindex_model` | `--pageindex-model` | `ollama/glm-5.3-flash:cloud` | LLM for PageIndex tree building |
| `force_pdf_kind` | `--force-ocr`, `--force-digital` | auto | Override automatic PDF classification |
| `verify_citations` | `--verify-citations` | `False` | Enable evidence-backed metadata verification |
| `citation_verifier_model` | `--citation-verifier-model` | none | Provider-qualified model used only for unresolved conflicts |
| `crossref_enabled` | `--no-crossref` | `True` | Disable exact DOI Crossref lookup |
| `offline_verification` | `--offline-verification` | `False` | Block Crossref and verifier-model requests (not normal ingestion fetching) |
| `registry_contact_email` | `--registry-contact-email` | none | Optional polite Crossref contact email; not persisted |
| `citation_style` | (API only) | `chicago-author-date` | CSL citation style for output |
| `corpus_root` | `--corpus-root` | `corpus` | Output directory for ingested artifacts |
| `schema_version` | `--schema-version` | `1.0.0` | Output schema version tag |
| (CLI only) | `--crawl-depth` | `2` | Max BFS crawl depth for `--all-url-article` |
| (CLI only) | `--crawl-max-pages` | `100` | Max pages for `--all-url-article` |
| (CLI only) | `--verbose`, `-v` | on | Enable verbose/debug logging |
| (CLI only) | `--quiet`, `-q` | off | Disable verbose/debug logging |

## Output

Each ingestion produces a content-addressed corpus folder (for example, `corpus/Author_2024_Title_a1b2c3d4e5f6/`) and a companion library Markdown file.

### Corpus artifacts (`corpus/Author_2024_Title_<12-char-hash>/`)

| File | Description |
|------|-------------|
| `csl.json` | Citation metadata (CSL-JSON with CiteIndex fields: `content_hash`, `merkle_root`, `source_type`, `ingestion_timestamp`) |
| `document.json` | Structured document tree — pages, paragraphs, and `section_tree` for URL articles and PageIndex-augmented PDFs |
| `pageindex_tree.json` | Persisted CiteIndex/PageIndex hierarchy with page ranges and summaries when PageIndex runs |
| `merkle.json` | SHA-256 Merkle tree for integrity verification |
| `transcript.json` | Timestamped transcript with speaker segments (media only) |
| `media_metadata.json` | Source media metadata (media only) |
| `ingestion_output.json` | Full ingestion result with all pipeline outputs |
| `citation_verification.json` | Evidence, Crossref provenance/digest, accepted corrections, and `needs_review` items (when verification is enabled) |
| `source.html` | Fetched HTML snapshot used as URL evidence (URL articles only) |
| `images/` | Extracted figures and illustrations when available |

### Library markdown (`library/Author_2024_Title_<12-char-hash>.md`)

Human-readable markdown with YAML front-matter, inline citation, page/section/timestamp headers with CSL-level detail, full extracted text, and footnotes. When PageIndex is available, digital PDFs emit section headings into the markdown instead of only flat page labels. Written to `library/` (sibling of `corpus/`).

### Agent-harness final audit

Codex, OpenCode, Claude Code, and Pi can load the shared
`citation-verification` skill after ingestion. The audit reads the original
source and `citation_verification.json`, then returns quotation-backed repair
recommendations. It never edits persisted `csl.json` directly.

The CLI itself always performs the core verification when
`--verify-citations` is supplied. The additional agent audit runs only through
an explicit harness workflow (for example OpenCode `/ingest-verified`); a raw
`citeindex` subprocess cannot detect or launch an agent harness.

### Ingestion log (`corpus/ingestion_log.jsonl`)

Appended on every ingestion with `input_ref`, `resource_type`, `csl_id`, `merkle_root`, and `ingestion_timestamp`.

### URL content hashes (`corpus/_url_content_hashes.json`)

Persisted URL → content-hash mapping used by `--update-url-article` for change detection.

### Return Value

The `ingest()` function returns a dict:

```python
{
    "schema_version": "1.0.0",
    "status": "ok",                    # "ok" or "blocked"
    "document_path": "corpus/Author_2024_Title",
    "standardized_csl_json": { ... },  # Full CSL-JSON with ci_ extensions
    "sub_pipeline_outputs": { ... },   # Raw pipeline results
    "ingestion_log_entry": { ... },     # Log entry with merkle_root
    "library_md_path": "library/Author_2024_Title.md",
}

# On failure:
{
    "status": "blocked",
    "source_id": "unknown",
    "stage": "detect_resource_type",
    "error_code": "unsupported_input",
    "error_message": "Unsupported input: ...",
    "next_action": "Provide PDF, URL, or media file",
}
```

### Batch URL Ingestion Return

The `ingest_all_urls()` method (triggered by `--all-url-article` / `--update-url-article`) returns:

```python
{
    "status": "ok",
    "root_url": "https://example.com/articles",
    "discovered": 25,      # total article URLs found
    "ingested": 20,        # newly ingested
    "updated": 2,           # re-ingested (content changed)
    "skipped": 3,           # unchanged (--update-url-article only)
    "failed": 0,            # errors
    "results": [            # per-URL status list
        {"url": "...", "status": "ok"},
        {"url": "...", "status": "unchanged"},
        ...
    ]
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

## Citation

If you use CiteIndex in your work, please cite it:

**APA:**

> ajia. (2025). *CiteIndex: Ingest sources with proper citation* (Version 0.13.0). MIT. https://github.com/ajia/citeindex

**BibTeX:**

```bibtex
@software{citeindex2025,
  author  = {Yongjia, Yuan},
  title   = {CiteIndex: Ingest sources with proper citation},
  version = {0.13.0},
  year    = {2025},
  license = {MIT},
  url     = {https://github.com/ajia/citeindex},
}
```

## License

MIT
