# Changelog

## v0.15.0 — 2026-10-17

Production hardening (Project 4 of the Data Scientist Pivot plan;
plan: `docs/plans/2026-10-16-p4-production-hardening-plan.md`).

### Added

- GitHub Actions CI running ruff + mypy + pytest on Python 3.12 and 3.13
  (`.github/workflows/tests.yml`).
- Pytest configuration with a branch-coverage gate (`fail_under = 39`,
  ratcheting) and a `dev` extra (`ruff`, `mypy`, `pytest-cov`, `psutil`).
- `Dockerfile` (python:3.12-slim, non-root) + `.dockerignore`; README Docker usage.
- Structured logging: `--log-format {console,json}` via a stdlib JSON
  formatter (`citeindex/logging_config.py`).
- Scale benchmark: `benchmarks/scale/run.py` with recorded runs under
  `benchmarks/runs/` — 10,052 unique pages processed at 44.1 ok-pages/min
  wall (3 workers), with documented failure modes.
- README Quickstart; `docs/design.md` (pipeline stages, outputs, extension points).

### Changed

- Ruff configured at `E4,E7,E9,F,I,UP,B` (line-length 120; E501 excluded for
  legacy long lines) — ~1,150 mechanical fixes (pyupgrade, import sorting) and
  44 manual fixes; clean.
- mypy configured (pragmatic baseline; `citeindex.vertical_llm` excluded with
  a TODO) — 89 errors fixed; clean.
- Package metadata and README citation snippet bumped to 0.15.0.

### Fixed

- `CitationLLM` called `_truncate_text(max_tokens=...)`, a keyword that did
  not exist; corrected to `max_chars` (that path previously raised
  `TypeError`). Found by the type-check pass.
- Assorted latent issues surfaced by lint/typing (see the plan's execution
  record).

### Notes

- No changes to ingestion semantics; persisted `csl.json` remains read-only
  for humans and agents.
- Known follow-ups: no supported flag disables LLM citation extraction
  (dominant source of benchmark failures); `vertical_llm.py` cleanup; OCR-path
  benchmarking.
