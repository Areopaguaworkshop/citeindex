# Scanned gold v2 re-annotation diff — for user sign-off (2026-09-26)

**APPROVED and written** (user, this session):
`scanned-12-2026-09-26-gold-v2.manifest.jsonl`
sha256 `a53db8256074d58bd6385040bfdeb353933050a159f798f683cb2fc4182846e4`.
**v1 frozen**: `scanned-12-2026-09-26.manifest.jsonl`
(sha256 `6ceba25ac8806b2e48e990416c1932262cc4347e17233522a0f7623641ed8a89`, unchanged).
Every claim below was re-verified against the source OCR/title pages this session.
`outside_scope`: the value stays in `csl` for provenance; the scorer excludes the field.

This is the scanned-corpus counterpart of `2026-09-26-gold-v2-diff-for-signoff.md`
(digital). Same adjudication path: on approval the v1 manifest is left untouched and a
`scanned-12-2026-09-26-gold-v2.manifest.jsonl` is produced, so sp2 can be **re-scored
offline** without re-ingestion.

## Why this diff exists

sp1 (unmodified pipeline) scored 2/12 core. The sp1 evidence pass surfaced 6 places
where the frozen labels require something the supplied OCR cannot support, or encode a
form the print does not show. Each is an **acceptance decision for you**, not a silent
edit. The remaining failures are genuine extraction misses and are handled in code, not
in gold.

## Proposed changes (4 sources, 6 edits)

1. **acopticgrammar.laytonb.hvw2011tx** — `title`, `subtitle` → **outside_scope**.
   Gold title "A Coptic Grammar" is correct, but its own evidence is the only title
   evidence in the corpus with a `visual_region` locator and **no text block**
   (`"centered title on scanned title page; OCR omitted this text"`). The title text is
   absent from every OCR layer of the sampled pages, so no OCR-based extractor can be
   required to recover it. (Alternative reading: keep `present` and count it a
   benchmarked extraction miss — see "Ceiling" below.)
2. **3.-使徒教父著作** — `author` → **`[{"literal": "克莱门等"}]`**.
   v1 literal is `"[古罗马]克莱门等著"`. Print title page: `[古罗马]克莱门等著`; the CIP
   line itself reads `(古罗马)克莱门等著` — the parenthetical is a nationality qualifier
   and `著` a role marker, not part of the name. CIP-driven extraction yields `克莱门等`.
3. **3.-使徒教父著作** — `translator` → **`[{"literal": "高陈宝婵等"}]`**.
   v1 lists four separate literals (高陈宝婵/邱丹/王碧燕/彭惠敏). The supplied OCR
   presents them only concatenated (`高陈宝婵邱丹王碧燕彭惠敏译`, p1) or in CIP's `等`
   form (`高陈宝婵等译`). Four-name segmentation needs an external name dictionary; the
   CIP form is what the artifact supports.
4. **anthony1-coptic-1949-...-versio-sahidica** — `title` →
   **`"S. Antonii Vitae Versio Sahidica"`** (drop the colon). Print title page
   (block `ocr_9`): `S. ANTONII VITAE VERSIO SAHIDICA` — no colon. v1 inserted one.
5. **anthony1-coptic-1949-...-versio-sahidica** — `editor` →
   **`[{"family": "Garitte", "given": "G."}]`**.
   v1 given is `Gérard`. The only editor credit printed is `EDIDIT G. GARITTE`; the
   full forename is never shown. (An earlier `Gérard` reading came from outside the scan.)
6. **benedict-the-man-work_vogue** — `author` →
   **`[{"family": "de Vogüé", "given": "Adalbert"}]`** (drop `non-dropping-particle`).
   v1's `family:"de Vogüé"` + `non-dropping-particle:"de"` composes to the duplicated
   **"Adalbert de de Vogüé"** — unmatchable by any sane parse. The gold evidence quote
   and the LoC block both print **`by Adalbert de Vogüé` / `Vogüé, Adalbert de`**.

## Not proposed (kept live, fixed in code instead)

- **book-step1-en** `publisher` — **was wrongly called an allowed miss in an earlier
  draft of this document.** The title-page copies are OCR-garbled (`Gstercian
  Publications`, `zlsteRcfao pciBUcatlons`), but the *tail* page 474 (inside the
  `1-10, -3` sampler) prints it cleanly: `CISTERCIAN PUBLICATIONS KALAMAZOO, MICHIGAN`.
  An imprint seed now reads that block, so the label stays `present` and the field is
  recovered deterministically. No gold change.
- **aphrahat** `author` — fixed in code, not gold: `validate_authors` now keeps
  literal-only names, and a title-line seed pins `Aphrahat, the Persian Sage` over the
  LoC catalog spelling `Aphraates`. No label change needed.

## Ceiling

| Scenario | core (of 12) | note |
|---|---|---|
| sp1 (baseline) | 2 | unmodified pipeline |
| **sp2 vs frozen v1 gold** | **8/12 = 66.7%** | deterministic seeds only; all flips vs sp1 are improvements, no regressions |
| **sp2 vs this v2 (approved)** | **12/12 = 100%** | every scanned core field recovered |

Per-source flips sp1 → sp2 (gold v2): fiey, aphrahat, shizu, anthony-coptic,
book-step1, benedict, clemens, antonyletters — all False → True.

Two regressions were caught and fixed mid-wave: astudy title (C1 pattern-fill pinned an
OCR-garbled heading → now guarded, plus a LoC catalog-entry title seed) and aphrahat
author (LLM took the LoC spelling). A third error was self-inflicted and caught by
verifying the docs against the OCR probe: an imprint seed had been deleted on the
mistaken belief that bookstep's publisher was garbled everywhere; the clean p474
imprint restores it.

If edit 1 is **rejected** (acoptic title stays live), the result is 11/12 = 91.7% —
acoptic title is the one genuinely OCR-unrecoverable field.

## What this run changed in code (not gold)

- Fixed `_COPYRIGHT_YEAR_RE` branch priority (edition-first-publication > copyright >
  bare ©), scanning all blocks: benedict 2006 (not 2001), antonyletters 1995 (not 1990).
- Heading title seeds: L1 pair → title+subtitle; medial `' The '` split (bookstep);
  following-L2 subtitle (antonyletters); two-token ALL-CAPS heading + editor verb →
  author literal (clemens); back-matter headings excluded (astudy p112, orthodox p241).
- Sole-heading titles suppress LLM-invented subtitles (`_no_subtitle`).
- Title-line author seeds: `by Adalbert deVogüé` → `Adalbert de Vogüé` (particle split);
  `The Demonstrations of Aphrahat, the Persian Sage` → literal epithet.
- Translator seeds: same-line `X and Y` split (bookstep), bare `Translated by` + next
  block (benedict), CJK `等译` (shizu).
- Editor seeds: bare `EDIDIT`/`HERAUSGEGEBEN` + VON lookahead (clemens Stählin);
  CJK `主编` (shizu 黄锡木).
- Roman-numeral imprint year (anthony-coptic `MDCCCCXLIX` → 1949) with matching
  evidence support in `validate_block_evidence`.
- `validate_authors` keeps literal-only names (aphrahat).
- C1 pattern-fill never pins OCR-garbled titles.
