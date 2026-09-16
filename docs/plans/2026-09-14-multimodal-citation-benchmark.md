# Multimodal source-citation benchmark

Updated 2026-09-16. Scope: the ingested source's own citation metadata and
evidence verification. No bibliography-of-cited-works extraction or QA.

## Pilot: 40 sources, not 40 PDFs plus other modalities

| Modality | Count | Coverage |
| --- | ---: | --- |
| Digital PDF | 16 | Books, articles, chapters/theses, multi-author, Latin/CJK/bilingual, editions |
| Scanned PDF | 12 | Clean/noisy OCR, title-page images, CJK/vertical text, colophons |
| Media | 4 | Audio/video, lecture/interview, multilingual, metadata present/absent |
| URL article | 8 | Citation guidance, structured-data conflicts, missing dates/authors |

The existing candidate pool is not a reviewed pilot. Select exactly 40 and
review actual modality/language/work family before freezing. A theology-heavy
local collection does not establish broad domain coverage; document this bias.
Do not claim 40-source accuracy from attempt-line counts.

## Human annotation contract

Every row needs id, modality, source_path, source_sha256 (saved HTML bytes for
URLs), work_family, language, review_status=reviewed, reviewer, a distinct
second_reviewer, adjudication_status=agreed or adjudicated, split, csl,
field_status, and field_evidence.

For each scored CSL field declare present, absent, illegible, or outside_scope.
Present fields require a value, exact source quotation and locator. Preserve
name order, contributor roles, edition/date precision, and host-versus-cited-work
identity. Gold labels must be reviewed against originals, not copied from an
extractor or registry. An OCR quote alone cannot certify the original scan.

Use physical_page_index (zero-based), not printed page number, for PDFs.
For native DSPy evidence retain block_id, quote, and the exact locator with
character offsets. For URLs retain snapshot and section location; for media
retain a timestamp or original descriptive metadata location. Differing but
valid evidence is flagged for attribution review, not silently called wrong.

## Checklist and sequence

### Code readiness

- [x] Preserve every attempt and count latest status separately from attempt history.
- [x] Lock predictions per output, refuse concurrent writers, log progress.
- [x] Timeout per source (default 90 minutes); clean tracked descendants.
- [x] Scorer rejects unreviewed/invalid manifests and source checksum mismatches.
- [x] Host field and ordered-name scoring; no cited-reference or marker scoring.
- [x] Evidence span checks and matches to independently reviewed attribution.
- [x] Per-modality counts, missing predictions, field F1, latency, unknown cost.
- [x] Adjudication helper, non-overwriting grouped/stratified split helper.
- [x] Reports compare only matching manifest digests and scorer versions.

### After offline tests, performed with the user

- [ ] Select the final 16/12/4/8 set with rights/provenance recorded.
- [ ] First annotation and independent second review of every pilot row.
- [ ] Resolve disagreements; record reasons rather than replacing provenance.
- [ ] Freeze development/validation/test families (approximately 20/7/13).
  Tiny language strata cannot have exact ratios; inspect the actual distribution.
- [ ] Freeze scoring rules and model/engine/configuration versions.
- [ ] Run baseline and candidate separately; never mix configurations in one file.
- [ ] Explicit optional GROBID digital-PDF comparison (separate installation).
- [ ] Review host attribution errors and evidence selection on development only.
- [ ] Publish report and README summary with denominators and limitations.
- [ ] Expand to 120 only after annotation/scoring are stable.

## Commands (do not run live ingestion until the user proceeds)

```bash
.venv/bin/python benchmarks/citations/adjudicate.py first.jsonl second.jsonl adjudicated.jsonl
.venv/bin/python benchmarks/citations/split.py adjudicated.jsonl frozen.jsonl
.venv/bin/python benchmarks/citations/validate_manifest.py frozen.jsonl

env PATH="$PWD/.venv/bin:$PATH" LITELLM_LOCAL_MODEL_COST_MAP=true \
  .venv/bin/python -u benchmarks/citations/ingest.py frozen.jsonl candidate.jsonl \
  --timeout-seconds 5400 --citation-engine dspy

# Resume failed/blocked sources; successful latest attempts are skipped.
.venv/bin/python -u benchmarks/citations/ingest.py frozen.jsonl candidate.jsonl --retry-failed

.venv/bin/python benchmarks/citations/run.py frozen.jsonl candidate.jsonl > candidate-report.json
.venv/bin/python benchmarks/citations/compare.py baseline-report.json candidate-report.json
```

Runner requires POSIX flock and the already-installed psutil package for
descendant tracking. New logs/corpus artifacts are stored next to predictions
in configuration-specific attempt directories. Ctrl-C records interruption and
stops the run; a source timeout records failure and proceeds. SIGKILL/power loss
cannot be cleanly handled; inspect a truncated JSONL tail rather than deleting
history blindly. An older runner without locking must be stopped first.

Baseline and candidate must use matching input hashes and explicit budgets.
Default PDF runner options disable layout/PageIndex only when no cli_args are
provided. To measure PageIndex, set explicit per-row arguments and record them.
Do not compare a disabled baseline with an enabled candidate without reporting
the extra work. GROBID selection is applicable to digital PDFs, not OCR/media/URL.

## Reporting contract

Report commit, dependencies, exact model identifier, signature version,
configuration, source/annotation digests, hardware/services, evaluated fields,
completion and failure denominators, per-modality metrics, examples of errors,
and p50/p95 elapsed attempt time. Report all retry costs, not just successful
last attempts. Tokens and USD remain null if not instrumented; collect provider
usage separately before claiming measured cost.

Do not equate structurally valid quotes with correct attribution. Report
reviewed attribution separately and expose unreviewed evidence. Model-generated
labels and registry agreement are not gold. A 40-source pilot is a diagnostic
sample, not proof of sub-percent errors or general superiority.

README currently records no valid accuracy measurement. Update it only from
a reviewed, reproducible report. No live benchmark is started by this code task.
