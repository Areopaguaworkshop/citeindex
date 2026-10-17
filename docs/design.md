# CiteIndex design

Current as of 2026-10-16. CiteIndex is a deterministic ingestion engine:
source files/URLs in, CSL-JSON citations + a Merkle-verified corpus out.
Python owns extraction plumbing and validation; LLM use (DSPy extraction,
online enrichment fallback) is optional and provider-configurable.

## Pipeline stages

1. **Detect and route** — `citeindex/ingestion/master.py`
   (`detect_resource_type`, `route_to_pipeline`). Office and DJVU inputs are
   converted to PDF first.
2. **Extract per source type** — `citeindex/ingestion/pipelines/`:
   - `digital_pdf.py` — text and layout via PyMuPDF (+ optional
     `pymupdf4llm` layout when the `layout` extra is installed).
   - `scanned_pdf.py`, `mineru.py`, `glm_ocr.py` — OCR engines for scans.
   - `url_article.py` — HTML articles; optional crawling via `crawl`.
   - `media.py` — media transcription (WhisperX, or the Wenbi ASR path).
   - `grobid.py` — optional GROBID citation engine (external service).
   - Citation extraction: `dspy_extract.py` (default) or GROBID.
3. **Validate authors** — author fallback/validation in `master.py` and the
   CSL field rules in `csl.py`.
4. **Verify metadata** — `citation_verification.py` resolves candidate
   metadata against registries with evidence-traced corrections.
5. **Enrich** — `metadata_registry.py` (Crossref/OpenAlex/OpenLibrary/DataCite
   lookups, tiered merges) and `online_enrichment.py` (fuzzy candidate
   matching against a 0.90 default score; opt-in via `--online-enrich`).
6. **Finalize** — `standardize_csl_json` (hash-stable document id), Merkle
   manifest, corpus artifacts, `library/<id>.md`, `ingestion_output.json`.

Failures never abort the caller with a traceback: the orchestrator returns a
structured failure (`error_code`, `next_action`) instead.

## Outputs

- `corpus/<doc-id>/` — `csl.json` (+ `csl-export.json`), `document.json`,
  `source_blocks.json`, `merkle.json`, `online_enrichment.json`,
  `ingestion_output.json`.
- `library/<doc-id>.md` — human-readable record with the bibliography entry.
- `logs/<source>_<timestamp>.json` — per-run log.

The corpus root defaults to `corpus/` and is set with `--corpus-root`.

## Configuration

`citeindex/ingestion/models.py` `IngestionConfig` is the single schema; the
CLI flags in `cli.py` map onto it. Enrichment is opt-in
(`online_enrich=False` by default). `--log-format json` switches logs to
one JSON object per line on stderr.

## Extension points

- **New pipelines** — add a module under `ingestion/pipelines/` and route
  to it from `master.py`.
- **New metadata providers** — `metadata_registry.py` (`_PROVIDER_URLS`,
  `lookup`, `search`, and the CSL normalizers) emits candidates with
  provenance (`request_url`, digest, retrieval time).
- **Authority sources (entity normalisation, P2)** —
  `ingestion/authority/` (ORCID, Wikidata, GeoNames; VIAF probed and
  dropped 2026-10-17) plugs into the same registry dispatch; candidates
  carry `type` + `authority` blocks and are never work-shaped, so the
  work matcher rejects them until a name/place policy exists.
- **Plugins** — installed packages can register the `citeindex.plugins.v1`
  entry point and post-process a finished corpus (see
  `docs/plans/2026-10-14-plugin-system-fanyi.md`).
- **Agents** — the CLI is the stable interface: agents run `citeindex`, read
  `corpus/` and `library/`, and never hand-edit persisted `csl.json`.

## Guarantees and limits

- 379 pytest tests; branch-coverage gate in CI (≈40% today, ratcheting
  toward 85% as legacy modules gain tests).
- CI runs ruff + mypy + pytest on Python 3.12 and 3.13
  (`.github/workflows/tests.yml`).
- Reproducible dev setup: `pip install -e '.[dev]'`; containerized run via
  the `Dockerfile`.
- Limits: DSPy citation extraction works best against an LLM endpoint;
  scanned OCR needs the relevant extras; benchmark gold manifests are
  curated by hand and are not shipped.
