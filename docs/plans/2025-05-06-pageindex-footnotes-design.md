# Design: Add Footnotes to PageIndex Tree JSON

**Date:** 2025-05-06
**Status:** Approved (Approach A)

## Problem

PageIndex (vendored from VectifyAI/PageIndex) builds a hierarchical section tree from PDFs but:
1. **No footnote awareness** — Footnote text is mixed into page body text with no extraction
2. **No footnote field** in the tree JSON — The LocatorNode schema has `text_blocks`, `figures`, `tables` but no `footnotes`

CiteIndex already detects footnotes via `layout.py` (position + font-size heuristics) and attaches them to `document_structure.pages[].footnotes`. But this data never reaches the PageIndex tree.

## Chosen Approach: Post-hoc Attachment (Approach A)

After PageIndex builds the tree, attach layout-detected footnotes to each LocatorNode by matching page ranges. This is:

- **Non-invasive** — no changes to vendored PageIndex code
- **Uses existing detection** — `layout.py` already works
- **Consistent schema** — same `footnotes` shape already used in document_structure pages
- **No header/footer changes** — `pdf_text_cleanup.py` already runs inside `get_page_tokens()`

### Data Flow

```
digital_pdf.run()
  ├─ layout.analyze_document_layout(pdf_path)  →  page_layouts (footnotes per page)
  ├─ pageindex_tree.run_pageindex_tree(pdf_path) → pi_result (tree)
  └─ pageindex_to_citeindex_tree(pi_result, ..., page_layouts=page_layouts)
         ↓
      LocatorNode adds: "footnotes": [{"footnote_id", "text", "marker"}]
```

## Changes Required

### 1. `pageindex_tree.py` — `_make_locator()`

Add a `footnotes` field to the LocatorNode dict:
- Accept `page_layouts` parameter propagated through the call chain
- For each LocatorNode, find footnotes from `page_layouts` whose page falls within the node's page range
- Include `footnote_id`, `text`, and `marker` fields

### 2. `pageindex_tree.py` — `pageindex_to_citeindex_tree()`

Add optional `page_layouts` parameter:
- Signatures: `pageindex_to_citeindex_tree(pi_result, doc_id, csl_data, page_number_map, merkle_root=None, page_layouts=None)`
- Propagate `page_layouts` through `_convert_sections() → _convert_subsections() → _convert_to_locators() → _make_locator()`

### 3. `digital_pdf.py` — Pass `page_layouts`

In the `run()` function, ensure layout analysis runs before PageIndex tree conversion, and pass the result:
- `page_layouts` is only available when `cfg.use_layout_analysis` is True
- Pass `page_layouts` (or `None`) to `pageindex_to_citeindex_tree()`

### 4. LocatorNode Schema Addition

New `footnotes` field on the LocatorNode:

```json
{
  "node_id": "doc:locator:0001",
  "locator_type": "page_range",
  "page_number": 5,
  "page_label": "5-7",
  "footnotes": [
    {
      "footnote_id": "p5_fn1",
      "text": "See Smith 2020, p.142.",
      "marker": "1"
    }
  ],
  "text_blocks": [],
  "figures": [],
  "tables": [],
  "paragraph_number": null,
  "paragraph_id": "doc:0001",
  "text": null,
  "start_time": null,
  "end_time": null,
  "speaker": null,
  "transcript_text": null,
  "children": []
}
```

### 5. `scanned_common.py` / Other Pipelines

For scanned PDFs, MinerU already produces footnotes from discarded blocks. The same `page_layouts` → `pageindex_to_citeindex_tree()` pattern applies. Ensure `scanned_common.py` passes available footnote data through.

## What We're NOT Changing

- **No changes to `pdf_text_cleanup.py`** — existing header/footer stripping stays as-is
- **No changes to vendored PageIndex code** — `page_index.py`, `page_index_md.py`, `utils.py` untouched
- **No LLM-based footnote detection** — purely position/font heuristics from `layout.py`

## Edge Cases

- **No layout analysis available** (e.g., `--no-layout` flag): `page_layouts=None` → `footnotes=[]` on all LocatorNodes
- **PageIndex returns no structure**: No tree to attach footnotes to — graceful fallback
- **Page range spans multiple pages**: Collect all footnotes from all pages in the range
- **Scanned PDFs**: MinerU footnote data flows through `page_layouts` the same way

## Testing

- Unit test: `_make_locator()` with mock `page_layouts` includes footnotes
- Integration test: `pageindex_to_citeindex_tree()` end-to-end produces `footnotes` on LocatorNodes
- Existing tests: `test_pageindex_integration.py` passes with new optional parameter