# P4 — citeindex production hardening (Data Scientist Pivot, Project 4)

Status: proposed — not started.
Date: 2026-10-16.

## Requested outcome and confirmed decisions

Close the feedback gap "Python-based pipeline development / small-scale"
(handbook Project 4) by bringing citeindex to production standard:
pytest + coverage gate, GitHub Actions CI, type hints + mypy, ruff, Docker
image, structured logging, data versioning, experiment tracking, a
one-command reproducible run, and a benchmark at ≥10,000 pages with
throughput and failure-mode numbers. Confirmed 2026-10-16: do this on
**citeindex first**, then replicate the playbook on **msocr** (appendix).
Budget ≈40 h over weeks 1–2; this is the critical path for the
easiest-to-reach application lanes.

## Evidence from the current repository

citeindex was inspected at repo state 2026-10-16 (read-only recon; anchors
are current lines, not proposed APIs).

| Finding | Source |
| --- | --- |
| 275 tests across 22 files; no `conftest.py`; pytest-cov only as a rye dev-dep, no pytest/coverage config | `pyproject.toml:42-44`; `tests/` |
| CI runs only secret scanning and PR-Agent — tests never run in CI | `.github/workflows/secret-scan.yml`, `.github/workflows/pr-agent.yml` |
| ruff has per-file-ignores only (no `select`, no line-length); mypy absent | `pyproject.toml:52-56` |
| No Dockerfile; logging is stdlib `basicConfig` | `cli.py:20-23` |
| No DVC, no MLflow/W&B; benchmark corpus/artifacts are gitignored | `benchmarks/citations/README.md` |
| Citation benchmark = 36 scored citations; scorer version pinned | `benchmarks/citations/run.py:334` (`host-v6-core-equivalence`) |
| Benchmark command | `python benchmarks/citations/run.py manifest.jsonl predictions.jsonl` |
| Build backend hatchling + hatch-vcs; Python ≥3.12 | `pyproject.toml:38-40`, `:21` |
| Enrichment/registry tests exist with fixtures | `tests/test_online_enrichment.py`, `tests/test_registry_enrichment.py`, `tests/fixtures/online_enrichment/registry_responses.json:14` |
| Never edit persisted `csl.json`; audit only via explicit harness wrapper | `AGENTS.md` |

## Recommended design

Nine bounded tasks, each with a verifiable output. No changes to ingestion
semantics, scoring semantics, or persisted artefacts.

**T1 — pytest + coverage config.** Add `[tool.pytest.ini_options]`
(`testpaths = ["tests"]`, `addopts = "--cov=citeindex --cov-report=term-missing"`)
and `[tool.coverage.*]` to `pyproject.toml`; record baseline coverage; set a
ratchet gate (target ≥85%; if baseline is lower, gate at baseline and raise
weekly). Command: `pytest` runs full suite with coverage.

**T2 — dev dependencies as a standard group.** Move the rye dev-deps
(`pytest`, `pytest-cov`, `psutil`) into a PEP 621 optional group or PEP 735
`[dependency-groups]` so `pip install -e '.[dev]'` works for CI and
contributors.

**T3 — tests CI.** New `.github/workflows/tests.yml`: `actions/checkout@v7`,
`actions/setup-python@v7` with matrix `3.12`/`3.13` and `cache: pip`; steps:
ruff → mypy → pytest with coverage. Gate on all. Keep existing
secret-scan/pr-agent untouched.

**T4 — ruff + mypy, pragmatically.** `[tool.ruff]` with `line-length` and
`lint.select = ["E","F","I","UP","B"]`; `[tool.mypy]` with a pragmatic
baseline (start `disallow_untyped_defs` on `citeindex/ingestion/`, allow
explicit per-module overrides with TODO notes rather than a broken gate).
Fix violations in bounded batches; do not rewrite working code for typing.

**T5 — Docker.** `Dockerfile` on `python:3.12-slim`, non-root user,
`pip install .`, entrypoint = `citeindex` CLI; `.dockerignore`; README
example: `docker run --rm -v $PWD:/data citeindex /data/file.pdf`.
No docker-compose (single CLI).

**T6 — structured logging.** Add `--log-format {console,json}`; default
console keeps today's format (`cli.py:20-23`), json emits one JSON object
per record via a stdlib `logging.Formatter` subclass. No new dependency
(structlog remains a later upgrade only if requested).

**T7 — one-command reproducible run + design doc.** README quickstart
section: install → `citeindex <file>` → outputs, plus the benchmark command
verbatim; short `docs/design.md` (pipeline stages, outputs, extension
points) — mirror the handbook requirement.

**T8 — scale benchmark ≥10,000 pages + run ledger.** New
`benchmarks/scale/`: runner over a fixed manifest of public-domain PDFs
(reuse local PG/IA material; per-page counting, per-stage timings,
failure modes: scanned-no-text, OCR timeout, conversion errors). Emit
`benchmarks/runs/*.jsonl` rows with `git_sha`, `scorer_version`,
`pages`, `pages_per_min`, `failures`. Version the corpus with DVC
(`dvc add benchmarks/scale/corpus`) if it exceeds ~100 MB; otherwise
git-manage a small manifest. MLflow is optional (local SQLite backend)
only if the ledger proves insufficient.

**T9 — release.** Bump to v0.15.0 (`hatch-vcs`) with a CHANGELOG entry;
README adds dated numbers: coverage %, benchmark throughput, downloads
snapshot (see below).

## Acceptance criteria and numbers

- `pytest` green with coverage gate (≥85% or documented baseline + ratchet).
- CI green: ruff + mypy + tests, on both Python versions.
- `docker run` from a clean checkout ingests a sample PDF end-to-end.
- Benchmark: ≥10,000 pages processed; report pages/min, per-stage timings,
  and failure modes in `benchmarks/scale/README.md`.
- One-command run documented; `docs/design.md` present.
- Interview line earned: "It runs in CI, it is reproducible, and here are
  the throughput numbers at 10k pages."

Downloads evidence (2026-10-16, pepy.tech): 8,614 total / 1,951 last 30
days; v0.14.0 released 2026-10-14. Use dated, as downloads only.

## Non-goals

- No changes to ingestion/routing/CSL semantics or the scorer
  (`benchmarks/citations/run.py` semantics stay pinned).
- No edits to persisted `csl.json` (AGENTS.md).
- No hosted service, no docker-compose, no dependency on network at test
  time (fixtures only).
- No MLflow/W&B beyond the optional local ledger unless explicitly asked.

## Risks

- mypy over 40+ modules can explode in scope → per-module overrides are the
  escape valve; gate stays green.
- A 10k-page corpus needs disk and time → prefer pages from local
  public-domain sources; the benchmark measures throughput, not accuracy.
- Coverage baseline may be far from 85% → ratchet rule above keeps honesty.

## Appendix — P4 replication on msocr (weeks 11–12, ≈20 h)

Checklist, reusing the same playbook:

- `[tool.pytest.ini_options]` + coverage gate over `tests/` (20 files,
  ~2,696 lines; no config today).
- CI workflow running pytest + ruff + mypy (today only `pr-agent.yml`).
- ruff/mypy pragmatic configs (`pyproject.toml` has no tool sections).
- Structured logging option over `msocr/utils/__init__.py:10-23`.
- Benchmark harness (lines/hour on a fixed page set) + report.
- Verify existing `Dockerfile` (non-root, healthcheck) still builds; keep.
- One-command README run: annotate → train → evaluate path.

## Sources checked

- actions/setup-python v7.0.0 and actions/checkout v7.0.1 (GitHub, 2026-10-16).
- pytest-cov config, mypy config, ruff config docs; DVC docs; MLflow docs;
  W&B pricing; python docker image README; structlog docs; HF model-card
  spec — all fetched 2026-10-16.
- pepy.tech/project/citeindex snapshot 2026-10-16.

## Execution record (2026-10-17)

Implemented and verified at git `9dc5f09` (release work pending commit).
All gates re-run on the final tree: `ruff check .` clean, `mypy citeindex`
clean (50 files), `pytest` = 379 passed / 6 skipped, coverage 40.04%.

- T1: pytest + branch coverage configured. Baseline 39.90% (376 passed,
  6 skipped) → gate set at 39, ratcheting toward 85%.
- T2: `dev` extra added; `uv pip install --dry-run -e '.[dev]'` resolves.
- T3: `.github/workflows/tests.yml` — ruff + mypy + pytest, Python 3.12/3.13.
  Both interpreters verified locally (3.13 in a clean venv: full suite green).
- T4: ruff `E4,E7,E9,F,I,UP,B` (line-length 120; E501 excluded — measured 333
  lines >120 across 46 files before deciding); 1,156 safe autofixes + 44
  manual fixes. mypy pragmatic, `citeindex.vertical_llm` excluded with TODO
  (104 legacy errors). Result: both clean; 2 documented `# type: ignore`
  (pageindex import fallback; glm_ocr tmpdir sentinel).
- T5: `Dockerfile` + `.dockerignore`; build verified; README run pattern
  verified end-to-end (sample PDF → corpus artifacts on the mounted dir);
  image 494 MB; non-root `appuser` confirmed.
- T6: `--log-format {console,json}`; e2e: 260/260 of our stderr records parse
  as JSON in json mode; console format unchanged.
- T7: README Quickstart + `docs/design.md`.
- T8: `benchmarks/scale/run.py` + ledgers in `benchmarks/runs/`. Unique-doc
  totals: 20 attempted, 14 ok, **10,052 pages** (target ≥10,000) of 14,215
  attempted. Throughput 44.1 ok-pages/min wall at 3 workers (≈1.1× the
  40.0 single-stream pilot — the pipeline does not parallelize well across
  documents). Failures: 1 × `ingestion_exception`, 5 × per-document timeouts;
  stalled processes are idle (no CPU, no sockets) → suspected LLM-citation
  waits; py-spy could not attach (needs elevated permissions).
- T9: version 0.15.0, `CHANGELOG.md`, README status block.

Deviations from this plan (all deliberate):

- E501 excluded rather than enforced (legacy long lines; numbers above).
- Per-stage timings replaced by per-document timing + failure stage; deeper
  stage instrumentation remains a follow-up.
- Recovery runs used a single worker; cumulative totals reported across runs;
  one aborted attempt left an empty ledger (kept for transparency).
- The pilot run's ledger was copied into `benchmarks/runs/` as a
  single-stream reference.

Follow-ups: add a supported way to disable LLM citation extraction (would
change benchmark numbers materially); clean up `vertical_llm.py` and drop its
mypy override; isolate the `ingestion_exception` root cause and the idle-stall
behavior; consider ruff `format` adoption; raise the coverage ratchet as P2
tests land.
