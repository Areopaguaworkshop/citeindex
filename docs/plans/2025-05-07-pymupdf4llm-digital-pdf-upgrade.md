# PyMuPDF4LLM Digital PDF Pipeline Upgrade

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace raw PyMuPDF text extraction + manual layout heuristics with PyMuPDF4LLM's GNN-classified blocks for the digital PDF pipeline, fixing the "merged header into body" problem and getting built-in footnote/header/footer/page-number classification.

**Architecture:** Keep the same pipeline structure (`run()` steps 1→3b→...→8) but replace the text extraction layer (Step 1) and layout analysis layer (Step 3b). `parse_document()` from `pymupdf4llm.helpers.document_layout` replaces both `_extract_pages()` + `_extract_page_paragraphs()` (Step 1) and `analyze_document_layout()` (Step 3b). The GNN classifies blocks into 11 DocLayNet labels, replacing our manual heuristics for footnotes, headers/footers, and page numbers. Downstream code (`_attach_layout_footnotes`, `_remove_header_footer_paragraphs`, `_apply_page_number_map`, `pageindex_tree.py`) continues to work because the output data structures are unchanged.

**Tech Stack:** pymupdf4llm (adds pymupdf_layout GNN ~1.8M params, CPU-only, ONNX runtime), Python 3.10+

---

## Design Decisions

1. **Single open, single classify**: Use `parse_document()` to open the PDF once — it runs the GNN and extracts text in one pass. No separate `fitz.open()` for Step 1 + Step 3b.

2. **Output shape preserved**: `analyze_document_layout_pymupdf4llm()` returns the same dict shape as the current `analyze_document_layout()` so all downstream consumers (`_attach_layout_footnotes`, `_remove_header_footer_paragraphs`, `build_page_number_map`, `pageindex_to_citeindex_tree`) keep working unchanged.

3. **Fallback to legacy**: If `pymupdf4llm` import fails (not installed) or `parse_document()` raises, fall back to the current `analyze_document_layout()` + `_extract_pages()` flow. This keeps the pipeline working without the new dependency.

4. **`clean_page_texts` still runs**: Needed for text segmentation (`split_paragraphs` uses `\n\n` breaks from cleaned text). The GNN gives classified blocks but we still need clean text for `page_paragraphs` used in nodes/merkle and `ordered_text` for citation extraction.

5. **No PolyForm license concern for now**: The `pymupdf_layout` GNN engine uses PolyForm Noncommercial license. We treat this as a dev/optional dependency — the pipeline works without it (fallback). For commercial deployment, a paid Artifex license would be needed. This is acceptable for current research use.

---

## Task 1: Install pymupdf4llm and verify GNN layout works

**Files:**
- Modify: `pyproject.toml` (or `requirements.txt`) — add optional dependency
- Test: run `python -c "from pymupdf4llm.helpers.document_layout import parse_document; print('OK')"` 

**Step 1: Install pymupdf4llm**

```bash
pip install pymupdf4llm
```

**Step 2: Verify the GNN layout engine is available**

```bash
python -c "
from pymupdf4llm.helpers.document_layout import parse_document
import pymupdf
doc = pymupdf.open('2-Brock-2017-Introduction-Syriac-Studies-25-51.pdf')
pdoc = parse_document(doc, pages=[0, 1, 2])
for page in pdoc.pages:
    print(f'--- Page {page.page_number} ---')
    for box in page.boxes:
        text = ''
        if box.textlines:
            text = ' '.join(s['text'] for tl in box.textlines for s in tl['spans'])[:60]
        print(f'  [{box.boxclass}] {text}')
"
```

Expected: Each page shows classified blocks like `[page-header]`, `[text]`, `[section-header]`, `[footnote]`, `[page-footer]`.

**Step 3: Verify the 3 leak cases are classified correctly**

Check specifically that on the Brock PDF:
- Page 1 (p.25): `"AN INTRODUCTION TO SYRIAC STUDIES"` should be classified as `page-header`
- Page 2 (p.26): `"26 \nAN INTRODUCTION TO SYRIAC STUDIES"` should be `page-header`  
- Page 3 (p.27): `"TOOLS 27"` should be `page-header`

If the GNN classifies them correctly, proceed. If not, we'll need a hybrid approach.

**Step 4: Add pymupdf4llm as optional dependency in pyproject.toml**

Add under `[project.optional-dependencies]`:
```toml
[project.optional-dependencies]
layout = ["pymupdf4llm>=0.0.20"]
```

**Step 5: Commit**

```bash
git add pyproject.toml
git commit -m "chore: add pymupdf4llm as optional layout dependency"
```

---

## Task 2: Create `analyze_document_layout_pymupdf4llm()` in layout.py

**Files:**
- Modify: `citeindex/ingestion/pipelines/layout.py`
- Test: `tests/test_pageindex_integration.py` (add new test)

This function replaces `analyze_document_layout()` when pymupdf4llm is available. It returns the **same dict shape** so all downstream code works unchanged.

**Step 1: Write the failing test**

```python
# In tests/test_pageindex_integration.py
def test_pymupdf4llm_layout_returns_same_shape_as_analyze_document_layout():
    """analyze_document_layout_pymupdf4llm returns same dict keys as analyze_document_layout."""
    from citeindex.ingestion.pipelines.layout import analyze_document_layout_pymupdf4llm

    pdf = "2-Brock-2017-Introduction-Syriac-Studies-25-51.pdf"
    result = analyze_document_layout_pymupdf4llm(pdf)
    assert len(result) > 0
    page0 = result[0]
    # Same keys as analyze_page_layout() returns
    for key in ("page_number", "columns", "footnotes", "headers_footers", "page_number_candidates", "ordered_text"):
        assert key in page0, f"Missing key: {key}"
```

**Step 2: Run test to verify it fails**

```bash
pytest tests/test_pageindex_integration.py::test_pymupdf4llm_layout_returns_same_shape_as_analyze_document_layout -v
```

Expected: FAIL with ImportError

**Step 3: Implement `analyze_document_layout_pymupdf4llm()`**

Add to `layout.py`:

```python
def analyze_document_layout_pymupdf4llm(pdf_path: str) -> List[Dict[str, Any]]:
    """Analyze layout using PyMuPDF4LLM's GNN classification.
    
    Returns the same dict shape as analyze_document_layout(), so all downstream
    consumers work unchanged. Uses pymupdf4llm's GNN to classify blocks into:
      page-header, page-footer, footnote, text, section-header, title, etc.
    
    Falls back to analyze_document_layout() if pymupdf4llm is not installed.
    """
    try:
        from pymupdf4llm.helpers.document_layout import parse_document
    except ImportError:
        logger.warning("pymupdf4llm not installed, falling back to heuristic layout")
        return analyze_document_layout(pdf_path)
    
    import pymupdf
    
    doc = pymupdf.open(pdf_path)
    try:
        pdoc = parse_document(doc, show_progress=False)
    except Exception:
        logger.warning("pymupdf4llm parse_document() failed, falling back", exc_info=True)
        doc.close()
        return analyze_document_layout(pdf_path)
    
    results: List[Dict[str, Any]] = []
    
    for page in pdoc.pages:
        page_number = page.page_number  # 1-based
        
        # Separate blocks by GNN class
        body_blocks = []     # text, section-header, title, list-item, caption
        footnote_blocks = [] # footnote
        header_blocks = []   # page-header
        footer_blocks = []   # page-footer
        
        for box in page.boxes:
            btype = box.boxclass
            # Extract text from box
            text = ""
            lines_data = []
            if box.textlines:
                for tl in box.textlines:
                    line_text = "".join(s["text"] for s in tl["spans"])
                    lines_data.append({
                        "text": line_text,
                        "bbox": list(tl["bbox"]) if hasattr(tl["bbox"], '__iter__') else [],
                    })
                    text += line_text + "\n"
                text = text.rstrip("\n")
            
            block_dict = {
                "page_number": page_number,
                "block_id": len(body_blocks) + len(footnote_blocks) + len(header_blocks) + len(footer_blocks),
                "text": text,
                "bbox": [box.x0, box.y0, box.x1, box.y1],
                "font_size": 0.0,  # Will compute from spans below
                "font_name": "",
                "lines": lines_data,
            }
            
            # Compute dominant font size from spans
            if box.textlines:
                sizes = []
                for tl in box.textlines:
                    for s in tl["spans"]:
                        char_count = len(s.get("text", ""))
                        sizes.extend([s.get("size", 0.0)] * char_count)
                if sizes:
                    from collections import Counter
                    block_dict["font_size"] = Counter(sizes).most_common(1)[0][0]
            
            if btype == "page-header":
                header_blocks.append(block_dict)
            elif btype == "page-footer":
                footer_blocks.append(block_dict)
            elif btype == "footnote":
                footnote_blocks.append(block_dict)
            else:
                # text, section-header, title, list-item, caption, table, picture, formula
                body_blocks.append(block_dict)
        
        # Build columns from body blocks (same column detection algorithm)
        columns = detect_columns(body_blocks, page.width)
        
        # Build column dicts (same shape as analyze_page_layout)
        column_dicts: List[Dict[str, Any]] = []
        for col_idx, col_blocks in enumerate(columns):
            col_sorted = sorted(col_blocks, key=lambda b: b["bbox"][1])
            paragraphs: List[Dict[str, Any]] = []
            for para_idx, block in enumerate(col_sorted, start=1):
                paragraphs.append({
                    "paragraph_id": f"p{page_number}_c{col_idx}_para{para_idx}",
                    "text": block["text"],
                    "lines": block["lines"],
                    "bbox": block["bbox"],
                })
            column_dicts.append({
                "column_id": col_idx,
                "paragraphs": paragraphs,
            })
        
        # Build footnote dicts (same shape)
        fn_dicts: List[Dict[str, Any]] = []
        for fn_idx, fn_block in enumerate(
            sorted(footnote_blocks, key=lambda b: b["bbox"][1]), start=1
        ):
            fn_dicts.append({
                "footnote_id": f"p{page_number}_fn{fn_idx}",
                "text": fn_block["text"],
                "bbox": fn_block["bbox"],
            })
        
        # Build header/footer dicts (same shape)
        hf_dicts: List[Dict[str, Any]] = []
        for hf_block in sorted(header_blocks + footer_blocks, key=lambda b: b["bbox"][1]):
            hf_dicts.append({
                "text": hf_block["text"],
                "bbox": hf_block["bbox"],
            })
        
        # Extract page number candidates from header/footer blocks
        page_num_candidates: List[int] = []
        for hf_block in header_blocks + footer_blocks:
            page_num_candidates.extend(
                extract_page_number_candidates(hf_block["text"])
            )
        
        # Build ordered text (body + footnotes)
        ordered_blocks = sorted(body_blocks, key=lambda b: (b["bbox"][0], b["bbox"][1]))
        ordered_blocks.extend(sorted(footnote_blocks, key=lambda b: b["bbox"][1]))
        ordered_text = "\n\n".join(b["text"] for b in ordered_blocks)
        
        results.append({
            "page_number": page_number,
            "columns": column_dicts,
            "footnotes": fn_dicts,
            "headers_footers": hf_dicts,
            "page_number_candidates": page_num_candidates,
            "ordered_text": ordered_text,
        })
    
    doc.close()
    return results
```

**Step 4: Run test to verify it passes**

```bash
pytest tests/test_pageindex_integration.py::test_pymupdf4llm_layout_returns_same_shape_as_analyze_document_layout -v
```

Expected: PASS

**Step 5: Commit**

```bash
git add citeindex/ingestion/pipelines/layout.py
git commit -m "feat: add analyze_document_layout_pymupdf4llm() with GNN classification"
```

---

## Task 3: Wire pymupdf4llm into digital_pdf.py run()

**Files:**
- Modify: `citeindex/ingestion/pipelines/digital_pdf.py` (Step 3b section)

**Step 1: Update the import and use the new function**

In `digital_pdf.py`, change the Step 3b section:

**Before:**
```python
from .layout import analyze_document_layout, build_page_number_map
```

**After:**
```python
from .layout import analyze_document_layout, analyze_document_layout_pymupdf4llm, build_page_number_map
```

In `run()`, Step 3b, change:

**Before:**
```python
            page_layouts = analyze_document_layout(pdf_path)
```

**After:**
```python
            page_layouts = analyze_document_layout_pymupdf4llm(pdf_path)
```

This is a single-line change. The fallback logic is inside `analyze_document_layout_pymupdf4llm()` itself — if pymupdf4llm is not installed, it calls the legacy `analyze_document_layout()`.

**Step 2: Run existing tests to verify nothing is broken**

```bash
pytest tests/test_pageindex_integration.py -v
```

Expected: All 27 tests PASS (or more if new ones were added in Task 2)

**Step 3: Commit**

```bash
git add citeindex/ingestion/pipelines/digital_pdf.py
git commit -m "feat: use pymupdf4llm GNN layout in digital PDF pipeline"
```

---

## Task 4: Replace Step 1 text extraction with pymupdf4llm blocks

Currently Step 1 (`_extract_pages` + `_extract_page_paragraphs`) opens the PDF with raw PyMuPDF, extracts text, and splits paragraphs. With pymupdf4llm, we already open the PDF in Step 3b via `parse_document()`. We can **reuse the classified blocks** for building `page_paragraphs` and `ordered_text`, eliminating the redundant `fitz.open()` in Step 1.

**Files:**
- Modify: `citeindex/ingestion/pipelines/digital_pdf.py`

**Step 1: Create `_extract_pages_from_layout()` builder**

Add a new helper that constructs the `page_paragraphs` and `ordered_text` from the GNN layout results, avoiding the duplicate `fitz.open()`:

```python
def _build_page_paragraphs_from_layout(
    page_layouts: List[Dict[str, Any]],
) -> Tuple[List[Tuple[int, List[str]]], str]:
    """Build page_paragraphs and ordered_text from GNN layout analysis.
    
    Returns (page_paragraphs, ordered_text) — same shape as the output of
    _extract_page_paragraphs() + ordered_text concatenation.
    Only includes body text (not headers/footers/footnotes) to match
    the current behavior where layout analysis removes those later.
    """
    page_paragraphs: List[Tuple[int, List[str]]] = []
    all_texts: List[str] = []
    
    for layout in page_layouts:
        page_num = layout.get("page_number", 0)
        paragraphs = []
        
        for col in layout.get("columns", []):
            for para in col.get("paragraphs", []):
                text = para.get("text", "")
                if text.strip():
                    # Split multi-line paragraphs the same way split_paragraphs does
                    for chunk in split_paragraphs(text):
                        if chunk.strip():
                            paragraphs.append(chunk)
        
        if not paragraphs:
            # Fallback: use ordered_text split by double newlines
            ordered = layout.get("ordered_text", "")
            for chunk in split_paragraphs(ordered):
                if chunk.strip():
                    paragraphs.append(chunk)
        
        page_paragraphs.append((page_num, paragraphs))
        all_texts.append(layout.get("ordered_text", ""))
    
    ordered_text = "\n\n".join(all_texts)
    return page_paragraphs, ordered_text
```

**Step 2: Restructure `run()` Step 1 and Step 3b to use shared layout**

The key change: when `use_layout_analysis=True`, we do layout FIRST (which gives us both classified blocks AND text), then derive `page_paragraphs` from it. When `use_layout_analysis=False`, we fall back to the old `_extract_pages()` + `_extract_page_paragraphs()` flow.

In `run()`, replace Steps 1 and 3b:

```python
    # ── Step 1: Text extraction + Layout analysis ─────────────────
    raw_pages: Optional[List[Dict[str, Any]]] = None
    page_layouts: Optional[List[Dict[str, Any]]] = None
    page_number_map: Dict[int, int] = {i: i + 1 for i in range(num_pages)}
    
    if cfg.use_layout_analysis:
        try:
            page_layouts = analyze_document_layout_pymupdf4llm(pdf_path)
            page_paragraphs, ordered_text = _build_page_paragraphs_from_layout(page_layouts)
        except Exception:
            logger.warning("pymupdf4llm layout failed, falling back to raw extraction", exc_info=True)
            page_layouts = None
    
    if page_layouts is None:
        # Fallback: raw PyMuPDF extraction (no layout classification)
        raw_pages = _extract_pages(pdf_path)
        page_paragraphs = _extract_page_paragraphs(raw_pages)
        ordered_text = "\n\n".join(p["text"] for p in raw_pages) if raw_pages else ""
    
    # ── Step 1b: Layout post-processing (footnotes, page numbers, headers/footers)
    if page_layouts is not None:
        _attach_layout_footnotes(document_structure, page_layouts)
        _remove_header_footer_paragraphs(document_structure, page_layouts)
        
        detected_map = build_page_number_map(page_layouts)
        if detected_map:
            page_number_map = detected_map
            logger.info(
                "Page number map from layout: offset=%d (covers %d/%d pages)",
                page_number_map.get(0, 1) - 1,
                len(page_number_map),
                num_pages,
            )
            _apply_page_number_map(document_structure, page_number_map)
```

Note: When `use_layout_analysis=True` and pymupdf4llm works, `_remove_header_footer_paragraphs()` becomes mostly a no-op because headers/footers are already excluded from the `columns[].paragraphs` in the layout output. But we keep it for safety — and for the fallback path.

**Step 3: Run tests**

```bash
pytest tests/test_pageindex_integration.py -v
```

Expected: All tests PASS

**Step 4: Commit**

```bash
git add citeindex/ingestion/pipelines/digital_pdf.py
git commit -m "feat: use pymupdf4llm layout for text extraction, removing duplicate fitz.open()"
```

---

## Task 5: Verify end-to-end with Brock and Chatonnet PDFs

**Files:**
- No code changes
- Manual verification

**Step 1: Run full pipeline on Brock PDF**

```bash
python -c "
from citeindex.ingestion.pipelines.digital_pdf import run
from citeindex.ingestion.markdown_export import generate_library_markdown

result = run('2-Brock-2017-Introduction-Syriac-Studies-25-51.pdf')
doc = result.document_json

# Check page numbers (should be 25-51, not 1-27)
pages = doc['structure']['pages']
print(f'First page number: {pages[0][\"page_number\"]}')
print(f'Last page number: {pages[-1][\"page_number\"]}')

# Check footnotes
total_fns = sum(len(p.get('footnotes', [])) for p in pages)
print(f'Total footnotes: {total_fns}')

# Generate library markdown
md = generate_library_markdown(
    csl_json=result.csl_json,
    document_json=doc,
    transcript_json=None,
    resource_type='digital_pdf',
)
with open('library/brock_2_brock_2017_introduction_syri.md', 'w') as f:
    f.write(md)

# Check for header leaks
for line_no, line in enumerate(md.split('\n'), 1):
    stripped = line.strip()
    if stripped in ('AN INTRODUCTION TO SYRIAC STUDIES', 'TOOLS'):
        print(f'LEAK at line {line_no}: {stripped}')
    if stripped.isdigit() and len(stripped) <= 3:
        print(f'PAGE NUMBER LEAK at line {line_no}: {stripped}')

print('Verification complete')
"
```

Expected:
- First page number: 25
- Last page number: 51
- Total footnotes: 8
- No header leaks (no "AN INTRODUCTION TO SYRIAC STUDIES", "TOOLS", or standalone page numbers in body)

**Step 2: Run on Chatonnet PDF (verify no regressions)**

```bash
python -c "
from citeindex.ingestion.pipelines.digital_pdf import run
result = run('1-Chatonnet-2023-Origins-A-Culture-of-Encounter.pdf')
doc = result.document_json
pages = doc['structure']['pages']
total_fns = sum(len(p.get('footnotes', [])) for p in pages)
print(f'Pages: {len(pages)}, First: {pages[0][\"page_number\"]}, Last: {pages[-1][\"page_number\"]}')
print(f'Footnotes: {total_fns}')
assert total_fns == 0, 'Chatonnet should have 0 footnotes!'
print('Chatonnet OK')
"
```

Expected: 0 footnotes, page numbers 1-24 (identity mapping)

**Step 3: Run all tests**

```bash
pytest tests/ -v
```

Expected: All PASS

---

## Task 6: Add integration tests for pymupdf4llm layout

**Files:**
- Modify: `tests/test_pageindex_integration.py`

**Step 1: Write test for GNN header/footer classification**

```python
def test_pymupdf4llm_classifies_headers_and_footers_on_brock():
    """GNN should classify 'AN INTRODUCTION TO SYRIAC STUDIES' and 'TOOLS N' as page-header."""
    from citeindex.ingestion.pipelines.layout import analyze_document_layout_pymupdf4llm
    
    pdf = "2-Brock-2017-Introduction-Syriac-Studies-25-51.pdf"
    layouts = analyze_document_layout_pymupdf4llm(pdf)
    
    # Collect all header/footer text
    hf_texts = set()
    for layout in layouts:
        for hf in layout.get("headers_footers", []):
            hf_texts.add(hf["text"].strip())
    
    # At least some headers should be detected
    assert len(hf_texts) > 0, "Expected some headers/footers to be detected"
    
    # No header text should appear in body paragraphs
    for layout in layouts:
        for col in layout.get("columns", []):
            for para in col.get("paragraphs", []):
                text = para["text"].strip()
                assert text != "AN INTRODUCTION TO SYRIAC STUDIES"
                assert text != "TOOLS"
```

**Step 2: Write test for GNN footnote classification**

```python
def test_pymupdf4llm_classifies_footnotes_on_brock():
    """GNN should find footnotes on the Brock PDF."""
    from citeindex.ingestion.pipelines.layout import analyze_document_layout_pymupdf4llm
    
    pdf = "2-Brock-2017-Introduction-Syriac-Studies-25-51.pdf"
    layouts = analyze_document_layout_pymupdf4llm(pdf)
    
    total_fns = sum(len(layout.get("footnotes", [])) for layout in layouts)
    assert total_fns >= 6, f"Expected >=6 footnotes, got {total_fns}"
```

**Step 3: Write test for Chatonnet (no false positive footnotes)**

```python
def test_pymupdf4llm_no_false_positive_footnotes_on_chatonnet():
    """GNN should not detect footnotes on Chatonnet (which has none)."""
    from citeindex.ingestion.pipelines.layout import analyze_document_layout_pymupdf4llm
    
    pdf = "1-Chatonnet-2023-Origins-A-Culture-of-Encounter.pdf"
    layouts = analyze_document_layout_pymupdf4llm(pdf)
    
    total_fns = sum(len(layout.get("footnotes", [])) for layout in layouts)
    assert total_fns == 0, f"Expected 0 footnotes on Chatonnet, got {total_fns}"
```

**Step 4: Run all tests**

```bash
pytest tests/test_pageindex_integration.py -v
```

Expected: All PASS

**Step 5: Commit**

```bash
git add tests/test_pageindex_integration.py
git commit -m "test: add integration tests for pymupdf4llm GNN layout classification"
```

---

## Task 7: Update docstring and module header

**Files:**
- Modify: `citeindex/ingestion/pipelines/digital_pdf.py` (module docstring)
- Modify: `citeindex/ingestion/pipelines/layout.py` (module docstring)

**Step 1: Update digital_pdf.py module docstring**

```python
"""Digital PDF ingestion pipeline (v0.4).

Uses PyMuPDF4LLM (GNN layout classification) as the primary tool —
falls back to raw PyMuPDF + heuristic layout if pymupdf4llm is not installed:
  1. PyMuPDF4LLM  — GNN-classified text blocks (footnote, header, footer, etc.)
  2. PageIndex    — LLM-driven section tree building (optional)
  3. Citation     — GROBID (if available) or LLM on raw text
  4. Document     — page-paragraph structure augmented with PageIndex headings
  5. Merkle tree  — deterministic hash chain
"""
```

**Step 2: Update layout.py module docstring**

```python
"""Layout analysis for PDF pages: column detection, footnote isolation,
page number extraction from headers/footers, reading order.

Two implementations:
  - analyze_document_layout_pymupdf4llm(): Uses PyMuPDF4LLM's GNN to classify
    blocks into DocLayNet labels (footnote, page-header, page-footer, etc.).
    Preferred when pymupdf4llm is installed.
  - analyze_document_layout(): Uses heuristic position/font-size analysis.
    Fallback when pymupdf4llm is not available.
"""
```

**Step 3: Commit**

```bash
git add citeindex/ingestion/pipelines/digital_pdf.py citeindex/ingestion/pipelines/layout.py
git commit -m "docs: update module docstrings for pymupdf4llm integration"
```