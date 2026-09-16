# Small-source smoke test

Status: Wenbi adapter implemented and offline-tested; ingestion not started. Permission review blocked
the launch pending explicit approval to send extracted local-source text to the
configured Ollama cloud model (`ollama/deepseek-v4.1-flash:cloud`). No accuracy or
completion results exist for this run.

The gitignored manifest is `benchmarks/citations/input/smoke-small-2026-09-16.jsonl`.
Sources remain in their original locations; no originals were changed.

| Source | Size | Extent | Selection evidence |
|---|---:|---:|---|
| Dingjuntao-2019-丁君涛-近三十年唐五代马匹贸易研究综述.pdf | 102,765 bytes | 7 pages | Embedded text on every page; visually checked article heading/byline/journal/date |
| 13b. William of Rubruck.pdf | 784,495 bytes | 5 PDF pages | No embedded text; scanned facing pages; first image visually checked |
| ware2-16-c6-Church-no-falling.mp4 | 1,042,662 bytes | 21 seconds | FFprobe duration/size; short local clip, sparse embedded metadata |
| GCDFL 2025 redesign notice | Not yet ingested | One page | Homepage link resolves through a meta-refresh shell; manifest uses canonical `/posts/about/2025-09-29-gcdfl-2025-10-改版通知/` URL |

The scan is an excerpt without a title/copyright page. It intentionally tests
abstention on missing edition/publisher/date, not recovery of unprinted metadata.
The media filename is not proof of speaker identity or publication date.

Run with the existing append-only runner after cloud-processing approval:

```bash
env PATH="$PWD/.venv/bin:$PATH" LITELLM_LOCAL_MODEL_COST_MAP=true \
  .venv/bin/python -u benchmarks/citations/ingest.py \
  benchmarks/citations/input/smoke-small-2026-09-16.jsonl \
  benchmarks/citations/predictions/smoke-small-2026-09-16.jsonl \
  --timeout-seconds 5400 --citation-engine dspy
```

The smoke manifest already selects the isolated Wenbi/FunASR adapter for its one
media row. For a reviewed benchmark manifest, the runner can apply the same media
configuration to every media row with `--media-asr-backend wenbi`,
`--wenbi-asr-provider funasr`, and `--wenbi-python`.

PageIndex/layout are disabled to keep this diagnostic small. Verification is
enabled without registry/verifier-model calls; extraction still uses the cloud
model. Review successful results using the citation-verification skill before
reporting correctness. These unreviewed rows are not scoring gold or a substitute
for the independently adjudicated 40-source pilot.

## Current media tools and narrow Wenbi reuse

CiteIndex invokes MediaInfo for local metadata and FFmpeg for mono 16 kHz audio.
WhisperX `base` on CPU/int8 with alignment remains the default, followed by
optional pyannote 3.1 speaker turns. The opt-in Wenbi adapter can replace that ASR
step. Remote media additionally uses yt-dlp. DSPy extracts citation metadata from
probe fields and timed transcript blocks.

Wenbi provides `wenbi.asr.transcribe_with_engine`, returning normalized timed
segments. This is the suitable optional reuse boundary: raw ASR and, if needed,
speaker turns only. Do not run Wenbi's rewrite, translation, topic grouping or
slide-combination stages for quotation evidence. Keep original segment text and
times, preserve backend identity, and never infer personal names from speaker IDs.
Wenbi `auto` may choose Gladia cloud when credentials exist, so CiteIndex requires
an explicit `funasr`, `whisper`, or `gladia` provider. Wenbi stays an optional
dependency and is loaded only when selected. `--wenbi-python` can invoke Wenbi's
own isolated environment, avoiding its Pillow dependency conflict with MinerU.

## Timestamp-preserving Wenbi integration plan

Updated after reviewing Wenbi's documented six-stage workflow and its
`wenbi.asr.transcribe_with_engine` implementation. The opt-in adapter is now
implemented; the real-source comparison remains outstanding.

```text
CiteIndex probe/download → FFmpeg audio (unchanged timeline)
  → optional Wenbi ASR + optional speaker turns
  → original timed segments → CiteIndex evidence blocks
  → DSPy host-source metadata → validation/finalization
  → transcript.json + quotation_locators.json + CSL artifacts
```

### Reuse boundary

- Keep CiteIndex's existing download/probe/audio extraction; do not duplicate
  Wenbi stage 1. Extract the complete audio without trimming or speed changes.
- Reuse Wenbi stage 2 (raw ASR), optionally stage 3 (speaker labels), through
  `transcribe_with_engine`, not the full `rewrite` or `speaker` CLI workflow.
- Exclude stages 4–6: rewriting, translation and slide combination. Also exclude
  topic grouping and fuzzy timestamp recovery. Rewritten or translated text must
  never replace original-language quotation evidence.
- Retain the existing WhisperX path as the default. Select Wenbi and its ASR
  provider explicitly; do not use `auto`
  or silently switch local processing to Gladia cloud.

### Timestamp and provenance contract

- Preserve each original segment's `start` and `end` as numeric seconds, including
  fractional precision, in `transcript.json`. Preserve original text and available
  speaker labels; diarization labels are not verified personal identities.
- Evidence blocks retain segment IDs and the same numeric start/end values.
  `quotation_locators.json` holds formatted citation timestamps and the final
  CSL item ID. Whole-recording duration stays separately in CSL `dimensions`.
- Current CiteIndex keeps segment-level numeric times but formats quotation
  labels to whole seconds. Its WhisperX adapter does not persist word-level
  alignment arrays. Do not claim word-level precision or VTT output today.
- Validate finite times, `0 <= start < end`, and consistency with source duration.
  Do not silently repair invalid timestamps or invent a transcript when ASR fails.
  Allow genuine overlapping speaker segments; do not shift them to avoid overlap.
- For any chunked ASR path, require original-media-relative timestamps with chunk
  offsets applied exactly once. Store source checksum, backend/model/version and
  actual provider used. Never derive timing from rewritten prose.
- ASR timestamps and text remain machine estimates. Verify quotations against
  the recording before calling them accurate; preserve missing-field warnings.

### Implementation and validation checklist

- [x] Add an opt-in Wenbi ASR adapter with lazy dependency loading and a clear
  unavailable-backend error; no absolute import of this machine's Wenbi checkout.
- [x] Map raw timed segments and speaker labels without text rewriting; retain
  backend provenance and validate timestamps at the shared adapter boundary.
- [x] Add offline checks for fractional timestamps, overlapping turns, chunk
  offsets, malformed times, empty ASR, and unchanged final citation IDs.
- [ ] Run the selected 21-second clip through the existing path first, then the
  explicitly selected Wenbi backend; keep separate attempt artifacts/configs.
- [ ] Listen to the clip and check opening/closing segment boundaries and quoted
  words. Report text/timing discrepancies and runtime, not just ingestion status.
- [ ] Run the other three small sources and evidence-review successful outputs.
  Only then proceed to the reviewed 40-source pilot.

Cloud extraction approval remains outstanding. Updating this plan does not
authorize cloud ASR uploads, cloud metadata extraction, or dependency installation.

Implementation validation: 97 offline tests passed (2 warnings). The adapter
preserves Wenbi-returned source-relative times without adding offsets, accepts
overlapping turns, rejects malformed/out-of-duration segments, retains speaker
labels and backend/model/version provenance, and leaves failed/empty ASR empty.
These tests do not establish transcription or citation accuracy.

First smoke attempt (2026-09-16): the scan, media and URL pipelines completed and
wrote artifacts, but the benchmark runner classified them as failed because a
PyMuPDF deprecation warning preceded their JSON on stdout. The runner now parses
one JSON object after harmless leading warnings. The digital PDF exposed a real
Python local-name shadowing error for `determine_doc_type`; that import was moved
to module scope. Wenbi/FunASR operationally returned eight timestamped segments
for the 21-second clip (1.81–20.15 seconds). Their textual and timing accuracy has
not been human-verified. Retry attempt history must remain append-only.
