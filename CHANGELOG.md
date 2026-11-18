# Changelog

## v0.16.0 — 2026-11-18

- Added MCP ingestion for PDFs, Office, DjVu, media, and URLs through the
  shared CLI pipelines, with status handles for longer jobs.
- Added source-bound citation replay and a host audit prompt for checking
  missing or disputed evidence against the original source.
- Added versioned PDF, HTML, and media quotation locators. When a quote has
  several matches, the MCP returns context and coordinates for user selection.
- Staged corpus and Markdown artifacts before publication and added rollback
  for publication failures.

## v0.15.2 — 2026-11-18

### Fixed

- Removed the invalid `pymupdf[mupdf-third]` extra from the base dependency.
  `mupdf-third` refers to source-build libraries, not a published pip extra, and
  produced `does not have an extra named 'mupdf-third'` warnings when resolving
  PyMuPDF 2.x. The dependency is now `pymupdf>=1.26.3`; deterministic digital-PDF
  ingestion was rechecked against PyMuPDF 2.1.

## v0.15.1 — 2026-11-18

HTML/URL pipeline hardening, CJK citation guidance, and the optional MCP
server. Published as one `citeindex` distribution; the MCP server is the
`[mcp]` extra, not a separate package.

### Added

- Deterministic HTML `<head>` metadata extraction in the URL article pipeline
  (Highwire Press / Google Scholar, Dublin Core, OpenGraph, Prism, Schema.org
  JSON-LD) with no external service or LLM required.
- `_field_evidence` binding and `source_snapshot_digest` (SHA-256) for HTML
  records; metadata now anchors title, DOI, container, publisher, issued,
  author, volume, issue, page, type, ISSN, and ISBN to source blocks.
- CJK citation guidance: 版权声明 / 版权申明 preambles used by gcdfl.org and
  ctcfol.org, bare 引用本文 / 引用此文 labels, and GB/T 7714
  `[J]/[D]/[M]/[C]/[N]/[R]/[S]/[P]/[EB/OL]` strings from CNKI/Wanfang/VIP
  exporters (authors, container, year, volume, issue, page, imprint, URL).
- Title-matched unlabeled GB/T self-citations; bibliography entries for other
  works are not adopted as the page's citation.
- MCP server: `citeindex[mcp]` extra and `citeindex-mcp` console script.
  Read-only library tools (`library_list`, `library_search`, `library_get`),
  `citation_verify`, `citation_format`, `citeindex_info`, write-gated
  `ingest_url`, corpus/skill resources, and `cite-this-source` /
  `ingest-verified` prompts. Supports stdio and Streamable HTTP across
  OpenCode, Codex, Pi, and OMP.
- The project `citation-verification` skill is packaged into the wheel and
  served as `citeindex://skills/citation-verification`.
- `use_llm_metadata` config flag so MCP/offline URL ingestion can skip DSPy
  metadata extraction; `ingest_url` uses deterministic metadata by default.
- Reproducible 20-URL no-LLM HTML benchmark (`benchmarks/html/`) with a
  manifest, scoring script, and recorded results.

### Changed

- JSON-LD titles prefer schema.org `name` over `headline`, fixing Wikimedia
  pages whose `headline` is a short description.
- Highwire `citation_conference_title` now maps to `paper-conference` instead
  of `article-journal`.
- Page ranges bind to joined first/last-page metadata spans and validate
  against both endpoints; metadata-block joins inside one `<head>` snapshot are
  allowed.
- MCP is released inside `citeindex` (extra `mcp`, script `citeindex-mcp`)
  rather than as a separate `citeindex-mcp` distribution.

### Fixed

- Policy headings such as “若要引用本文，请参考版权申明” no longer pre-empt the
  real citation block.
- Zero-padded Chinese dates (`2026年08月19日`) now count as source support.
- Base installs keep the `citeindex-mcp` entry point but exit with a clear
  `pip install 'citeindex[mcp]'` message instead of an import traceback.

## v0.15.0 — 2026-10-17

Production hardening (Project 4 of the Data Scientist Pivot plan;
plan: `docs/plans/2026-10-16-p4-production-hardening-plan.md`).

### Added

- GitHub Actions CI running ruff + mypy + pytest on Python 3.12 and 3.13
  (`.github/workflows/tests.yml`).
- Pytest configuration with a branch-coverage gate (`fail_under = 39`,
  ratcheting) and a `dev` extra (`ruff`, `mypy`, `pytest-cov`, `psutil`).
- `Dockerfile` (python:3.12-slim, non-root) + `.dockerignore`; README Docker usage.
- Structured logging: `--log-format {console,json}` via a stdlib JSON
  formatter (`citeindex/logging_config.py`).
- Scale benchmark: `benchmarks/scale/run.py` with recorded runs under
  `benchmarks/runs/` — 10,052 unique pages processed at 44.1 ok-pages/min
  wall (3 workers), with documented failure modes.
- README Quickstart; `docs/design.md` (pipeline stages, outputs, extension points).

### Changed

- Ruff configured at `E4,E7,E9,F,I,UP,B` (line-length 120; E501 excluded for
  legacy long lines) — ~1,150 mechanical fixes (pyupgrade, import sorting) and
  44 manual fixes; clean.
- mypy configured (pragmatic baseline; `citeindex.vertical_llm` excluded with
  a TODO) — 89 errors fixed; clean.
- Package metadata and README citation snippet bumped to 0.15.0.

### Fixed

- `CitationLLM` called `_truncate_text(max_tokens=...)`, a keyword that did
  not exist; corrected to `max_chars` (that path previously raised
  `TypeError`). Found by the type-check pass.
- Assorted latent issues surfaced by lint/typing (see the plan's execution
  record).

### Notes

- No changes to ingestion semantics; persisted `csl.json` remains read-only
  for humans and agents.
- Known follow-ups: no supported flag disables LLM citation extraction
  (dominant source of benchmark failures); `vertical_llm.py` cleanup; OCR-path
  benchmarking.
