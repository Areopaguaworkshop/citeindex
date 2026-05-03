# Scanned PDF OCR Backend Plan

## Goal

Replace the current hardwired scanned-PDF flow with an explicit scanned-document backend architecture.

Target behavior:

- Remove `ocrmypdf` as a selectable engine.
- Make `mineru` the default scanned-PDF backend.
- Allow `glm-ocr` as an optional backend.
- Do not route scanned PDFs through the current digital PDF pipeline.
- Generate `document.json`, `library/*.md`, image exports, citation metadata, Merkle artifacts, and optional `pageindex_tree.json` directly from the scanned-document backend outputs.

## Desired User Experience

Examples:

```bash
# default scanned backend
citeindex scanned.pdf

# explicit MinerU
citeindex scanned.pdf --ocr-engine mineru

# explicit GLM-OCR via local Ollama
citeindex scanned.pdf --ocr-engine glm-ocr

# optional model override for GLM-OCR path
citeindex scanned.pdf --ocr-engine glm-ocr --ocr-model glm-ocr:latest
```

Proposed CLI/config shape:

- `--ocr-engine` with values: `mineru`, `glm-ocr`
- default `ocr_engine="mineru"`
- `--ocr-model` for model-backed OCR engines, initially restricted to `glm-ocr`
- `--ollama-host` for local GLM-OCR service, default `http://localhost:11434`
- `--mineru-backend` to choose MinerU execution backend when needed, default `pipeline`

## Locked Requirements

The following decisions are now fixed for the first implementation:

- preserve only CiteIndex artifacts in the corpus
- `mineru` is the default scanned backend
- `glm-ocr` is the only optional model-backed scanned backend for now
- `glm-ocr` starts with layout-aware region OCR, not page-only OCR
- `glm-ocr` should use an external layout detector from the start rather than local heuristics
- export only extracted images, not full page snapshots by default
- PageIndex runs by default on scanned-PDF backends
- scanned PDFs do not use GROBID
- scanned citation metadata should come from structured MinerU / GLM-OCR outputs via DSPy-backed extraction
- scanned citation metadata should include translator, editor, series, and edition in the first version
- DSPy is allowed to overwrite pattern-extracted scanned metadata when it has better structured output
- scanned PDFs should keep the current `determine_doc_type()` heuristic
- GLM-OCR output should be normalized into a MinerU-like intermediate schema

## Current State

Today the scanned pipeline in `citeindex/ingestion/pipelines/scanned_pdf.py` does this:

1. optional vertical-text detection with PaddleOCR helpers
2. OCR normalization to a searchable PDF
3. a mostly unused OCR cleaning pass
4. handoff into `digital_pdf.run(...)`

That means scanned PDFs currently inherit the PyMuPDF-first digital pipeline instead of using a dedicated structured scanned-document pipeline.

## New Architecture

### High-Level Flow

```text
scanned_pdf.run()
  -> choose backend
     -> mineru backend (default)
     -> glm-ocr backend
  -> normalize backend output to CiteIndex document model
  -> citation extraction cascade
  -> optional PageIndex tree build
  -> write corpus artifacts
  -> generate library markdown
```

### Non-Goal

The scanned pipeline should no longer do this:

```text
scanned_pdf -> searchable pdf -> digital_pdf.run()
```

That coupling should be removed.

## Backend 1: MinerU Default

### Why MinerU

MinerU is already a layout-aware parser that can produce Markdown, JSON, reading-order content, table/formula/image extraction, and scanned-document OCR.

Relevant upstream signals:

- local CLI: `mineru -p <input> -o <output>`
- explicit pure-CPU backend: `mineru -p <input> -o <output> -b pipeline`
- structured outputs: Markdown plus rich JSON artifacts

### Proposed MinerU Flow

```text
PDF
  -> MinerU CLI/API
  -> middle JSON + content_list + markdown + extracted images
  -> convert MinerU output to CiteIndex document structure
  -> run citation extraction cascade on structured text
  -> run optional PageIndex on MinerU markdown / structure
  -> write corpus artifacts and library markdown
```

### Planned Output Mapping

MinerU outputs should be normalized into:

- `document.json`
- `csl.json`
- `merkle.json`
- `pageindex_tree.json` when enabled
- `pageindex_tree.json` with scanned backends enabled by default
- `library/<slug>.md`
- `images/` copied into the corpus folder

Raw MinerU-native artifacts should not be persisted in the first implementation;
the corpus should contain only CiteIndex-native artifacts.

### Document Model Strategy

Use MinerU as the source of truth for scanned-document structure:

- headings
- paragraphs
- footnotes
- tables
- figure captions
- reading order
- page numbers

The conversion layer should build CiteIndex-native `document.json` directly from MinerU JSON instead of flattening back to a digital-style page-text structure.

## Backend 2: GLM-OCR via Local Ollama

### Why GLM-OCR

`glm-ocr` is available in the Ollama library and is specialized for OCR/document understanding rather than being a generic vision model.

Relevant upstream signals:

- official Ollama library model: `glm-ocr`
- OCR-specific prompts:
  - `Text Recognition: ./image.png`
  - `Table Recognition: ./image.png`
  - `Figure Recognition: ./image.png`
- official deployment guidance recommends Ollama native `/api/generate` for stability

### Proposed GLM-OCR Flow

```text
PDF
  -> rasterize pages / candidate regions to images with PyMuPDF
  -> perform layout-aware region OCR with local Ollama glm-ocr
  -> collect OCR text + table/figure outputs
  -> build scanned document structure
  -> export extracted images into corpus/images/
  -> run citation extraction cascade
  -> run optional PageIndex tree build
  -> write corpus artifacts and library markdown
```

### Important Constraint

GLM-OCR in Ollama is image-driven, not PDF-native. The pipeline must therefore:

1. render PDF pages to images
2. submit those images to Ollama
3. reconstruct page-aware text/structure afterward

### First Implementation Scope

The first GLM-OCR version should focus on:

- layout-aware region OCR from the start
- explicit handling of text, figures, and tables as separate region classes when possible
- stable local execution over maximum speed

## PageIndex in the New Scanned Flow

Both backends should run PageIndex by default.

### MinerU + PageIndex

PageIndex should operate on MinerU markdown or equivalent structured text rather than on a re-extracted PDF text layer.

### GLM-OCR + PageIndex

PageIndex should operate on the normalized page/markdown output produced from GLM-OCR results.

### Storage

Both backends should persist:

- `pageindex_tree.json`
- heading-aware `document.json`
- heading-aware library markdown

## Image Export Requirement

Both backends must export images similar to the current digital pipeline.

### MinerU

Use MinerU-produced extracted images where possible, copy them into:

- `corpus/<slug>/images/`

and link them from `document.json` and library markdown.

### GLM-OCR

Persist only extracted images and figure/table crops needed for downstream references.

Minimum requirement:

- extracted figures / illustrations available in `corpus/<slug>/images/`

## Citation Extraction Plan

Scanned backends should use a scanned-document-specific citation cascade after OCR/layout parsing:

1. pattern extraction from backend-structured output
2. DSPy signature extraction from normalized backend text / markdown
3. file metadata fallback

GROBID should not be used for scanned PDFs in this new flow.

### Reuse From Existing Project

There is already a useful MinerU-oriented extraction module in
`citeindex/ingestion/pipelines/dspy_extract.py`:

- pattern extraction from `content_list`
- DSPy fallback via `ExtractDocumentMetadata`
- author parsing and CSL-compatible field shaping

The plan should reuse that work for the MinerU backend rather than inventing
a second metadata path.

### Planned Metadata Extraction Split

#### MinerU

Use a two-stage metadata path based on the existing code:

1. deterministic extraction from MinerU content/layout artifacts
2. DSPy fallback on MinerU markdown / normalized structured text

#### GLM-OCR

Introduce a parallel scanned-document DSPy signature for GLM-OCR normalized output.

Because GLM-OCR will not naturally emit MinerU-style `content_list`, the GLM-OCR
path will first be normalized into a MinerU-like intermediate schema, then flow
through the same metadata extraction path as much as possible.

### Expanded Metadata Scope

The first scanned-document metadata version should cover at least:

- title
- subtitle when present
- author
- translator
- editor
- series
- edition
- container-title
- publisher
- publication year / issued date
- volume
- issue
- page range
- DOI
- abstract
- keyword

### CSL Output

Both scanned backends should still normalize into the same CiteIndex CSL-shaped
output used elsewhere in the project: title, author, container-title, publisher,
issued year/date, volume, issue, page, DOI, abstract, keyword, and related fields.

## Library Markdown Plan

The scanned backends should generate library markdown from their own normalized structured outputs.

That markdown should include:

- YAML front matter
- inline citation
- visible page separators with `---`
- headings/subheadings from the scanned backend structure and optional PageIndex tree
- extracted text
- image references
- footnotes

The markdown should not depend on the digital PDF pipeline.

## Implementation Phases

### Phase 1: CLI and Config

1. add `ocr_engine` and related fields to `IngestionConfig`
2. add CLI flags
3. remove scanned-path dependence on `ocrmypdf`

### Phase 2: MinerU Default Backend

1. introduce a dedicated MinerU adapter in the scanned pipeline
2. normalize MinerU outputs into CiteIndex artifacts
3. export images
4. reuse existing pattern + DSPy metadata extraction where possible
5. generate library markdown directly from normalized structure

### Phase 3: GLM-OCR Backend

1. add Ollama client support for OCR images
2. add an external layout detector for region proposals
3. render PDF pages and layout regions to images
4. build layout-aware region OCR flow
5. normalize GLM-OCR outputs into a MinerU-like intermediate schema, then into CiteIndex artifacts
6. extend metadata extraction to cover translator / editor / series / edition fields
7. export extracted images and markdown

### Phase 4: PageIndex Integration

1. run PageIndex by default on normalized scanned output
2. persist `pageindex_tree.json`
3. inject heading structure into `document.json` and library markdown

### Phase 5: Comparison and Documentation

1. compare MinerU vs GLM-OCR on representative scanned PDFs
2. document tradeoffs in accuracy, layout fidelity, speed, and hardware requirements
3. update README and CLI help

## Risks

### MinerU

- CLI/API behavior has evolved and should be wrapped conservatively
- output formats may change across MinerU versions
- CPU/GPU backend selection needs explicit handling

### GLM-OCR

- Ollama path is convenient but may be slower than vLLM/SGLang
- PDF page rasterization quality will affect OCR accuracy
- layout reconstruction is more work than with MinerU

## Research Notes

### MinerU

- official repo: https://github.com/opendatalab/MinerU
- current local usage: `mineru -p <input> -o <output>`
- CPU mode: `-b pipeline`

### Ollama Vision

- official docs: https://docs.ollama.com/capabilities/vision
- image input is supported in CLI and API via `images`

### GLM-OCR

- Ollama model: https://ollama.com/library/glm-ocr
- official repo: https://github.com/zai-org/GLM-OCR
- Ollama deployment guide recommends `/api/generate`

## Open Questions

One implementation choice remains open:

1. For the external layout detector in the GLM-OCR backend, should the first implementation reuse the existing Paddle-based layout tooling already present in the repo, or should it introduce the GLM-OCR-recommended PP-DocLayoutV3 path directly?

## Recommendation

Recommended initial product behavior:

- default scanned backend: `mineru`
- optional scanned backend: `glm-ocr`
- no `ocrmypdf` backend option
- no scanned handoff into `digital_pdf.run()`
- both backends responsible for extracted images, structure, citation enrichment, PageIndex integration, and library markdown
