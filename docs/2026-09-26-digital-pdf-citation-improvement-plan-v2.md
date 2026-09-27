# Digital-PDF Citation Extraction — Improved Plan v2 (research-backed)

**Date**: 2026-09-26 · **Status**: proposed, pending approval
**Supersedes**: the extractor (P1) portion of `2026-09-26-digital-pdf-citation-improvement-plan.md` §3. The v5 scorer (P0) stays — proven, zero side effects.
**Research method**: 3 parallel librarian lanes (page location / field extraction / ZH CIP + LLM-era methods), all claims URL-cited; unverified items flagged.

## 1. What the 2026-09-26 benchmark run proved

- Scorer v5: title F1 0.125→0.500, exact 0.0625→0.125, zero non-title drift. **Keep.**
- Extractor P1 (wider window 1-10, budget 18k): no benefit, possible mild regression (title 0.500→0.375, author 0.538→0.333 single-run). Widening a fixed window is the wrong lever — the research below explains why and what replaces it.

## 2. Key research findings

### 2.1 Nobody locates book imprint pages with an LLM — and we shouldn't either
- **GROBID** has a 16-label monograph model in source (incl. a "publisher page" label: cover/title/publisher/TOC/preface/index…) but it is **dormant**: no shipped model dir, no `grobid.yaml` entry, no REST endpoint, training corpus = 1 sample from 2012 (github.com/kermitt2/grobid `MonographParser.java`, `GrobidModels.java`). Its shipped header model is article-only, F1 0.7425, "quality for other languages unpredictable". There is **no published benchmark** for locating a book's imprint page. → No off-the-shelf locator exists; we must build a thin one.
- **PageIndex Flash** (open, MIT-ish) has the best concrete prior art, directly portable to PyMuPDF: cover-like-page detection (`page_chars < 0.5 × min(doc_median, 5000)` in first `1+min(15, n/5)` pages), title-candidate gates (`char_count < 400`, `height < 2×width`, font-size vs **document body-font baseline**, centered), and **watermark detection**: skewed blocks with x-centre outside the central 80% of page width whose normalized text recurs on ≥3 pages → drop (github.com/VectifyAI/PageIndex `flash/title/scoring.py`, `flash/classification/toc_boilerplate.py`). Also ships multilingual dictionaries (42-language TOC titles, 参考文献 variants, 章/部/节/篇/卷).
- **CERMINE** zone features worth copying: char-density (sparse zone ⇒ furniture, not body), cue-phrase test (`although, therefore…` present ⇒ body text, not metadata).
- **Japanese 奥付** sits at the *back* of the book, but ZH/EN imprints do not — our "last 3 pages" window is only a JP-specific win. ZH CIP (版权页) = title-page **verso, physical ~2–4**; EN copyright page = title verso, often physical 4–9 with front cover/blank pages offsetting it.
- **Decisive design rule** (all lanes converge): do location **deterministically over all pages first**, then hand the LLM a ranked, annotated shortlist. Our measured failures (corrupt `ALEXANDRI A` title blocks beating the clean CIP on the *same page*; imprints missed at pages 6/9) are exactly the failure mode of asking an LLM to infer location from ~1385-char previews that hide marker words and font statistics.

### 2.2 ZH CIP and EN imprints are grammar-regular — parse them with regex, validate by ISBN checksum
- **GB/T 12451—2023** (effective 2024-04-01) defines the full CIP field grammar with fixed punctuation: `正书名 = 并列书名 : 其他书名信息 / 第一责任说明 ; 其他责任说明 . -- 版本说明 . -- 出版地 : 出版者 , 出版日期 . -- （丛书名 ; 丛书编号）. -- ISBN : 定价` + `Ⅰ. 分类号` + 核字号 (10-char: year + 6 chars incl. ≥1 uppercase letter). Header anchor `图书在版编目（CIP）数据`; cert anchor `…CIP数据核字（YYYY）第N号` (openstd.samr.gov.cn, hcno=4AF9725FBE25C9A878BAC0D0CA8E119D).
- **Existing OSS parsers to port (not write from scratch)**:
  - `charlesilcn/PDF2BOOK` `cip_extractor.py` — CIP regexes with OCR-tolerant classes (`[．.·一。]`, `[（(]`, `[：:]`), handles CIP spanning two pages, normalizes `2019年6月→2019.6`.
  - `sabercomo/MEFinder` `bibliographic_metadata.py` — the best found: **confidence-scored candidates with explicit `metadata_conflicts`** (CIP statement 0.995, ©-year 0.99, LoC bracketed-year 0.9…), publisher-suffix regex (`出版(?:集团|社)|印书馆|书局`), publisher→place map (上海人民出版社→上海), and LoC RDA-block regexes (`Title:|Names:|Identifiers:|Description:` — block format stable since 2015-10-01, wayback loc.gov/publish/cip/techinfo/databook.html).
  - `DoiiarX/NLCISBNPlugin` — NLC OPAC ISBN lookup (Calibre plugin, Chinese CLC data).
- **ISBN validation is normative** (GB/T 5795-2006): mod-10 weighted checksum, weights alternating 1,3; `check=(10−sum%10)%10`. Also normative traps: multi-volume sets get one ISBN but **per-volume ISBNs coexist** when volumes sell separately (our "Homilies I–II" case); **reprints share the ISBN, editions don't** — never key edition year off ISBN alone.

### 2.3 Field extraction: verbatim discipline + authority merge + abstention
- **BiblioPage** (arXiv 2503.19658, 2,118 scanned title pages — the closest public benchmark to our task): all models struggle most with **title vs subtitle**; title errors concentrate on **multi-line / varying-font titles** — precisely our "Homilies I–II", "160 Homilies", "(7th–9th Century)" drops. Its annotation rule is the fix: *attribute = exact text printed on the page*; VLM+OCR best 70 mF1.
- **NuExtract** template discipline: declare `verbatim-string` types — value must be text exactly as it appears; absent → null (github.com/numindai/nuextract).
- **Self-verification pass with provenance spans** (arXiv 2306.00024): verification is asymmetric vs generation — cheap second pass that checks each accepted value against its quote. We already collect spans; this is a check pass, not a new extractor.
- **Abstain > guess** (arXiv 2405.02228): under uncertain evidence, emitting null beats hallucinating — our "Crack by RAOGY." author case.
- **ISBN-first authority merge is a measured win for EN**: on 11 ISBNs harvested from our own benchmark evidence, **OpenLibrary resolved 10/11** (Crossref only 1/11). OL gives `subtitle`, `publishers` as a **list** (joint publishers → our caridi failure), `publish_date`. GROBID's own consolidation (Crossref) lifted header F1 74.59→88.89 — the pattern works. **ZH authority is unsolved**: Douban API now key-gated (`apikey_required`), OpenLibrary romanizes ZH titles (1/5 ZH probe, and "The Apostolic Fathers" instead of 使徒教父著作), NLC OPAC is scraping-only. → EN merge only; flag ZH as open.
- **CSL semantics** (docs.citationstyles.org): `subtitle` is *not* in the official CSL-JSON schema (citeproc-js splits `title` internally) — our internal split is fine but must compose on export (v5 scorer now consistent); `collection-title` = series for books; `publisher` is a single string (join with "; "); CJK names = family/given order or `literal`; `editor`+`translator` both legal and expected.
- **VLM-on-PNG is NOT proven to beat the text layer** (arXiv 2505.05666: OCR-based generalizes better on degraded docs; no study for front matter). ConfBench (2608.01792): OCR+image modality improves *confidence estimates*, not extraction. → Skip vision for now.
- **Caution for the regex path** (arXiv 2606.12903): a rule-only extractor scored 1.000 on template-conformant pages and **0.0 once templates broke** — deterministic parses must be *candidates ranked by confidence*, never unconditional winners.

## 3. Improved plan

### Phase 1 — Deterministic front-matter locator (no LLM, ~1 focused module)
New `citeindex/ingestion/pipelines/frontmatter.py` (pure functions, PyMuPDF in, candidates out):
1. **All-page marker scan**: one regex pass over every page with a weighted lexicon — ZH anchors `图书在版编目`/`CIP数据核字` (w=10), `版权所有/出版发行/责任编辑` (2–6), EN `©|Copyright|All rights reserved|First published|Library of Congress|Cataloguing-in-Publication` (4–6), `ISBN` regex `97[89](?:-?\d){10}` (5). Output: ranked pages, top scorer always kept + 2 adjacent. Fixes the missed-page-6/9 failure class *by construction* (scan is not windowed).
2. **CIP/LoC-block parser** (port PDF2BOOK/MEFinder regexes, GB/T 12451—2023 grammar): emits title/subtitle/authors/place/publisher/year/series/ISBN as **candidate CSL fields with quotes + confidence** (CIP ≈ 0.99). Validate ISBN with the mod-10 checksum; reject parse if it fails.
3. **Noise/watermark filter** (PageIndex rules): drop skewed side-margin blocks recurring on ≥3 pages (kills "Crack by RAOGY"); collapse repeated-char runs before scoring (deflates "ALEXANDRI A" garbage lines).
4. **Title-page scoring** (PageIndex/CERMINE features): font-size vs doc body baseline, sparse-page, centered — as *tie-breaker only*. **Rule: marker-bearing pages outrank large-font pages.**

### Phase 2 — Restructure the DSPy stage around candidates
5. `LocateBibliographicPages` stops choosing pages blind: it receives the ranked shortlist with per-page annotations (marker hits, font stats, char count) and assigns roles (title page vs imprint) — location is now evidence-backed, and the quote validator stays.
6. Extraction merges candidates: deterministic CIP/LoC values are candidates with fixed high confidence; the LLM fills the rest verbatim (NuExtract/BiblioPage rules: *value = exact printed text; keep multi-line titles intact; series is its own role; null when evidence is garbage*).
7. Confidence-scored conflict resolution (MEFinder model): when candidates disagree (corrupt title page vs clean CIP on same page), higher-confidence wins and the loser is recorded in `metadata_conflicts` — this *by construction* fixes the cyril case.
8. Self-verification pass over accepted fields (quote must entail value); abstain on failure.

### Phase 3 — Authority merge (EN only) + cross-checks
9. ISBN → OpenLibrary (`/isbn/{isbn}.json`): merge subtitle, joint publishers (list), publish_date. **Merge, never overwrite** source-validated values. Skip for ZH (flagged open).
10. Cross-checks: ISBN checksum (already in 1.2), `issued ≥ original-date` for translations, volume-vs-set ISBN choice for multi-volume works.

### Rollback of the failed P1
- **User decision (2026-09-26): keep `page_range` default `"1-10, -3"`** (models.py:44, cli.py:151, README:313, regression test at test_host_metadata_regressions.py:38). Honest caveat: the 2026-09-25 baseline ran with the old `1-5, -3` window, so the window widening remains part of the extractor delta; the single-run noise caveat applies, and Phase 1's deterministic locator supersedes window-based locating for front matter anyway (the window remains the fallback for non-front-matter evidence).
- Keep: locate budget 18k, doc-type additions, rejection warnings.

### Measurement sequence (user decision 2026-09-26)
1. Implement Phases 1–3, then benchmark config **(a) `--no-layout --no-pageindex`** first.
2. Only if (a) shows no improvement: run **(b) layout-on, `--no-pageindex`** to test whether the ingestion layout step helps citation extraction.

### Explicitly not doing
- VLM-on-PNG extraction (no evidence it beats text layer; revisit with our own BiblioPage-style eval if OCR-noise cases persist).
- GROBID monograph / AnyStyle / biblio-glutton adoption (dormant, reference-string-only, or requires self-hosted ES).
- Douban / PDC / NLC scraping for ZH authority (gated or undocumented — open problem).
- Back-matter window widening for ZH/EN (only helps Japanese 奥付).

## 4. Failure-mode → fix mapping (measured 2026-09-26 corpus)

| Observed failure | Fix |
|---|---|
| cyril: corrupt `ALEXANDRI A` blocks beat clean CIP on same page | P1.2+P2.7 (CIP candidate 0.99 > OCR garbage) |
| augustin: `Crack by RAOGY.` author | P1.3 watermark filter + P2.8 abstention |
| gno1 `I–II` / final_160 `160` / byzantium `(7th–9th c.)` title drops | P2.6 verbatim multi-line title rule |
| caridi joint publishers lost | P3.9 OL `publishers` list |
| ambrose series-vs-paren confusion | P2.6 series as separate role + P1.2 丛书 field |
| issued missing ×5–6 | P3.9 publish_date merge + P1.2 CIP year |
| Imprints on pages 6/9 missed | P1.1 all-page scan |

## 5. Verification

1. **DONE 2026-09-26 — Parser self-check**: `tests/test_frontmatter.py`, 12 tests (GB/T §8.3 template, real CIP records, LoC RDA block, ISBN checksum, watermark/ranking, no-false-positive). Suite: 139 passed, 3 skipped.
2. **DONE 2026-09-26 — Marker-scan eval, 16 sources (offline, free)**: `rank_candidate_pages(top_k=8)` covers ≥1 gold-evidence page on 14/16; 9 fully. Misses: cyprian gold p.15 (Brill encyclopedia *entry* page — mid-doc, not imprint; comes from region hints/window), byzantium gold p.1 (title page, zero markers — covered by the 1-10 window union). Key win: the previously-missed imprints (debates/caridi pp. 5–6) now rank #1-2. Design consequence: shortlist must UNION with the window, not replace it.
3. **DONE 2026-09-26 — Phase 2 wiring (implemented orchestrator-direct after 3 fixer-lane spawn failures)**: window union with marker-scan hits (`_deterministic_scan` in `locate_bibliographic_pages`); evidence-gated CIP/LoC seeding (`_seed_deterministic_candidates`, confidence ≥0.95 only, `validate_block_evidence` enforced — deterministic provenance is not a bypass; whole-volume vs chapter field gating per `_CIP_ALLOWED_BY_TYPE`); watermark-line filtering off ranked pages (`_filter_repeated_lines` + `frontmatter.repeated_lines`); `_metadata_conflicts` audit key (underscore namespace — never a CSL field). Tests: `tests/test_candidate_merge.py` (9 tests, incl. two-page CIP span, unanchored-CIP rejection, no-marker regression). Suite: 157 passed, 3 skipped.
4. **DONE 2026-09-26 — Phase 3 (OpenLibrary lane, via fixer)**: `lookup_openlibrary_isbn` + `normalize_isbn` + `normalize_openlibrary_edition` in `metadata_registry.py` (mirrors Crossref provenance shape; publishers/places lists joined " and "; publish_date → year); `openlibrary_enabled` config flag; `verify_citation_metadata` falls back DOI→ISBN registry when Crossref misses (single-registry flow); `subtitle` added to `_RECONCILABLE_FIELDS`. Tests: +10 (mocked, no network). LLM self-verification pass (plan item 8) deferred — quote validation + seeding covers the measured failure modes; add only if benchmark (a) still shows evidence-ignoring values.
5. **DONE 2026-09-26 — Benchmark (a) run p2** (config `--no-layout --no-pageindex`, frozen gold, v5 scorer; `citation-pages-digital-2026-09-26-p2.jsonl` + `-report-v5.json`): title F1 0.375→**0.5625** (tp 6→9), exact records 1→**2** (caridi, 2003_希腊文圣经史, chrysostom → exact; 0 title regressions), issued 0.857 held, type 0.875 held, ISBN 0.833 (fn 1). Attribution: ALL title fixes came from the **window union** (imprint pages 5-6 + neighbors now searchable; 10/16 sources had expanded `search_pages`); CIP/LoC seeding contributed 0 — post-run log analysis found the seeding crashed (`TypeError: unhashable dict` on LoC author values) in real runs, i.e. p2 measured window-union-only. Two seeding bugs fixed after p2: (1) dedup key now `json.dumps` (dict values), (2) LoC `author` dict needs the CSL list wrap + `"author"` added to `_CIP_FIELD_MAP`/`_CIP_ALLOWED_BY_TYPE` (whole-volume types only — chapter/article keep imprint fields only, the CIP author is the book's, not the chapter's). Suite 157 passed, 3 skipped after fixes.
6. **DONE 2026-09-26 — Benchmark (a) runs p3-p5** (same config/gold/scorer; `citation-pages-digital-2026-09-26-p{3,4,5}.jsonl` + reports): **p5 final: title F1 0.688 (tp 11/16), exact records 0.1875 (3/16), zero title regressions vs p2** — the ≥0.6 bar cleared. Trajectory: p2 0.5625 (window-union only, seeding crashed) → p3 0.500 (seeding live but truncated-subtitle bug, caridi regressed) → p4 0.5625 (truncation guard fixed, p2 concordance restored) → p5 0.688 (OCR-tolerant evidence matching added, augustin + gregory fixed). p2↔p4 concordance on all 16 title states = the window-union improvement is stable, not seed noise. Side metrics held throughout: issued 0.857, type 0.875, ISBN 0.833, publisher 0.692; author 0.4→0.462 net.
   - **OCR-tolerant evidence matching** (added after p4, measured by p5): `_quote_in_block`/`_compact` in `citation_verification.py` — quotes matching modulo whitespace still validate (`论三位一\n体` → `论三位一体`, `A ND MACARIU\nS` → `Gregory of Nyssa and Macarius`); exact-substring fast path unchanged; invented values still rejected. This was plan §4's "gno1 `I–II` / final_160 `160` / byzantium multi-line title" fix class. 3 regression tests added.
   - **Remaining 5 title failures are gold-convention, not extraction, errors**: ambrose (gold embeds series `(The Fathers of the Church, Vol 65)` in title), 12-nestorian + gno1 + byzantium (gold embeds scope/subtitle in title; predictions carry them as separate subtitle or printed-clean titles), final_160 (gold embeds quantity `160` in title). Predictions are defensible printed-title readings; resolving these needs a gold convention decision (compose subtitle into title at scoring, or accept both forms), not more extraction work.
   - **Author diffs are editorial convention** (Saint Ambrose vs Ambrose; full-form vs short-form names), not fabrications.
7. Single-run noise caveat: title-state concordance p2↔p4 (all 16) and p4↔p5 (13/16, 2 fixed 1 gold-convention flip) makes the win credible; author/subtitle single-run diffs remain within noise.

## 6. Effort estimate

- Phase 1: one module + tests, ~1 focused session (regexes are ported, not invented).
- Phase 2: moderate rework of `dspy_extract.py` locate/merge paths.
- Phase 3: small (`metadata_registry.py` already exists, DOI-only — extend to ISBN/OpenLibrary).
- Verification: steps 1–2 are free/offline; step 3 is one ~20-min cloud run (pre-approved pattern).

## 7. Full-workflow scope: ingestion + citation (added 2026-09-26)

Plan v2 phases originally target **Step 5 only** — citation/metadata extraction. The complete digital-PDF ingestion pipeline (`citeindex/ingestion/pipelines/digital_pdf.py:run()`) is:

| # | Step | Location | Touched by this plan? |
|---|---|---|---|
| 0 | Routing (digital vs scanned) | `master.py:route_to_pipeline`, `pdf_classifier` | no |
| 1 | Raw PyMuPDF extraction + cleanup | `digital_pdf.py:45`, `:477` | **indirectly** — watermark filter (new) |
| 2 | Layout analysis (pymupdf4llm GNN / heuristic) | `:483-515` | no (integration-tested, see below) |
| 3 | Image extraction | `:517-525` | no |
| 4 | Doc structure: footnotes, headers/footers, page-number map | `:528-574` | no |
| 5 | PageIndex section tree (LLM) | `:576-588` | no (integration-tested) |
| 6 | Citation extraction on RAW blocks | `:590-618`, `dspy_extract.py` | **yes — Phases 1-3** |
| 7 | CSL assembly, `_field_status`, nodes, Merkle, evidence locators | `:620-698` | **yes — candidates + `metadata_conflicts`** |

**Key integration fact**: citation evidence is deliberately built from *raw pre-layout blocks* (`:598-600` comment: "original PyMuPDF blocks before layout cleanup removes imprint text") — this is the same data `frontmatter.py` (Phase 1) consumes, so the locator serves both workflows.

**Benchmark caveat discovered**: the citation benchmark forces `--no-layout --no-pageindex` (`ingest.py:107-108` for PDFs with null `cli_args`) — i.e. steps 2+5 were in their weakest configuration during all measurements. Production defaults run layout + PageIndex. Nothing has ever measured extraction accuracy under the production configuration.

### Ingestion lane additions

- **L1 — Watermark/noise block filter** (PageIndex-Flash rules, Phase 1.3): apply at block level so corpus paragraphs, `source_blocks`, and evidence locators are all cleaned. Grounded: `flash/classification/toc_boilerplate.py` (skewed side-margin blocks recurring ≥3 pages → drop; repeated-char collapse). Fixes "Crack by RAOGY"-class garbage in the *corpus*, not just citation evidence.
- **L2 — Front-matter page tagging**: frontmatter.py's page-role output (title/imprint/CIP) annotates `document_json` pages, making imprint pages identifiable in the corpus (today: ordinary paragraphs). Small: one annotation pass in step 7.
- **L3 — Integration verification, three configs** (measures whether ingestion steps 2/5 help citation extraction; gold scores CSL only, so these remain citation-accuracy measurements under different ingestion inputs):
  - **(a) Baseline-comparable**: `--no-layout --no-pageindex` — identical to all prior runs; the primary post-v2 measurement.
  - **(b) Layout-on**: manifest variant with `cli_args: ["--no-pageindex"]` → `use_layout_analysis=True`, heuristic path (`pymupdf4llm` is NOT installed in the venv — verified 2026-09-26; the GNN path would need a new dependency and risks stripping imprint text from `ordered_text` per `digital_pdf.py:483-515`). Predicted effect on citation metrics ≈ 0 (evidence blocks are raw by design `:598-600`); real effects: printed-page locators, cleaner corpus. Cost: local CPU only, same cloud spend as (a).
  - **(c) Production config**: `cli_args: ["--force-digital"]` → layout + PageIndex tree (LLM-per-doc). Costs more; run only with approval.
  - Decision rule: if (b) ≥ (a), make layout-on the benchmark default going forward (closer to production); if (b) < (a), keep `--no-layout` and record that layout hurts citation evidence.
  - **Flagged**: installing `pymupdf4llm` (GNN) is a separate decision — the code deliberately routes citation evidence around it, and its body-only `ordered_text` would remove the imprint/CIP text Phase 1 depends on. Do not install as part of this plan.

### Not in scope (unchanged)
- Scanned-PDF pipeline (incl. the `dspy_extract.py:918-921` pattern fallback bug, excluded by earlier decision).
- Replacing PageIndex with deterministic Flash-style outlines (no measured failure; YAGNI — revisit if PageIndex cost becomes a problem).
- New gold annotations for ingestion quality (paragraph segmentation, footnote removal) — no benchmark exists; structural smoke checks + end-to-end citation metric only.

## 8. Open problems (flagged, unsolved)

- ZH authoritative metadata source: Douban key-gated, OL romanizes, NLC scrape-only. The CIP parse itself is the best ZH authority we have.
- Gold manifest lacks reviewer/adjudication metadata (validate_rows blocks `run.py main()`); the frozen-slice + direct-`score()` workaround in the v1 plan doc remains the honest measurement path until review completes.