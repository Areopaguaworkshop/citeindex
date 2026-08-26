---
name: citation-verification
description: Verify CiteIndex output against its original source, with quotation-backed CSL corrections
---

# CiteIndex citation verification

Use this after `citeindex` finishes, or when reviewing an existing CiteIndex corpus directory.

1. Locate the original input, `csl.json`, `ingestion_output.json`, and library Markdown. For URL ingestion, use the saved HTML snapshot when available.
2. Check the title page, imprint/copyright page, first article page, DOI, and explicit `Cite this` guidance before trusting extracted metadata.
3. Compare only `author`, `title`, `issued`, `publisher`, `publisher-place`, `container-title`, `DOI`, `URL`, and `page`.
4. For every correction, emit the old and new values, exact source quotation, locator, and confidence. Apply no change without source evidence.
5. Mark conflicts or absent evidence as `needs-review`.

## Source precedence

1. Publisher citation guidance or the work's title/imprint page
2. DOI registration data printed in the source
3. Article first page / journal masthead
4. GROBID, PDF embedded metadata, OCR, and model output

Return a compact report with `verified`, `corrected`, and `needs-review` fields. Never claim a citation is accurate without checking the source.
