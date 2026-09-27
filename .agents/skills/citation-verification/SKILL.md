---
name: citation-verification
description: Verify CiteIndex output against its original source, with quotation-backed CSL corrections
---

# CiteIndex citation verification

Use this after `citeindex` finishes, or when reviewing an existing CiteIndex corpus directory.
This is an explicitly invoked harness audit. A raw CiteIndex CLI process never
starts this skill or a verifier/enricher agent by itself.

1. Locate the original input, `csl.json`, `ingestion_output.json`, and library Markdown. For URL ingestion, use the saved HTML snapshot when available.
2. Check the title page, imprint/copyright page, first article page, DOI, and explicit `Cite this` guidance before trusting extracted metadata.
3. Compare the ingested source's own CSL metadata, including author/editor/translator, title, edition, issued, publisher/place, container, identifiers, type, volume/issue/page. Works cited inside it are out of scope.
4. For every correction, emit the old and new values, exact source quotation, locator, and confidence. Apply no change without source evidence.
5. Mark conflicts or absent evidence as `needs-review`.

Keep this review source-only. Read `online_enrichment.json` when present to
distinguish `registry-sourced` fills and `registry-corrected` values from printed
source support. Registry agreement, successful ingestion, or a provider citation
cannot make an unprinted field source-verified. Report source accuracy and registry
completion separately. If T1/T2 superseded a source-supported or repaired value,
retain its original quote and both ordered audit events; describe the source repair
as superseded rather than lost or still final.

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

## Constrained registry enrichment

When registry completion or correction is requested, use CiteIndex's registry
stage through `citeindex original.pdf --online-enrich`, or pass a registry proposal
through `--enrich-proposal registry-proposal.json`. The original PDF is required;
v1 does not replay a corpus folder. Use the same extraction/verification settings
and any source repair proposal used to generate the candidate. Never edit persisted
CSL or write model-authored CSL values. Never fetch a proposal URL in a shell or
browser: the CLI resolves DOI/ISBN using its fixed provider endpoints, fetches fresh
records, validates host/edition identity, and regenerates all dependent artifacts.

For a registry proposal, use this schema (placeholder values are not evidence):

```json
{"schema_version":"1.0","source_sha256":"<original PDF SHA-256>","base_candidate_digest":"<candidate_input_digest>","proposals":[{"kind":"registry_fill","provider":"crossref","identifier":"10.1000/host","field":"publisher","old_value":null,"proposed_value":"Example Press","evidence_reference":{"provider_record_id":"10.1000/host","request_url":"https://api.crossref.org/works/10.1000%2Fhost","response_digest":"<received-byte SHA-256>"}}]}
```

Allowed providers are `crossref`, `datacite`, and `openlibrary`. The identifier is
a DOI or ISBN string. Use `registry_correction` for an existing value and copy its
exact old value. Copy proposed values and evidence references from actual registry
evidence; a reference is an audit trail, not authority to fetch an arbitrary URL.
Source quotations belong in source repair proposals, not registry proposals.

Generate against the post-source-repair, pre-enrichment candidate: take
`candidate_input_snapshot` and `candidate_input_digest` from the enrichment report,
not the persisted final CSL or its ID. Prefer an initial `--no-online-enrich` run
for proposal preparation. If both proposals are used, run
`citeindex original.pdf --repair-proposal source-proposal.json --enrich-proposal registry-proposal.json`.
The CLI applies source repair first, validates the registry proposal as one unit,
then discovers further only for remaining missing fields when enrichment is enabled.
`--no-online-enrich` disables automatic discovery, not an explicit registry proposal.
`--offline-verification` rejects explicit registry proposals before network/persistence.

Accepted T1 DOI and T2 edition-validated ISBN changes automatically fill and correct
eligible fields, including source-supported and newly source-repaired values; no
per-record confirmation is needed. Preserve old/new values and registry evidence.
T4 AI-located identities are opt-in and fill-only; T3 identifier-free writes and T5
are deferred. Failed/stale/tampered proposals remain unapplied and must be reported
as failures; regenerate a stale proposal instead of weakening digest/old-value checks.
Keep older corpus versions. Default enrichment remains disabled during development
until independent held-out release gates pass; this is not a per-record approval gate.
