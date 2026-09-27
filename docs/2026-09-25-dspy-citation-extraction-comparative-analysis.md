# Why CiteIndex's DSPy citation extraction fails its own benchmark, compared with GROBID, CERMINE, PaperQA2, and PageIndex

Date: 2026-09-25. Scope: diagnostic and comparative analysis. No code changed, no benchmark re-run, no gold annotation modified. All local claims carry `file:line`; all external claims carry a URL. Every place evidence is absent is marked rather than inferred.

---

## 1. Verdict

Three things are all called "citation extraction" in this discussion. CiteIndex does the first; the projects it is being compared against mostly do the second and third. Once the tasks and the scoring tiers are separated, the picture is:

1. **CiteIndex's headline number (11.1% strict, 25.0% equivalent) is not comparable to GROBID's headline 0.90.** CiteIndex scores **whole-record exact match**; GROBID's 0.90 is **field-level on references**. On the same whole-record tier, GROBID's own header extraction collapses to **1.95–12.2 recall** — the same neighbourhood as CiteIndex.
2. **The benchmark is not "good vs bad" across a shared task; it is a whole-record metric on a small, multilingual, scanned, historically hard corpus with no published external baseline.** No project found implements CiteIndex's design (bounded page window plus quotation evidence validation), so there is no head-to-head baseline.
3. **There are nonetheless real, code-level defects that depress the score independently of scoring-tier effects.** Confirmed: the default search window excludes physical page 6 and page 9 where several sources keep their imprint; the scanned path discards its pattern fallback entirely; accepted fields cannot be revised; PageIndex region scoring is dead once page selection succeeds. These are fixable and are the honest explanation for part of the failure.

The user's framing ("their benchmark is good, mine failed") is half right. CiteIndex's **record** metric is genuine and the 90% target is genuinely unmet. But a substantial part of the apparent gap is metric tier and corpus, not extractor quality.

---

## 2. The distinction that governs the whole comparison

Three separate tasks are routinely conflated:

| | Task | Question answered |
|---|---|---|
| **(a)** | **Own header metadata** | What is *this* document's title, author, date, publisher? |
| **(b)** | **Reference-list parsing** | What works are *cited inside* this document? |
| **(c)** | **Grounded answer citation** | Which retrieved passage supports this generated answer? |

CiteIndex's benchmark scope is explicitly (a), stated at `benchmarks/citations/README.md:28` ("The scope is host-source metadata, not works cited inside a source") and `docs/plans/2026-09-14-citation-accuracy-and-benchmark.md` ("metadata of ingested source itself; parsing works cited inside a source is out of scope").

Task assignment of the comparison targets, established with sources in §5:

- **GROBID** — (a) **and** (b). https://grobid.readthedocs.io/en/latest/Introduction/
- **CERMINE** — (a) **and** (b). https://github.com/CeON/CERMINE
- **biblio-glutton** — (b) with DOI resolution. https://biblio-glutton.readthedocs.io/en/latest/Benchmarking/
- **PaperQA2** — **(c) only**. It does grounded RAG; its metadata fetching is provider lookup for already-indexed papers, not extraction from an arbitrary PDF. https://github.com/Future-House/paper-qa
- **PageIndex** — **(c) only**. Hierarchical tree index plus reasoning-based retrieval. No bibliographic-field output. https://github.com/VectifyAI/PageIndex
- **Marker / Nougat** — page-to-markdown/OCR; not (a) or (b). https://github.com/datalab-to/marker

**Consequence:** PaperQA2's 66.0% accuracy on LitQA2 and PageIndex's 98.7% on FinanceBench cannot be cited as evidence that CiteIndex's metadata extraction is worse. They are not measured on the same task. This is the single largest source of confusion in the question as posed.

---

## 3. What CiteIndex actually does (verified)

The design is unusual and, as far as the sources checked for this report show, **has no published external equivalent**.

### 3.1 Two-stage bounded extraction

Stage A, `locate_bibliographic_pages()` — `citeindex/ingestion/pipelines/dspy_extract.py:177-229`:
- Window: `set(parse_page_range(config.page_range, total_pages))` at `:181`, filtered to valid pages at `:182`. Default is `page_range: str = "1-5, -3"` (`citeindex/ingestion/models.py:44`) = first five plus last three physical pages, **8 pages** on a document longer than eight pages.
- Preview budget: `page_budget = max(1, 12000 // max(len(window), 1))` at `:186` — 12,000 characters split across the window (≈1,500 per page at the default window).
- The model returns `document_type`, `type_evidence`, and `selected_pages` with an exact `quote` (`:155-174`).
- Returns empty and leaves `status="needs_review"` when previews are empty, when a returned span's `quote` is not a substring of the block text, when the page does not match, or when the type is outside `{book, chapter, article-journal, report, thesis}` (`:198-228`).

Stage B, `_run_dspy_extraction()` — `:763-875`:
- `for attempt in range(2)` at `:820` — **at most two predictor invocations**.
- Character budget `12000` per attempt at `:821`; per-page `12000 // page_count` when a selection exists at `:832`.
- Only `carry = selected[:4]` survives into attempt two (`:844`) — **exactly four blocks**.
- **Accepted fields are immutable**: `if field in accepted or field not in values: continue` at `:853`. An already-accepted wrong value can never be corrected on a later attempt.
- Stop condition at `:862-866`: break as soon as `{title, issued, type}` (plus `publisher` for book/bookchapter) and `author` or `editor` are accepted. Translator, ISBN, edition, volume, and series need not be complete.

### 3.2 Evidence validation

`validate_block_evidence()` — `citeindex/ingestion/citation_verification.py:25-76`. Every field must pass `valid_host_value()` for CSL shape (`citeindex/ingestion/csl.py:23-46`) **and** supply spans whose `quote` is a substring of a named block (`:42-45`), subject to exclusion rules: `title` evidence rejected when `metadata_key` is `filename` or `source_path` (`:50`), `issued` rejected for `modified`/`event-date` (`:54-56`), `accessed` requires `metadata_key == "accessed"` (`:52`). A field that fails is dropped silently with a debug log (`dspy_extract.py:860-861`).

**This validation is the project's main safety property and it is what turns model output into rejection.** The failure report records rejections for `论三位一体`, Cyril of Alexandria, Gregory/Macarius, and Divine Liturgy, with the note that "logs alone do not establish whether each rejection was caused by model quotations, missing spans, or validation normalization" (`docs/2026-09-25-pdf-benchmark-failure-analysis.md:85`).

### 3.3 Scanned path defect

`extract_metadata_with_dspy_priority()` — `dspy_extract.py:891-937`. Pattern extraction runs on front matter (`:907`), but when `total_pages is not None` the function **returns `dspy_csl` unconditionally at `:918-921`**, before the `if not dspy_csl: return pattern_csl` fallback at `:922-924`. The scanned pipeline always passes `total_pages` (`citeindex/ingestion/pipelines/scanned_common.py:185`). Therefore:

- `pattern_csl` is computed and **discarded** on every scanned document.
- A failed DSPy extraction yields **empty metadata with no deterministic fallback**, tagged `_extraction_method="bibliographic_pages+dspy"`.

This contradicts `README.md:209` ("DSPy is allowed to overwrite pattern-extracted metadata fields for scanned documents"). The claim understates the behaviour: pattern fields never contribute at all.

### 3.4 PageIndex is effectively bypassed

With a truthy `selection`, `gather_evidence_candidates()` is not called and the region-scoring loop iterates `[]` (`dspy_extract.py:799-806`). Since a successful locate yields a truthy selection, **PageIndex headings and `candidate_regions` do not influence metadata ranking in practice**. This corroborates the failure report's own finding (`docs/2026-09-25-pdf-benchmark-failure-analysis.md:120`).

---

## 4. CiteIndex's actual measured results

From `benchmarks/citations/README.md` and the failure analysis, all self-reported:

| Measure | Value |
|---|---|
| Record accuracy, strict | 4/36 (11.1%) — `benchmarks/citations/README.md:58-60` |
| Record accuracy, bibliographic equivalence | 9/36 (25.0%) — same |
| By modality (equivalent) | digital 2/16, scanned 1/12, URL 6/8 — `:60-61` |
| Field F1 (repaired candidate) | title 0.444, author 0.491, issued 0.926, publisher 0.824, type 0.912 — `:61-62` |
| Field F1 (sampled baseline) | title 0.559, author 0.571, issued 0.833, publisher 0.727, type 0.765 — `:54-55` |
| Latest page-selection experiment | digital 2/16 equivalent, 1/16 strict; scanned 0/12 — `docs/2026-09-25-pdf-benchmark-failure-analysis.md:14-16` |
| Ingestion completion | digital 16/16, scanned 12/12 — same, `:13` |

Note the shape: **`issued` (0.926) and `type` (0.912) are strong; `title` (0.444) is the collapse.** The failure is concentrated in one field, not uniform.

---

## 5. How external extractors work and what they report

### 5.1 Task (a) — own header metadata

| System | Method | Corpus | Reported numbers | Source |
|---|---|---|---|---|
| **GROBID** | CRF cascade (Wapiti) or DeLFT BiLSTM/BERT-CRF; 55 leaf labels; biblio-glutton/Crossref consolidation | PMC 1,943 docs | Header strict: title F1 **84.25**, authors **92.86**, abstract **16.20**, macro **58.79**, micro **30.82**. Levenshtein: macro **88.51**. **Instance-level recall strict 10.5**, Levenshtein 73.6 | https://grobid.readthedocs.io/en/latest/benchmarks/Benchmarking-pmc/ |
| **GROBID** | same | bioRxiv 2,000 docs | Header strict: title **76.93**, authors **84.68**, abstract **2.34**, macro **53.14**. **Instance-level recall strict 1.95** | .../Benchmarking-biorxiv/ |
| **GROBID** | same | PLOS 1,000 / eLife 984 | PLOS strict title **94.94**, macro **61.26**, instance recall **12.2**; eLife strict title **88.30**, macro **54.22** | .../Benchmarking-plos/, .../Benchmarking-elife/ |
| **GROBID** (single model, 10-fold CV) | CRF | — | Header CRF F1 **0.7425**; citation **0.9448** | https://grobid.readthedocs.io/en/latest/benchmarks/Benchmarking-models/ |
| **GROBID** (DocBank, 500K arXiv pages) | — | DocBank | title F1 **0.91**, abstract **0.82**, authors **0.52** | https://arxiv.org/abs/2303.09957 |
| **CERMINE** | CRF + SVM + rules | — | Zone classification F **95.3**; reference parsing F **93.3**; **full metadata workflow P 81.0 / R 74.7 / F 77.5** | https://github.com/CeON/CERMINE, https://link.springer.com/article/10.1007/s10032-015-0249-8 |
| **Docling** | layout + rules | — | **No published bibliographic-metadata metric found.** Its README lists metadata extraction under "Coming soon" while the 2024 tech report describes it as delivered | https://github.com/docling-project/docling, https://arxiv.org/abs/2408.09869 |
| **science-parse / SPv2** | CRF / BiLSTM | — | **No published metric found in repo** | https://github.com/allenai/science-parse |

### 5.2 Task (b) — reference-list parsing

| System | Corpus | Reported numbers | Source |
|---|---|---|---|
| **GROBID** | PMC 90,125 refs; bioRxiv 98,753 refs | Reference parsing ≈**.87 F1** (PMC), ≈**.90** (bioRxiv); isolated refs **>.90 instance / .95 field**; PMC strict field micro **83.13**, instance precision **45.22** | https://grobid.readthedocs.io/en/latest/Introduction/, .../End-to-end-evaluation/ |
| **GROBID vs LLMs** | SSH benchmark | CEX: GROBID **0.856** vs best LLM Qwen3-VL-32B **0.7475**; EXCITE: Mistral-Small-3.2-24B **0.8623** vs GROBID 0.856; LinkedBooks: DeepSeek-V3.1 **0.8047** vs GROBID 0.6866 | https://arxiv.org/abs/2603.13651 |
| **biblio-glutton** | 17,015 ref/DOI pairs | CRF **P 97.33 / R 95.52 / F1 96.42**; Crossref REST **97.19 / 94.26 / 95.69** | https://biblio-glutton.readthedocs.io/en/latest/Benchmarking/ |
| **GROBID / CERMINE / ParsCit** | 6,306 papers, 506,540 refs | GROBID **F1 0.89** out of box, **0.92** retrained; CERMINE 0.83→0.92; ParsCit 0.75 | https://arxiv.org/abs/1802.01168 |

### 5.3 Task (c) — the projects that are NOT comparable

| System | Task | Benchmark | Reported numbers | Source |
|---|---|---|---|---|
| **PaperQA2** | (c) | LitQA2, 248 MCQ | precision **85.2%**, accuracy **66.0%**, insufficient-info 21.9%; human PhD precision 73.8% / accuracy 67.7% | https://arxiv.org/abs/2409.13740 |
| **PageIndex** | (c) | FinanceBench | **98.7%** accuracy (Mafin2.5) | https://vectify.ai/blog/Mafin2.5 |
| **PageIndex** | (c) | OSS benchmark: 62 lookup questions, 34 PDFs, 1,945 pages | no single headline figure in the repo | https://github.com/VectifyAI/PageIndex-OSS-Benchmark |
| **PageIndex** | (a)? | — | **No published metadata-extraction metric found.** Cloud metadata is a free-form user dict | https://docs.pageindex.ai/sdk/documents |

### 5.4 LLM-based task (a) — the relevant reference point

| System | Benchmark | Metric | Score | Source |
|---|---|---|---|---|
| Gemini 2.5 Pro | MOLE (6 languages) | avg F1 | **76.40** | https://arxiv.org/abs/2505.19800 |
| GPT-4o | MOLE | avg F1 | 73.56 | same |
| Claude 3.5 Sonnet | MOLE | avg F1 | 72.18 | same |
| DeepSeek V3 / Llama4 / Gemma3 / Qwen2.5 | MOLE | avg F1 | 71.17 / 70.57 / 69.17 / 68.52 | same |
| keyword baseline / random | MOLE | avg F1 | **48.91 / 33.43** | same |
| GPT-4o (+OCR) | BiblioPage (Czech, scanned) | mF1 | **67 (70 with OCR)** | https://arxiv.org/abs/2503.19658 |
| YOLOv11m + tOCR | BiblioPage | mF1 / mAP | 59 / 52 | same |

Two things stand out. First, **the best multilingual LLM metadata F1 is ~76, not ~95** — the high numbers people associate with "citation extraction" come from task (b) on English life-science text, a materially easier setting. Second, the **keyword baseline reaches 48.91 on MOLE**, so the gap between a sophisticated LLM and a trivial regex is far smaller than field-level reference numbers suggest.

**Documented LLM task-(a) failure modes**, all of which recur in CiteIndex's report:

- Hallucinating non-existent attributes, and browsing contaminating in-document fields — MOLE, https://arxiv.org/abs/2505.19800
- Context-length sensitivity: truncation degrades some models dramatically — MOLE, same
- Multi-line / varying-font titles defeating line-based extraction, and models retaining generic terms like "author" — BiblioPage, https://arxiv.org/abs/2503.19658
- Structured-output brittleness under noisy layouts; **footnote-heavy layouts are "the dominant failure mode for all systems"** — https://arxiv.org/abs/2603.13651
- Evaluator gap: on DocBench, GPT-4 as judge agrees with humans **98%** where string matching agrees only **40.0–65.0%**; metadata questions are 23.4% of that benchmark — https://arxiv.org/abs/2407.10701

---

## 6. Why their benchmarks look good and CiteIndex's looks bad

Five separable causes. Only the last is an extractor-quality gap.

### 6.1 Metric tier — the dominant effect

CiteIndex reports **whole-record accuracy**: `run.py` computes `exact_record_accuracy` and `equivalent_record_accuracy` as the fraction of records where **every** evaluated field matches (`benchmarks/citations/run.py:145-152`). One missing ISBN, or `DeKalb, IL` vs `DeKalb, Illinois`, fails the entire record. The failure report states this explicitly: "A missing author and a location abbreviation can therefore both cause a whole-record failure, despite very different practical severity" (`docs/2026-09-25-pdf-benchmark-failure-analysis.md:19`).

GROBID publishes both tiers on identical corpora, which exposes the gap directly:

| Setting | Field-level | Whole-record |
|---|---|---|
| PMC header strict | macro F1 58.79 | instance recall **10.5** |
| bioRxiv header strict | macro F1 53.14 | instance recall **1.95** |
| PLOS header strict | macro F1 61.26 | instance recall **12.2** |
| PMC citations strict | micro F1 83.13 | instance precision **45.22** |

Sources: https://grobid.readthedocs.io/en/latest/benchmarks/Benchmarking-pmc/, .../Benchmarking-biorxiv/, .../Benchmarking-plos/

**GROBID's own header extraction scores 1.95–12.2 on the whole-record tier.** CiteIndex's 11.1% strict sits inside that band. The headline contrast "GROBID 0.90 vs CiteIndex 0.11" is a comparison of GROBID's best field-level task-(b) number against CiteIndex's whole-record task-(a) number. It is not a valid comparison.

Matching tier swings numbers by tens of points even within field-level scoring: PMC abstract F1 moves 16.20 strict → 62.43 soft → 89.08 Levenshtein. GROBID also cautions that its gold derives from publisher JATS, which "contains some encoding errors … more a relative indication of error rates than trustful absolute accuracy performances" (https://grobid.readthedocs.io/en/latest/benchmarks/Benchmarking/).

### 6.2 Corpus and language

GROBID, CERMINE, and the DocBank/Tkaczyk evaluations are **English, life-science, born-digital, clean-JATS-gold**. CiteIndex's pilot is **multilingual, heavily scanned, patristic/theological books** with human-reviewed gold, including Syriac, Coptic, Chinese, and German material. GROBID's own CJK/Arabic support is stated without published evaluation figures (https://grobid.readthedocs.io/en/latest/Introduction/). The nearest genuinely multilingual task-(a) benchmarks are MOLE (76 F1 best) and BiblioPage (67–70 mF1 best on Czech scans) — nowhere near 90.

So the correct external band for CiteIndex's setting is roughly **0.6–0.8 field-level F1**, not 0.9. CiteIndex's `issued` 0.926 and `publisher` 0.824 are at or above that band; `title` 0.444 and `author` 0.491 are below it.

### 6.3 Sample size and statistical fragility

36 reviewed records, 16 digital and 12 scanned. One record is 6.25% of the digital set and 8.3% of the scanned set. The difference between "digital 2/16" and "digital 3/16" is a single document. No confidence interval is published, and none is derivable from the artifacts present. **No claim about the size of the digital/scanned gap should be made from these numbers.**

### 6.4 Task mismatch in the comparison as posed

PaperQA2 (66.0%) and PageIndex (98.7%) are task (c). Their benchmarks score answer accuracy and retrieval, not bibliographic fields. They are not evidence about CiteIndex's extractor.

### 6.5 Genuine extractor defects

Detailed in §7. These explain the low `title` F1 and part of the record failures, and they are real.

---

## 7. CiteIndex's real failures, ranked

Each is verified in source, not inferred from the report's prose.

### 7.1 The default window excludes pages the corpus needs — confirmed

Default `page_range = "1-5, -3"` (`citeindex/ingestion/models.py:44`) yields first five plus last three, and only those blocks are previewed (`dspy_extract.py:181-184`). For any document longer than eight pages, **physical page 6, 7, 8, … are never seen.**

The failure report confirms this against saved text: "Grace for Grace, Making Martyrs, and Seven Exegetical Works have publication information on physical page 6. Saved digital text confirms that information exists. Clemens Alexandrinus has detailed title-page evidence on page 9" (`docs/2026-09-25-pdf-benchmark-failure-analysis.md:77`). These are exactly the missing-ISBN and missing-year failures.

This is a **configuration ceiling**, not a model incapacity. The report is explicit that expanding it is "a separate experiment, not a silent change to the user's constraint" (`:130`).

### 7.2 Preview budget truncation — confirmed by construction

`page_budget = max(1, 12000 // max(len(window), 1))` (`dspy_extract.py:186`). At the default 8-page window that is ≈1,500 characters per page, and later blocks on a page can be truncated to empty and dropped (`:192-193`). A copyright page whose identifying line sits low on the page can be cut. No measurement exists of how many failures this causes; it is a structural risk, not a measured one.

### 7.3 OCR quality is upstream of extraction — confirmed

"A PDF having extractable text does not prove that text is usable. DSPy cannot recover absent evidence reliably from text alone" (`docs/2026-09-25-pdf-benchmark-failure-analysis.md:81`). The same page records MinerU omitting a visibly printed title that Tesseract read correctly (`benchmarks/citations/README.md:87-89`), and garbage strings `ALEXANDRI A`, `TW O REDISCOVERED W ORKS`, `LI+URGY` (`:81`). No prompt or model change fixes missing evidence.

### 7.4 Evidence validation rejects correct values — confirmed, cause partly unresolved

`title` evidence is rejected when `metadata_key` is `filename`/`source_path` (`citation_verification.py:50`), and values must be substring- or token-supported by a returned quote (`:29-38`). The failure report records title rejections with the correct title page selected for Cyril of Alexandria, Gregory/Macarius, and Divine Liturgy, then notes honestly: "It is incorrect to attribute every rejected field to model misunderstanding" (`docs/2026-09-25-pdf-benchmark-failure-analysis.md:85`).

**Not established anywhere in the available artifacts:** whether the dominant cause is a bad model quote, a missing source span, or normalization behaviour inside `validate_block_evidence`. The logs do not resolve it (`:85`).

### 7.5 The refinement loop cannot correct itself — confirmed

- Two attempts maximum (`dspy_extract.py:820`).
- Only four blocks carried forward (`:844`).
- **Accepted fields are never revisited** (`:853`).
- Stop as soon as the essentials are present (`:862-866`).

So a first-attempt wrong-but-supported title is permanent, and attempt two is a near-repetition with four blocks. The failure report: "Two extraction calls do not necessarily provide a useful second chance" (`:93`).

### 7.6 Unsupported document types — confirmed

`LocateBibliographicPages` supports book, chapter, article-journal, report, thesis (`dspy_extract.py:164`, enforced `:220`). **Entry-encyclopedia and manuscript are absent**, making the correct reviewed types for Cyprian of Carthage and Divine Liturgy unreachable (`docs/2026-09-25-pdf-benchmark-failure-analysis.md:89`). No evidence exists that adding them is sufficient, only that their absence is currently decisive for those two sources.

### 7.7 Scanned path loses its deterministic fallback — confirmed

`dspy_extract.py:918-921` returns `dspy_csl` whenever `total_pages` is set, and the scanned pipeline always sets it (`scanned_common.py:185`). `pattern_csl` is discarded; a failed extraction yields empty metadata, not the pattern result. This is the strongest purely mechanical defect found and it directly contradicts `README.md:209`.

### 7.8 PageIndex investment does not reach the extraction — confirmed

With a truthy selection, region scoring is skipped (`dspy_extract.py:799-806`). Both benchmark batches also disabled PageIndex outright (`docs/2026-09-25-pdf-benchmark-failure-analysis.md:104`). The footnotes and tree work described in `docs/plans/2025-05-06-pageindex-footnotes-design.md` and `-impl.md` is therefore not exercised in the measured path.

### 7.9 Scoring conflates severity — confirmed

Representation differences (city/state, honorifics, Latin name forms, series text inside gold titles, duplicated particles) are scored identically to wrong types and absent authors (`docs/2026-09-25-pdf-benchmark-failure-analysis.md:97`). The report correctly refuses to loosen gold to improve the number. But the metric is blunt, and whole-record accuracy over 36 sources has no error budget.

---

## 8. Implications, grounded and marked as proposals

Nothing here is implemented. Each is tied to a confirmed defect above and to an external practice that already exists.

1. **Report field-level F1 alongside record accuracy, and label the tier.** The project already computes field F1 (`benchmarks/citations/README.md:54-62`) but headlines the record number. GROBID publishes both because only together are they interpretable (§6.1). This is a reporting change, not an accuracy claim.
2. **Make the search window adaptive rather than a fixed ceiling.** MOLE finds "most of the metadata can be extracted at the upper part of the paper" (https://arxiv.org/abs/2505.19800), which supports front-matter weighting, but the corpus shows imprint pages at 6 and 9 (§7.1). An expanded window is a distinct experiment under the user's stated constraint.
3. **Restore the pattern fallback on the scanned path** (§7.7). Deleting the early return at `dspy_extract.py:918-921` costs nothing and makes `README.md:209` true. This is the highest-value, lowest-risk item found.
4. **Allow accepted fields to be revised when later evidence contradicts them** (§7.5). The current monotonic `accepted` set is the mechanism.
5. **Add a visual page-role detector for imprint pages rather than heading matching.** The failure report reaches the same conclusion independently (`docs/2026-09-25-pdf-benchmark-failure-analysis.md:119,128`), and it is exactly the kind of layout labelling GROBID and CERMINE obtain from CRF zone classification (§5.1).
6. **Add entry-encyclopedia and manuscript to the accepted type set** (§7.6).
7. **Investigate rejection causes before changing the validator** (§7.4). The validator is the project's integrity guarantee; the report explicitly does not know whether it or the model is at fault.

---

## 9. Limitations and what was not verified

- **No head-to-head run.** GROBID was not executed on CiteIndex's corpus, and the benchmark's own GROBID control is documented but has no recorded result (`benchmarks/citations/README.md:24-26`). All cross-project comparisons are therefore method-and-tier comparisons, not measured on shared inputs.
- **No published external task-(a) result on a multilingual scanned book corpus of this type was found.** The nearest points are MOLE and BiblioPage (§5.4). Absence is stated rather than filled.
- **CiteIndex record scores are self-reported** from local, git-ignored artifacts (`docs/2026-09-25-pdf-benchmark-failure-analysis.md:23-29`). They were not recomputed for this analysis.
- **The 2026-09-25 experiment was not controlled**, by its own statement: different signatures between batches, a mid-batch fallback change, different scanned coverage, and PageIndex/layout disabled (`:99-105`). It establishes concrete failures, not comparative performance.
- **Docling and science-parse publish no bibliographic-metadata metric**; that absence is reported as absence, not as poor quality.
- **`docs/2026-09-25-pdf-benchmark-failure-analysis.md:140` records that direct upstream PageIndex source retrieval failed** during its own follow-up; its code-level PageIndex claims refer to the local vendored copy. The same caveat applies to the PageIndex statements in §3.4 and §7.8, which are grounded in local source, not upstream.
- No accuracy percentage for CiteIndex was invented, and no gold annotation was altered.

---

## 10. References

**Local source**

- `citeindex/ingestion/pipelines/dspy_extract.py:155-229, 763-875, 891-937`
- `citeindex/ingestion/models.py:33, 44, 49`
- `citeindex/ingestion/citation_verification.py:25-76`
- `citeindex/ingestion/csl.py:14, 23-46, 60-80`
- `citeindex/ingestion/pipelines/scanned_common.py:185`
- `benchmarks/citations/run.py:145-152`, `benchmarks/citations/README.md`
- `docs/2026-09-25-pdf-benchmark-failure-analysis.md`
- `docs/plans/2026-09-14-citation-accuracy-and-benchmark.md`, `docs/plans/2026-09-14-multimodal-citation-benchmark.md`
- `docs/plans/2026-08-26-citation-metadata-verification.md`

**External**

- GROBID principles: https://grobid.readthedocs.io/en/latest/Principles/ · introduction: https://grobid.readthedocs.io/en/latest/Introduction/ · model benchmarks: https://grobid.readthedocs.io/en/latest/benchmarks/Benchmarking-models/ · PMC: https://grobid.readthedocs.io/en/latest/benchmarks/Benchmarking-pmc/ · bioRxiv: https://grobid.readthedocs.io/en/latest/benchmarks/Benchmarking-biorxiv/ · PLOS: https://grobid.readthedocs.io/en/latest/benchmarks/Benchmarking-plos/ · eLife: https://grobid.readthedocs.io/en/latest/benchmarks/Benchmarking-elife/ · end-to-end: https://grobid.readthedocs.io/en/latest/End-to-end-evaluation/
- GROBID evaluation dataset: https://huggingface.co/datasets/sciencialab/grobid-evaluation
- CERMINE: https://github.com/CeON/CERMINE · https://link.springer.com/article/10.1007/s10032-015-0249-8
- biblio-glutton: https://biblio-glutton.readthedocs.io/en/latest/Benchmarking/
- PaperQA2 / LitQA2: https://arxiv.org/abs/2409.13740 · https://github.com/Future-House/paper-qa
- PageIndex: https://github.com/VectifyAI/PageIndex · https://docs.pageindex.ai/sdk/documents · https://github.com/VectifyAI/PageIndex-OSS-Benchmark · https://vectify.ai/blog/Mafin2.5
- Docling: https://github.com/docling-project/docling · https://arxiv.org/abs/2408.09869
- Marker: https://github.com/datalab-to/marker
- Nougat: https://arxiv.org/abs/2308.13418
- science-parse / SPv2: https://github.com/allenai/science-parse
- MOLE (multilingual metadata, LLM failure modes): https://arxiv.org/abs/2505.19800
- BiblioPage (Czech scanned title pages): https://arxiv.org/abs/2503.19658
- DocBank cross-tool metadata comparison: https://arxiv.org/abs/2303.09957
- Tkaczyk reference-parsing comparison: https://arxiv.org/abs/1802.01168
- SSH parsing, GROBID vs LLMs: https://arxiv.org/abs/2603.13651
- DocBench (evaluator gap): https://arxiv.org/abs/2407.10701
