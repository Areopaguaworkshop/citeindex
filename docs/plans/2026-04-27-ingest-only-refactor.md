# CiteIndex Ingest-Only Refactor Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Transform CiteIndex from a monolithic research system into a focused CLI + Python API tool that does one thing well: ingest sources with proper citation.

**Architecture:** Strip everything except the ingestion pipeline. The CLI becomes a single-command `citeindex <path_or_url>` tool. The Python API exposes `citeindex.ingest()` as the primary callable. All search, chat, memory, agents, Rust, TUI, plugin, and agent-harness code is removed.

**Tech Stack:** Python 3.12+, argparse (CLI), dataclasses (models), existing extraction deps (GROBID, MinerU, DSPy, etc.)

---

## What Stays vs What Goes

### KEEP (ingestion-only)

| Path | Reason |
|------|--------|
| `citeindex/ingestion/` | Core ingestion pipeline — the entire point |
| `citeindex/file_converter.py` | Office/DJVU → PDF conversion, used by `master.py` |
| `citeindex/model.py` | `CitationLLM`, `ParseCitationString` — DSPy extraction used by pipelines |
| `citeindex/llm.py` | `get_llm_model()` — used by `dspy_extract.py` and `url_article.py` |
| `citeindex/utils.py` | `to_csl_json`, `ensure_searchable_pdf`, `parse_page_range`, etc. — used by pipelines |
| `citeindex/search.py` | `search_for_missing_info()` — used by `common.py` for citation metadata |
| `citeindex/citation_style.py` | `format_bibliography()` — used for CSL rendering in markdown export |
| `citeindex/type_judge.py` | `determine_document_type()` — used by `common.py` |
| `citeindex/vertical_handler.py` | CJK vertical text detection — used by `scanned_pdf.py` |
| `citeindex/vertical_llm.py` | `VerticalCitationLLM` — used by `common.py` |
| `citeindex/ocr_lang_detect.py` | Language detection for OCR — used by `scanned_pdf.py` |
| `citeindex/ocr_text_clean_before_llm.py` | OCR text cleaning — used by `scanned_pdf.py` |
| `citeindex/page_extractor.py` | Page number extraction from MinerU — used by `digital_pdf.py` |
| `citeindex/styles/` | CSL style files for `citation_style.py` |
| `pyproject.toml` | Build config — needs heavy editing |
| `.gitignore` | Needs corpus/ rule kept/added |

### DELETE (non-ingestion)

| Path | Reason |
|------|--------|
| `citeindex-rs/` | Entire Rust workspace — TUI, kernel, plugins, all gone |
| `agent-harness/` | REPL, session, project commands — all gone |
| `citeindex/agents/` | 20+ agent modules (chat, search, memory, retrieval, generation, integrity, v12_runtime, etc.) |
| `citeindex/main.py` | Legacy `CitationExtractor` (URL-only, superseded by ingestion pipeline) |
| `citeindex/cli.py` | Will be **rewritten** — current has search/chat/memory/plugin subcommands |
| `.agent/` | Pipeline YAML definitions, schemas, skills for the full system |
| `instruction/` | Contracts, summaries, PDFs for the full system |
| `example/` | 56 example CSL-JSON files — reference data for removed features |
| `docs/project-report.md` | Describes the full system; will be replaced |
| `tests/test_style.py` | Tests for bibliography formatting |
| `tests/` backup files | `.backup`, `.backup2`, `.backup3` files — debris |
| `citeindex/tests/` | Agent/v12 tests — for removed features |
| `Citation-Extractor-logo.PNG` | Unnecessary asset |
| `Agent.md` | Agent system documentation |
| `logs/` | Runtime logs |
| `dist/` | Build artifacts |
| `.swarm/`, `.ropeproject/` | IDE/tool debris |

---

## Task Breakdown

### Task 1: Delete the Rust workspace

**Files:**
- Delete: `citeindex-rs/` (entire directory)

**Step 1: Remove the directory**

```bash
rm -rf citeindex-rs/
```

**Step 2: Commit**

```bash
git add -A
git commit -m "refactor: remove Rust workspace (core, kernel, tui, plugins)"
```

---

### Task 2: Delete the agent-harness package

**Files:**
- Delete: `agent-harness/` (entire directory)

**Step 1: Remove the directory**

```bash
rm -rf agent-harness/
```

**Step 2: Commit**

```bash
git add -A
git commit -m "refactor: remove agent-harness package"
```

---

### Task 3: Delete the agents directory

**Files:**
- Delete: `citeindex/agents/` (entire directory, all 22 files)

**Step 1: Remove the directory**

```bash
rm -rf citeindex/agents/
```

**Step 2: Commit**

```bash
git add -A
git commit -m "refactor: remove agents directory (chat, search, memory, retrieval, integrity, v12)"
```

---

### Task 4: Delete non-ingestion top-level modules

**Files:**
- Delete: `citeindex/main.py` (legacy CitationExtractor)

**Step 1: Remove the file**

```bash
rm citeindex/main.py
```

**Step 2: Commit**

```bash
git add -A
git commit -m "refactor: remove legacy main.py CitationExtractor"
```

---

### Task 5: Delete contract/instruction/spec/system files

**Files:**
- Delete: `.agent/` (entire directory)
- Delete: `instruction/` (entire directory)
- Delete: `example/` (entire directory)

**Step 1: Remove directories**

```bash
rm -rf .agent/ instruction/ example/
```

**Step 2: Commit**

```bash
git add -A
git commit -m "refactor: remove contracts, instructions, example data"
```

---

### Task 6: Delete debris and unrelated files

**Files:**
- Delete: `Citation-Extractor-logo.PNG`
- Delete: `Agent.md`
- Delete: `logs/`
- Delete: `dist/`
- Delete: `.swarm/`
- Delete: `.ropeproject/`
- Delete: `docs/project-report.md`
- Delete: `docs/plans/2026-04-21-*` (old plans for the full system)
- Delete: all `tests/*.backup` and `tests/*.backup2`, `tests/*.backup3` files
- Delete: `tests/main.py.backup*`
- Delete: `tests/ocr_lang_detect.py.backup*`
- Delete: `tests/utils.py.backup*`
- Delete: `tests/vertical_handler.py.backup*`
- Delete: `tests/vertical_llm.py.backup*`
- Delete: `citeindex/tests/` (agent/v12 tests for removed features)
- Delete: test PDF/DJVU files in `tests/` (they're test fixtures for removed features)

**Step 1: Remove all debris**

```bash
rm -f Citation-Extractor-logo.PNG Agent.md
rm -rf logs/ dist/ .swarm/ .ropeproject/
rm -f docs/project-report.md
rm -f docs/plans/2026-04-21-cli-anything-citeindex-design.md
rm -f docs/plans/2026-04-21-cli-anything-citeindex-impl-plan.md
rm -f tests/*.backup tests/*.backup2 tests/*.backup3
rm -f tests/main.py.backup tests/main.py.backup2 tests/main.py.backup3
rm -f tests/ocr_lang_detect.py.backup tests/ocr_lang_detect.py.backup2 tests/ocr_lang_detect.py.backup3
rm -f tests/utils.py.backup tests/utils.py.backup2 tests/utils.py.backup3
rm -f tests/vertical_handler.py.backup tests/vertical_llm.py.backup
rm -rf tests/__pycache__
rm -rf citeindex/tests/
# Remove test fixture PDFs/DJVUs that are not .py
find tests/ -type f ! -name '*.py' -delete
```

**Step 2: Commit**

```bash
git add -A
git commit -m "refactor: remove debris, test fixtures, docs for removed features"
```

---

### Task 7: Remove corpus/ from git tracking

**Files:**
- Modify: `.gitignore` (ensure `corpus/` is listed)

**Step 1: Remove corpus from git tracking (keep local files)**

```bash
git rm -r --cached corpus/ 2>/dev/null || echo "corpus/ not tracked"
```

**Step 2: Ensure .gitignore has corpus/**

Verify `.gitignore` already contains `corpus/`. If not, add it.

**Step 3: Commit**

```bash
git add -A
git commit -m "refactor: remove corpus/ from git tracking"
```

---

### Task 8: Update .gitignore for ingest-only project

**Files:**
- Modify: `.gitignore`

**Step 1: Rewrite .gitignore**

```gitignore
# Python
__pycache__/
*.py[oc]
build/
dist/
wheels/
*.egg-info
.venv

# Environment
.env

# Corpus (user data, never committed)
corpus/

# Test output
test_output/

# IDE / editor
.vscode/
.idea/
*.swp
*.swo
*~

# Rust (no longer present, but prevent accidents)
target/
Cargo.lock
*.rs.bk
```

**Step 2: Commit**

```bash
git add .gitignore
git commit -m "refactor: update .gitignore for ingest-only project"
```

---

### Task 9: Rewrite cli.py as ingest-only single command

**Files:**
- Rewrite: `citeindex/cli.py`

**Step 1: Write new cli.py**

The new CLI is a single-command tool. `citeindex <path_or_url>` ingests directly. No subcommands.

```python
"""CiteIndex CLI — ingest sources with proper citation.

Usage:
    citeindex <path_or_url>              # ingest a file or URL
    citeindex <path_or_url> [options]    # ingest with options
"""

import argparse
import json
import logging
import sys

from citeindex.ingestion import CiteIndexIngestionOrchestrator
from citeindex.ingestion.models import IngestionConfig


def _configure_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="citeindex",
        description="CiteIndex — ingest sources with proper citation",
    )
    parser.add_argument(
        "input",
        help="Input file path or URL to ingest",
    )
    parser.add_argument(
        "--corpus-root",
        default="corpus",
        help="Corpus output root directory (default: corpus)",
    )
    parser.add_argument(
        "--schema-version",
        default="1.0.0",
        help="Schema version tag (default: 1.0.0)",
    )
    parser.add_argument(
        "--llm",
        default="ollama/qwen3",
        help="LLM model for citation extraction (default: ollama/qwen3)",
    )
    parser.add_argument(
        "--text-direction",
        "-td",
        choices=["horizontal", "auto", "vertical"],
        default="horizontal",
        help="Text direction for PDF processing (default: horizontal)",
    )
    parser.add_argument(
        "--vertical-lang",
        choices=["ch", "japan"],
        default="ch",
        help="Language for vertical text OCR: ch or japan (default: ch)",
    )
    parser.add_argument(
        "--lang",
        "-l",
        default="auto",
        help="OCR language (default: auto-detect)",
    )
    parser.add_argument(
        "--page-range",
        "-p",
        default="1-5, -3",
        help='Page range for extraction (default: "1-5, -3")',
    )
    parser.add_argument(
        "--type",
        "-t",
        choices=["book", "thesis", "journal", "bookchapter"],
        help="Override automatic document type detection",
    )
    parser.add_argument(
        "--no-layout",
        action="store_true",
        help="Disable layout analysis (column/footnote detection)",
    )
    parser.add_argument(
        "--is-primary",
        action="store_true",
        help="Mark source as primary (line-level granularity). Default: secondary (paragraph-level)",
    )
    parser.add_argument(
        "--use-pageindex",
        action="store_true",
        help="Use PageIndex LLM-driven tree building for section hierarchy (requires Ollama)",
    )
    parser.add_argument(
        "--pageindex-model",
        default="ollama/qwen3.5:cloud",
        help="LLM model for PageIndex tree building (default: ollama/qwen3.5:cloud)",
    )
    parser.add_argument(
        "--all-url-article",
        "-aua",
        action="store_true",
        help="Crawl the input URL and ingest all discovered article pages",
    )
    parser.add_argument(
        "--update-url-article",
        "-uua",
        action="store_true",
        help="Crawl and compare content hashes; skip unchanged, re-ingest updated",
    )
    parser.add_argument(
        "--crawl-depth",
        type=int,
        default=2,
        help="Max BFS crawl depth for --all-url-article (default: 2)",
    )
    parser.add_argument(
        "--crawl-max-pages",
        type=int,
        default=100,
        help="Max pages for --all-url-article (default: 100)",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="Enable verbose/debug logging",
    )

    args = parser.parse_args()
    _configure_logging(args.verbose)

    config = IngestionConfig(
        llm_model=args.llm,
        text_direction=args.text_direction,
        vertical_lang=args.vertical_lang,
        lang=args.lang,
        page_range=args.page_range,
        doc_type_override=args.type,
        use_layout_analysis=not args.no_layout,
        is_primary=args.is_primary,
        use_pageindex=args.use_pageindex,
        pageindex_model=args.pageindex_model,
    )

    orchestrator = CiteIndexIngestionOrchestrator(
        corpus_root=args.corpus_root,
        schema_version=args.schema_version,
    )

    if args.all_url_article or args.update_url_article:
        output = orchestrator.ingest_all_urls(
            root_url=args.input,
            config=config,
            update=args.update_url_article,
            max_depth=args.crawl_depth,
            max_pages=args.crawl_max_pages,
        )
    else:
        output = orchestrator.ingest(args.input, config=config)

    print(json.dumps(output, indent=2, ensure_ascii=False, sort_keys=True))
    sys.exit(1 if output.get("status") == "blocked" else 0)


if __name__ == "__main__":
    main()
```

**Step 2: Commit**

```bash
git add citeindex/cli.py
git commit -m "refactor: rewrite CLI as ingest-only single command"
```

---

### Task 10: Rewrite __init__.py for ingest-only API

**Files:**
- Rewrite: `citeindex/__init__.py`

**Step 1: Write new __init__.py**

Expose `ingest()` as the primary Python API, plus key types.

```python
"""
CiteIndex — ingest sources with proper citation.

Primary API:

    from citeindex import ingest
    result = ingest("paper.pdf")
    result = ingest("https://example.com/article", config=IngestionConfig(...))

CLI:

    citeindex paper.pdf
    citeindex https://example.com/article
"""

__version__ = "0.12.0"

from citeindex.ingestion import CiteIndexIngestionOrchestrator
from citeindex.ingestion.models import IngestionConfig, IngestionFailure, PipelineResult


def ingest(
    input_ref: str,
    corpus_root: str = "corpus",
    schema_version: str = "1.0.0",
    config: IngestionConfig | None = None,
) -> dict:
    """Ingest a file or URL and return structured citation data.

    Parameters
    ----------
    input_ref : str
        Path to a local file (PDF, Office, DJVU, media) or URL.
    corpus_root : str
        Root directory for storing ingested artifacts.
    schema_version : str
        Schema version tag.
    config : IngestionConfig, optional
        Ingestion configuration. Uses defaults if not provided.

    Returns
    -------
    dict
        Ingestion result with status, CSL JSON, Merkle tree, etc.
    """
    orchestrator = CiteIndexIngestionOrchestrator(
        corpus_root=corpus_root,
        schema_version=schema_version,
    )
    return orchestrator.ingest(input_ref, config=config)


__all__ = [
    "ingest",
    "CiteIndexIngestionOrchestrator",
    "IngestionConfig",
    "IngestionFailure",
    "PipelineResult",
]
```

**Step 2: Commit**

```bash
git add citeindex/__init__.py
git commit -m "refactor: rewrite __init__.py as ingest-only API"
```

---

### Task 11: Update pyproject.toml for ingest-only

**Files:**
- Modify: `pyproject.toml`

**Step 1: Rewrite pyproject.toml**

Key changes:
- Version bump to 0.12.0
- Description updated to reflect ingest-only purpose
- Remove dependencies only needed for removed features (search/chat/memory agents)
- Keep all ingestion-related deps
- Entry point stays `citeindex.cli:main`

```toml
[project]
name = "citeindex"
version = "0.12.0"
description = "Ingest sources with proper citation — PDF, URL, media, Office, DJVU"
authors = [{ name = "ajia", email = "yyjfwoaini@gmail.com" }]
dependencies = [
  "ocrmypdf>=16.10.4",
  "pymupdf[mupdf-third]>=1.26.3",
  "requests>=2.31.0",
  "python-dateutil>=2.8.0",
  "lxml>=4.9.0",
  "urllib3>=2.0.0",
  "trafilatura>=1.6.0",
  "pymediainfo>=7.0.1",
  "dspy-ai>=2.6.27",
  "pypinyin>=0.51.0",
  "citeproc-py>=0.7.0",
  "crawl4ai>=0.7.0",
  "yt-dlp>=2025.7.21",
  "paddleocr>=3.1.0",
  "paddlepaddle>=3.1.0",
  "setuptools>=80.9.0",
  "fasttext>=0.9.2",
  "playwright>=1.40.0",
  "readability-lxml>=0.8.1",
  "whisperx>=3.1.0",
  "pyannote-audio>=3.1.0",
  "jsonschema>=4.20.0",
  "pyyaml>=6.0.0",
  "mineru[all]>=2.6.4",
  "litellm>=1.83.0",
  "PyPDF2>=3.0.1",
  "python-dotenv>=1.1.0",
]
readme = "README.md"
requires-python = ">= 3.12"
license = "MIT"

[project.scripts]
citeindex = "citeindex.cli:main"

[build-system]
requires = ["hatchling==1.26.3", "hatch-vcs"]
build-backend = "hatchling.build"

[tool.rye]
managed = true
dev-dependencies = ["pytest>=7.0.0", "pytest-cov>=2.12.1"]

[tool.hatch.build.targets.wheel]
packages = ["citeindex"]
```

**Step 2: Commit**

```bash
git add pyproject.toml
git commit -m "refactor: update pyproject.toml for v0.12.0 ingest-only"
```

---

### Task 12: Clean up schema_validator.py — remove references to .agent/

**Files:**
- Modify: `citeindex/ingestion/schema_validator.py`

**Step 1: Simplify schema_validator.py**

The `.agent/schema/` directory was deleted. This module currently walks up from the file to find `.agent/schema/stage/`. Since those schemas are gone, the validator should become a no-op or be removed entirely.

Decision: **Remove it.** Schema validation was best-effort anyway and the schemas are gone. The `master.py` import is guarded with `try/except` and already tolerates absence.

```bash
rm citeindex/ingestion/schema_validator.py
```

Also remove the 3-line import block from `master.py`:

In `citeindex/ingestion/master.py`, remove lines ~104-111:

```python
            # Phase 1.5: Schema validation (best-effort)
            try:
                from .schema_validator import validate_ingestion_output

                errors = validate_ingestion_output(output)
                if errors:
                    logger.warning("Schema validation warnings: %s", errors)
                    output["schema_validation_warnings"] = errors
            except Exception:
                pass
```

**Step 2: Commit**

```bash
git add -A
git commit -m "refactor: remove schema_validator and its usage (.agent/ schemas deleted)"
```

---

### Task 13: Clean up citation_style.py — remove if unused by ingestion

**Files:**
- Examine: `citeindex/citation_style.py`
- Examine: `citeindex/ingestion/markdown_export.py` (uses `format_bibliography`)

**Context:** `citation_style.py` is imported by `markdown_export.py` which IS part of ingestion. So we **keep** `citation_style.py` and `styles/`.

No changes needed — just verify the dependency chain is intact.

---

### Task 14: Verify the ingestion pipeline runs end-to-end

**Step 1: Run a smoke test**

```bash
cd /home/ajiap/project/citeindex
python -c "from citeindex import ingest; print('API import OK')"
python -c "from citeindex.cli import main; print('CLI import OK')"
python -c "from citeindex.ingestion import CiteIndexIngestionOrchestrator; print('Orchestrator import OK')"
```

**Step 2: If imports fail, fix the import chains**

Common breakages after removing `agents/`, `main.py`:
- Any `from citeindex.agents` → remove or replace
- Any `from citeindex.main` → remove or replace
- `__init__.py` references to removed modules → already handled in Task 10

**Step 3: Run existing tests**

```bash
cd /home/ajiap/project/citeindex
python -m pytest tests/ -v --tb=short 2>&1 | head -80
```

Note: Some tests may reference removed modules. Either delete those tests or skip them.

**Step 4: Commit any fixes**

```bash
git add -A
git commit -m "refactor: fix import chains after removing non-ingestion modules"
```

---

### Task 15: Update README.md

**Files:**
- Rewrite: `README.md`

**Step 1: Write new README**

The README should reflect the ingest-only purpose. Keep it minimal — installation, quick start, supported formats, Python API reference.

```markdown
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
```

**Step 2: Commit**

```bash
git add README.md
git commit -m "refactor: rewrite README for ingest-only CiteIndex v0.12.0"
```

---

### Task 16: Final cleanup — remove empty directories, stale imports

**Step 1: Check for any remaining references to removed modules**

```bash
cd /home/ajiap/project/citeindex
grep -rn "from citeindex.agents" citeindex/
grep -rn "from citeindex.main" citeindex/
grep -rn "import main" citeindex/
grep -rn "from .agents" citeindex/
grep -rn "schema_validator" citeindex/
```

**Step 2: Remove any remaining `__pycache__` directories**

```bash
find citeindex/ -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
find tests/ -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null
```

**Step 3: Remove empty directories**

```bash
# Check if tests/ has any .py files left
ls tests/*.py 2>/dev/null || echo "No test files"
```

If `tests/` is empty or only has debris, remove it entirely.

**Step 4: Final verification**

```bash
python -c "
from citeindex import ingest, IngestionConfig
from citeindex.cli import main
from citeindex.ingestion import CiteIndexIngestionOrchestrator
from citeindex.ingestion.models import PipelineResult, IngestionFailure
print('All imports OK')
"
```

**Step 5: Commit**

```bash
git add -A
git commit -m "refactor: final cleanup, remove stale imports and empty directories"
```

---

## Summary of Changes

| What | Action |
|------|--------|
| `citeindex-rs/` | **DELETE** — entire Rust workspace |
| `agent-harness/` | **DELETE** — REPL/session package |
| `citeindex/agents/` | **DELETE** — all 22 agent modules |
| `citeindex/main.py` | **DELETE** — legacy CitationExtractor |
| `citeindex/cli.py` | **REWRITE** — single-command ingest-only CLI |
| `citeindex/__init__.py` | **REWRITE** — expose `ingest()` API |
| `citeindex/ingestion/schema_validator.py` | **DELETE** — schemas removed |
| `.agent/` | **DELETE** — pipeline definitions for removed features |
| `instruction/` | **DELETE** — contracts for removed features |
| `example/` | **DELETE** — example CSL-JSON files |
| `pyproject.toml` | **UPDATE** — v0.12.0, ingest-only description |
| `README.md` | **REWRITE** — ingest-only docs |
| `.gitignore` | **UPDATE** — simplified for ingest-only project |
| `corpus/` | **GIT RM --CACHED** — remove from tracking |
| Debris files | **DELETE** — logos, backups, logs, dist, .swarm, etc. |
| `tests/` test fixtures | **DELETE** — PDF/DJVU fixtures, .backup files |

**Total files deleted:** ~200+ (Rust crates, agent modules, contracts, examples, debris)
**Total files kept:** ~25 (ingestion pipeline, supporting modules, styles, pyproject.toml)