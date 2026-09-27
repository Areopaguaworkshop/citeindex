# Digital-PDF Citation Extraction Improvement Plan — 2026-09-26

**Status**: Approved for implementation
**Scope**: Digital-PDF citation extraction accuracy only. The scanned-path pattern fallback bug (`dspy_extract.py:918-921`) is explicitly out of scope.
**Benchmark model**: `ollama/deepseek-v4.1-flash:cloud` — the user's instruction "do the benchmark test again" is the approval gate for cloud usage (cloud runs previously worked on this machine on 2026-09-24/25).

## 1. Measured Baseline (2026-09-25 run, 16 digital sources)

Source: `benchmarks/citations/predictions/citation-pages-digital-report-2026-09-25.json`

- exact_record_accuracy 0.0625 (1/16), equivalent_record_accuracy 0.125 (2/16)
- Field F1: title 0.125 (tp=2/fp=14/fn=14), type 0.812, author 0.538, editor 0.750, translator 0.800, issued 0.800 (5 missing), publisher 0.833, publisher-place 0.889, container-title 0.500, collection-title 0.667, original-date 1.0
- `field_errors`: title {different: 14}, issued {missing: 5}
- `gold_evidence_against_run_blocks`: checked 63, external_or_visual 45, unresolved 10, value_not_literal 7, quote_not_in_block 3
- Real remaining failures: byzantium (lost "(7th–9th Century)", type book vs chapter), gno1 (lost "I–II"), final_160 (lost "160"), gregory-of-nyssa (filename fallback), cyril ("new doc 6" — fully diagnosed: model cited corrupted title-page blocks `ALEXANDRI A` on the correct selected page, title rejected → legacy embedded-metadata fallback fired), augustin ("Crack by RAOGY." hallucination), ambrose (series in `collection-title` vs gold paren-in-title)

## 2. Root Cause Analysis

### 2.1 Title F1 = 0.125 is mostly a scoring artifact (P0)

`_equivalent` (`benchmarks/citations/run.py:78-82`) already composes `title` + `": " + subtitle` for record equivalence, but `_score_values` (field F1) and `field_errors` use raw `_norm` on each field independently. A prediction that correctly splits the title into `title` + `subtitle` (correct CSL semantics, per `README.md:342` export contract) scores as a wrong title and a spurious subtitle.

Offline estimate: ~9/16 titles pass under composition (≈0.56 title F1). Must be verified by offline re-score, not assumed.

### 2.2 Extractor misses (P1)

Measured from the failure evidence (`docs/2026-09-25-pdf-benchmark-failure-analysis.md`):

1. **Page-window too narrow**: imprints located on physical pages 6 and 9 fall outside the default `page_range "1-5, -3"`. Default change fixes without any config: `citeindex/ingestion/models.py:44`, `citeindex/cli.py:151` (CLI default + help text), README config table.
2. **Doc-type taxonomy gap**: `entry-encyclopedia` and `manuscript` not in the `LocateBibliographicPages` accepted set (`dspy_extract.py:155-174`, ~:218-220) nor in `doc_type_to_csl_type` (`common.py:374-377`, falls back to `document`).
3. **Rejection opacity**: rejections logged as debug (`dspy_extract.py:860-861`) leave "rejection causes unresolved" in the failure report.

## 3. Changes

### P0 — Scorer (file: `benchmarks/citations/run.py`)

1. Extract shared `_composed_title(csl)` helper: join `title` + `": " + subtitle` unless subtitle text already inside title. `_equivalent` (:78-82) already implements this inline — factor it out and reuse.
2. Use composed title in `_score_values` for field `title`.
3. Use composed title in `field_errors` title comparison.
4. Use composed title in `exact_record_accuracy` title comparison (:145-152).
5. "Subtitle covered" skip: gold `subtitle` is None + pred `subtitle` non-None + composed title matches → do not count pred subtitle as a separate record-field difference.
6. Bump `scorer_version` (:245): `"host-v4-run-accessed-equivalence"` → `"host-v5-composed-title"`.

**Explicitly NOT doing**: forcing title+subtitle into one string in the model — that contradicts CSL semantics and the export contract (`README.md:342`).

### P1 — Extractor (files: models.py, cli.py, dspy_extract.py, common.py, README.md)

1. Default `page_range`: `"1-5, -3"` → `"1-10, -3"` in `models.py:44` and `cli.py:151` (default + help). Update README config table.
2. Locate preview budget: 12000 → 18000 chars (`dspy_extract.py:186`), keeping ≈1385 chars/page across the wider 13-page window.
3. Add `entry-encyclopedia` + `manuscript` to `LocateBibliographicPages` docstring (:155-174) and accepted set (~:218-220); add to `doc_type_to_csl_type` map (`common.py:374-377`) → `entry-encyclopedia` → `entry-encyclopedia`, `manuscript` → `manuscript`; mention both in ExtractDocumentMetadata docstring.
4. Add `_rejection_reason` diagnostic helper + `logger.warning` replacing the debug log at `dspy_extract.py:860-861`.

### Deferred (with reasons)

- Accepted-field revision loop (:853): no observed frozen-wrong-accept case; confirmed failures are rejections, not accepts.
- Extract-stage budget stays 12000.
- Trailing-numeral prompt tweak ("Keep numbered volume/part designators (e.g. I–II, 160) inside the title"): optional; prompt changes have wider blast radius, revisit after re-measurement.

## 4. Verification Procedure

Three-way decomposition, so the scorer fix and extractor fix are measured separately:

- **(a) Old-extractor + old-scorer**: existing saved report (`citation-pages-digital-report-2026-09-25.json`) — already measured.
- **(b) Old-extractor + new-scorer**: offline re-score of saved `citation-pages-digital-2026-09-25.jsonl` with the new scorer — free, no cloud calls.
- **(c) New-extractor + new-scorer**: cloud re-run on a digital-only manifest slice.

Final report presents all three honestly. If (c) regresses on any field, the plan doc records it rather than hiding it.

## 5. Benchmark Re-run Configuration

- Manifest: digital-only slice, 16 rows (from `benchmarks/citations/manifest.jsonl`; cli_args all null → `ingest.py:107-108` forces `["--no-layout", "--no-pageindex"]` for PDFs — same config as baseline = controlled comparison).
- New predictions filename per README guidance (never overwrite): `citation-pages-digital-2026-09-26.jsonl`.
- Run: `python benchmarks/citations/ingest.py <digital-manifest> <new-predictions-file>`, serial, 5400s/source timeout, ~25-35 min total expected (54-155s/source last run).
- Environment: use `.venv/bin/python` (system python lacks dspy/fitz — pytest collection fails with ModuleNotFoundError outside venv).
- Scoring: `run.py` → `compare.py` → append Results section to this doc.

## 6. Safety / Rollback

- Scorer: version-bumped, old report preserved — A/B comparison remains reproducible.
- Extractor: default changes are opt-out via explicit config; no persisted `csl.json` is ever edited directly (any repair regenerates artifacts per project invariant).
- Gold annotations: never loosened.
- Single commit boundary per lane; revert by reverting the lane's commit.

## 7. Results (2026-09-26 re-run)

### Method correction

The published 2026-09-25 report was produced by an unrecoverable intermediate scorer build against a **sparser gold snapshot** (second review later expanded `field_status` coverage: e.g. edition 1→15, editor 4→15, translator 4→15 evaluated). All comparisons below therefore hold gold fixed at the frozen slice `predictions/digital-16-2026-09-26.manifest.jsonl` (sha256 `b7057891…`, 16 rows, from current `manifest.jsonl`). Delta (a) uses a v4-semantics simulation (raw title comparison, verified to produce zero non-title differences vs v5 on identical inputs — the only v4→v5 change is title composition).

### Three-way comparison (identical gold, 16 digital sources, `ollama/deepseek-v4.1-flash:cloud`, `--no-layout --no-pageindex`, 16/16 ok, 1207s total)

| Metric | (a) old preds + v4-sim | (b) old preds + v5 | (c) new preds + v5 |
|---|---|---|---|
| title F1 (tp) | 0.125 (2) | **0.500 (8)** | 0.375 (6) |
| exact_record_accuracy | 0.0625 (1) | **0.125 (2)** | 0.0625 (1) |
| equivalent_record_accuracy | 0.125 | 0.125 | 0.0625 |
| issued F1 (tp) | 0.769 (10) | 0.769 (10) | **0.857 (12)** |
| type F1 (tp) | 0.812 (13) | 0.812 (13) | **0.875 (14)** |
| author F1 (tp) | 0.538 (7) | 0.538 (7) | 0.333 (4) |
| publisher F1 (tp) | 0.800 (10) | 0.800 (10) | 0.692 (9) |
| ISBN F1 (tp) | 0.667 (3) | 0.667 (3) | 0.545 (3, +2 fp) |

### Findings

1. **Scorer fix (P0) — proven, keep.** (a)→(b): title F1 0.125→0.500, exact 0.0625→0.125, and **every non-title field is byte-identical** (verified by v4-sim differential). This is a pure scoring-artifact fix with zero side effects. 8 new scorer tests; full suite 127 passed, 3 skipped.
2. **Extractor changes (P1) — no measurable benefit on this run; net slightly worse.** (b)→(c): title F1 0.500→0.375, exact 2→1, author F1 −0.21, publisher −0.11. Wins: cyril title fixed ("new doc 6" garbage gone, F→T), debates gained full exact+equiv match, issued +2 tp, type +1 tp. Losses: title regressions on caridi, 2003_希腊文圣经史, chrysostom-bibliography; author regressions on augustin, final_160, climacus; ISBN/publisher-place flips lost 2 previously-exact records (ethiopian, climacus).
3. **Variance caveat.** Each cell is a single cloud run at temperature 0.1; n=16 flips of ±3 sources are within plausible run-to-run model noise. The hypothesized imprint-page-6/9 fix did **not** materialize (publisher fn 3→4). This single run cannot statistically separate the extractor effect from noise — but it provides zero evidence of improvement, and some evidence of regression.

### Recommendation

- **Keep** the v5 scorer (deterministic, proven, zero side effects).
- **Extractor P1**: no demonstrated benefit; the page-window widening is at worst mildly harmful on this corpus. Cheapest honest options: (1) revert `page_range` default to `"1-5, -3"` and keep the harmless diagnostics/doc-type additions, or (2) run 2–3 more seeds to separate noise before deciding. Absent further runs, treat (1) as the default posture.

### Artifacts

- New predictions: `predictions/citation-pages-digital-2026-09-26.jsonl` (16/16 ok) + report `citation-pages-digital-2026-09-26-report-v5.json`
- Corrected baselines on frozen gold: `citation-pages-digital-2026-09-25-baseline-v4sim.json` (a), `citation-pages-digital-2026-09-25-rescored-v5.json` (b)
- Frozen gold slice: `predictions/digital-16-2026-09-26.manifest.jsonl`