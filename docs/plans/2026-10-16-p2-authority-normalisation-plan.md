# P2 — Entity-normalisation pipeline for citeindex (Data Scientist Pivot, Project 2)

Status: proposed — not started.
Date: 2026-10-16.

## Requested outcome and confirmed decisions

Close the feedback gap "no entity normalisation against authority sources"
by extending citeindex so extracted authors, journals and works are
normalised against **OpenAlex, Crossref, ORCID, VIAF**, and places against
**GeoNames / Wikidata**; candidate generation uses fuzzy matching **plus
embeddings**; ranking and a documented no-match threshold are explicit; a
hand-labelled gold set of 500–1,000 entities measures precision, recall and
the no-match rate; OpenRefine reconciliation is the comparison baseline.
Syriac/Greek/Latin names get the differentiator treatment (Syriaca.org,
Pleiades) where authorities hold entries.

Budget ≈45 h over weeks 3–5. Repo: citeindex. Optional stretch (only if the
cycle is ahead): the BHL re-aim — GBIF/WoRMS/IPNI/ZooBank + Darwin Core
output — deferred by default.

## Evidence from the current repository

| Finding | Source |
| --- | --- |
| Provider dispatch: registry URLs, enabled-check, lookup, search, normalizers already exist for crossref/openalex/openlibrary/datacite | `citeindex/ingestion/metadata_registry.py:365-370`, `:535-545`, `:700-738`, `:796-876`, normalizers `:126`, `:166`, `:430`, `:466` |
| Enrichment flow: orchestrator → field states → resolve → candidate loop → select → match → merge | `online_enrichment.py:383-510`, `:54-96`, `:306-320`, `:451-462`, `:220-238`, `:155-208`, `:241-270` |
| Candidate result shape (what new providers must emit) | `metadata_registry.py:204` (`status`/`candidate`/`provenance{provider, request_url, response_digest, source_id, retrieved_at}`) |
| Config surface: providers tuple, min-score 0.90, timeout, cache TTL, contact email | `models.py:31-95` (`online_enrich_providers` :63, `min_score` :64) |
| CLI flags already shipped for enrichment | `cli.py:216-232` (`--online-enrich`, providers, min-score, timeout, cache-ttl, ai-fallback), `:260` contact email |
| CSL authors carry `{literal|family|given|suffix|dropping-particle|non-dropping-particle}` only — no ORCID, no affiliation; place fields are bare strings (`publisher-place`, `event-place`) | `csl.py:28-33`, `:8-13` |
| AGENTS.md forbids editing persisted `csl.json` | `AGENTS.md` |
| Benchmark harness + manifest format + scorer | `benchmarks/citations/run.py:105-338`; manifest JSONL `{id, source_path, source_sha256, csl}` |
| No ORCID / VIAF / GeoNames / Wikidata / embeddings / rapidfuzz anywhere in repo or docs | recon 2026-10-16 (grep, Q9) |

## Recommended design

**D1 — new authority clients.** Add `citeindex/ingestion/authority/` with one
module per source (`orcid.py`, `viaf.py`, `geonames.py`, `wikidata.py`),
each exposing `search()` → canonical candidate dicts matching
`metadata_registry.py:204`, registered through the existing dispatch
(`_PROVIDER_URLS` :365, `_enabled` :535). Keep Crossref/OpenAlex as the
author/work backbone; the new sources fill name/place gaps.

API facts (verified 2026-10-16, unverified items marked): OpenAlex now issues
free API keys (10 req/s keyless, 30 req/s keyed; polite-pool `mailto` is
replaced by keys). Crossref polite pool via `mailto=` in URL/UA. ORCID
public API is read-only without auth (`/v3.0/expanded-search/`); rate limits
**unverified** — cache aggressively. VIAF's machine-readable JSON behaviour
is **uncertain after the 2026 redesign** (307 to a Next.js UI in recon) —
validate with a spike first; if dead, ship without VIAF and say so. GeoNames
requires a username (free, 20,000 credits/day). Wikidata SPARQL: 60 s query
limit, 5 parallel/IP, and a v2 endpoint migration is announced for
2026–2027.

**D2 — candidate generation with embeddings.** In the candidate loop
(`online_enrichment.py:451-462`), add a second generator: rapidfuzz
(`WRatio`, `token_set_ratio`) + optional dense embeddings
(`sentence-transformers` MiniLM, CPU) behind an optional extra
(`.[authority]`), merged with the existing anchors (numerals, type, edition,
name, year). Keep a pure-rapidfuzz path working with no extra installed
(ponytail: optional dependency, not a hard one).

**D3 — ranking + no-match policy.** Extend `select_candidate`
(`:220-238`) into a scored decision: weighted(fuzzy, anchor agreement,
embedding similarity) with per-field thresholds (author vs journal vs work
vs place); keep 0.90 as the default work threshold; record
`no_match` explicitly in provenance with the reasons. Document the policy
(ambiguous, homonym, script-mismatch cases) in the module docstring and the
report.

**D4 — sidecar links, not CSL mutation.** Persist results as
`authority_links.json` in corpus artifacts (one entry per entity:
system, id/URI, score, provenance), because CSL is a strict dict whitelist
(`csl.py:5-100`) and `csl.json` is untouchable. Library Markdown may show a
linked-author line; nothing else changes.

**D5 — gold set + evaluation.** `benchmarks/authority/` with
`gold.jsonl` (500–1,000 hand-labelled entities: authors incl. Syriac, Greek
and Latin names from the candidate's own library; journals; works;
publisher-places), labelling instructions, and `score.py` reporting
precision/recall/F1 per type + no-match rate. Compare three systems on the
same gold set: (a) today's `match_candidate` SequenceMatcher path,
(b) the new pipeline, (c) OpenRefine reconciliation on a documented subset
(reconciliation API spec; Wikidata service as the endpoint example).

**D6 — Syriac/Greek/Latin edge.** Gold set must include non-Latin names;
Syriaca.org (CC BY 4.0) and Pleiades (stable numeric place IDs, JSON per
place, dumps available) are the authority references. Syriaca download/API
URLs were **uncertain** in recon — confirm manually before coding that
client; if unavailable, document and use Wikidata for those names.

## Phases

| Phase | Weeks | Deliverable |
| --- | --- | --- |
| 1. Client spikes + provider modules (ORCID, GeoNames, Wikidata, VIAF-or-drop) | 3 | Modules registered; fixture-backed tests |
| 2. Embeddings + rapidfuzz candidate generation; ranking/threshold policy | 4 | `.[authority]` extra; pure-fuzzy fallback; policy docstring |
| 3. Gold set labelling (500–1,000 entities) + `score.py` | 4–5 | `benchmarks/authority/gold.jsonl` + report |
| 4. OpenRefine comparison + README/report; optional BHL stretch decision | 5 | Numbers table; interview line |

## Acceptance criteria and numbers

- Gold set ≥500 entities, with labelling provenance; stored under DVC or as
  small git JSONL.
- Report: P/R/F1 per entity type + no-match rate; delta vs the
  SequenceMatcher baseline; latency per 100 entities and per-provider cache
  behaviour.
- No fabricated matches: unmatched entities are reported as `no_match`, and
  the rate is a headline number, not a failure to hide.
- Interview line: "On 500 hand-labelled entities, author matches went
  from X to Y F1; the no-match rate is Z and here is the policy."

## Non-goals

- No UI; no changes to ingestion defaults (enrichment stays opt-in:
  `online_enrich=False` default, `models.py:62`).
- No mutation of `csl.json`; no new always-on dependency.
- No BHL/taxonomy track unless explicitly approved after M-P2.

## Risks

- VIAF/ORCID/Syriaca API uncertainty (validate before building; drop with a
  documented caveat rather than fake results).
- Embeddings add ~400 MB of torch if installed — kept optional.
- Syriac/Greek names may simply be absent from authorities → honest
  `no_match`, reported.

## Sources checked

- OpenAlex auth/search/disambiguation docs; Crossref REST + etiquette;
  ORCID public API pages; VIAF search probe (uncertain); Wikidata Query
  Service user manual (limits + v2 migration); GeoNames web services +
  credits; Syriaca.org (CC BY 4.0, URLs uncertain); Pleiades JSON/downloads;
  GBIF/WoRMS/IPNI/ZooBank + Darwin Core terms (BHL stretch only);
  reconciliation-api spec; rapidfuzz docs. All fetched 2026-10-16.

## Execution record — Phase 1 / D1 (2026-10-17)

Live spikes (2026-10-17): ORCID expanded-search works keyless read-only
with `Accept: application/json` (XML otherwise); Wikidata
`wbsearchentities` chosen over SPARQL for name search (plain public JSON
endpoint, no query-timeout policy); GeoNames free server is
`http://api.geonames.org` (its https certificate does not match the
`api.` subdomain) and requires `GEONAMES_USERNAME`.
**VIAF dropped**: `viaf.org/viaf/search` 307-redirects to the human UI
even with `httpAccept=application/json`, and non-browser clients receive
Cloudflare challenges (403). Revisit only if VIAF publishes a stable
machine-readable API.

Implementation: new package `citeindex/ingestion/authority/`
(`orcid.py`, `wikidata.py`, `geonames.py`) owns query shape, record
paths, and candidate normalisation; transport — pacing, the 12-request
budget, bounded reads, caching, provenance — stays in
`RegistryClient`. Sources register through `_PROVIDER_URLS`,
`_PROVIDER_INTERVALS`, `_PROVIDER_HEADERS` (ORCID JSON negotiation),
and `_SEARCH_RECORD_PATHS` in `search()`. Authority candidates are
`{type, authority{system,id,uri}, …}` and never work-shaped, so the
work matcher rejects them by construction. The GeoNames username is
redacted from provenance URLs. `models.py` accepts the new provider
names; defaults are unchanged (opt-in via `--online-enrich-providers`).
Deviation from the D1 text: modules expose query builders and
normalisers rather than standalone `search()` functions, so caching,
pacing, and budget live in one transport.

Evidence: ruff + mypy clean (55 source files); pytest **396 passed**
(+17 fixture-backed authority tests), coverage 40.53%; live smoke
through `RegistryClient`: ORCID "Wright, Nathan" → 5 candidates;
Wikidata "Antioch" → 5 (Q200441, "ancient Greek city in southern
Turkey").

Manual step for later phases: create a free GeoNames account, enable
the API, and export `GEONAMES_USERNAME` (the demo account is exhausted
and must not be used).

## Execution record — Phase 2 / D2 + D3 (2026-10-17)

Implementation: `citeindex/ingestion/authority/matching.py` owns the
scored policy. `fuzzy_similarity` tiers: rapidfuzz
`max(WRatio, token_set_ratio)/100` when installed (stdlib
`SequenceMatcher` otherwise; explicit `engine=` override). Optional
`MiniLMEmbedder` (all-MiniLM-L6-v2, lazy process singleton,
`None` without the extra). `match_entity` scores
`(fuzzy, embedding, anchor)` — person: family+given agreement with
initial-tolerant given names; place: country/admin1 as positive-evidence
anchor; script mismatch (CJK regex) skips embeddings and is recorded.
`select_authority_candidate` emits explicit decisions: `linked`
(unique top ≥ threshold and ≥0.05 clear of the runner-up),
`no_match` (`below_threshold`, `ambiguous_identity`, `no_candidates`),
`skipped` (`budget_exhausted`), dedupes by `(system,id)`, ignores
work-shaped results, and snapshots source + provenance + runner-ups.
Initial thresholds — person 0.85, place 0.80, journal 0.90 — are
conservative and tuned on the phase 3 gold set, not anecdotes.
Documented no-match cases: homonym ambiguity, transliteration drift
(Antakya vs Antioch), cross-script surfaces.

Wiring: `resolve_authority_entities` in `online_enrichment.py` — opt-in
via `online_enrich_providers`; persons (first three authors) → ORCID,
publisher/event place → GeoNames + Wikidata pooled; decisions land in
`report["authority_decisions"]` and never mutate the CSL record
(sidecar persistence is deferred to the gold-set phase). Work matching
(`match_candidate`, 0.90 title threshold) is untouched — it is the
benchmark baseline D5 must not regress.

Packaging: `authority = ["rapidfuzz>=3.0", "sentence-transformers>=2.7"]`
extra; rapidfuzz also in `dev` so CI exercises the rapidfuzz tier; the
embedding tier is stub-tested (torch stays out of CI).

Evidence: ruff + mypy clean (56 source files); pytest **412 passed**
(+16 matching tests), coverage 41.19%; rapidfuzz 3.14.6 installed in
the dev venv via uv (no pip in venv).

## Execution record — Phase 3 / D5 (2026-10-17)

Gold set: `benchmarks/authority/gold.jsonl` — **640 rows** (target ≥500):
480 persons (81 pre-modern *absent* across latin 48 / greek 21 / syriac 12
scripts, 399 present), 131 works, 9 journals, 20 places. Every label is
machine-asserted by the registry or hand-verified; unverified candidates
were excluded, not guessed (documented per-row `provenance` +
`label_status`). Seeds: `premodern.jsonl`, `verified_orcids.jsonl` (5),
`verified_places.jsonl` (20, Wikidata Q-ids; compound publisher lines
excluded); `review/` dumps preserve the hand-verification evidence.

Two real D1 registry bugs surfaced during gold build and are fixed +
regression-tested (uncommitted with the rest of P2):
1. ORCID returns `{"expanded-result": null}` on zero hits — was classified
   `malformed_response`, now `not_found`.
2. Search cache keys were clobbered by the record-path loop reusing the
   `key` variable, so every search of a provider overwrote one file;
   renamed to `segment` + regression test. Corrupted cached responses
   deleted before evaluation.

Evaluator: `benchmarks/authority/score.py` — one fresh `RegistryClient`
per row (12-request cap is per client), fresh candidate pools per row,
both systems score the same pools. `new` = P2 policy
(`select_authority_candidate`; works through the untouched production
`select_candidate`), `baseline` = stdlib SequenceMatcher top-hit at the
same threshold. Outcome rules: present+correct id → tp; present+wrong id
→ fp; present+no_match → fn; absent+no_match → tn; absent+link → fp.

Headline numbers (report `benchmarks/authority/reports/20261017-113353.json`,
631 scored rows — 9 journal rows excluded from scoring because journal
candidate generation is deferred (D2 record); labels are kept in the gold
set):

| type | rows | P (new) | R (new) | F1 (new) | F1 (base) | no-match (new) |
|------|------|---------|---------|----------|-----------|----------------|
| person | 480 | 0.980 | 0.486 | 0.650 | 0.581 | 58.8% (98.8% correct on 81 pre-modern absent) |
| place | 20 | 1.000 | 0.300 | 0.462 | 0.645 | 70% |
| work | 131 | 1.000 | 0.855 | 0.922 | 0.922 | 14.5% (new == baseline by design: control) |

The policy trades ~4.5pp person recall for +34pp precision over the
SequenceMatcher baseline and correctly rejects 80/81 pre-modern names.
Place recall is honestly low: the GeoNames provider is unavailable (no
`GEONAMES_USERNAME`), so pools are Wikidata-only and the labelled Q-id is
often outside the top-5 Wikidata hits for forms like "Athens, Greece".
Latency: 29.9s per 100 entities live, 0.07s cached.

Deviations (documented in `benchmarks/authority/README.md`): field-sampled
scholars stand in for "the candidate's own library" (sampled from the
candidate's actual journal field set); known-item bias means recall is an
upper-bound signal while precision and no-match rate are the honest
headlines; works serve as an unchanged control.

Evidence: ruff + mypy clean; pytest **424 passed** (+9: 8 gold-set tests in
`tests/test_authority_gold.py` incl. offline evaluator math, +1 cache-key
regression test), 6 skipped, coverage 41.36% (gate 39%). D1–D5 remain
uncommitted per the no-unsolicited-commits rule; commit decision pending.
