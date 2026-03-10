<p align="center">
  <img src="./Citation-Extractor-logo.PNG" alt="CiteIndex Logo" width="150">
</p>

<h1 align="center">CiteIndex</h1>

<p align="center">
  <strong>An AI research agent that never hallucinates — every claim is traced, verified, and cited.</strong>
</p>

<p align="center">
  <a href="#why-this-exists">Why</a> •
  <a href="#how-it-works">How It Works</a> •
  <a href="#quick-start">Quick Start</a> •
  <a href="#usage">Usage</a> •
  <a href="#architecture">Architecture</a> •
  <a href="#contributing">Contributing</a>
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-blue.svg" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/rust-1.75+-orange.svg" alt="Rust 1.75+">
  <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="License: MIT">
  <img src="https://img.shields.io/pypi/v/cite-extractor.svg" alt="PyPI version">
</p>

---

## Why This Exists

Large Language Models write fluently but **cannot cite their sources**. When an LLM tells you about a study, a historical event, or a legal precedent, there is no way to verify the claim, trace it back to a page, or reproduce the evidence chain. For scholars, this makes LLM output fundamentally unusable in serious work.

**CiteIndex is an AI research agent — like Claude Code, but for academic scholarship.** Instead of writing code, it reads your research materials, indexes them into a Merkle-verified knowledge base, and answers your questions with deterministic, trace-bound citations. Every claim maps to a specific text passage, verified by cryptographic hash, with a full Merkle proof from leaf node to document root.

### What CiteIndex does for researchers

- **Ingests any source** — PDFs (digital or scanned), URLs, DJVU, EPUB, DOCX, video/audio — into a structured, hash-verified corpus.
- **Answers questions** with Chicago author-date citations, where every inline reference traces to a specific passage in your documents.
- **Eliminates hallucination** by design: BM25 deterministic retrieval (no embeddings), mandatory evidence-to-claim mapping, and fail-closed integrity verification.
- **Handles CJK vertical text**, multi-column layouts, footnote isolation, and scanned documents with automatic OCR language detection.
- **Provides a terminal UI** (ratatui) for interactive research sessions: chat, search, ingest, and browse your citation graph — all from the terminal.

---

## How It Works

CiteIndex enforces a strict contract: **no claim without evidence, no evidence without a hash, no hash without a Merkle proof.**

```
Document → Ingest → Nodes (paragraph/line) → SHA-256 hashes → Merkle tree
                                                    ↓
Query → BM25 Retrieval → Ranked Evidence → Generation (LLM or extractive)
                                                    ↓
                                          Integrity Verifier (fail-closed)
                                                    ↓
                                          Answer + Chicago citations + Merkle proofs
```

**7 deterministic agents** form the pipeline:

1. **Ingestion** — Parse documents into structural nodes with hierarchical Merkle trees
2. **Indexing** — Build inverted index, section index, and cross-source links
3. **Query Planning** — Classify intent, detect ambiguity, emit search plan
4. **Retrieval** — Three-stage BM25: metadata filter → keyword search → trace filter
5. **Clarification** — Ask up to 3 questions when the query is ambiguous
6. **Generation** — Produce answers strictly from evidence, with Chicago citations
7. **Integrity** — Recompute hashes, verify Merkle proofs, resolve citation keys. Reject if any check fails.

---

## Quick Start

### Installation

```bash
pip install cite-extractor
```

### System Dependencies

```bash
# Ubuntu/Debian
sudo apt-get install tesseract-ocr mediainfo ffmpeg

# macOS
brew install tesseract mediainfo ffmpeg

# LLM backend (required for chat/generation)
curl -fsSL https://ollama.ai/install.sh | sh
ollama pull qwen3
```

### Optional Services

```bash
# GROBID — primary citation extraction (recommended)
docker run -d -p 8070:8070 lfoppiano/grobid:0.8.1

# Playwright browsers — for JS-rendered URL fetching
playwright install chromium

# Zotero translation-server — rich URL metadata
docker run -d -p 1969:1969 zotero/translation-server
```

---

## Usage

### Ingest documents

```bash
# Ingest a PDF into the corpus
citeindex ingest "research-paper.pdf"

# Scanned PDF with auto-detected OCR language
citeindex ingest "scanned-book.pdf" --lang auto

# Vertical CJK text
citeindex ingest "chinese-manuscript.pdf" --text-direction vertical

# Primary source (line-level granularity)
citeindex ingest "ancient-text.pdf" --is-primary

# URL article
citeindex ingest "https://www.nature.com/articles/s41586-023-06627-7"
```

### Search your corpus

```bash
# BM25 deterministic search
citeindex search "Kantian categorical imperative"

# Return more results
citeindex search "machine learning fairness" --top-k 50
```

### Chat with trace-bound citations

```bash
# Single-shot question
citeindex chat --prompt "What does the author argue about social contract theory?"

# Interactive chat session
citeindex chat

# Specify LLM backend
citeindex chat --llm ollama/qwen3 --prompt "Compare the two authors' positions on free will"
```

### Memory & plugins

```bash
# Search past conversations
citeindex memory search "social contract"

# List installed plugins
citeindex plugin list

# Install a plugin
citeindex plugin install ./my-plugin
```

### Terminal UI (Rust)

```bash
# Build and run the TUI
cd citeindex-rs && cargo run

# Keyboard shortcuts:
#   Ctrl+T  — toggle dark/light theme
#   Ctrl+B  — toggle side panel
#   /search — switch to search mode
#   /ingest — switch to ingest mode
#   /quit   — exit
```

---

## Architecture

CiteIndex is a **hybrid Rust + Python** system:

| Layer | Language | Role |
|-------|----------|------|
| **TUI & Orchestrator** | Rust | Terminal UI (ratatui), plugin system, memory DAG, Python IPC |
| **AI Engine** | Python | 7-agent pipeline, LLM integration, document parsing, OCR |
| **Storage** | Files | `corpus/{hash}/` with `csl.json`, `document.json`, `merkle.json` |

### Key design rules

- **No embeddings.** All retrieval is BM25 keyword search — deterministic and reproducible.
- **Merkle-verified.** Every text node has a SHA-256 hash. Document integrity is a Merkle tree: `line → paragraph → column → page → document`.
- **Fail-closed integrity.** The integrity verifier rejects answers where any hash, Merkle proof, or citation key fails to resolve.
- **Citation cascade.** GROBID (deterministic) → LLM extraction (fallback) → PDF metadata (last resort).

### Corpus layout

```
corpus/
└── {sha256-hash}/
    ├── csl.json          # CSL-JSON citation metadata
    ├── document.json     # Structural nodes (pages → columns → paragraphs → lines)
    ├── merkle.json       # Hierarchical Merkle tree
    └── retrieval.json    # Inverted index for BM25
```

---

## Contributing

```bash
git clone https://github.com/areopaguaworkshop/citation.git
cd citation

# Python development
pip install -e ".[dev]"
pytest

# Rust development
cd citeindex-rs
cargo build
cargo test
```

Contributions welcome — especially for:
- Additional citation styles beyond Chicago
- Language-specific OCR improvements
- New ingestion pipelines (e.g., EPUB, LaTeX)
- TUI enhancements (Refs mode, Project mode)

## License

MIT License

---

<p align="center">
  <em>Every claim deserves a source. Every source deserves a hash. Every hash deserves a proof.</em>
</p>
