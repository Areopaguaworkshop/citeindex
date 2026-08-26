# Citation Metadata Verification and Registry Reconciliation

**Goal:** Produce evidence-backed CSL metadata for every CiteIndex source by combining local extraction, identifier registries, and a focused model verifier before artifacts are persisted.

**Phase-one scope:** Digital PDFs, scanned PDFs, URLs, books, and scholarly records with a DOI. Media retains its current metadata path and is explicitly deferred from evidence verification. This plan does not make external lookup mandatory: offline and unavailable-service paths must retain the existing ingestion behavior.

## Target workflow

```text
source → resource pipeline → candidate CSL → author validation/fallback
  → source evidence + DOI extraction → Crossref lookup → deterministic reconciliation
  → model conflict resolver → verification report → standardize/hash/store/render
  → optional harness audit (Codex, Claude Code, OpenCode, Pi)
```

The verifier and all existing author fallback run before `standardize_csl_json()` in `CiteIndexIngestionOrchestrator.ingest()`. This ensures any accepted correction is reflected in the CSL ID, content hash, folder name, ingestion output, and library Markdown. Artifacts are consistently finalized in this order; this plan does not promise filesystem transactions.

## Field evidence and precedence

Resolve each field independently. Never accept a registry or model value without supporting evidence.

```text
publisher “Cite this” guidance / title page / imprint
  > exact identifier registry record
  > high-confidence bibliographic registry match
  > GROBID or structured web metadata
  > OCR or draft LLM extraction
```

The accepted fields are `author`, `title`, `issued`, `publisher`, `publisher-place`, `container-title`, `DOI`, `URL`, and `page`. Each accepted change must include a quotation and a persisted non-CSL evidence record:

```text
source_digest                # source PDF/content digest; URL snapshot digest when applicable
snapshot_artifact            # "document.json" or persisted URL "source.html"
physical_page_index       # zero-based PDF/OCR page, if applicable
printed_page_label        # optional page label rendered by the source
node_id                   # stable page/paragraph/content node
char_start, char_end      # offsets into the stable node
quote
bbox                      # optional PDF/OCR coordinates
```

`CSL.page` remains a bibliographic page range, not a source locator. Conflicts without evidence are `needs-review`.

## Registry rollout

### Phase one: DOI only

| Available evidence | Lookup | Acceptance rule |
| --- | --- | --- |
| DOI | Crossref `/works/{doi}` | Exact normalized DOI match, still recorded as provenance |

### Later phases

DataCite, NCBI/PMID, arXiv, Open Library/ISBN, OpenAlex/ISSN, bibliographic title search, and URL-specific registries use the same contract only after the Crossref slice is proven. URL source evidence remains current page metadata, explicit citation guidance, and the persisted HTML snapshot.

External responses are provenance-bearing candidates. The source remains authoritative when it conflicts with a registry.

## Implementation tasks

### 1. Add verification models and configuration

**Files:** `citeindex/ingestion/models.py`, `citeindex/cli.py`

- Add `verify_citations`, `citation_verifier_model`, `crossref_enabled`, `offline_verification`, and a contact-email setting for polite registry requests to `IngestionConfig`.
- Add CLI flags: `--verify-citations`, `--citation-verifier-model`, `--no-crossref`, and `--offline-verification`.
- Keep verification opt-in initially; current ingestion remains the fallback when disabled or unavailable. `offline_verification` blocks every remote registry and verifier-model request, but does not change existing extraction, PageIndex, GROBID, or URL-fetch behavior.

### 2. Build a small registry client module

**New file:** `citeindex/ingestion/metadata_registry.py`

- Use the already-installed `requests` package.
- Normalize DOI candidates before lookup.
- Implement bounded timeouts and no retry loop beyond a single safe retry.
- Return normalized CSL candidates plus provider, request identifier, response status, and raw-response digest. Do not persist raw responses or contact email.
- Never send document text or PDFs to a registry; send identifiers or short bibliographic query fields only.

### 3. Collect candidate evidence from every pipeline

**Files:** `citeindex/ingestion/pipelines/common.py`, `citeindex/ingestion/pipelines/digital_pdf.py`, `citeindex/ingestion/pipelines/scanned_pdf.py`, `citeindex/ingestion/pipelines/url_article.py`

- Preserve the current GROBID, OCR, URL metadata, and in-page citation-guidance results as candidates rather than final truth.
- Capture source excerpts and stable node/offset locators for title, author, publisher, date, DOI, and page ranges. PDF/OCR evidence includes physical page index, optional printed label, and optional bbox.
- Persist a URL HTML snapshot into the corpus and record its digest before using it as evidence; a temporary path is not a locator.
- For born-digital scholarly articles, use GROBID as the preferred first candidate; it remains optional and is never used for scans or generic web pages.

### 4. Reconcile deterministically before involving a model

**New file:** `citeindex/ingestion/citation_verification.py`

- Compare candidate values field-by-field using the precedence rules above. Registry-only metadata without a source quote remains a candidate/`needs-review` rather than an automatic correction.
- Accept exact identifier matches and source-supported values without an LLM call.
- Mark absent, contradictory, or low-score matches for model resolution.
- Produce a typed verification report with `verified`, `corrected`, and `needs-review` outcomes.

### 5. Add the focused strong-model conflict resolver

**Files:** `citeindex/ingestion/citation_verification.py`, `citeindex/model.py`

- Use an explicit provider-qualified, fail-closed LiteLLM/DSPy model factory. Keep `citation_verifier_model` separate from the low-cost extraction model.
- Send only the disputed field, candidate values, registry values, and relevant source excerpts.
- Require schema-constrained output: selected value, exact quotation, locator, confidence, rationale, and verdict.
- Reject any response that changes a field without evidence. An unavailable credential, invalid model identifier, or `offline_verification` mode yields `needs-review` and never falls back to the draft model. The model resolves ambiguity; it does not invent bibliographic facts.

### 6. Persist verified data consistently with normal artifacts

**Files:** `citeindex/ingestion/master.py`, `citeindex/ingestion/storage.py`, `citeindex/ingestion/markdown_export.py`

- Move existing author validation and fallback before `standardize_csl_json()`, then apply accepted verification changes before folder naming.
- Write `citation_verification.json` alongside `csl.json`.
- Include a compact verification status in `ingestion_output.json` and library Markdown front matter.
- Leave final CSL unchanged when the verdict is `needs-review`, but persist the disagreement and evidence.

### 7. Complete the cross-harness final audit

**Files:** `.agents/skills/citation-verification/SKILL.md`, `.claude/skills/citation-verification/SKILL.md`, `.codex/skills/citation-verification/SKILL.md`, `.opencode/agents/citation-verifier.md`, `.opencode/commands/ingest-verified.md`, `AGENTS.md`

- Keep one shared verification policy with harness-specific discovery entry points.
- After agent-driven ingestion, the skill reviews `citation_verification.json` and source evidence independently. A raw `citeindex` subprocess cannot infer or invoke Codex/OpenCode/Claude/Pi merely from its parent terminal; only an explicit harness command, skill, or wrapper runs this audit.
- The audit never edits persisted `csl.json` directly. A later repair command must re-run finalization so dependent hashes and Markdown remain valid.
- Plain `citeindex` use receives the core verification stage; the agent audit is an additional final stage only when a harness runs the ingestion.

### 8. Test without live external dependencies

**Files:** new unit tests under `tests/`

- Mock Crossref responses only.
- Cover exact DOI, malformed response, registry timeout, `offline_verification` mode, and source-vs-registry conflict.
- Assert that an accepted correction and author fallback finalize before CSL hash, ID, folder path, JSON artifacts, and Markdown are generated; no artifact mutates after persistence.
- Add fixtures for a digital article, scanned/CJK book, URL with explicit citation guidance, and a conflicting online/print date.

## Acceptance criteria

1. A DOI-backed article returns one CSL record with Crossref provenance and source evidence.
2. Registry-only metadata or an ambiguous match produces `needs-review`, never an automatic guess.
3. `--offline-verification` performs no Crossref or strong-verifier traffic and leaves existing pipeline fetching/extraction behavior unchanged.
4. Accepted corrections occur before hash, folder, artifact, and Markdown generation.
5. Every harness can load the shared verification skill; only an explicit agent-driven command/skill/wrapper invokes the independent audit stage.
6. Tests use recorded/mock responses and do not require live registry accounts or services.

## Deliberate deferrals

- DataCite, NCBI, arXiv, Open Library, OpenAlex, bibliographic title matching, and media verification are introduced after the Crossref contract and tests are stable; the common registry interface avoids pipeline-specific integrations.
- No custom cache service, database, or background queue. Add caching only if registry rate limits or repeated batch lookups require it.
- No direct post-hoc artifact editor. Add a repair command only after finalization can regenerate all dependent artifacts safely.

## Implementation status

The phase-one tasks above are implemented: pipeline-specific stable locators,
field-aware reconciliation with source precedence, constrained per-field DSPy
review, all-or-nothing CSL finalization, explicit Codex/Claude Code/OpenCode/Pi
harness entry points, and mocked digital/scanned/CJK/URL/date-conflict tests.
The registry integrations listed under “Later phases” remain intentionally
deferred.
