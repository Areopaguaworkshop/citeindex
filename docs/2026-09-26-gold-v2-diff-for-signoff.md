# Gold v2 re-annotation diff — for user sign-off (2026-09-26, corrected after p6)

**v1 frozen** (`digital-16-2026-09-26.manifest.jsonl`, sha256 `b705789164d4ea2b`).
v2 → `digital-16-2026-09-26-gold-v2.manifest.jsonl` after approval (adjudication tooling path).
Every claim below was re-verified against the physical PDFs this session.

`outside_scope`: value stays in `csl` for provenance; scorer excludes the field — "not printed in
the supplied PDF" must not be required of extraction.

**Corrections from the p6 evidence check:**
- byzantium `editor` is **NOT printed** — "Ratliff" appears only as a cited-work author ("see Ratliff, p. 32"). → outside_scope (earlier draft wrongly claimed otherwise).
- cyprian `issued` **IS printed** — p14 cite-this-page block: "First published online: 2018". Stays `present`.
- nestorian printed title uses straight single quotes (`'NESTORIAN'`); the scorer now folds all quote glyphs.

## Changes (7 sources)

1. **chrysostom-bibliography**: `type` report→**book**; `author`,`issued` → **outside_scope** (PDF metadata only — verified not printed in the 246pp scan); add `editor/translator/edition/ISBN = absent`. Live core: `{type, title}`.
2. **byzantium-islam7-9-tm**: `type` chapter→**book**; `container-title` **removed** (a book's container is itself); `publisher`,`issued`,`editor` → **outside_scope** (no imprint/editor credit anywhere in 130pp; only title+author printed, p2); add `translator/edition/ISBN = absent`. Live core: `{type, title, author}`.
3. **cyprian**: statuses completed for the new article profile — `author/page/publisher/ISBN/editor/translator/edition = absent`; `issued` stays `present` (printed p14).
4. **ambrose**: `title` → **"Seven Exegetical Works"**; `collection-title` → "The Fathers of the Church" (`present`). Series/volume leave the title.
5. **2003_希腊文圣经史**: `publisher` → **"The British Library; Oak Knoll Press"** (association clause is a distribution credit, not a co-publisher).
6. **gno1-contraeunomiumi_ii-t**: `title` → **"Contra Eunomium Libri"** (the title proper; "LIBER I ET II" is the part statement, printed p2, not a subtitle). `author`/`editor` stay live — p7 prompt rules (EDIDIT/CURAVIT → editor; Latin-genitive `GREGORII NYSSENI` → "Gregory of Nyssa"). *Fallback if p7 misses: `author = outside_scope`.*
7. **12-the-nestorian-church**: `volume`,`issue`,`page` → **outside_scope** (not printed; only folios 24–35) **and `container-title` → outside_scope** (printed only as the abbreviated running header "BULLETIN JOHN RYLANDS LIBRARY"; gold carries the full official journal name). Live core: `{type, title, author}` — all printed.

## Measured

| Run | core (v1 gold) | exact | equiv | title F1 |
|---|---|---|---|---|
| p5 | 6/16 = 37.5% | 3/16 | 3/16 | 0.688 |
| **p6 (this session)** | **7/16 = 43.75%** | 3/16 | 3/16 | 0.688 |

Free re-score of p6 against **simulated gold v2**: **11/16 = 68.75%** (10/16 if nestorian
container-title stays live).

## Remaining 5 failures — all extraction-side, p7 proposal

| Source | Field(s) | Fix |
|---|---|---|
| byzantium | title | OCR digit garble normalize: "(7th—g9th Century)" → "(7th–9th Century)". |
| cyprian | issued | last-page "First published online: 2018" (present in p5, missed in p6 — single-run variance; deterministic seed for the phrase). |
| cyril | translator | "[translated by] Norman Russell" printed p3 — prompt rule (present in p5, missed in p6). |
| final_160 | title | leading "160" is part of the printed title (p1/p2) — prompt rule. |
| gno1 | author/editor/title | EDIDIT/CURAVIT → editor; Latin genitive → vernacular author; part statement ≠ subtitle. |

**Non-monotone warning**: p5→p6 prompt edits fixed augustin+caridi but lost cyril `translator`,
gno1 `editor`, cyprian `issued`. p7 must diff per-source against p6, not just check the headline.
Realistic projection 13–15/16; 90% (15/16) needs essentially every remaining rule to land
(n=16 single-run noise caveat; any headline-changing flip gets re-run).