# Post-extraction online enrichment — plan (2026-09-27)

Status: **plan only, no code written.** Owner decisions recorded below.
Scope: **PDF modalities only** (digital + scanned) in v1.

Implementation details and updated execution order are maintained in
[the revised implementation plan](2026-09-27-online-enrichment-plan-v2.md).
Owner-confirmed update: retain automatic T1/T2 overwrites, including source-repaired
values; apply source repair before enrichment; verify explicit enrichment proposal
identifiers first, then continue discovery for remaining missing fields.
AI discovery supports Claude, Gemini, and OpenAI native web search; search execution and
supporting citations are mandatory whenever AI discovery is used. See v2 for the
provider contracts and independent registry-validation requirements.

## 1. Problem

CiteIndex resolves online metadata today only by **exact identifier**:
`metadata_registry.lookup_crossref_doi` (`metadata_registry.py:198`) and
`lookup_openlibrary_isbn` (`:271`). If extraction finds a **title (+author) but no
DOI/ISBN**, there is no path to complete the record. If extraction finds **no title**,
searching is too ambiguous and the stage must skip.

## 2. Two post-extraction stages (clarified)

Both run after DSPy/deterministic extraction. They are independent and both may apply
changes per the trust tiers in §6.

| Stage | Where | Trigger | Existing? |
|---|---|---|---|
| **A. Online enrichment** | CLI, inside ingestion before finalize | automatic, flag default **on** | new (this plan) |
| **B. Re-verification / repair** | Agent harness (skill + subagent) | manual / agent-driven | exists (`citation-verification` skill, `@citation-verifier`), needs extension |

Verified facts behind this split:
- `--verify-citations` is a **CLI** stage (`cli.py:191` → `cfg.verify_citations` →
  `master.py:143`), not harness-driven.
- The harness (`citation-verifier.md`) sets `webfetch: deny`, `websearch: deny`, and
  only emits proposals; `--repair-proposal` requires a **source quote** and
  re-ingestion reproduction, so it cannot carry a registry-derived fill.
- Therefore Stage B needs a **new proposal kind** (`registry_fill`) and a **new apply
  path** to fill fields the source never printed. Stage A uses the same primitives
  in-pipeline.

Decision 13.1 (earlier open question) is resolved: **keep the two stages separate**
rather than merging `verify_citations` and enrichment. Rationale: they answer different
questions — *"did we transcribe the print correctly?"* (source evidence) vs *"does an
external catalog agree / can it complete us?"* (registry evidence). A single precedence
table (§6) governs both.

## 3. Placement

```
extraction (dspy + deterministic seeds)
   → candidate_csl + _field_status
→ author validation                          master.py:112-140
→ verify_citations (CLI, source-evidence only)   master.py:142-146
→ [NEW] online enrichment                    master.py ~:147
→ repair_proposal                            master.py:148
→ standardize_csl_json (id, content_hash, merkle_root)   master.py:155
→ csl_folder_name → store_corpus_artifacts → library markdown
```

Must run **before `standardize_csl_json`**: `content_hash`, `id`, and `csl_folder_name`
all derive from the CSL (AGENTS.md: never edit a persisted `csl.json` directly;
regenerate dependent hash/folder/JSON/Markdown).

Gate: run only when `title` is source-supported (not `provisional-filename`/empty) and
at least one target field is `missing`. Skip under `offline_verification`.

## 4. Control flow

1. Gate (§3). If no title → skip, log reason.
2. Targets = fields where `_field_status[field] == "missing"`.
3. If record already has DOI/ISBN → verify + fetch (§5 step A/A').
4. Else catalog fan-out by title(+author/year) → score → accept (§5 B).
5. Else AI identifier-locate (whitelisted domains) → verify (§5 C).
6. Merge per trust tiers (§6); annotate provenance + new field states (§7).
7. Render Chicago locally (§9). Then return to the normal finalize path.

## 5. Cascade (endpoints verified during research)

| Step | Source | Call | Role |
|---|---|---|---|
| A | Crossref | `works/{doi}/agency` then `works/{doi}` | DOI authority + full record |
| A' | DataCite | `dois/{doi}` | non-Crossref DOIs (theses/datasets/reports) |
| B | OpenAlex | `works?search=` / `filter=title.search:` | free, no key; article coverage |
| B' | Crossref | `works?query.bibliographic=` | fuzzy title+author discovery |
| B'' | OpenLibrary | `search.json?title=&author=&fields=` then hydrate `edition_key` → `/books/OL…M.json` | books/ISBN |
| B''' | DataCite | `dois?query=titles.title:…` | theses/datasets |
| C | AI web search | hosted API, `allowed_domains` = {doi.org, crossref.org, openlibrary.org, datacite.org, publisher sites} | returns an **identifier only**, or null |

Scoring: normalize title (casefold, strip punctuation/leading articles/diacritics) →
`token_set_ratio`; require **≥ 0.90** + author surname overlap + year ±1. Ties →
unresolved (never pick arbitrarily). CJK runs the full cascade (owner decision) with
exact-normalized title + publisher/year; **CJK vs Latin recall reported separately** in
P5 (OpenAlex has no ISBN/place; OpenLibrary is community data; CJK tokenization is
weaker — lower recall is expected, not a bug).

## 6. Trust tiers and precedence

Resolution precedence (highest first):

| Tier | Basis | Fill missing | Overwrite source value |
|---|---|---|---|
| T1 | **DOI round-trip** (Crossref/DataCite agency) | yes | **yes, freely** (unique identifier is authoritative) |
| T2 | **ISBN round-trip + title/author agreement** | yes | **yes**, agreement-gated |
| T3 | Catalog match, **no identifier**, ≥2 sources agree | yes | no |
| T4 | **AI-located identifier → verified** | yes | no |
| T5 | AI-authored fields, no identifier | suggestions → `needs_review` | no — **deferred** behind P5 |

T5 is not live in v1. Owner allowed "2+ catalogs corroborate" in principle; it is
gated on the P5 measurement because corroboration is only as trustworthy as the
underlying matching.

## 7. Evidence and provenance contract

Reuse the `_result()` shape (`metadata_registry.py:174-195`): `{status, candidate,
provenance{provider, request_identifier, request_url, http_status, response_digest}}`;
no raw bytes, no contact email persisted.

- New field states: `registry-sourced` (filled missing), `registry-corrected`
  (overwrote), alongside existing `missing` / `source-supported` / `unverified`.
- New report block (in `citation_verification.json` or a sibling
  `online_enrichment.json`): per field `{tier, provider, request_url, source_id,
  raw_sha256, retrieved_at, matched_title, matched_author, score}`.
- Rule: **a fill with no provenance is not written.**

## 8. Config and flags

| Flag | Default | Behavior |
|---|---|---|
| `--online-enrich` / `--no-online-enrich` | **on** | master switch |
| `online_enrich_providers` | `openalex,crossref,openlibrary,datacite` | catalog set |
| `online_enrich_ai_fallback` | off | enable step C |
| `online_enrich_ai_model` | none | provider-qualified model |
| `online_enrich_min_score` | `0.90` | title match threshold |
| `online_enrich_cache_ttl` | 7d (proposed) | TTL cache |
| honors | — | `offline_verification` (→ skip), `registry_contact_email` (UA) |

Politeness (none of this exists today — must be added): TTL cache; backoff + 429
handling (currently one bare retry); Crossref/OpenAlex `mailto` polite pool;
OpenLibrary 1–3 req/s; DataCite 500/5 min; per-source timeout; a failing provider is
skipped, never blocks ingestion.

## 9. Chicago rendering

Use the existing, currently-unused renderer: `citation_style.py:34
format_bibliography` + `citeindex/styles/chicago-author-date.csl`. **Never** ask a
model to format — it silently alters years.

## 10. Agent-harness extension (Stage B)

Today the harness cannot do this. Required additions:
1. Web-allowlist capability for a **new** `citation-enricher` subagent (catalog domains
   only). Keep `@citation-verifier` strictly source-only.
2. New evidence type `registry_fill` (identifier round-trip + provenance, no source
   quote).
3. New apply path `--enrich-proposal proposal.json` (sibling of `--repair-proposal`)
   that verifies the round-trip and regenerates CSL/hash/folder/JSON/Markdown.
4. Skill update: add an "online enrichment (registry-backed)" section stating the tiers
   and the no-model-fields rule.

## 11. Testing and measurement (P5 = go/no-go)

- Offline unit fixtures per provider (no network in tests).
- Held-out fill test: blank one field on known-good rows; measure **recovery rate** and
  **wrong-work rate**; report CJK vs Latin separately.
- Go/no-go: *correct* fills (not fills), wrong-work rate, % core requirements improved.
- T5 flips on only if P5 shows corroboration is safe.

## 12. Phasing and exit criteria

| Phase | Deliverable | Exit |
|---|---|---|
| P0 | contract + evidence/provenance types (shared by A and B) | reviewed spec |
| P1 | OpenAlex + Crossref search client | offline fixtures green |
| P2 | scoring + merge + tiers T1–T4 | tier tests green |
| P3 | identifier verify + cache + backoff + gates | no unhandled 429; offline skip verified |
| P4 | harness assets (enricher subagent, `registry_fill`, `--enrich-proposal`) | agent can fill a missing field on a real PDF folder |
| P5 | measurement | wrong-work rate acceptable; T5 decision |

## 13. Scope, risks, non-goals

Scope: **PDF only** (digital + scanned) in v1; URL/media deferred (owner).

Risks / flagged tensions:
1. Two registry stages with distinct rules — resolved in §2/§6 (separate stages, one
   precedence table).
2. Default-on network calls change offline/CI behavior — must degrade gracefully and
   honor `offline_verification`.
3. Wrong-work matches are the real hazard → P5 gate before trusting T5 / default-on CJK.
4. Coverage holes are expected (OpenAlex lacks ISBN/place; OpenLibrary is community
   data; Crossref lacks many monographs) — not bugs.
5. AI web-search + strict JSON-schema is not documented by providers — validate
   empirically in P3/P4.

Non-goals: no AI-authored fields into CSL in v1; no AI formatting; no direct edit of a
persisted `csl.json`.
