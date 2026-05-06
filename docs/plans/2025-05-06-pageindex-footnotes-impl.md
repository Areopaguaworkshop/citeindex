# PageIndex Tree Footnotes — Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add layout-detected footnotes to PageIndex tree LocatorNodes by post-hoc attachment from existing `layout.py` detection.

**Architecture:** After PageIndex builds its tree, `pageindex_to_citeindex_tree()` will accept an optional `page_layouts` parameter containing per-page layout data (with footnotes). Each LocatorNode finds footnotes whose page falls within its page range and includes them in a new `footnotes` field.

**Tech Stack:** Python, no new dependencies. Changes in `pageindex_tree.py`, `digital_pdf.py`, `scanned_common.py`, and tests.

---

### Task 1: Add `_collect_footnotes_for_range()` helper to `pageindex_tree.py`

**Files:**
- Modify: `citeindex/ingestion/pipelines/pageindex_tree.py` (add after `_page_range_str()` at ~L203)

**Step 1: Write the failing test**

In `tests/test_pageindex_integration.py`, add:

```python
def test_collect_footnotes_for_range():
    from citeindex.ingestion.pipelines.pageindex_tree import _collect_footnotes_for_range

    page_layouts = [
        {"page_number": 1, "footnotes": [{"footnote_id": "p1_fn1", "text": "Note 1.", "marker": "1"}]},
        {"page_number": 2, "footnotes": [{"footnote_id": "p2_fn1", "text": "Note 2.", "marker": "2"}]},
        {"page_number": 3, "footnotes": []},
    ]

    # Range spanning pages 1-2
    result = _collect_footnotes_for_range(1, 2, page_layouts)
    assert len(result) == 2
    assert result[0]["footnote_id"] == "p1_fn1"
    assert result[1]["footnote_id"] == "p2_fn1"

    # Range on a single page with no footnotes
    result = _collect_footnotes_for_range(3, 3, page_layouts)
    assert result == []

    # None range (no page bounds)
    result = _collect_footnotes_for_range(None, None, page_layouts)
    assert result == []

    # None page_layouts
    result = _collect_footnotes_for_range(1, 2, None)
    assert result == []
```

**Step 2: Run test to verify it fails**

Run: `cd /home/ajiap/project/citeindex && python -m pytest tests/test_pageindex_integration.py::test_collect_footnotes_for_range -v`
Expected: FAIL with `ImportError: cannot import name '_collect_footnotes_for_range'`

**Step 3: Write minimal implementation**

In `citeindex/ingestion/pipelines/pageindex_tree.py`, add after `_page_range_str()` (~L203):

```python
def _collect_footnotes_for_range(
    start_page: Optional[int],
    end_page: Optional[int],
    page_layouts: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    """Collect footnotes from page_layouts that fall within a page range.

    Parameters
    ----------
    start_page, end_page : int or None
        1-based page numbers (inclusive). If None, returns empty list.
    page_layouts : list of dict or None
        Per-page layout dicts with ``footnotes`` key. If None, returns empty list.

    Returns
    -------
    list of dict
        Flattened footnote dicts with ``footnote_id``, ``text``, and ``marker``.
    """
    if not page_layouts or start_page is None or end_page is None:
        return []

    footnotes: List[Dict[str, Any]] = []
    for layout in page_layouts:
        page_num = layout.get("page_number")
        if not isinstance(page_num, int):
            continue
        if start_page <= page_num <= end_page:
            for fn in layout.get("footnotes", []):
                entry: Dict[str, Any] = {
                    "footnote_id": fn.get("footnote_id", ""),
                    "text": fn.get("text", ""),
                }
                if fn.get("marker"):
                    entry["marker"] = fn["marker"]
                footnotes.append(entry)
    return footnotes
```

**Step 4: Run test to verify it passes**

Run: `cd /home/ajiap/project/citeindex && python -m pytest tests/test_pageindex_integration.py::test_collect_footnotes_for_range -v`
Expected: PASS

**Step 5: Commit**

```bash
git add citeindex/ingestion/pipelines/pageindex_tree.py tests/test_pageindex_integration.py
git commit -m "feat(pageindex-tree): add _collect_footnotes_for_range helper"
```

---

### Task 2: Add `page_layouts` parameter to the converter chain in `pageindex_tree.py`

**Files:**
- Modify: `citeindex/ingestion/pipelines/pageindex_tree.py`

**Step 1: Write the failing test**

In `tests/test_pageindex_integration.py`, add:

```python
def test_pageindex_to_citeindex_tree_with_footnotes():
    from citeindex.ingestion.pipelines.pageindex_tree import pageindex_to_citeindex_tree

    pi_result = {
        "structure": [
            {
                "title": "Chapter 1",
                "node_id": "0001",
                "start_index": 1,
                "end_index": 3,
                "summary": "A chapter",
                "nodes": [
                    {
                        "title": "Section 1.1",
                        "node_id": "0002",
                        "start_index": 1,
                        "end_index": 2,
                        "summary": "A section",
                        "nodes": [],
                    }
                ],
            }
        ],
    }
    csl_data = {"id": "doc1", "type": "book", "title": "Test Doc"}
    page_number_map = {0: 1, 1: 2, 2: 3}
    page_layouts = [
        {"page_number": 1, "footnotes": [{"footnote_id": "p1_fn1", "text": "See Smith.", "marker": "1"}]},
        {"page_number": 2, "footnotes": [{"footnote_id": "p2_fn1", "text": "Also Jones.", "marker": "2"}]},
        {"page_number": 3, "footnotes": []},
    ]

    tree = pageindex_to_citeindex_tree(
        pi_result=pi_result,
        doc_id="doc1",
        csl_data=csl_data,
        page_number_map=page_number_map,
        page_layouts=page_layouts,
    )

    # Should have footnotes on LocatorNodes within page range
    locator = tree["level_1"][0]["children"][0]["children"][0]
    assert "footnotes" in locator
    assert len(locator["footnotes"]) == 2
    assert locator["footnotes"][0]["footnote_id"] == "p1_fn1"
    assert locator["footnotes"][1]["footnote_id"] == "p2_fn1"
```

**Step 2: Run test to verify it fails**

Run: `cd /home/ajiap/project/citeindex && python -m pytest tests/test_pageindex_integration.py::test_pageindex_to_citeindex_tree_with_footnotes -v`
Expected: FAIL — either `TypeError: unexpected keyword argument 'page_layouts'` or footnotes missing

**Step 3: Implement the changes in `pageindex_tree.py`**

Change 1: Update `pageindex_to_citeindex_tree()` signature (L134):

```python
def pageindex_to_citeindex_tree(
    pi_result: Dict[str, Any],
    doc_id: str,
    csl_data: Dict[str, Any],
    page_number_map: Dict[int, int],
    merkle_root: Optional[str] = None,
    page_layouts: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
```

Change 2: Propagate `page_layouts` to `_convert_sections()` at L163:

```python
    level_1 = _convert_sections(structure, doc_id, page_number_map, page_layouts)
```

Change 3: Update `_convert_sections()` signature (L205) and pass `page_layouts`:

```python
def _convert_sections(
    nodes: List[Dict[str, Any]],
    doc_id: str,
    page_number_map: Dict[int, int],
    page_layouts: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    # ... existing body ...
    # Pass page_layouts to children:
            "children": _convert_subsections(
                node.get("nodes", []), doc_id, pi_id, page_number_map, page_layouts
            ),
```

Change 4: Update `_convert_subsections()` signature (L232) and pass `page_layouts`:

```python
def _convert_subsections(
    nodes: List[Dict[str, Any]],
    doc_id: str,
    parent_id: str,
    page_number_map: Dict[int, int],
    page_layouts: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    # ... existing body ...
    # Pass page_layouts to children:
                "children": _convert_to_locators(
                    children_nodes, doc_id, pi_id, page_number_map, page_layouts
                ),
    # ... and in leaf branch:
                "children": [
                    _make_locator(node, doc_id, pi_id, page_number_map, page_layouts)
                ],
```

Change 5: Update `_convert_to_locators()` signature (L286) and pass `page_layouts`:

```python
def _convert_to_locators(
    nodes: List[Dict[str, Any]],
    doc_id: str,
    parent_id: str,
    page_number_map: Dict[int, int],
    page_layouts: Optional[List[Dict[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    # ...
        locators.append(_make_locator(node, doc_id, parent_id, page_number_map, page_layouts))
```

Change 6: Update `_make_locator()` signature (L299) and add footnotes:

```python
def _make_locator(
    node: Dict[str, Any],
    doc_id: str,
    parent_id: str,
    page_number_map: Dict[int, int],
    page_layouts: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Build a single CiteIndex LocatorNode from a PageIndex leaf node."""
    pi_id = node.get("node_id", "")
    start_page = _map_page(node.get("start_index"), page_number_map)
    end_page = _map_page(node.get("end_index"), page_number_map)

    # Collect footnotes that fall within this locator's page range
    footnotes = _collect_footnotes_for_range(start_page, end_page, page_layouts)

    return {
        "node_id": f"{doc_id}:locator:{pi_id}",
        "locator_type": "page_range",
        "page_number": start_page,
        "page_label": _page_range_str(
            node.get("start_index"), node.get("end_index"), page_number_map
        ),
        "footnotes": footnotes,
        "text_blocks": [],
        "figures": [],
        "tables": [],
        "paragraph_number": None,
        "paragraph_id": f"{doc_id}:{pi_id}",
        "text": node.get("text"),
        "start_time": None,
        "end_time": None,
        "speaker": None,
        "transcript_text": None,
        "children": [],
    }
```

**Step 4: Run test to verify it passes**

Run: `cd /home/ajiap/project/citeindex && python -m pytest tests/test_pageindex_integration.py::test_pageindex_to_citeindex_tree_with_footnotes -v`
Expected: PASS

**Step 5: Verify existing tests still pass**

Run: `cd /home/ajiap/project/citeindex && python -m pytest tests/test_pageindex_integration.py -v`
Expected: ALL PASS

**Step 6: Commit**

```bash
git add citeindex/ingestion/pipelines/pageindex_tree.py tests/test_pageindex_integration.py
git commit -m "feat(pageindex-tree): propagate page_layouts through converter chain, add footnotes to LocatorNodes"
```

---

### Task 3: Wire `page_layouts` into `digital_pdf.py`

**Files:**
- Modify: `citeindex/ingestion/pipelines/digital_pdf.py`

**Step 1: Write the failing test**

In `tests/test_pageindex_integration.py`, add an integration-style test:

```python
def test_pageindex_to_citeindex_tree_without_layout_returns_empty_footnotes():
    from citeindex.ingestion.pipelines.pageindex_tree import pageindex_to_citeindex_tree

    pi_result = {
        "structure": [
            {
                "title": "Chapter 1",
                "node_id": "0001",
                "start_index": 1,
                "end_index": 1,
                "summary": "Summary",
                "nodes": [],
            }
        ],
    }
    csl_data = {"id": "doc1", "type": "book", "title": "Test Doc"}
    page_number_map = {0: 1}

    # No page_layouts → footnotes should be empty list
    tree = pageindex_to_citeindex_tree(
        pi_result=pi_result,
        doc_id="doc1",
        csl_data=csl_data,
        page_number_map=page_number_map,
    )

    locator = tree["level_1"][0]["children"][0]["children"][0]
    assert locator["footnotes"] == []
```

**Step 2: Run test (should pass immediately since `page_layouts` defaults to `None`)**

Run: `cd /home/ajiap/project/citeindex && python -m pytest tests/test_pageindex_integration.py::test_pageindex_to_citeindex_tree_without_layout_returns_empty_footnotes -v`
Expected: PASS

**Step 3: Wire `page_layouts` in `digital_pdf.py`**

In `digital_pdf.py`, modify the `run()` function. The layout analysis already runs at L392-398. We need to make `page_layouts` available at L460-473.

Change: Add a `page_layouts` variable early, populate it during layout analysis, and pass it to `pageindex_to_citeindex_tree()`.

Around L357 (before Step 1), add:
```python
    page_layouts: Optional[List[Dict[str, Any]]] = None
```

Around L392-398, modify to capture `page_layouts`:
```python
    # Attach layout-detected footnotes when layout analysis is enabled.
    if cfg.use_layout_analysis:
        try:
            page_layouts = analyze_document_layout(pdf_path)
            _attach_layout_footnotes(document_structure, page_layouts)
        except Exception:
            logger.warning("Layout footnote extraction failed", exc_info=True)
```

Around L464-470, pass `page_layouts`:
```python
        ci_tree = pageindex_to_citeindex_tree(
            pi_result=pageindex_tree_json,
            doc_id=source_id,
            csl_data=csl,
            page_number_map=page_number_map,
            merkle_root=merkle_tree.get("root"),
            page_layouts=page_layouts,
        )
```

**Step 4: Run all tests**

Run: `cd /home/ajiap/project/citeindex && python -m pytest tests/test_pageindex_integration.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add citeindex/ingestion/pipelines/digital_pdf.py tests/test_pageindex_integration.py
git commit -m "feat(digital-pdf): wire page_layouts into PageIndex tree conversion"
```

---

### Task 4: Update `scanned_common.py` to support `page_layouts` (no footnotes)

**Files:**
- Modify: `citeindex/ingestion/pipelines/scanned_common.py`

**Step 1: Check scanned pipeline for footnote data availability**

The scanned pipeline uses MinerU or GLM-OCR. MinerU already produces footnotes in its structure (`mineru.py:399-403`). However, scanned pipelines don't use `layout.py` directly — they produce footnote data differently.

For now, we'll pass `page_layouts=None` to maintain the signature contract. Later, when MinerU/GLM footnote data is adapted to the same layout format, it can be wired in.

**Step 2: Update `scanned_common.py` call site**

At `scanned_common.py:198-204`, the call to `pageindex_to_citeindex_tree()` currently doesn't pass `page_layouts`. Since `page_layouts` defaults to `None`, this call site is **already compatible** — no change needed for functionality. But to make the intent explicit:

```python
                ci_tree = pageindex_to_citeindex_tree(
                    pi_result=pi_result,
                    doc_id=source_id,
                    csl_data=csl,
                    page_number_map=page_number_map,
                    merkle_root=merkle_tree.get("root"),
                    page_layouts=None,  # scanned PDFs don't have layout analysis
                )
```

**Step 3: Update `url_article.py` call site similarly**

At `url_article.py:606-612`:

```python
            ci_tree = pageindex_to_citeindex_tree(
                pi_result=pi_result,
                doc_id=source_id,
                csl_data=csl_json,
                page_number_map={},  # URLs have no physical page numbers
                merkle_root=merkle_tree.get("root"),
                page_layouts=None,  # URL articles have no layout analysis
            )
```

**Step 4: Run all tests**

Run: `cd /home/ajiap/project/citeindex && python -m pytest tests/test_pageindex_integration.py -v`
Expected: ALL PASS

**Step 5: Commit**

```bash
git add citeindex/ingestion/pipelines/scanned_common.py citeindex/ingestion/pipelines/url_article.py
git commit -m "chore: add explicit page_layouts=None to other pipeline call sites"
```

---

### Task 5: Add integration test for full digital PDF footnote flow

**Files:**
- Modify: `tests/test_pageindex_integration.py`

**Step 1: Write a comprehensive integration test**

```python
def test_pageindex_tree_footnotes_match_page_ranges():
    """Integration test: footnotes are attached to the correct LocatorNodes by page range."""
    from citeindex.ingestion.pipelines.pageindex_tree import pageindex_to_citeindex_tree

    pi_result = {
        "structure": [
            {
                "title": "Introduction",
                "node_id": "0001",
                "start_index": 1,
                "end_index": 2,
                "summary": "Intro",
                "nodes": [
                    {
                        "title": "1.1 Background",
                        "node_id": "0002",
                        "start_index": 1,
                        "end_index": 1,
                        "summary": "Background",
                        "nodes": [],
                    },
                    {
                        "title": "1.2 Scope",
                        "node_id": "0003",
                        "start_index": 2,
                        "end_index": 2,
                        "summary": "Scope",
                        "nodes": [],
                    },
                ],
            },
            {
                "title": "Methods",
                "node_id": "0004",
                "start_index": 3,
                "end_index": 5,
                "summary": "Methods chapter",
                "nodes": [],
            },
        ],
    }
    csl_data = {"id": "doc1", "type": "book", "title": "Test Paper"}
    page_number_map = {0: 1, 1: 2, 2: 3, 3: 4, 4: 5}
    page_layouts = [
        {
            "page_number": 1,
            "footnotes": [
                {"footnote_id": "p1_fn1", "text": "Ref 1.", "marker": "1"},
                {"footnote_id": "p1_fn2", "text": "Ref 2.", "marker": "2"},
            ],
        },
        {
            "page_number": 2,
            "footnotes": [
                {"footnote_id": "p2_fn1", "text": "Ref 3.", "marker": "3"},
            ],
        },
        {"page_number": 3, "footnotes": []},
        {
            "page_number": 4,
            "footnotes": [
                {"footnote_id": "p4_fn1", "text": "Ref 4.", "marker": "4"},
            ],
        },
        {"page_number": 5, "footnotes": []},
    ]

    tree = pageindex_to_citeindex_tree(
        pi_result=pi_result,
        doc_id="doc1",
        csl_data=csl_data,
        page_number_map=page_number_map,
        page_layouts=page_layouts,
    )

    # Locator for "1.1 Background" (page 1) should have 2 footnotes
    bg_locator = tree["level_1"][0]["children"][0]["children"][0]
    assert len(bg_locator["footnotes"]) == 2
    assert bg_locator["footnotes"][0]["footnote_id"] == "p1_fn1"

    # Locator for "1.2 Scope" (page 2) should have 1 footnote
    scope_locator = tree["level_1"][0]["children"][1]["children"][0]
    assert len(scope_locator["footnotes"]) == 1
    assert scope_locator["footnotes"][0]["footnote_id"] == "p2_fn1"

    # Locator for "Methods" (pages 3-5) should have 1 footnote (p4_fn1)
    methods_locator = tree["level_1"][1]["children"][0]["children"][0]
    assert len(methods_locator["footnotes"]) == 1
    assert methods_locator["footnotes"][0]["footnote_id"] == "p4_fn1"
```

**Step 2: Run test**

Run: `cd /home/ajiap/project/citeindex && python -m pytest tests/test_pageindex_integration.py::test_pageindex_tree_footnotes_match_page_ranges -v`
Expected: PASS

**Step 3: Commit**

```bash
git add tests/test_pageindex_integration.py
git commit -m "test(pageindex-tree): add integration test for footnote page-range matching"
```

---

### Task 6: Update markdown export to render footnotes from LocatorNodes (optional future step)

This task is **deferred** — the current `markdown_export.py` already collects footnotes from `document_structure.pages[].footnotes`. Once LocatorNodes have footnotes, we could optionally add footnote rendering from the tree, but this isn't needed for the initial implementation because `document_structure` already carries footnotes through layout analysis.

**No code changes in this task.** Mark as a future enhancement.