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
Unknown token usage or priced cost remains null. No live pilot has been run as
part of this implementation, and no valid accuracy measurement is published.

To use Wenbi only for media rows, pass `--media-asr-backend wenbi` plus an
explicit provider. `--wenbi-python ../wenbi/.venv/bin/python` keeps Wenbi's
dependencies isolated from MinerU. The runner records the resulting CLI arguments
per attempt. Do not select Gladia unless uploading audio is explicitly approved.
