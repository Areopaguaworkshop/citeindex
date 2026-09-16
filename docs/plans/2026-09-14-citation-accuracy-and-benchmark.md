# CiteIndex source-citation accuracy plan

Updated 2026-09-16 after scope clarification. This checklist supersedes the
earlier bibliography/reference-extraction proposal.

## Scope

CiteIndex extracts citation metadata **of the ingested source itself**, including
the particular edition/version. “Native DSPy bibliography parsing” means parsing
works cited *inside* a source: it is explicitly **out of scope**, as are
citation-marker linking, cited-reference scoring, and document question answering.

DSPy remains the default digital-PDF engine. GROBID is a separately installed,
explicitly selected alternative, never an automatic fallback. A DSPy signature
is an extraction contract, not GROBID's trained model; architecture reuse does
not imply equal accuracy.

Borrow only these principles:

- GROBID: separate region selection, metadata parsing, validation, and finalization.
- PaperQA2: gather original evidence, rank, extract, and refine with a fixed budget.
- PageIndex: find title/imprint/colophon regions; summaries are navigation, not evidence.

Upstream references retained from the original research:
[GROBID](https://github.com/grobidOrg/grobid),
[Paper-QA](https://github.com/Future-House/paper-qa),
[PageIndex](https://github.com/VectifyAI/PageIndex).
No new upstream comparison or accuracy experiment was run for this implementation.

## Implementation checklist

- [x] Remove automatic native cited-reference parsing and numeric marker linking
  from digital and scanned ingestion.
- [x] DSPy signature returns typed CSL objects and per-field original-block quotes.
  Preserve author/editor/translator roles and ordered name arrays.
- [x] Share host field/type and quote-span validation. Reject invented block IDs,
  unsupported values, and fields outside the host schema.
- [x] Retain original digital text blocks and structured OCR blocks with physical
  page coordinates, separately from printed page labels.
- [x] Rank source blocks cheaply and use digital PageIndex physical-page ranges.
  Negative bibliography hints reduce cited-work contamination.
- [x] Bound native extraction to two predictor invocations and 12,000 text
  characters per evidence batch. Provider-internal retries are separate.
- [x] Require source digest, current old value, allowed field/value, exact quote
  and locator for repair proposals.
- [x] Re-finalize accepted repairs through normal ingestion, synchronizing CSL,
  embedded pipeline CSL, document title, PageIndex citation root, hashes,
  destination and Markdown. Older output directories remain untouched.
- [x] Audit proposal template and explicit harness verification instructions;
  raw CLI does not start an independent audit.
- [x] Noninteractive ingestion cannot wait for an author prompt.
- [x] Resolve MinerU beside the virtualenv interpreter without resolving its symlink.
- [x] Benchmark runner: append-only attempt history, exclusive output lock,
  per-source logs, heartbeat, 5,400-second deadline, descendant cleanup,
  blocked-result error capture, explicit retry and engine flags.
- [x] Host-only scorer: typed normalization, precision/recall/F1, exact records,
  source-span validity, reviewed attribution matches, failures/missing sources,
  modality results, latency, and unknown rather than fabricated costs.
- [x] Review/adjudication validation, work-family split tooling, report comparison.
- [ ] Complete the independently reviewed pilot, freeze its actual split, and run
  live baseline/candidate and explicit GROBID comparisons.
- [ ] Publish measured accuracy and measured cost/latency in README.

## Limits that remain visible

Span validation establishes that text exists at a location. It does **not**
prove that a title, year, or DOI belongs to the host rather than a cited work.
That requires source/edition review. Classification type is not normally a
literal printed string; its evidence is still an attribution judgment.

Pattern, filename and embedded-PDF fallbacks remain unverified candidates.
OCR mistakes cannot be repaired merely by matching an OCR quote. Scanned
PageIndex markdown structure is navigation; do not pretend line numbers are
physical PDF pages. Web and media use separate DSPy signatures and original
HTML/metadata or timed-transcript evidence, not the PDF extraction contract.
Each uses one call with a 12,000-character source-text budget. HTML script/JSON-LD
content is not included in native evidence blocks; provider candidates may
still expose that metadata as unverified. Contributor attribution still needs review.

### Modality contracts implemented (2026-09-16)

- [x] Web subtype, personal/corporate names, site versus publisher, full issued
  date precision, observed URL/accessed date, and revision-date exclusion.
- [x] Media subtype and contributor roles; uploader/platform are not authorship
  or publisher defaults. Event date and upload/publication date remain separate.
- [x] Observed duration/medium and separate timestamp quotation items; no fake
  transcript on transcription failure. Unknown recording subtype is `document`.
- [x] Shared typed field/evidence validation and re-finalization repair support
  web metadata locations and media segments as well as PDF pages.
- [x] Preserve enriched `csl.json`; add supported-subset `csl-export.json` item
  array with internal fields in `custom`. Existing Chicago style configuration
  remains unchanged; this is not a new citation-style renderer.
- [x] Source-type-aware benchmark profiles and offline role/date/locator tests.

Scanned PageIndex headings are reused for OCR-block ranking before extraction;
their markdown ranges are deliberately not used as physical-page coordinates.

Balanced budget is the default. No DSPy optimizer, extra ranking model, new QA
stack, or automatic GROBID fallback is added. Optimize only after development
labels establish a measurable need. Token pricing is not available for every
route; null cost means unmeasured, not free.

Repair re-ingests the source and rejects a stale proposal if extraction or
source bytes change. It is not an in-place edit of persisted CSL. Changing a
field does not certify the entire record.

## Validation and stop condition

Run offline fixtures before any real ingestion:

```bash
LITELLM_LOCAL_MODEL_COST_MAP=true .venv/bin/python -m pytest -q
```

Tests must cover forged digest/locator/value proposals, physical versus printed
pages, no invented DSPy values, bounded calls, retained failed attempts,
concurrent writer rejection, timeout cleanup, and consistent final artifacts.
Passing fixtures is implementation evidence, **not citation accuracy**.

Validation on 2026-09-16 before modality-contract additions: 84 offline tests passed (2 warnings); Python compile
check, git diff whitespace check, runner help, and audit skill validation passed.
No real source ingestion or paid model call was used for this validation.

After modality-contract additions: 92 offline tests passed (2 warnings), including
separate signatures, precise dates, contributor roles, media repair, CSL export,
empty failed transcripts, snapshot digests and forged timestamp rejection.
Compile and whitespace checks passed. These fixtures do not measure live accuracy.

User explicitly deferred live benchmarking until code is finished. Do not
start ingestion, deploy GROBID, invent human reviews, or publish numerical
accuracy from the existing failed attempts. See the
[multimodal pilot plan](2026-09-14-multimodal-citation-benchmark.md).
