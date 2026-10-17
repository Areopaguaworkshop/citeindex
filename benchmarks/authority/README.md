# Authority Matching Gold Set + Evaluator (P2 / D5)

A labelled benchmark for entity-authority matching: 640 known items across
persons, works, journals, and places, scored against the P2 policy
(`citeindex/ingestion/authority/matching.py`) and a SequenceMatcher baseline.

## Layout

- `gold.jsonl` — the gold set. One row per entity:
  `{id, entity_type, source, label:{system, id}, label_status: present|absent, origin, provenance}`
- `build_gold.py` — rebuilds `gold.jsonl` and the `review/` dumps. Live
  network (Crossref/ORCID), paced; caches into `cache/`.
- `seeds/` — hand-curated labels that are merged into the gold set:
  - `premodern.jsonl` — 81 pre-1900 authors (48 Latin, 21 Greek, 12 Syriac
    scripts). All are `absent` by construction: ORCID/Crossref assert no ORCID
    for people who died before identifiers existed, so a link to any of them
    is a fabricated-authority signal.
  - `verified_orcids.jsonl` — 5 ORCID labels verified by hand against
    ORCID's own profile pages (exact-name work-history match).
  - `verified_places.jsonl` — 20 place labels (Wikidata Q-ids) verified by
    hand from ORCID/Wikidata location records.
- `review/` — candidate dumps behind the hand-verification decisions.
- `score.py` — the evaluator (below).
- `reports/` — timestamped run outputs; committed as frozen evidence
  (the live API drifts, so re-runs are not byte-identical).
- `cache/` — API cache, gitignored.

## Labelling rules (certain labels only)

Every label is either machine-asserted by the registry itself or verified by
hand; nothing is guessed:

- **person/present** — ORCID iD asserted by Crossref on a deposited work
  (field-sampled), or hand-verified profile match (`verified_orcids.jsonl`).
- **person/absent** — pre-1900 author (`premodern.jsonl`).
- **work** — DOI of the Crossref deposit the row was sampled from
  (`has-orcid:true, from-pub-date:2016-01-01` filter), or library works
  accepted by title-fuzzy ≥0.8 + author/year agreement.
- **journal** — ISSN verified via Crossref works search (the `/journals`
  index truncates titles; do not trust it).
- **place** — hand-verified Wikidata Q-id.

Rows whose label could not be verified with certainty were **excluded**, not
guessed (e.g. ambiguous ORCID pools for common names, compound publisher
lines like "London and New York").

## Deviations and known limitations

- **Field-sampled scholars** as "the candidate's own library": sampled from
  works in the candidate's actual journal field set (Vigiliae Christianae
  etc.), per the plan's intent — the candidate's own library is not fully
  ingested yet.
- **Known-item bias**: labels come from registry deposits, so the pool
  usually contains the answer. Recall is therefore an upper-bound signal;
  precision and the no-match rate are the honest headline numbers.
- **GeoNames unavailable** (no `GEONAMES_USERNAME`): the place pool is
  Wikidata-only, which under-counts place recall — the labelled Q-id is
  often not among the top-5 Wikidata hits for forms like "Athens, Greece".
- **Journal scoring deferred**: journal labels are in `gold.jsonl`, but no
  journal candidate generator exists yet (D2 record), so `score.py` skips
  them and prints a note.
- **Works: new == baseline by design** — P2 deliberately left the work
  matcher untouched; both systems run the same `select_candidate` path, so
  the work row delta is 0 and serves as a control.

## Running

```
.venv/bin/python benchmarks/authority/score.py            # all types
.venv/bin/python benchmarks/authority/score.py --types person,place,work
```

One fresh `RegistryClient` per row (12-request cap is per client). First run
is live and paced (~30s per 100 entities); re-runs hit `cache/` (30-day TTL).
Set `GEONAMES_USERNAME` to add GeoNames to the place pool.

## Headline numbers (2026-10-17 run, report `20261017-113353.json`)

640 gold rows (480 persons incl. 81 absent, 131 works, 20 places; 9 journal
rows excluded from scoring as above):

| type | rows | P (new) | R (new) | F1 (new) | F1 (base) | no-match (new) |
|------|------|---------|---------|----------|-----------|----------------|
| person | 480 | 0.980 | 0.486 | 0.650 | 0.581 | 58.8% (98.8% correct on premodern absent) |
| place | 20 | 1.000 | 0.300 | 0.462 | 0.645 | 70% |
| work | 131 | 1.000 | 0.855 | 0.922 | 0.922 | 14.5% |

The policy trades ~4.5pp person recall for +34pp precision over the
SequenceMatcher baseline, and correctly rejects 80/81 pre-modern names.