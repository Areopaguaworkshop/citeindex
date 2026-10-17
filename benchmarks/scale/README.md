# Scale benchmark — throughput and failure modes

Recorded 2026-10-16/17 at git `9dc5f09`. Corpus: 22 local Patrologia Graeca
volumes (public-domain Migne scans with text layers; kept outside the repo at
`/home/ajiap/project/cfocr/sources/PG/Greek/`). Nothing from the corpus is
committed; the ledgers in this directory are the evidence.

## What it measures

The pipeline's real output with default routing and configuration:

- each document runs in its own subprocess with a hard per-document timeout
  (`--workers` runs several at once);
- pages are counted with PyMuPDF; throughput is reported both over wall time
  and over successful-document time only;
- each ledger row records status, seconds, pages, bytes, sha256, and
  `error_code`/`stage` on failure; corpus artifacts go to a scratch directory
  and are deleted after each document (the corpus is never copied in).

## How to run

```bash
python benchmarks/scale/run.py --corpus <dir-or-glob> [--limit N] [--workers W] [--timeout-seconds S]
```

## Recorded runs

| Run | Config | Docs | Ok | Ok pages | Wall | Pages/min (wall) | Pages/min (ok-only) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Pilot `20261016-175528` | 1 worker, 900 s | 1 | 1 | 564 | 846 s | 40.0 | 40.1 |
| Main `20261016-181515` | 3 workers, 3600 s | 16 | 12 | 8,595 | 11,701 s (195 min) | 44.1 | 24.7 |
| Recovery `20261016-235324` | 1 worker, 2400 s | 4 | 2 | 1,457 | 8,129 s (135 min) | 10.8 | 26.3 |
| Aborted `20261016-233054` | 1 worker | 0 | 0 | — | terminated (stalled document) | — | — |

**Unique-document totals across runs:** 20 documents attempted, 14 ok,
**10,052 ok pages** (target: ≥10,000) out of 14,215 attempted. The pilot
document (`u04`) also appears in the main run, so it is a single-stream
reference, not extra pages.

## Per-document results

Main run (3 workers):

| # | Status | Seconds | Pages | Volume |
| --- | --- | --- | --- | --- |
| 1 | ok | 1127.5 | 742 | PG001_text.pdf |
| 2 | ok | 1070.6 | 652 | PG002_text.pdf |
| 3 | ok | 1018.6 | 596 | PG003_text.pdf |
| 4 | ok | 2026.9 | 859 | PG_0331886_text.pdf |
| 5 | blocked | 430.2 | 634 | epistolai00eutygoog_text.pdf |
| 6 | ok | 2839.8 | 855 | patrologiaecurs128migngoog_text.pdf |
| 7 | ok | 2020.9 | 702 | patrologiaecurs261unkngoog_text.pdf |
| 8 | ok | 1880.6 | 717 | patrologiaecurs26hopfgoog_text.pdf |
| 9 | timeout | 3600.0 | 735 | patrologiaecurs31hopfgoog_text.pdf |
| 10 | ok | 1932.2 | 753 | patrologiaecurs45hopfgoog_text.pdf |
| 11 | timeout | 3600.0 | 695 | patrologiaecurs45migngoog_text.pdf |
| 12 | ok | 1537.7 | 727 | patrologiaecurs86migngoog_text.pdf |
| 13 | ok | 2209.0 | 786 | patrologiaecurs92migngoog_text.pdf |
| 14 | ok | 653.3 | 564 | patrologiaecursu04mignuoft.pdf |
| 15 | timeout | 3600.0 | 826 | patrologiaecursu10mignuoft.pdf |
| 16 | ok | 2524.9 | 642 | patrologiaecursu161mignuoft.pdf |

Recovery run (1 worker, fresh volumes):

| # | Status | Seconds | Pages | Volume |
| --- | --- | --- | --- | --- |
| 1 | ok | 1558.2 | 700 | patrologiaecursu36mignuoft.pdf |
| 2 | timeout | 2400.0 | 660 | patrologiaecursu38mignuoft.pdf |
| 3 | timeout | 2400.0 | 613 | patrologicursus45migngoog_text.pdf |
| 4 | ok | 1767.4 | 757 | patrologicursus55migngoog_text.pdf |

## Failure modes (6 failures / 20 documents)

- **1 × `ingestion_exception`** (`master_ingestion`): `epistolai00eutygoog`
  after 430 s. A later retry stalled indefinitely as an idle process
  (~0 % CPU, no I/O, no sockets); it was terminated. `py-spy` could not attach
  in this environment (needs elevated permissions), so the stack was not
  captured — root cause remains a follow-up.
- **5 × per-document timeout**: `31hopf`, `45mign`, `u10` (3600 s, 3-worker
  run) and `u38`, `45mign-2` (2400 s, solo). The stall is per-document, not
  purely contention: several volumes exceed an hour even alone while others
  finish at ~2.2–2.3 s/page.
- **Idle-stall pattern**: timed-out processes show near-zero CPU and no
  network sockets — consistent with long waits in the optional LLM citation
  path. There is currently **no supported flag to disable LLM citation
  extraction**; adding one (or a local deterministic-only mode) is the clear
  follow-up and would materially change these numbers.

## Throughput notes

- **Parallelism does not help much**: 3 workers reached 44.1 ok-pages/min wall
  versus 40.0 single-stream — ~1.1×. The pipeline is limited by a shared
  per-document bottleneck, not CPU.
- Success-only rates: 24.7–40.1 pages/min. Wall rates are dragged down by
  timeouts (recovery run: 10.8 pages/min wall because two 2400 s timeouts
  dominated its 135 min wall time).
- Per-document times on successes: ~1.5 s/page (pilot) up to ~3.6 s/page
  under load.

## Ledger schema

One JSONL row per document in `benchmarks/runs/<run-id>.jsonl`:

`path`, `index`, `status` (`ok` / `timeout` / `blocked` / `exception` /
`no-output`), `seconds`, `pages`, `bytes`, `sha256`, `exit_code`,
`stderr_tail` (last stderr line), and on failure `error_code` / `stage`.

Each run also writes `<run-id>.summary.json` with totals, `by_status`, and a
failures list.

## Scope and limits

- Digital path only (text-layer PDFs); OCR engines are not exercised.
- Default DSPy citation configuration (no model keys configured): LLM/network
  behavior dominates run-time variance, which is why failures cluster in
  wall-clock rather than CPU.
- The corpus stays outside the repository; ledgers and summaries here are the
  committed evidence.
