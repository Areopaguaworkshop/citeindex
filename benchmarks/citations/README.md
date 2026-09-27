# Citation benchmark

`manifest.jsonl` is deliberately local and untracked: each reviewed row needs
`id`, `source_path`, `source_sha256`, and a human-reviewed `csl` object.
Predictions use the same `id` and a `csl` object. Run:

```sh
python benchmarks/citations/run.py manifest.jsonl predictions.jsonl
```

The runner counts every gold record, including missing predictions. Keep PDFs
out of this directory unless their redistribution rights permit it.

Before scoring, annotate each row with reviewed CSL/evidence and set
`review_status` to `reviewed`; model output is never gold. Create reproducible
splits with `python benchmarks/citations/split.py manifest.jsonl split.jsonl`.
Merge independent reviews with `python benchmarks/citations/adjudicate.py
reviewer1.jsonl reviewer2.jsonl adjudicated.jsonl`; disagreements remain
`needs_adjudication` until resolved.
Create an agent-review proposal with `python benchmarks/citations/audit.py
source.pdf proposal.json`, then re-ingest with `--repair-proposal proposal.json`.
Compare saved reports with `python benchmarks/citations/compare.py baseline.json
candidate.json`.
For the optional engine control, run the same reviewed PDF once with
`citeindex source.pdf --citation-engine grobid`; this requires a separately
running GROBID service and never changes DSPy-mode behavior.

The scope is host-source metadata, not works cited inside a source. See the
updated [pilot checklist](../../docs/plans/2026-09-14-multimodal-citation-benchmark.md)
for the required field_status, independent reviewers, source checksums and
frozen work-family split. Unreviewed manifests cannot produce accuracy scores.

Scorer `host-v3-modality` requires annotation statuses for the source's CSL-type
profile plus explicitly annotated fields. A URL journal article is not a generic
webpage; a media record does not require book ISBN fields. Evidence validation
supports PDF pages, HTML sections, metadata snapshots and timed transcript
segments. Profile-excluded fields are not evaluated. Recording duration and
quotation timestamps are distinct. Use the same scorer version for comparisons.

The POSIX runner uses installed psutil for descendant cleanup. It preserves
attempt history, locks the output, logs each source separately and prints a
30-second heartbeat. Default timeout is 5400 seconds; override with
`--timeout-seconds`. Use `--retry-failed` to retry the latest failed/blocked
attempt, not to erase failures. Use a new predictions filename for each engine
or configuration. Stop any older unlocked runner before starting this version.
Unknown token usage or priced cost remains null. The original 40-source
DeepSeek V4.1 Flash cloud run (2026-09-24) returned 33 `ok`, 3 scanned-PDF
timeouts, and 4 media failures. Its earlier scorer reported 6/36 strict record
matches (16.7%) across the 36 PDF/URL rows with usable gold metadata. The later
sampled run completed those 36 rows. Re-scoring with
`host-v4-run-accessed-equivalence` gives 3/36 strict and equivalent record
matches (8.3%). It uses the run's September 24 retrieval date instead of the
gold file's September 25 date and correctly disqualifies two redirect-only
GCDFL snapshots. Baseline sampled-run field F1: title 0.559, author 0.571,
issued 0.833, publisher 0.727, type 0.765. URL and observed accessed-date F1
are each 1.00 on 8 URL rows. These values describe a development pilot only.

The repaired 40-source cloud candidate completed 40/40 ingestions. On the 36
reviewed PDF/URL rows, it scored 4/36 strict records (11.1%) and 9/36 under
the separately defined bibliographic equivalence rule (25.0%). Equivalent
accuracy by modality: digital PDF 2/16, scanned PDF 1/12, URL 6/8. Field F1:
title 0.444, author 0.491, issued 0.926, publisher 0.824, type 0.912. All
248 returned model evidence spans resolve to source blocks, but only 171 field
values match reviewed gold and only 55 spans match the exact reviewed quote and
attribution. A valid quote can still describe an internal chapter rather than
the host work. The candidate's two GCDFL source digests now refer to destination
articles, so this is not a same-snapshot causal comparison with the old run.
Four media records are structurally `ok` with empty transcripts and
`_citation_status=incomplete`; their intentionally unknown metadata remains
unscored. The 90% production target is not met. See the ignored local source
reviews at `reports/cloud-40-candidate-2026-09-25-review.md` and
`reports/cloud-web-final-2026-09-25-review.md`. Prediction,
adjudication and score artifacts remain ignored local files; token/cost
telemetry is unknown.

### Citation-page selection experiment (2026-09-25)

The new page-locator/type-classifier was exercised on the 16 reviewed digital
PDFs and 12 reviewed scanned PDFs, using physical pages 1-5 and the final 3.
For scanned PDFs, benchmark OCR itself was restricted to those pages. The
role-aware extraction revision was run on the scanned set; digital results
preceded that final prompt addition. Compared with the existing sampled cloud
candidate under the same scorer, digital equivalent-record accuracy was 2/16
(12.5%), unchanged; scanned equivalent-record accuracy was 0/12 versus 1/12
(8.3%) on the previous candidate. This experiment did not improve citation
accuracy. Source type F1 was 0.8125 digital and 0.9167 scanned; scanned title
F1 was 0.4167. On the inspected Coptic Grammar page, MinerU OCR omitted a
clearly printed title that system Tesseract read correctly. Page selection
found the correct title/copyright pages, so remaining errors include OCR and
field extraction/attribution, not just page discovery. Do not treat the
selection approach as production-validated. Local prediction and report files
remain ignored under `predictions/`.

To use Wenbi only for media rows, pass `--media-asr-backend wenbi` plus an
explicit provider. `--wenbi-python ../wenbi/.venv/bin/python` keeps Wenbi's
dependencies isolated from MinerU. The runner records the resulting CLI arguments
per attempt. Do not select Gladia unless uploading audio is explicitly approved.


### Filling local media records

For local audio/video, begin with the filename as provisional evidence; do not treat a filename date as an event or publication date without confirmation. Ask the user to supply or verify the title, contributors and roles (speaker, interviewer, translator, etc.), event date, recording/publication date, and any publisher, series, or stable URL. Check embedded file metadata and the original event programme or upload page when available. Keep event date distinct from publication/upload date. Add transcript-segment evidence with timestamps only when a real transcript exists; failed transcription must not create transcript text. Leave unknown CSL fields out of the CSL object and keep the row `needs_review`; `field_status` uses the benchmark values `present`, `absent`, `illegible`, or `outside_scope` when the annotation is ready for review. Do not set `review_status` to `reviewed` until the source details are confirmed.

### Online enrichment evaluation

`ingest.py` always pins `--no-online-enrich` unless a manifest row's `cli_args`
explicitly contains `--online-enrich`. Use separate prediction files and retain
failed attempts. The runner saves `online_enrichment` alongside source verification.
The development default remains **false** until held-out gates pass.

Score independently reviewed registry completion separately from the existing
source-only scorer:

```sh
.venv/bin/python benchmarks/citations/enrichment.py reviewed-enrichment.jsonl enriched-predictions.jsonl > enrichment-report.json
.venv/bin/python -m pytest -q tests/test_enrichment_benchmark.py
```

Each reviewed enrichment JSONL row has this contract:

| Field | Meaning |
| --- | --- |
| `id`, `source_sha256` | Stable input label and SHA-256 of the independently reviewed source |
| `source_review_status`, `registry_review_status` | Both must be `reviewed` |
| `registry_gold_origin` | Must be `independent_review`; provider output alone is never gold |
| `source_csl` | Only values supported by the original source |
| `registry_csl` | Independently adjudicated expected completion/correction values, including all changed fields |
| `registry_absent_fields` | Explicitly adjudicated fields that should remain absent |
| `source_field_status` | Optional source annotations; `illegible`/`outside_scope` exclude a field from source core scoring |
| `modality`, `script` | `digital_pdf`/`scanned_pdf`; `CJK`/`Latin`/`mixed` |
| `work_family`, `split` | Frozen work-family grouping and `development` or `heldout`; families may not cross splits |
| `eligible`, `masked_fields` | Reviewed eligibility and frozen masked-field denominator, including skipped/failed inputs |
| `change_review` | Independent `{review_status:"reviewed",prediction_digest:"...",same_work_and_edition:true}` tied to the evaluated final CSL |
| `safety_review_status`, `safety_checks_passed` | Reviewed result of the separate safety/regression fixture run; required for release eligibility |

`change_review.prediction_digest` is produced by
`benchmarks.citations.enrichment.candidate_digest(final_csl)`. A reviewer records
the work/edition verdict; the provider or enrichment model cannot supply it.
Regenerate the review if the evaluated CSL changes. Missing/stale identity review
prevents changed fields from counting as correct and blocks release. Reviewers must
resolve catalog conflicts independently; do not copy a provider response into gold
and call the same response a successful prediction.

Each prediction row uses `id`, `source_sha256`, `status`, final `csl`, and
`online_enrichment`. Supply `before_csl` or retain the report's
`candidate_input_snapshot` (post-source-repair/pre-enrichment). The report includes
`decisions` with `field`, `old_value`, `new_value`, `action` (`fill`/`overwrite`),
`tier`, and `provenance` (`provider`, `request_identifier`, `response_digest`).
`source_repaired_fields` records which pre-enrichment values came from accepted
source repairs, allowing subsequent overwrite decisions to count repair reversals.
It also supplies measured `request_count`, `cache_hits`, `elapsed_seconds` and
optional `cache_misses`, `provider_errors`, `ai_cost_usd`, `offline_requests`,
`unsafe_url_fetches`, `failed_proposal_successes`. Nested `transport` is accepted
for these telemetry fields. Unknown cache-hit denominators and AI costs remain
null; a cache-hit count alone does not establish a hit rate. Missing predictions
remain in eligible, masked-field and source-core denominators. Latest attempts are
scored; original attempt history stays in the predictions file.

For masked recovery, call
`mask_candidate(candidate_csl, ["publisher", "issued"], no_identifiers=True)`.
This returns a copy containing CSL host fields and retained source evidence/status,
removes the selected fields and their evidence/status, optionally removes DOI/ISBN,
and drops citation status, provider candidates, enrichment reports and other internal
metadata. Pass this candidate to the enrichment stage rather than passing a masked
value beside an unmasked report/cache. Do not reuse a cached candidate from the
unmasked input. Run both identifier-present and DOI/ISBN-masked tracks. Include
real extraction failures, wrong/unverified anchors, adversarial edition/translation/
chapter matches, and independently reviewed correction cases, including source
repairs later superseded by T1/T2.

The report publishes numerator/denominator/rate triples for recovery, fill and
correction precision, overall applied precision, wrong-work/edition changes,
abstention, harmful overwrites and source-core accuracy. It separately counts source
overwrites and source-repair reversals, audits old/new values, and groups input,
coverage and applied-field counts by modality, script, work type, provider and tier.
Latency p50/p95 and request/cache/error/cost telemetry are descriptive measured
values, not evidence that safety fixtures passed. `safety_checks_passed` must come
from reviewed timeout/429, offline, unsafe URL, failed proposal and affected
regression checks; it must never be inferred from a green ingestion status.

Release gates are fixed: independently reviewed held-out data only; zero wrong
work/edition changes with at least 300 changed records and at least 50 changed
records each in CJK and Latin; at least 99% fill, correction and applied precision;
10 percentage points of correct masked recovery gain with no source-core accuracy
decrease; no unprovenanced writes, incorrect audit values or T3/T4 overwrites;
reviewed safety checks; no offline/unsafe fetches or falsely successful proposals;
observed 20-second/12-attempt budgets. Empty fill/correction cohorts do not satisfy
their precision gates. For zero wrong identities, the one-sided exact 95% upper
bound is `1 - 0.05 ** (1 / changed_records)`: zero/300 is about 0.994%, not proof
of zero risk. The 50-record subgroup requirement establishes coverage only.
Freeze tuning on development data before scoring held-out data. Do not lower
thresholds or remove difficult cases after seeing held-out results.

Current evidence: the synthetic unit cases exercise the scorer and gates only.
No independent held-out enrichment measurements or real digital/scanned PDF
proposal round trips have been collected by this tooling change. P5 real-PDF
acceptance, P6 default-on release, and P7 per-provider native-search smoke/recovery
evaluation remain evidence gates; passing unit tests does not complete them.

### Explicit harness support

Codex and Claude Code load `.codex/skills/citation-verification/SKILL.md` and
`.claude/skills/citation-verification/SKILL.md`; Pi uses its matching skill and
`.pi/prompts/ingest-verified.md`. These loaders share
`.agents/skills/citation-verification/SKILL.md`. OpenCode's configured instructions
and `/ingest-verified` command delegate source review to `@citation-verifier` and
requested registry work to `@citation-enricher`. The latter denies general web
search/fetch and invokes CiteIndex's constrained CLI. These are explicit wrappers;
raw `citeindex` processes never load a harness audit. Source verification remains
quotation-backed and source-only; registry completion has a separate report.

Registry proposals use schema `1.0`, original `source_sha256`, and
`base_candidate_digest` from `online_enrichment.candidate_input_digest`. Entries
contain `kind` (`registry_fill`/`registry_correction`), `provider`
(`crossref`/`datacite`/`openlibrary`), DOI/ISBN `identifier`, `field`, exact
`old_value`, `proposed_value`, and `evidence_reference`. See the shared skill for
the complete JSON example. Apply with `citeindex original.pdf --enrich-proposal
proposal.json`; source repair may precede it in the same invocation. T1/T2 may
automatically supersede source-supported/repaired values and retain both audit
events. T4 cannot overwrite; T3/T5 writes stay deferred. Neither wrapper nor
enricher edits persisted `csl.json`; fresh validation and normal finalization create
a new output while retaining older corpus folders.
