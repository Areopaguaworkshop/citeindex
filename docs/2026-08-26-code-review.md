# CiteIndex code and security review

**Date:** 2026-08-26  
**Scope:** current worktree, including the citation-verification changes and existing ingestion pipelines  
**Verdict:** Request changes before exposing the CLI to agent-driven or untrusted inputs.

## Executive summary

The local test suite passes, and reviewed subprocess calls do not use `shell=True`. However, the project still has correctness defects in PDF classification, evidence locators, and verification status semantics. URL ingestion is also an SSRF/resource-exhaustion boundary. Artifact writes are not collision-safe or atomic.

## Findings

### High

#### 1. URL ingestion permits SSRF and unbounded downloads

`detect_resource_type()` accepts loopback and cloud-metadata addresses as ordinary article URLs. Requests, Playwright, crawling, and `yt-dlp` then follow URLs without private-address rejection, redirect revalidation, content-type checks, or response-size limits.

Evidence: [master.py](../citeindex/ingestion/master.py:199), [url_article.py](../citeindex/ingestion/pipelines/url_article.py:48), [url_article.py](../citeindex/ingestion/pipelines/url_article.py:65), [url_crawler.py](../citeindex/ingestion/url_crawler.py:133).

Reproduced:

```text
http://127.0.0.1:8000/private       -> url_article
http://169.254.169.254/latest/...   -> url_article
```

Recommended fix: centralize URL policy; allow HTTP(S) only, reject userinfo and non-public resolved addresses, re-check every redirect, intercept browser subrequests, and enforce byte/time limits. Make private-network access an explicit opt-in.

#### 2. Verification can certify unverified metadata

A Crossref `not_found` result currently becomes `status="verified"` and `verified=true` when no field conflict is found. This was reproduced with a mocked Crossref 404.

Structured dates are flattened into independent substrings, so a full date such as `2019-01-01` can be accepted from text containing only `2019` and unrelated `1` characters.

Evidence: [citation_verification.py](../citeindex/ingestion/citation_verification.py:126), [citation_verification.py](../citeindex/ingestion/citation_verification.py:278).

Recommended fix: require `registry.status == "found"` for a verified registry result; otherwise report `needs_review` or an explicit `unverified` status. Validate dates as complete date tokens with field-specific normalization.

The model contract also needs an explicit decision: either restrict the selected value to the normalized draft/registry candidates, or define and validate a separate source-derived correction type.

#### 3. Evidence locators can point to the wrong paragraph

`attach_evidence_locators()` scans every node for matching text and chooses the first match. Repeated identical paragraphs therefore receive the same `node_id`; a two-paragraph synthetic input produced `['n1', 'n1']`. The scan is also quadratic for large documents.

Evidence: [common.py](../citeindex/ingestion/pipelines/common.py:184).

Recommended fix: preserve paragraph/line indexes from the pipeline and map by physical page plus ordinal position. Build a lookup index once and represent line ranges explicitly.

#### 4. Image coverage is silently disabled in PDF classification

`fitz` is imported locally inside `classify_pdf()` but referenced by `_get_bitmap_coverage()`. The resulting `NameError` is swallowed by a broad exception handler, returning zero image coverage. A synthetic full-page image returned `(0.0, 0)`.

Evidence: [pdf_classifier.py](../citeindex/ingestion/pdf_classifier.py:178).

Recommended fix: import the PyMuPDF module at module scope (or pass it explicitly), stop swallowing this programming error, and add digital/scanned/mixed fixture tests.

#### 5. Corpus artifacts can collide or be partially overwritten

Folder names are derived from truncated citation metadata without a unique suffix. `store_corpus_artifacts()` then writes individual files directly into that directory. Two records with the same author/year/title prefix can mix or overwrite artifacts, and interruptions can leave a partial corpus.

Evidence: [storage.py](../citeindex/ingestion/storage.py:47), [storage.py](../citeindex/ingestion/storage.py:95).

Recommended fix: append a stable content/Merkle hash, write to a staging directory, validate completion, and atomically rename it into place.

### Medium

#### 6. Temporary files and persisted output are inconsistent

URL and media pipelines set `cleanup_source_snapshot` but the master orchestrator does not remove the temporary files on success or failure. Temporary absolute paths can also be persisted. `ingestion_output.json` is written before `library_md_path` is added to the returned object.

Evidence: [url_article.py](../citeindex/ingestion/pipelines/url_article.py:600), [master.py](../citeindex/ingestion/master.py:132), [master.py](../citeindex/ingestion/master.py:173), [master.py](../citeindex/ingestion/master.py:185).

Recommended fix: use one master-level `finally`, remove temporary paths from serialized output, and write the final output only after all artifacts—including Markdown—exist.

#### 7. Importing the package has unwanted network/cache side effects

Running `citeindex --help` attempted a LiteLLM request to GitHub for a model cost map and attempted to initialize a disk cache. This comes from eager imports of heavy pipelines.

Evidence: [master.py](../citeindex/ingestion/master.py:11).

Recommended fix: lazy-load the selected pipeline/model and keep import/help/offline verification side-effect free.

#### 8. Dependency and static-quality debt is masking failures

Fresh checks reported:

- `47 passed`
- `17%` overall coverage
- `263` Ruff findings, including `48` undefined names
- `uv pip check`: three incompatibilities (`datasets`/`dill`, `datasets`/`fsspec`, `langchain-core`/`packaging`)
- Requests emitted a dependency compatibility warning

Notable undefined-name paths include [pdf_classifier.py](../citeindex/ingestion/pdf_classifier.py:178), [pageindex/utils.py](../citeindex/ingestion/pipelines/pageindex/utils.py:256), and repeated dead blocks in [vertical_llm.py](../citeindex/vertical_llm.py:325).

Recommended fix: make CI fail on undefined names and dependency conflicts, then add mocked tests for URL policy, Crossref 404s, date matching, duplicate/line locators, cleanup-on-failure, and artifact collisions.

#### 9. Automatic model/data downloads lack integrity controls

The fastText language model is downloaded at runtime over HTTPS without a timeout, pinned checksum, atomic replacement, or explicit user action.

Evidence: [ocr_lang_detect.py](../citeindex/ocr_lang_detect.py:26).

Recommended fix: package or prefetch the model, enforce a timeout and maximum size, verify a published SHA-256, and replace atomically.

## Security-positive observations

- No `shell=True`, `eval()`, `exec()`, `pickle`, or `verify=False` usage was found in the reviewed `citeindex/` paths.
- Subprocesses use argument arrays, which avoids shell-string injection.
- Citation model decisions require an allowed verdict, confidence, exact quote, and a valid evidence locator before application.
- Citation folder slugs remove path separators and unsafe punctuation.

## Recommended implementation order

1. Enforce URL/private-network and resource-size policy.
2. Fix verification status/date semantics and locator mapping.
3. Repair PDF classifier imports and add scanned/digital fixtures.
4. Make corpus finalization unique, staged, and atomic.
5. Clean temporary files and remove import-time network activity.
6. Add non-interactive automation mode for agent harnesses.
7. Tighten dependency/static checks and add focused integration coverage.
8. Sandbox Chromium, LibreOffice, OCR, MinerU, and media tools for untrusted inputs.

## Validation limitations

GROBID, Crossref, Playwright, MinerU, OCR, and model-provider integrations were not exercised live because they require external services or credentials. No source files were changed during the audit; this document is the review artifact.
