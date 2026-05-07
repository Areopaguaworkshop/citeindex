# PDF Layout Analysis Tool Comparison

> Research date: 2025-05-07  
> Context: citeindex project — detecting footnotes, page numbers, headers/footers in digital PDFs

## 1. Tools Already Evaluated

### 1.1 PyMuPDF (Current Implementation)

**What it is:** Python bindings to the MuPDF C library. Fast, lightweight, no ML models. Used for text extraction + custom layout heuristics.

**How we use it:** Text extraction via `fitz.open()`, then custom heuristics in `layout.py` for:
- Footnote detection: font-size analysis (small-font blocks at bottom of page)
- Page number detection: positional heuristics (top 12% / bottom 12%) + regex patterns
- Header/footer removal: font-size + positional filtering

**Strengths:**
- Ultra-fast: ~1000 pages/sec
- Zero ML model weights
- AGPL license (compatible with our project)
- Full text extraction with position, font, size metadata per span
- Mature, well-maintained library

**Weaknesses:**
- No built-in block type classification — all text is just text
- Merges header text into body paragraphs (e.g., `"AN INTRODUCTION TO SYRIAC STUDIES"` gets merged into the next paragraph)
- Merges running headers with page numbers (e.g., `"26 \nAN INTRODUCTION TO SYRIAC STUDIES"` as a single block)
- Block boundaries depend on PyMuPDF's internal grouping algorithm, which doesn't understand layout semantics
- Heuristics are fragile — font-size filtering misses body-font headers like `"TOOLS 27"`
- No concept of "footnote" vs "body" — must be inferred from position and font size

**Known issues in citeindex:**
- 3 header/footer leaks in Brock PDF: `"AN INTRODUCTION TO SYRIAC STUDIES"`, `"TOOLS"`, `"27"`
- These occur because PyMuPDF merges header text into body paragraphs rather than creating separate blocks

---

### 1.2 Docling (IBM)

**What it is:** MIT-licensed document conversion toolkit. Uses RT-DETR / RT-DETRv2 object detection models (Heron/Egret families) trained on DocLayNet + proprietary data (~150K documents).

**Layout model capabilities:**

| Label | DocLayNet Instances | AP Range | Status |
|-------|-------------------|----------|--------|
| `FOOTNOTE` | 6,318 | 83-91% | ✅ Built-in |
| `PAGE_HEADER` | 58,022 | 85-98% | ✅ Built-in |
| `PAGE_FOOTER` | 70,878 | 93-100% | ✅ Built-in |
| `PAGE_NUMBER` | — | — | ❌ Not a label |
| `TEXT` | 2.3M+ | 84-88% | ✅ Built-in |
| `SECTION_HEADER` | — | 83-84% | ✅ Built-in |

**Architecture:**
- Processes page images at 72 dpi
- RT-DETR model predicts bounding boxes with class labels
- Post-processing merges boxes with PDF text cells
- `ContentLayer` system: `BODY` / `FURNITURE` / `BACKGROUND` — headers/footers auto-assigned to `FURNITURE`, excluded from default exports
- Footnotes stored as `TextItem(label=FOOTNOTE)` linked to `FloatingItem` via `footnotes` refs

**Strengths:**
- Built-in `FOOTNOTE` label — no custom regex guessing
- Built-in `PAGE_HEADER`/`PAGE_FOOTER` with `FURNITURE` content layer — clean separation from body
- MIT license — no restriction
- Model weights ~200-500MB
- Reasonable speed: ~600ms/page on CPU (layout only), ~30ms/page on A100 GPU
- 93% mAP (Heron), 78% mAP (Heron-101)
- Well-documented Python API, integrated with LangChain/LlamaIndex

**Weaknesses:**
- **No `PAGE_NUMBER` label** — still need regex extraction from header/footer blocks
- **No inline footnote reference detection** — same gap we have now
- **No cross-page header/footer deduplication** — per-page detection only
- **Known issue** (GitHub #2650): footnotes "flow with main text" when spatial separation is unclear
- **Known issue** (GitHub #1272): headers/footers sometimes treated as sections due to format; community workaround uses similarity-based dedup
- Works on page **images**, not on text blocks — need to correlate with PyMuPDF text extraction
- Slower than PyMuPDF by ~600× for layout-only on CPU
- Requires `transformers`, `torch`, `safetensors` dependencies

---

### 1.3 MinerU / PDF-Extract-Kit

**What it is:** End-to-end PDF structuring pipeline. AGPL-3.0 licensed. Uses PaddleOCR + LayoutLMv3 for layout detection.

**Layout detection:**
- Custom layout model trained on ~21K annotated pages
- Categories: title, body, image, caption, table, table_caption, footnote, header, footer, page_number, discard
- **Has `page_number` as a dedicated discard category** — best for page number extraction
- **Has `footnote` as a layout category**
- Highest layout detection mAP (97.5%) per CodeSOTA benchmarks

**Strengths:**
- Best layout detection accuracy (97.5% mAP)
- `page_number` as a dedicated block type — no regex needed
- Handles headers, footers, rotated layouts effectively
- Especially strong for Chinese/Asian documents and academic papers
- Full pipeline: OCR + layout + table structure + formula recognition

**Weaknesses:**
- **AGPL-3.0 license** — may require source disclosure
- **Very slow**: 30-100× slower than PyMuPDF (~10-13 seconds/page on CPU, ~2-4 pages/sec on GPU)
- **Heavy dependencies**: PaddlePaddle, PaddleOCR, large model weights (~1-2GB)
- Complex setup, GPU recommended
- Overkill for digital-born PDFs (OCR unnecessary)
- Less mature Python API compared to Docling

---

## 2. Additional Tools Researched

### 2.1 PyMuPDF4LLM

**What it is:** A higher-level wrapper around PyMuPDF that adds a **Graph Neural Network (GNN)** for layout classification. Since ~Nov 2024, the GNN-based `pymupdf-layout` module is bundled by default and activates automatically.

**How it works:**
- Two-layer system: legacy heuristic mode (font-size headings, margin clipping) + GNN layout mode (default since v1.27+)
- The GNN classifies every text block on a page into **11 DocLayNet classes** using PDF-internal features (not rendered images)
- Output includes `page_boxes` with semantic labels per block

**Block type classification (11 DocLayNet classes):**

| Class | Markdown Behavior | F1 Score (PDF-features GNN) |
|-------|-------------------|------------------------------|
| `text` | Plain paragraph | 0.8675 |
| `title` | `# ` (h1) | 0.7672 |
| `section-header` | `## ` (h2) | 0.7823 |
| **`page-header`** | Removable via `header=False` | **0.8387** |
| **`page-footer`** | Removable via `footer=False` | **0.7973** (0.9277 image-augmented) |
| **`footnote`** | Rendered as `> ` blockquote | **0.7217** |
| `list-item` | `- ` prefixed with hierarchy | 0.8737 |
| `table` | GFM pipe tables | 0.6886 |
| `picture` | Image reference/embed | 0.2462 |
| `caption` | Normal text | 0.8157 |
| `formula` | Like picture | — |

**Strengths:**
- **Same PyMuPDF foundation** we already use — minimal migration effort
- GNN classifies blocks as `page-header`, `page-footer`, `footnote` — solves our "merged header" problem
- `header=False` / `footer=False` params cleanly remove headers/footers from output
- **CPU-only, no GPU needed** — GNN is only ~1.8M parameters (vs 140M+ for vision models)
- ~10× faster than vision-based alternatives (Docling, MinerU)
- Full JSON output with `page_boxes` containing class + bbox + text position per block
- Low-level API: `parse_document()` gives `LayoutBox` objects with `boxclass` field
- Superscript heuristics for footnote reclassification (if a `text` block starts with superscript, reclassified as `footnote`)

**Weaknesses:**
- **No footnote reference linking** — explicitly out of scope per maintainers (GitHub #116: "will probably never support")
- **No separate `page_number` label** — page numbers are inside `page-footer` blocks, need regex extraction
- **No cross-page header/footer deduplication** — per-page detection only
- **AGPL-3.0 for PyMuPDF4LLM**, but **pymupdf-layout engine has Polyform Noncommercial license** (free for non-commercial, commercial license required)
- Footnote F1 (0.72) lower than Docling (0.83-0.91)
- GNN model quality on academic PDFs with mixed-font headers unclear
- Image-augmented features improve `page-footer` to 0.93 but increase dependency complexity

**Integration potential for citeindex:**
- Could replace our custom `layout.py` heuristics with GNN-classified blocks
- `page_boxes` output gives us labeled blocks we can map to our `analyze_document_layout()` output
- `header=False` / `footer=False` approach simpler than our current `_remove_header_footer_paragraphs()`
- License concern: Polyform Noncommercial for pymupdf-layout may conflict with commercial use

---

### 2.2 LayoutParser

**What it is:** An academic layout detection library. Uses Detectron2-based models (Faster R-CNN, Mask R-CNN) trained on PubLayNet and other datasets.

**Status: Effectively abandoned.** Last release v0.3.4 (April 2022), 4+ years ago. 117 open issues, most unanswered.

**Model zoo — available label taxonomies:**

| Dataset | Labels |
|---------|--------|
| PubLayNet | `Text`, `Title`, `List`, `Table`, `Figure` (5 classes) |
| PRImA | `TextRegion`, `ImageRegion`, `TableRegion`, `MathsRegion`, `SeparatorRegion`, `OtherRegion` |
| HJDataset | `Page Frame`, `Row`, `Title Region`, `Text Region`, `Title`, `Subtitle`, `Other` |
| NewspaperNavigator | `Photograph`, `Illustration`, `Map`, `Comics/Cartoon`, `Editorial Cartoon`, `Headline`, `Advertisement` |
| TableBank | `Table` (1 class) |
| MFD | `Equation` (1 class) |

**Critical gap: No DocLayNet models.** None of the 9 pretrained models support `footnote`, `page_header`, `page_footer`, or `page_number` labels. You would need to train a Detectron2 model on DocLayNet yourself (~28GB dataset).

**Strengths:**
- Apache 2.0 license (permissive)
- Well-designed data structures for layout manipulation (`Layout`, `Rectangle`, `TextBlock`)
- Good for PubLayNet-style documents (scientific papers with basic layout)
- Visual debugging tools

**Weaknesses:**
- **Zero support for footnotes, page headers, page footers, page numbers** — not in any label taxonomy
- **Abandoned since 2022** — no maintenance, known breakage with Pillow ≥10
- Detectron2 is difficult to install (CUDA-specific, version-locked)
- No reading-order inference
- No header/footer removal
- No PDF-native processing — requires rendering to images first
- Superseded by Docling in every dimension (better models, more labels, active maintenance, simpler install)

**Verdict: Skip entirely.** Docling has superseded LayoutParser for all practical purposes. LayoutParser was academically important but is now obsolete.

---

### 2.3 pdfstruct (Kyros-Groupe-Ltd/pdfstruct, PyPI: `pdfstructx`)

**What it is:** A heuristic-based PDF structure extraction library. Uses `pdfminer.six` for text extraction + font analysis + spatial geometry for structure detection. Alpha status (v0.2.4), Apache 2.0 license.

**Note:** There are three projects with similar names. This section covers Kyros-Groupe-Ltd/pdfstruct (the most relevant). The others are:
- ChrizH/pdfstructure: Abandoned, pre-alpha, font-style heading detection only
- stanfordnlp/pdf-struct: ML-based (CRF + RandomForest), single-column only, no headers/footers/footnotes

**Approach: Pure heuristic (no ML).** Pipeline: pdfminer.six extraction → global font stats → header/footer detection → column detection → reading-order sort → heading detection → paragraph grouping → table detection.

**Block type classification — output types:**

| Type | How Detected | Notes |
|------|-------------|-------|
| `is_header` | Spatial zone (top 8%) + cross-page repetition (≥70% of pages) | Marks repeating page headers |
| `is_footer` | Spatial zone (bottom 8%) + cross-page repetition | Marks repeating page footers |
| `is_heading` | Font size > body OR bold + short text | H1-H6 levels |
| Page numbers | Regex `^(?:page\s*)?\d{1,4}(?:\s*(?:of\|/)\s*\d{1,4})?$` within header/footer zone | Folded into `is_footer`/`is_header` |
| Paragraphs | Font analysis + spacing | Grouped into paragraphs |
| Tables | Grid detection + whitespace analysis | With row/column structure |
| Lists | Bullet/number pattern detection | Bulleted, numbered, lettered, roman |

**Strengths:**
- **Cross-page header/footer detection via text repetition** — normalizes digits to `#` for comparison, detects if text appears on ≥70% of pages. This is better than per-page-only detection.
- Simple heuristic approach — no ML models, no GPU, no heavy dependencies
- Fast: 10-13 pages/sec for large documents
- Apache 2.0 license (permissive)
- Rich structural output: H1-H6 hierarchy, paragraph grouping, list detection, table detection
- Column detection + reading-order sorting built in
- Image extraction with metadata
- Tunable parameters (`zone_ratio`, `min_pages_for_detection`, `similarity_threshold`)

**Weaknesses:**
- **No footnote detection at all** — no code references, no labels, no heuristics
- **No footnote reference detection** — no superscript handling
- **Page number detection is partial** — regex only catches standalone `"42"`, `"Page 42"`, `"42 of 100"`. Misses embedded numbers like `"Chapter 3 — 42"` or `"TOOLS 27"`
- **Header/footer detection is binary** — no distinction between "page number in footer" vs "chapter title in header" vs "date in footer"
- Requires ≥3 pages for header/footer detection (won't work on single-page docs)
- Very young project (v0.2.4, 8 commits total, 0 GitHub stars)
- No published benchmarks or academic evaluation
- Uses pdfminer.six (heavier than PyMuPDF, slower for text extraction)

**Key difference from our approach:** pdfstruct uses cross-page **repetition** to detect headers/footers — if the same text (after normalizing digits) appears on ≥70% of pages, it's a header/footer. Our current approach uses **positional** heuristics (top/bottom 12% of page). pdfstruct's approach would catch `"TOOLS 27"` → `"TOOLS 28"` → `"TOOLS 29"` as repeating headers because `"TOOLS #"` normalizes to the same string across pages. This could complement our positional approach.

---

## 3. Comparison Summary

### Capability Matrix

| Capability | PyMuPDF (current) | Docling | MinerU | PyMuPDF4LLM | LayoutParser | pdfstruct |
|---|---|---|---|---|---|---|
| **Footnote block detection** | ⚠️ Custom heuristics | ✅ 83-91% AP | ✅ Layout model | ✅ GNN F1=0.72 | ❌ | ❌ |
| **Footnote reference linking** | ❌ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Page header detection** | ⚠️ Positional | ✅ 85-98% AP | ✅ Layout model | ✅ GNN F1=0.84 | ❌ | ✅ Repetition-based |
| **Page footer detection** | ⚠️ Positional | ✅ 93-100% AP | ✅ Layout model | ✅ GNN F1=0.80 | ❌ | ✅ Repetition-based |
| **Page number detection** | ⚠️ Regex+position | ⚠️ Inside header/footer | ✅ Dedicated label | ⚠️ Inside footer | ❌ | ⚠️ Regex (partial) |
| **Cross-page header dedup** | ❌ | ❌ | ❌ | ❌ | ❌ | ✅ 70% threshold |
| **Block type labels** | ❌ None | ✅ 11-17 classes | ✅ ~10 classes | ✅ 11 classes | ⚠️ 5 classes max | ⚠️ Binary only |
| **Digital PDF speed (CPU)** | ~1000 pgs/s | ~1.5 pgs/s | ~0.08 pgs/s | ~100 pgs/s* | ~2-5 pgs/s | ~10-13 pgs/s |
| **GPU required** | No | Optional | Recommended | No | Recommended | No |
| **Model weights** | 0 | ~200-500MB | ~1-2GB | ~few MB (GNN) | ~200MB | 0 |
| **License** | AGPL-3.0 | MIT | AGPL-3.0 | AGPL-3.0 / Polyform NC | Apache 2.0 | Apache 2.0 |
| **Maintenance** | Active | Very active | Active | Active | **Abandoned** | Alpha |

*PyMuPDF4LLM speed estimate based on GNN being ~10× faster than vision models while sharing PyMuPDF's text extraction speed.

### What Each Tool Adds vs. Our Current PyMuPDF Heuristics

| Tool | Key New Capability | Migration Effort |
|------|-------------------|------------------|
| **Docling** | ML-classified blocks (FOOTNOTE, PAGE_HEADER, PAGE_FOOTER labels) — would solve our merged-header problem | High (new pipeline, image-based, ~200MB model) |
| **MinerU** | Dedicated page_number label — best page number extraction | Very high (AGPL, full pipeline swap, heavy deps) |
| **PyMuPDF4LLM** | GNN-classified blocks on same PyMuPDF foundation — easiest migration | Low (same library, just add GNN classification layer) |
| **LayoutParser** | Nothing new for our use case | N/A (abandoned, no relevant labels) |
| **pdfstruct** | Cross-page repetition-based header/footer detection (complementary to our positional approach) | Medium (different extraction engine, but could port the repetition heuristic) |

### The "Merged Header" Problem

Our current 3 leaks (`"AN INTRODUCTION TO SYRIAC STUDIES"`, `"TOOLS"`, `"27"`) all stem from PyMuPDF merging header text into body paragraphs. Tool-by-tool assessment:

| Tool | Would It Fix This? | How |
|------|-------------------|-----|
| **Docling** | ✅ Likely yes | GNN classifies the block as `page_header` → `FURNITURE` layer → auto-excluded |
| **MinerU** | ✅ Likely yes | Layout model classifies as `header`/`discard` |
| **PyMuPDF4LLM** | ✅ Likely yes | GNN classifies as `page-header` → `header=False` removes it |
| **LayoutParser** | ❌ No | No relevant labels |
| **pdfstruct** | ⚠️ Maybe | Repetition detection would catch `"TOOLS #"` as a repeating pattern, but `"AN INTRODUCTION TO SYRIAC STUDIES"` on p.25 only appears once (first page of chapter) so would need ≥3 pages of repetition |

---

## 4. Recommendation

### Immediate Fix (Low Risk, High Value)

Fix the 3 remaining header leaks in our current PyMuPDF heuristics by:
1. Improving `_remove_header_footer_paragraphs()` to split paragraphs at header boundaries (when a `headers_footers` block's text partially matches a paragraph)
2. Borrowing pdfstruct's **cross-page repetition heuristic**: track repeating text patterns across pages (normalizing digits), classify as headers/footers when appearing on ≥70% of pages
3. Adding font-size-based classification for small-font or bare-number blocks at top/bottom of pages

This maintains zero additional dependencies and matches our current architecture.

### Medium-Term: Add PyMuPDF4LLM as Optional Layout Backend

If we need better classification beyond heuristics, PyMuPDF4LLM is the most pragmatic upgrade:
- Same PyMuPDF foundation — minimal code change
- GNN classifies blocks as `page-header`, `page-footer`, `footnote` — directly solves our merged-header problem
- ~1.8M parameter model, CPU-only, ~10× faster than vision-based alternatives
- `page_boxes` output with semantic labels maps cleanly to our document structure

**Caveat:** The `pymupdf-layout` GNN engine uses **Polyform Noncommercial License** — need to verify compatibility with our usage.

### Long-Term: Consider Docling for Maximum Accuracy

If classification accuracy becomes critical (e.g., handling diverse PDF formats beyond academic books):
- Docling's RT-DETR models achieve 85-100% AP on headers/footers
- `FURNITURE` content layer cleanly separates body from headers/footers
- But requires ~200-500MB model weights, GPU recommended for speed
- Would need to correlate Docling's image-based blocks with PyMuPDF text extraction
- MIT license is most permissive

### Do Not Pursue

- **LayoutParser**: Abandoned, no relevant labels, no maintenance
- **MinerU**: AGPL license, 30-100× slower, overkill for digital PDFs
- **pdfstruct**: Useful heuristic ideas (cross-page repetition), but too immature as a dependency and no footnote detection