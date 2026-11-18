# HTML / URL metadata benchmark

Live benchmark for the deterministic part of `citeindex/ingestion/pipelines/url_article.py`:

```
fetch (_fetch_article) -> _extract_html_head_metadata
```

No Zotero translation server, no DSPy/LLM call, no PageIndex. The score therefore
reflects what a bare CiteIndex install extracts from `<head>` metadata alone
(Highwire Press / Google Scholar, Dublin Core, OpenGraph, Prism, Schema.org JSON-LD).

## Run

```bash
uv run python benchmarks/html/run.py
uv run python benchmarks/html/run.py --json benchmarks/html/results/$(date +%F).json
```

The manifest (`manifest.jsonl`) has one JSON object per line:

```json
{"kind": "scholarly", "url": "https://…", "title_contains": "…",
 "doi": "10.…" | null, "container": "…" | null,
 "min_authors": 5, "date": true}
```

* `doi: null` / `container: null` means "not expected", not "must be absent".
* `min_authors: 0` skips author scoring for that item.
* A title matches when `title_contains` appears in the extracted title
  (HTML tags stripped, whitespace collapsed, case-insensitive).

Bot interstitials ("Just a moment…", "Checking your browser", "Client Challenge")
and HTTP failures are reported separately and excluded from extraction accuracy;
they are not extraction failures. Each URL is tried twice with a short delay, and
requests are spaced to reduce rate limiting.

## Baseline — 2026-11-17

20 URLs (12 scholarly + 8 reference/blog/news), 17 extractable, 2 bot interstitials,
1 HTTP 403 (`journals.aps.org`):

| Metric (extractable URLs) | Result |
| --- | --- |
| Title | 17/17 (100%) |
| Scholarly title | 9/9 (100%) |
| Scholarly DOI | 8/8 (100%) |
| Scholarly authors | 9/9 (100%) |
| Scholarly date | 9/9 (100%) |
| Scholarly container-title | 6/6 (100%) |

Raw run: [`results/2026-11-17.json`](results/2026-11-17.json).

### Findings from this run

* Wikipedia/Wikidata JSON-LD sets `headline` to a short description; the parser now
  prefers schema.org `name`, so page titles are no longer replaced by descriptions.
* Deterministic meta-tag evidence now also anchors `volume`, `issue`, `ISSN`, and
  `ISBN`, so these fields no longer fall back to `"unverified"` when the source
  carries `citation_volume` / `citation_issue` / etc.
* `journals.aps.org` (403), PubMed Central (captcha), and IOPscience (challenge)
  intermittently block non-browser clients; reruns succeed without code changes.
  Re-check those rows before treating a run as a regression.
