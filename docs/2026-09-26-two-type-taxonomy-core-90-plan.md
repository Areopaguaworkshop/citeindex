# Two-Type Taxonomy + Core-Requirement 90% Plan (2026-09-26)

## 0. User decisions (this session)

1. **Two-type model**: `article` (cited with pages in bibliography) vs `book` (no pages).
2. **Book category core fields**: `type`, `author`/`editor`/`translator` (any present), `title`, `publisher`, `issued`.
3. **Article category core fields**: `type`, `author`, `title`, `container-title` (journal name), `volume`+`issue` (number), `page` (range). Chapter (article-in-book): same minus volume/issue, plus `editor`.
4. **Remove from book category**: `report`, `manuscript`, `entry-encyclopedia`.
5. Publisher-place remains second-rank (not core).
6. Scanned-PDF corpus shares the same type system (this plan's changes apply pipeline-wide; scanned re-measurement out of scope here).

### Decision points (need user confirmation, defaults chosen)
- **DP1 — report/manuscript → map to `book`** (default: yes; they're standalone works, cited like books).
- **DP2 — entry-encyclopedia**: recommended **article category** (entry lives in a container; cite container-title like a chapter: `entry-encyclopedia` profile = chapter profile). Alternative: fold to `book` and lose the container citation. Corpus impact: cyprian.
- **DP3 — thesis**: untouched by user's list → keep as its own type with book profile (no corpus impact; 0 sources).

## 1. Where we are under the NEW core definition (measured, p5, no code changes yet)

**Core-exact: 5/16 = 31.2%** (an-ethiopian, debates, florovsky, climacus, gregory).

Per-source blockers (verified against p5 predictions):

| Source | Category | Blocking core fields | Fix class |
|---|---|---|---|
| caridi | book | publisher: pred "Northern Illinois University Press" vs gold joint "Cornell University Press; Northern Illinois University Press" (BOTH printed on p1) | B: extraction |
| cyril | book | author: pred subject name "Cyril, Saint…" vs gold "Russell, Norman" (author printed on cover/title) | B: extraction |
| augustin | book | issued missing (year not yet found in blocks — verify printed year exists) | B: extraction (verify first) |
| 2003_希腊文圣经史 | book | publisher form: "The British Library; Oak Knoll Press" vs gold "…in association with…" | C/D: normalization or gold form |
| ambrose | book | title (gold embeds series), author "Saint Ambrose", publisher leading "The" | C+D: normalization + gold title |
| chrysostom | book (report→book) | author (gold: Mayer the compiler; pred: Chrysostom), issued (2021-07 missing) | B: extraction |
| final_160 | book | title (gold embeds "160"), author pred "Final" (garbage; true author Jacob of Serugh printed) | B+D |
| gno1 | book | title (gold embeds "Libri I–II"), author pred "Gregorii Nysseni" (that's the SERIES title, not the author) | B+D |
| cyprian | per DP2 | publisher over-extraction ("Brill" extra) if book; under article-profile core becomes container-title (✓ matches) | DP2 + D |
| byzantium | article (chapter) | author (Evans IS printed on title page, pred garbage), container-title/publisher/issued not extracted (chapter inside same-named catalog) | B: hard |
| 12-nestorian | article (article-journal) | container-title abbreviated ("Bulletin John Rylands Library" — full name likely on garbled first page), **volume/issue/page NOT printed anywhere in the supplied PDF** (verified: only running headers + folios 24–35) | E: authority lookup or gold field_status |

**Type accuracy under the collapsed taxonomy: 15/16 = 93.75%** (only byzantium wrong).

## 2. Honest 90% assessment

- Now: 5/16.
- High-confidence extraction fixes (caridi, cyril, augustin, chrysostom, final_160-author, gno1-author): → **9–10/16**.
- With approved scoring normalizations (Saint-prefix authors, leading-The publishers, publisher-join separator) + gold re-issue v2 (titles: printed title + separate subtitle, no series/volume/quantity embedded — ambrose, gno1, final_160): → **12–13/16**.
- The last 3 (byzantium, nestorian, 2003/cyprian edge cases) are each independently uncertain → **90% (15/16) is the ceiling, not the expectation**. Expected outcome after this plan: **65–80%**. 90% requires ALL of: gold v2 edits (user-gated), byzantium's same-name-chapter extraction landing, and nestorian's numbers coming from Crossref title-lookup (new capability) or a gold field_status ruling ("not printed in supplied scan" → excluded from evaluation).
- Nestorian's page range could be folio-derived (first/last printed folio = 24–35) but gold says 23–35 — likely the true first page is the OCR-garbled PDF page 1. Off-by-one risk documented.

## 3. Phases

### Phase A — Taxonomy collapse (mechanical, ~1 lane)
- `csl.py`: move `entry-encyclopedia` to article-category profile (`chapter` branch: container-title + page per DP2); `evaluation_fields` for book drops nothing (report/manuscript never had distinct profiles).
- `common.py:doc_type_to_csl_type`: `report|manuscript` → `book` (keep `thesis`).
- `dspy_extract.py`: locate accepted set → {book, chapter, article-journal, thesis, unknown}; prompt line updated; `_CIP_ALLOWED_BY_TYPE` report/manuscript/entry-encyclopedia keys removed (book/chapter semantics already cover them).
- Tests: update locate tests; add mapping regression (report→book, entry-encyclopedia profile change).
- `CSL_TYPES` validation stays lenient (old artifacts must still load); only *emission* collapses.

### Phase B — Core-field extraction fixes (dspy_extract prompt + wiring)
- **Title-page author priority**: author comes from title/copyright pages, never from the subject of a work (cyril), never a series title (gno1 "Gregorii Nysseni Opera" → collection-title), never title words (byzantium "Byzantium", final_160 "Final"). Prompt rule + the evidence validator already gates.
- **Joint publishers**: when two presses are printed together, emit both in printed order joined with "; " (caridi p1_b1, 2003 imprint). Matches gold convention.
- **Bibliographies/reports**: "compiled by X" → author=X (chrysostom→Mayer).
- **issued**: search-window already includes imprint pages; verify augustin/chrysostom printed years exist in blocks first (offline, free); if absent, mark abstain (never guess) and record in plan §open problems.
- Each fix lands with a fake-LLM unit test like tests/test_candidate_merge.py.

### Phase C — Scoring normalization (equivalence, not extraction; values stay verbatim)
In `benchmarks/citations/run.py` `_equivalent` only (extraction untouched — printed values are preserved):
- Author equivalence: strip honorific prefixes ("Saint"/"St.") on comparison.
- Publisher equivalence: strip leading "The "; treat "; " and " and " joins as equal sets.
- These rules are listed in the report; they are measurement definitions, user-approved via this plan.

### Phase D — Gold re-issue v2 (USER GATED — do not touch gold without explicit approval)
- Re-type: chrysostom report→book; cyprian per DP2.
- Title convention: gold titles become printed title + separate `subtitle`; series/volume/quantity move out of title (ambrose, gno1, final_160).
- Nestorian: user ruling — either field_status `outside_scope` for volume/issue/page (not printed in supplied scan) or leave as stretch goal for Phase E.
- 2003 publisher full form; cyprian publisher per DP2.
- Output: `digital-16-2026-09-26-gold-v2.manifest.jsonl`, sha recorded, v1 kept. Free (offline).

### Phase E — Article numerics (optional, only for nestorian-class cases)
- Folio-derived page range: first/last printed folio of the article (already have printed_page_label per block) — try for nestorian; verify PDF p1 garbled folio recovery.
- Crossref bibliographic title-search fallback in `metadata_registry.py` (new capability, mocked tests; production call is 1 request/source) — resolves volume/issue/page for articles whose scans omit them. Flag: only worth it if the user wants nestorian counted without a gold edit.

### Phase F — Measurement (config (a) `--no-layout --no-pageindex`, unchanged)
1. **Free first**: re-score existing p5 predictions under new core definition + Phase C normalizations + (if approved) gold v2 → predicts the C+D ceiling with zero cloud spend.
2. One cloud re-run after A+B (+E if built): new predictions file, score under new core, per-source grid vs the 90% line.
3. Success bar: core-exact ≥ 10/16 required; stretch 14–15/16. Single-run noise caveat (±3) persists for borderline sources; re-run any single-source flip that changes the headline.

## 4. Verification order (cheapest first)
1. Phase A+B offline: unit tests + full suite green (baseline 160 passed, 3 skipped).
2. Free re-score of p5 (F.1) → decides whether Phase E is worth building.
3. Cloud run (F.2) → the measured number.
4. Plan doc updated with per-source grid; honest deltas only.

## 5. Open problems
- byzantium: chapter whose title ≈ container title; author printed but container/publisher/issued may only exist on the catalog's copyright page (extraction may still miss → candidate for OpenLibrary/Crossref once ISBN/DOI known, or accept as hard case).
- nestorian 23 vs 24 first-folio.
- Scanned-PDF corpus re-measurement under the new taxonomy (separate plan).
- ZH authority (unchanged, plan v2 §8).

## 6. Execution record (2026-09-26)

- **Phase A landed**: locate emission folds report/manuscript→book (`dspy_extract.py`), `doc_type_to_csl_type`, `_CIP_ALLOWED_BY_TYPE`; `entry-encyclopedia` → article-category profile (`csl.py` chapter branch: container-title+page).
- **Phase B landed**: prompt rules (author from title/imprint only, Latin-genitive collection → person, compiled-by → author, joint publishers "; "), OCR-garbled CIP recovery in `frontmatter.parse_cip` (fuzzy anchors + collapsed-body parse, imprint fields only — augustin 2005).
- **Phase C landed**: scorer equivalence conventions (Saint-prefix, leading-The, joint-publisher set, quote-glyph fold) + `core_record_accuracy`/`core_records` per category, honoring `outside_scope`. Suite **170 passed, 3 skipped** (+10 tests, 0 regressions).
- **p6 benchmark** (config a, 16/16 ok, `citation-pages-digital-2026-09-26-p6.jsonl`): core **7/16 = 43.75%** on v1 gold (p5: 6/16), title F1 0.688 held, exact/equiv 3/16. Fixed vs p5: augustin (CIP seed), caridi (joint publisher). Regressed vs p5: cyril translator, gno1 editor, cyprian issued (prompt edits are non-monotone).
- **Simulated gold v2** (free re-score of p6): **11/16 = 68.75%**; remaining 5 failures are extraction-side (byzantium OCR digit, cyprian last-page year, cyril translator, final_160 leading numeral, gno1 editor/title/author). Diff doc: `docs/2026-09-26-gold-v2-diff-for-signoff.md`.
- **Next**: user sign-off on gold v2 (incl. nestorian container-title scope) → write v2 via adjudication tooling → p7 extraction rules for the remaining 5 → re-score.

## 7. Result — target met (2026-09-26)

Gold v2 approved and written: `digital-16-2026-09-26-gold-v2.manifest.jsonl`
sha256 `87c85649e4403673b6ae3645549d96d49c5d276a49218e4f9c974b40edd91e47` (7 rows changed, v1 frozen).

**Headline: 15/16 = 93.75% core-requirement accuracy** (title F1 0.875, exact records 0.3125).

| Run | core (gold v2) | note |
|---|---|---|
| p6 | 11/16 = 68.75% | taxonomy + CIP recovery + equivalence rules |
| p7 | 13/16 = 81.25% | + prompt rules (leading numeral, translator, EDIDIT) |
| p8 | 12/16 = 75% | **regression**: prompt bloat (volume/part, vernacular Latin, OCR-numeral clauses) |
| **p9** | **15/16 = 93.75%** | bloat reverted; deterministic translator seed + OCR-ordinal repair |
| p10 | 15/16 = 93.75% | confirming replay (LLM cache; zero field drift) |

Only miss: **gno1** (`author` Gregory of Nyssa / `editor` Jaeger printed only in Latin
genitive/nominative — `GREGORII NYSSENI`, `WERNERUS JAEGER`). The allowed miss.

### Key lesson: prompt prose is non-monotone
Adding constraint clauses to the `ExtractDocumentMetadata` docstring regressed sources that had
passed (byzantium title → filename, ambrose year → 2003 paperback reprint, caridi publisher →
NIU-only). Working rule: keep the prompt minimal; move every *deterministic* normalization into
code (`repair_ocr_numerals`, CIP role-aware seeding `_person_with_role`), and always diff
per-source between runs rather than tracking only the headline.

### Remaining known gap
- gno1 Latin-form names: would need either a gold edit (author literal `Gregorii Nysseni`) or a
  targeted translation map. Left as the allowed miss to keep the prompt from bloating again.
- Independent (non-cached) confirmation of 15/16 would require cache bypass; p9/p10 are
  deterministic replays of the same state.

## 8. Scanned-12 result — target met (2026-09-26)

Same two-category model + core-requirement metric, scanned slice. Full ingestion
pipeline per source; sampler narrowed to `1-10, -3`, `--no-layout --no-pageindex`.

Gold v2 approved and written: `scanned-12-2026-09-26-gold-v2.manifest.jsonl`
sha256 `a53db8256074d58bd6385040bfdeb353933050a159f798f683cb2fc4182846e4`
(6 edits across 4 sources; v1 frozen, sha `6ceba25a…` unchanged).
Diff/approval record: `docs/2026-09-26-scanned-gold-v2-diff-for-signoff.md`.

**Headline: 12/12 = 100% core-requirement accuracy** (vs frozen v1: 8/12 = 66.7%).

| Run | core (frozen v1) | core (gold v2) | note |
|---|---|---|---|
| sp1 | 2/12 = 16.7% | — | unmodified pipeline |
| sp2 | 8/12 = 66.7% | 12/12 = 100% | deterministic seeds + approved label review |

All per-source flips sp1→sp2 are forward (fiey, aphrahat, shizu, anthony-coptic,
book-step1, benedict, clemens, antonyletters); no regressions in the final state.

No remaining miss. An earlier draft called **book-step1-en** `publisher` an allowed miss
on the belief that every OCR copy was garbled; that was wrong — the tail page 474
(inside the `1-10, -3` sampler) prints `CISTERCIAN PUBLICATIONS KALAMAZOO, MICHIGAN`
cleanly. The imprint seed reads that block, so the field is recovered. (Digital's gno1
remains its own allowed miss; scanned has none.)

### What moved into code (no prompt edits this wave)

- `dspy_extract.py`: heading title/subtitle/author seeds (front-block bound, series/
  front-matter/garble skips, medial `' The '` split, following-L2 subtitle, two-token
  ALL-CAPS + editor-verb → author literal); copyright-year branch priority; imprint
  publisher/place seed (digit/garble-guarded, `edition` not a suffix word); translator
  credits (Latin same-line/bare-`by`, CJK `等译`); editor credits (bare Latin verb + VON
  lookahead, CJK `主编`); Roman-numeral imprint year; byline/title-line author seeds;
  scanned-only gate (`content_list is not None`) so the digital path is untouched;
  C1 pattern-fill never pins OCR-garbled titles.
- `citation_verification.py`: Roman-numeral imprint year vouches for its Arabic
  `date-parts` year.
- `common.py`: `validate_authors` keeps literal-only names.

Verification: `/tmp/opencode/seed-harness.py` (offline seed check vs all 12 OCR probes),
`/tmp/opencode/sp2-score.py` (frozen + v2 + flip report). Suite **190 passed, 3 skipped**
(+30 in `tests/test_scanned_seed_rules.py`).

### Key lesson (same as digital, reinforced)

Regressions were introduced *within* this wave and caught only by scoring the finished
run, not by the offline seed harness: astudy title (C1 pattern-fill pinned a garbled
heading when the LLM rejected its own title in that run) and aphrahat author (the LLM
preferred the LoC catalog spelling over the title-page spelling). Both are run-varying
LLM choices that the deterministic layer must override — the harness only tests seeds,
so per-source diffing of the finished run remains mandatory.

A third failure mode was different in kind: I deleted a working imprint seed and wrote
"garbled in every occurrence" into the README, the gold-diff doc, and this plan doc,
based on reading only the first two OCR occurrences. Reading the *whole* probe (all
sampled pages, tail included) showed a clean imprint. Lesson: before declaring a field
unrecoverable, exhaust every sampled page — a tail page can carry the clean copy. The
false claim was caught only when the docs were re-verified against the probe.