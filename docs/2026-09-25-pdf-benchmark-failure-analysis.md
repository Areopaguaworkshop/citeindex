# PDF benchmark failure analysis and PageIndex's role

Date: 2026-09-25. Scope: saved results from the first-five/last-three physical-page experiment. This is a diagnostic report, not an implementation change. No annotations were changed or new benchmark runs performed for this analysis.

## Conclusion

The latest experiment did not demonstrate improvement. Earlier confidence that finding bibliographic pages would resolve the benchmark failures was premature. Page coverage, text quality, classification, evidence validation, refinement, and scoring all contribute.

All 28 PDFs completed ingestion. Completion does not imply an accurate or complete citation.

| Measure | Digital PDFs | Scanned PDFs |
|---|---:|---:|
| Completed ingestion | 16/16 | 12/12 |
| Previous cloud candidate: equivalent whole records | 2/16 | 1/12 |
| Latest experiment: equivalent whole records | 2/16 | 0/12 |
| Latest experiment: strict whole records | 1/16 | 0/12 |
| Correct document type | 13/16 | 11/12 |

Equivalent comparison still requires every evaluated field to match after limited normalization. It handles title/subtitle separation, some punctuation, and some name representations. A missing author and a location abbreviation can therefore both cause a whole-record failure, despite very different practical severity.

## Evidence artifacts

- Reviewed reference: `benchmarks/citations/predictions/candidate-v3.gold-36.jsonl`.
- Previous candidate: `benchmarks/citations/predictions/cloud-40-candidate-2026-09-25.jsonl`.
- Latest digital predictions: `benchmarks/citations/predictions/citation-pages-digital-2026-09-25.jsonl`.
- Latest digital report: `benchmarks/citations/predictions/citation-pages-digital-report-2026-09-25.json`.
- Latest scanned predictions: `benchmarks/citations/predictions/citation-pages-scanned-role-aware-2026-09-25.jsonl`.
- Latest scanned report: `benchmarks/citations/predictions/citation-pages-scanned-report-2026-09-25.json`.
- Per-attempt `stderr.log` files under the corresponding artifact directories.

These benchmark artifacts are local and may be Git-ignored. Digital predictions contain 17 attempts for 16 sources; comparisons use the latest attempt per source. Parse JSONL using newline delimiters, not Unicode-aware `splitlines()`, because embedded source text contains Unicode line separators.

## Digital PDFs: remaining differences after equivalence normalization

| Source | Observed result |
|---|---|
| An Ethiopian Reading of the Bible | Passes equivalent comparison; title and subtitle stored separately. |
| Grace for Grace | Missing 2014 and ISBN. Publication evidence is on physical page 6, outside the window. |
| Making Martyrs East and West | Missing ISBN on page 6. Both publishers extracted, but comma versus semicolon differs; DeKalb, IL versus DeKalb, Illinois also differs. |
| Seven Exegetical Works | Missing year and ISBN, with publication information on page 6. Saint Ambrose versus Ambrose; title lacks the series/volume parenthetical included in gold. |
| 论三位一体 | Title became `Crack by RAOGY.` through the old PDF-metadata fallback. Missing year and translator 周伟驰. Logs show title, translator, and year rejected by evidence validation. |
| The Bible as Book | Publisher omits “in association with the Scriptorium: Center for Christian Antiquities.” |
| Chrysostomica / Chrysostom bibliography | Book instead of report; Chrysostom instead of Wendy Mayer; missing reviewed metadata date July 2021. |
| The “Nestorian” Church | Article type correct. Missing year, DOI, volume, issue, pages; abbreviated journal title; title/subtitle punctuation differs. Reviewed year evidence on page 13 was inside the window but not selected. |
| Cyprian of Carthage | Chapter instead of entry-encyclopedia; this is the only remaining equivalent mismatch. |
| Cyril of Alexandria | Title became `new doc 6` from metadata. Author Cyril instead of reviewed Norman Russell. Correct title page selected, but title rejected. |
| Byzantium and Islam | Book instead of chapter; author Byzantium instead of Helen C. Evans; title omits (7th–9th Century). Reviewed page-1 evidence was not selected. |
| Bible, Church, Tradition | Missing Collected Works and collection number 1; logs show both rejected. |
| 160 Unpublished Homilies of Jacob of Serugh | Title loses 160; volume retains I but loses Homilies 1–72. |
| John Climacus | Passes equivalent comparison. |
| Two Rediscovered Works of Ancient Christian Literature: Gregory of Nyssa and Macarius | Filename used as title. Correct title page selected; title and subtitle rejected. |
| Contra Eunomium Libri I–II | Title loses I–II; volume missing. Gregorii Nysseni / Wernerus Jaeger differ from Gregory of Nyssa / Werner Jaeger; name forms alone do not prove wrong identity. |

## Scanned PDFs: remaining differences after equivalence normalization

| Source | Observed result |
|---|---|
| A Coptic Grammar | OCR omitted visible main title and subtitle; filename used. Language missing. Third Edition, Revised versus 3; series punctuation differs. |
| Psalms and the Life of Faith | Only remaining differences: Archimandrite retained in author name; Athens versus Athens, Greece. |
| Pour un Oriens Christianus Novus | Volume 49 missing. Output includes Pour un, absent from reviewed title; source blocks contain POUR UN, so this raises an annotation question rather than proving extraction failure. The joined title comparison also reports a subtitle mismatch. |
| The Demonstrations of Aphrahat | Author Aphrahat, the Persian Sage and ISSN missing. |
| 使徒教父著作 | 克莱门 instead of explicitly approved [古罗马]克莱门等著. |
| S. Antonii Vitae: Versio Sahidica | Year missing; collection number I instead of 13; incomplete series title. G. versus Gérard, Parisiis versus Paris, and title punctuation differ. |
| The Book of Steps | Cistercian Publications and collection number 196 missing; both rejected by validation. |
| Orthodox Canon Law Reference Book | Author Vasile Mihai missing. |
| Saint Benedict: The Man and His Work | Collection title, number 40, and original date 2001 missing. Gold also stores de inside family and as a separate name particle, producing a name-comparison discrepancy. |
| Clemens Alexandrinus | Author, editor Otto Stählin, series, subtitle Protrepticus und Paedagogus missing. Erster Band versus I. Detailed reviewed title-page evidence is on page 9, outside window. |
| The Letters of St. Antony | Only remaining mismatch: Minneapolis versus Minneapolis, MN. |
| A Study of the Divine Liturgy of St John Chrysostom | Book instead of manuscript; filename with .pdf used as title. OCR title badly corrupted; extracted title rejected. |

The existing user review remains authoritative for this comparison. These findings do not require repeating the whole annotation exercise, and no gold values were changed.

## Ranked failure analysis

### 1. Necessary pages excluded — confirmed

Grace for Grace, Making Martyrs, and Seven Exegetical Works have publication information on physical page 6. Saved digital text confirms that information exists. Clemens Alexandrinus has detailed title-page evidence on page 9. First 5 + last 3 can be an initial search window, but is insufficient as an absolute ceiling for these files. Under the user-imposed ceiling, unavailable fields must remain explicitly incomplete.

### 2. Correct page, unreliable text — confirmed

A Coptic Grammar's selected title page visibly contains its title, absent from saved OCR. Other text layers contain `ALEXANDRI A`, `TW O REDISCOVERED W ORKS`, `A ND`, broken Chinese names, and `LI+URGY`/`LIFURGY`. A PDF having extractable text does not prove that text is usable. DSPy cannot recover absent evidence reliably from text alone.

### 3. Rejected fields followed by unsuitable fallback titles — confirmed, exact rejection causes partly unresolved

Logs record title rejections for 论三位一体, Cyril, Gregory/Macarius, and Divine Liturgy. Outputs retain metadata placeholders or filenames. Damaged spacing and fragmented quotations plausibly contribute, but logs alone do not establish whether each rejection was caused by model quotations, missing spans, or validation normalization. It is incorrect to attribute every rejected field to model misunderstanding.

### 4. Required document types excluded by implementation — confirmed

`LocateBibliographicPages` and its acceptance logic support book, chapter, article-journal, report, thesis, and unknown. They do not support entry-encyclopedia or manuscript. The corresponding reviewed types for Cyprian and Divine Liturgy are therefore impossible through this classifier. Chrysostomica and Byzantium also show semantic classification errors.

### 5. Refinement does not reliably revisit failed evidence — confirmed implementation weakness

`_run_dspy_extraction` can stop after title, issued, type, publisher for books/chapters, and author or editor. Translator, ISBN, volume, and other evaluated fields need not be complete. Supplied blocks are marked used; only the first four are carried forward; accepted fields cannot be revised. Logs show second attempts with only four blocks after first-attempt rejection. Two extraction calls do not necessarily provide a useful second chance.

### 6. Scoring combines serious errors with representation differences — confirmed

City/state abbreviation, honorifics, Latin names, series text within gold titles, and duplicated name particles affect whole-record accuracy. These differences do not explain away wrong types, absent authors/years, or garbage titles. No alternative accuracy percentage was invented or gold loosened to improve results.

## Experimental limitations

- Digital and scanned batches did not use the same final extraction signature: explicit bibliographic-page roles were added before the scanned batch.
- Digital-title fallback changed during the digital batch.
- Scanned coverage changed from the previous larger sample to first 5 + last 3.
- PageIndex and layout were disabled in these runs.
- Thus this is not a controlled experiment isolating the page-selection prompt. It establishes concrete failures, not PageIndex/PaperQA performance or production readiness.

## PageIndex: verified role and limitations

### What it does

Official PageIndex documentation describes a hierarchical document tree followed by reasoning-based tree search. The local vendored PDF implementation finds/extracts tables of contents, associates headings with physical positions, and supports structure generation when a usable contents list is unavailable. This is section navigation, not a guaranteed inventory of cover, title page, copyright page, and colophon.

The local `add_preface_if_needed` helper inserts a generic Preface node at physical page 1 when the first indexed section begins later. It does not independently identify the role of every preceding page. An unnumbered copyright page may therefore sit inside a broad front-matter range without its own node.

### Current CiteIndex integration

- `digital_pdf.py` can build the PageIndex tree before citation extraction.
- `select_candidate_regions` classifies existing headings containing copyright/imprint/colophon/版权/出版 as imprint; introduction/title/前言/序 as front matter; bibliography/reference/参考文献/书目 as references.
- This is heading matching, not a visual copyright-page detector. It does not explicitly match cover/封面; even a generic Preface label is not in its front-matter marker list.
- The new `_run_dspy_extraction` selection branch bypasses PageIndex region scoring once bibliographic-page selection is active. Merely enabling PageIndex will therefore not automatically make it a second-stage correction mechanism.
- Both measured batches explicitly disabled PageIndex.
- Scanned Markdown heading/line ranges must not be interpreted as physical PDF pages. Use an explicit mapping to original OCR blocks and physical page coordinates.

### Where it could help — proposed use, not implemented or benchmark-proven

After initial bibliographic-page selection, PageIndex can provide a secondary search route for missing fields: examine front matter, publication/copyright sections, colophons, chapter openings, or how-to-cite sections; resolve candidates to original pages; then classify page roles and extract quoted CSL evidence from those pages. It can also help distinguish a chapter within an edited book from the parent work.

Page roles still require direct evidence: copyright/©/ISBN/CIP/版权/版次/印刷 for an imprint page; prominent title and contributor statements for a title page; visual layout for a cover. These are proposed detector cues, not guaranteed native PageIndex labels. PageIndex summaries are navigation aids and must not become source quotations.

If the relevant pages were never indexed/OCRed, PageIndex cannot retrieve their content. With the current strict first-five/last-three benchmark ceiling, page 6 or page 9 remains unavailable even if a tree suggests it. Expanding that ceiling would be a separate experiment, not a silent change to the user's constraint.

### References

- Local integration: `citeindex/ingestion/pipelines/pageindex_tree.py`, `digital_pdf.py`, and `dspy_extract.py` (`select_candidate_regions`, `locate_bibliographic_pages`, `_run_dspy_extraction`).
- Local vendored PageIndex: `citeindex/ingestion/pipelines/pageindex/page_index.py` (`check_toc`) and `utils.py` (`add_preface_if_needed`).
- Official overview: https://docs.pageindex.ai/
- Official document/tree API: https://docs.pageindex.ai/sdk/documents
- Official tree-search tutorial: https://docs.pageindex.ai/tutorials/tree-search

Online documentation was checked for general capabilities. Direct upstream source retrieval failed during this follow-up; detailed code claims above refer to the inspected local vendored implementation, not an assumed current upstream version.
