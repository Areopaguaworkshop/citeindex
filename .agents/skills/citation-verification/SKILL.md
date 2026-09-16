---
name: citation-verification
description: Verify CiteIndex output against its original source, with quotation-backed CSL corrections
---

# CiteIndex citation verification

Use this after `citeindex` finishes, or when reviewing an existing CiteIndex corpus directory.

1. Locate the original input, `csl.json`, `ingestion_output.json`, and library Markdown. For URL ingestion, use the saved HTML snapshot when available.
2. Check the title page, imprint/copyright page, first article page, DOI, and explicit `Cite this` guidance before trusting extracted metadata.
3. Compare the ingested source's own CSL metadata, including author/editor/translator, title, edition, issued, publisher/place, container, identifiers, type, volume/issue/page. Works cited inside it are out of scope.
4. For every correction, emit the old and new values, exact source quotation, locator, and confidence. Apply no change without source evidence.
5. Mark conflicts or absent evidence as `needs-review`.

## Source precedence

1. Publisher citation guidance or the work's title/imprint page
2. DOI registration data printed in the source
3. Article first page / journal masthead
4. GROBID, PDF embedded metadata, OCR, and model output

Return a compact report with `verified`, `corrected`, and `needs-review` fields. Never claim a citation is accurate without checking the source.

For corrections, also write a machine-readable proposal before changing data:

```json
{"source_sha256":"<SHA256 of original file or saved HTML>","proposals":[{"status":"proposed","field":"title","old_value":"Draft","proposed_value":"Title","quote":"Title","locator":{"block_id":"p1_b1","physical_page_index":0,"printed_page_label":"1","char_start":0,"char_end":5},"reason":"Title on this edition's title page"}]}
```

Apply proposals only by re-running CiteIndex with
`--repair-proposal proposal.json`; this verifies the source digest and quote,
current value, and exact locator, then regenerates CSL, hashes, folder, JSON,
and Markdown artifacts. Copy the actual locator from source-block evidence;
the example coordinates are not defaults. Re-ingestion must reproduce the old
value or the proposal is rejected. This creates a new finalized output; it
does not delete or rewrite older corpus directories. Report proposals separately
from applied repairs, and retain unresolved attribution as needs-review.
