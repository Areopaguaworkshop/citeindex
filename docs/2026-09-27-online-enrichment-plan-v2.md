# Post-extraction online enrichment — implementation plan v2

Date: 2026-09-27. Status: proposed implementation plan; no feature code changed.
Revises [the original proposal](2026-09-27-online-enrichment-plan.md).
Baseline: HEAD `030d77c`, **including the current uncommitted working tree**.

## 1. Recommendation and decision boundaries

Keep online enrichment separate from source verification, but share registry clients
and finalization. Implement one small `online_enrichment.py` module, extend the
existing registry module, and use the current ingestion/finalization path for both
automatic enrichment and explicit proposals. No provider plugin framework, new
database, separate persistence pipeline, or new matching dependency is needed initially.

Preserve the recorded requirements: digital and scanned PDFs; source-supported
title required; default-on enrichment at release; offline opt-out; all four catalog
providers; CJK goes through the same available cascade; AI identifier location is
opt-in; no AI-authored CSL or model formatting; preserve original corpus versions.

### Owner-confirmed policy (updated after discussion)

Minimize human involvement. T1/T2 automatically fill and overwrite eligible CSL
fields, including source-supported values and values just applied by source repair.
No per-record confirmation is required. T1 requires DOI round-trip verification;
T2 requires ISBN round-trip plus title/author agreement. Host/edition validation
establishes identity; after acceptance, field disagreement alone does not block an
overwrite. Record every old/new value and its registry evidence.

Source repair runs first, then enrichment remains enabled. Explicit enrichment
proposals resolve their identifiers first; continue catalog discovery when eligible
fields remain missing. Retain the original missing-field trigger for ordinary
automatic discovery; an explicit proposal may request T1/T2 corrections even when
no fields are missing.

T3 identifier-free writes remain gated on corroboration measurement; T4 remains
fill-only; T5 stays deferred. Build with a disabled development default and deliver
the requested default-on release after evaluation. These development/release gates
are not human approval steps for individual ingestions.

## 2. Findings from the current project

Paths and line numbers refer to the reviewed working tree and will move during edits.

| Current implementation | Consequence for this work |
|---|---|
| `master.py:112–155` validates/replaces authors, optionally verifies, applies source repair, then standardizes | Integrate once here; avoid separate implementations in OCR and digital extraction. |
| `digital_pdf.py:626–638` and `scanned_common.py:199–211` build `_field_status` using `evaluation_fields` | States cover an evaluation profile, not every enrichable field. Publisher-place and other eligible fields can lack a state. |
| `master.py:112–140` changes authors after those states were computed | Reconcile states with actual current values before matching; filename/prompt authors must not become source evidence. |
| `citation_verification.py:416–512` already queries Crossref/OpenLibrary and accepts changes only with source evidence | It is a registry-assisted, source-constrained verifier, not a network-free source check. Reuse fetched results where possible; keep its acceptance policy separate. |
| `citation_verification.py:478–489` computes report states, including proposed corrections | Do not blindly copy report states into CSL: unresolved conflicts can prevent the proposed corrections from applying. |
| `metadata_registry.py:198` round-trips the returned DOI; `:271` resolves ISBN | Reuse both clients. OpenLibrary currently injects the requested ISBN during normalization rather than checking returned edition ISBNs. |
| `metadata_registry.py:150–171` omits OpenLibrary authors | T2 author agreement requires bounded author hydration or edition-linked author evidence. Current client cannot satisfy it alone. |
| `master.py:256–316` checks source digest, exact old value, quote, and locator for source repair | Automatic enrichment before repair would invalidate legitimate old-value checks. Define ordering and replay explicitly. |
| `master.py:434–451` hashes the entire candidate CSL plus source Merkle root/type | Keep retrieval times, latency, errors, and report data outside CSL. Otherwise identical bibliographic results gain different IDs/folders. Merkle root comes from source content, not citation formatting. |
| `master.py:160–176` updates embedded citations, document title, and PageIndex level 0 | Preserve this path for all enriched and repaired outputs. |
| `storage.py:109–167` writes exports and explicitly enumerates optional sidecars | Add enrichment sidecar writing **and cleanup**, or a later disabled run may retain obsolete evidence. |
| `markdown_export.py:53` uses hand formatting; `citation_style.py:34` supplies citeproc rendering | Wire local rendering into Markdown after finalization, using `export_csl`, not raw internal CSL. |
| `models.py:26–60`, `cli.py:191–245` expose existing registry/offline controls | Existing provider disables must apply to enrichment too. `openlibrary_enabled` currently has no matching CLI disable flag. |
| `benchmarks/citations/ingest.py:123` launches the CLI without a fixed enrichment setting | Pin extraction-only versus enrichment runs explicitly before changing the default. |

All ingestion paths converge on the orchestrator. Office/DJVU conversions are relabeled
as PDFs in `master.py:61–95`; **v1 should gate on original PDF input as well as the
PDF pipeline type**, so conversion support does not accidentally expand scope.

## 3. Execution order and field contract

```text
PDF extraction → author validation/fallback
  → optional current source-constrained verification
  → explicit source repair (when supplied)
  → refresh field states from actual values and accepted evidence
  → resolve/apply explicit registry proposal first (when supplied)
  → automatic enrichment; discover further only if eligible fields remain missing
  → standardize once → update embedded copies → persist → render library Markdown
```

Source-repair runs keep automatic enrichment enabled unless the user explicitly
selects `--no-online-enrich` or offline mode. Validate repair old values before any
registry mutation. T1/T2 can subsequently overwrite the repaired values; preserve
both events in order and mark the repair as superseded in the final audit view.

Allow both proposal flags together: source repair first, registry proposal second.
The registry proposal's base digest and old values must refer to the post-repair,
pre-enrichment candidate. Then discover further only if fields remain missing.
`--no-online-enrich` disables automatic discovery, not an explicitly requested
`--enrich-proposal`. `--offline-verification` rejects explicit registry application
before network or persistence; automatic enrichment simply returns `skipped`.

Eligibility:

- Original input is a PDF, pipeline is `digital_pdf` or `scanned_pdf`, switch is on,
  offline flag is false, and the current title has validated host-source evidence.
- At least one field in an explicit type-specific enrichment target set is missing.
  Do not use `evaluation_fields` alone as a completeness schema. Start with author,
  issued, publisher/place, container, volume/issue/page, DOI/ISBN/ISSN, edition,
  editor/translator and language where appropriate to the host type. Exclude generated
  IDs, access dates, abstracts, arbitrary notes, and source-derived structure.
- A valid present value with absent/stale status is `unverified`, never “missing.”
  Empty/invalid values are reported and handled explicitly; do not overwrite a
  conflicting value by relabeling it missing.
- Only validated source anchors contribute to host matching. A year or author supplied
  from filename/model fallback is not an independent corroborating anchor.
- If no eligible field is missing, v1 skips. Correction-only scans of complete records
  are available through explicit T1/T2 proposals. Once the automatic stage is
  triggered, T1/T2 may overwrite present eligible fields as well as fill missing ones.

Use `valid_host_value` (`csl.py:26`) for every candidate field. Preserve `_field_evidence`
as source evidence only. New state `registry-sourced` means a registry fill, not source
verification; `registry-corrected` marks an automatic T1/T2 overwrite. If a source
value is superseded, archive its evidence in the audit report rather than attaching
that quotation to the new registry value.
Keep `_citation_status` truthful about source support and expose enrichment status
separately. Do not turn registry completeness into `verified=true`.

## 4. Discovery, identity matching, and acceptance

Separate **discovery origin** (`source_identifier`, `catalog_search`, `ai_search`) from
**verification basis** (`doi_round_trip`, `isbn_edition_round_trip`, `catalog_agreement`).
Do not let an AI-discovered DOI silently acquire overwrite privileges as T1.

1. Resolve existing valid host identifiers first. Try Crossref, then DataCite when
   unresolved; an agency lookup may route an unknown DOI but is not required as an
   extra call before every successful Crossref lookup. Report unsupported agencies.
2. If unresolved, search in deterministic, type-aware order: Crossref/OpenAlex for
   articles; OpenLibrary editions for books; DataCite for theses/reports/datasets.
   Continue through the remaining enabled catalogs when needed. The same provider
   availability applies to CJK. Prefer bounded sequential calls over unconditional
   concurrent fan-out; stop once identity and all available target fills are settled.
3. Normalize search results, group duplicate DOI/edition identities, hydrate plausible
   candidates, and compare distinct works/editions. A duplicate result is not a tie.
4. Use Unicode normalization and whitespace/punctuation normalization for comparison
   only. Preserve output spelling and diacritics. Retain subtitle, numeral, volume,
   edition and language distinctions; avoid blanket removal of leading words.
5. Start with stdlib `difflib.SequenceMatcher(..., autojunk=False)` for Latin title
   similarity. Its score is **not** `token_set_ratio`; treat 0.90 as an initial tuning
   value, not a calibrated confidence. CJK starts with exact normalized title matching.
6. For discovered identities require title agreement plus at least one available,
   source-supported discriminator (author/editor, publication year, publisher/container),
   with no contradiction in other reliable anchors. Short/generic titles require two
   discriminators. This permits missing-author or missing-year recovery without
   treating missing anchors as a match. Insufficient anchors means abstention.
7. Require author role compatibility, appropriate work type, and edition/language
   compatibility. Year ±1 is a candidate-discovery tolerance. After T1/T2 identity
   acceptance, the registry publication year may overwrite the source year under
   the agreed policy. A book ISBN must not convert a chapter into its containing book.
8. Require a unique passing identity; ties and near ties abstain. Start with a 0.05
   top-two score margin on comparable Latin candidates and calibrate on development
   data. Exact CJK matches still need discriminators and uniqueness.
9. Round-trip discovered DOI/ISBN through the authority endpoint. For ISBN, check
   returned edition identifier membership; search-work `first_publish_year`, a work's
   first ISBN, or an arbitrary `edition_key` is not edition evidence. Normalize ISBN-10
   and ISBN-13 equivalence deliberately with checksum tests if extending current
   ISBN-13-only support.
10. Merge only supported fields from the accepted identity/edition; do not combine
    unrelated candidates into a seemingly complete record. T1/T2 field disagreements
    are applied automatically and logged; unresolved identity or same-tier authority
    ambiguity causes abstention, not a request for routine human approval.

| Tier | Fill missing | Overwrite present/source-repaired values | Requirement |
|---|---|---|---|
| T1 | Yes | Yes, automatically | Accepted host DOI and authority round-trip |
| T2 | Yes | Yes, automatically | ISBN edition round-trip and title/author agreement |
| T3 | After measurement gate | No | Accepted identifier-free match, corroborated catalogs |
| T4 | Yes | No | AI-located identifier, registry round-trip and host match |
| T5 | Deferred | No | Outside v1 |

Apply precedence T1 > T2 > T3 > T4. A lower tier cannot undo a higher-tier change;
AI discovery remains T4. Record field decisions even when the source disagrees.
Provider count alone cannot establish independent provenance.

## 5. Registry transport and provenance

Extend `metadata_registry.py`, retaining the existing `{status, candidate, provenance}`
shape and injected `session` testing seam. Add plain functions for search and DataCite/
OpenAlex normalization; use a small dispatch map rather than a class hierarchy.

Initial proposed operational limits, all testable with an injected clock/session:

- Whole automatic stage: 20 seconds; at most 12 HTTP attempts including retries,
  hydration, and agency calls; at most five search results per provider. Stop with
  `budget_exhausted` and keep ingestion usable. Make the stage deadline configurable.
- Each request gets at most five seconds and never more than the remaining budget.
  Check deadline while reading bounded responses; a socket timeout alone is not a
  whole-stage deadline. Bound response size to 2 MiB; oversized responses are errors.
- At most one retry for transient transport errors, 429, or 5xx. Honor `Retry-After`
  (seconds or HTTP date) and provider headers; if the wait exceeds remaining budget,
  skip rather than sleeping through ingestion. Do not retry 400/401/403/404 blindly.
- Per-provider pacing, including hydration calls. Share a request context between
  verification and enrichment so identical successful lookups are reused. Preserve
  verifier behavior and public client signatures where possible.
- Use a versioned JSON cache under the corpus root's hidden cache directory, written
  atomically with existing `write_json`. Key by provider, operation, normalized query/
  identifier, and normalization version. Cache normalized responses plus original
  byte digest; seven-day positive TTL, one-hour not-found TTL; never cache errors.
  Corrupt/unwritable cache falls back to fetching. No raw bodies or credentials stored.
- Per-process pacing is the initial ceiling; if batch work later spans concurrent
  processes, coordinate budgets before advertising aggregate rate-limit guarantees.

Persist `online_enrichment.json` separately. Include schema/policy version, status,
source digest, candidate-input digest, attempted providers, skip reasons, rejected
identities, and per-field decisions: old/new value, discovery origin, verification
basis/tier, provider record ID, sanitized request URL, `response_digest`, retrieval
time, match anchors, score and decision reason. Keep search and hydration provenance
when both contribute; store before/after accepted values, not just a scalar score.

Use `response_digest` consistently instead of introducing a synonymous `raw_sha256`.
It is the SHA-256 of received bytes, not a signature or a reproducible snapshot.
Keep normalized candidate evidence in the report; freshness is established by a new
request during explicit application. Strip contact email, API keys and authorization
from persisted URLs, errors, config snapshots and logs. Build requests from fixed
provider endpoints; never fetch an arbitrary proposal-supplied URL. Validate redirects
or disable them except for explicitly allowed provider paths/hosts.

### Provider documentation checked for this revision

- [OpenAlex authentication](https://help.openalex.org/api/authentication/) currently
  permits basic keyless use and offers a larger budget with a free key. Add optional
  environment-based credentials and quota handling; do not rely on “free, no key”
  as a bulk-ingestion guarantee or assume a Crossref-style mailto pool.
- [Crossref access](https://www.crossref.org/documentation/retrieve-metadata/rest-api/access-and-authentication/)
  distinguishes public/polite pools and exposes rate limits in response headers.
  Use those rather than a permanently assumed rate.
- [OpenLibrary limits](https://openlibrary.org/developers/api) document 1 request/sec
  anonymously and 3/sec for identified requests. Its [search documentation](https://openlibrary.org/dev/docs/api/search)
  distinguishes work results from edition results; hydration must retain that boundary.
- [DataCite limits](https://support.datacite.org/docs/rate-limit) distinguish anonymous,
  identified and authenticated requests (500/1000/3000 per five minutes respectively).
  Treat the original 500 figure as the anonymous tier, not a universal limit.

These are documentation checks, not live endpoint smoke tests. Recheck contracts at
implementation time. Local OpenCLI was unavailable and Jina DNS failed; the checks
used browser search/direct official documentation. Search batch: one query per provider
covering authentication, rate limits, or edition search; no social/AI-site searches.

## 6. Configuration, output, and harness

Add `online_enrich`, provider selection, minimum score, stage timeout, cache TTL,
`online_enrich_ai_fallback`, `online_enrich_ai_model`, and `enrich_proposal` to
`IngestionConfig`; validate ranges and names in CLI and programmatic entry paths.
Use paired `--online-enrich`/`--no-online-enrich`; add `--enrich-proposal` and an
OpenLibrary disable flag. Existing `--no-crossref` and `openlibrary_enabled=False`
must override provider selection. Treat the offline flag as disabling verification/
enrichment networking, **not** as making OCR, extraction models or URL ingestion offline.

Persist the report in the sidecar and returned ingestion output; update optional-sidecar
cleanup. Avoid report timestamps in CSL. After `standardize_csl_json`, call
`format_bibliography([export_csl(standardized)], cfg.citation_style)` for PDF Markdown.
Pass the rendered citation into the existing Markdown builder. Check the renderer's
error-string behavior and add a deterministic fallback with an explicit render warning.
Do not store render errors as citations or put rendered text into metadata identity.

Stage B adds `registry_fill` and `registry_correction` proposals containing schema
version, source SHA-256,
base candidate digest, provider, identifier/edition identity, field, old value, proposed
value and evidence reference. CLI recomputes the source and base digests, validates all
entries, fetches the identifiers afresh, reruns identity matching, and requires each
proposed field/value to be present in that verified candidate before applying anything.
Reject duplicate fields, unknown providers, changed old values, stale source, and
unsupported values. Network failure during explicit application is a clear unapplied
failure; it must not be presented as success. Apply the proposal as one validated unit.
Then resume discovery for remaining missing fields if automatic enrichment is enabled,
reusing resolved identities. A later discovery failure does not undo the validated
proposal. Corrections require T1/T2; T3/T4 cannot overwrite through a proposal.

Generate proposals against the **post-source-repair, pre-enrichment candidate** and record the extraction/
verification settings and any source-repair proposal digest needed to reproduce it. Prefer `--no-online-enrich` when producing
a proposal. A proposal generated from an already enriched output needs the recorded
pre-enrichment snapshot; never compare an old persisted final ID to a fresh extraction
hash as if they were the same stage. Nondeterministic extraction can still make a
proposal stale: reject and regenerate rather than weakening old-value checks.

Reuse normal finalization and retain old corpus folders. No folder-only replay engine
in v1: the original PDF must remain available, and re-ingestion/OCR may be necessary.

Update the shared `.agents/skills/citation-verification/SKILL.md` and its wrappers;
add the new enricher instructions in the actual configured harness surfaces (including
`.opencode/agents/citation-enricher.md`). Keep the verifier source-only. The enricher
should invoke the constrained registry code rather than unrestricted shell/web fetches.
Raw CLI runs never auto-start a harness audit. New registry reporting must not cause
the source verifier to call an unprinted but registry-sourced field source-verified.

### Mandatory online search for AI discovery (owner requirement)

Implement **Claude, Gemini, and OpenAI** as selectable AI discovery providers. AI discovery
remains opt-in, but once invoked its native online search is mandatory. There is no
memory-only completion fallback and no setting that disables search within this stage.
Use one configured provider per invocation; supporting three providers does not mean
calling all three on every record. Keep extraction-model selection independent of discovery-provider
selection, so an existing DeepSeek extraction model can coexist with Claude/Gemini/OpenAI search.

“Claude Code API” is implemented here as the **Anthropic Claude Messages API with its
hosted web-search tool**. A Claude Code CLI/Agent SDK runner is a different integration
and is not needed for this HTTP-based ingestion feature. No local browser, MCP server,
or additional search-service subscription is required for any of the three selected providers.

| Provider | Required native capability | Required response evidence |
|---|---|---|
| Claude | Messages API with supported `web_search` tool version enabled, bounded `max_uses`, concrete `allowed_domains` | Successful server web-search call/results and citations linking the candidate identifier to retrieved sources |
| Gemini | Gemini API with `google_search` grounding enabled on a supported model/endpoint | Actual search execution metadata and source citations/grounding support linking the candidate identifier to retrieved sources |
| OpenAI | Responses API with `web_search`, `external_web_access: true`, required web-search tool choice, and `filters.allowed_domains` | Completed `web_search_call`, returned source URLs, and citation annotations linking the candidate identifier to retrieved sources |

The implementation must pin and test each endpoint/model/tool combination. Gemini
response contracts differ by endpoint: Interactions exposes search call/result steps
and annotations; GenerateContent uses grounding metadata. Do not mix those schemas.
Do not assume Gemini exposes Claude's domain-filter parameters: use native restrictions
where documented; otherwise constrain queries and validate cited source URLs against
the acceptance allowlist. Query instructions alone are not a hard search-domain boundary.

Acceptance is enforced by code, not merely by a prompt:

1. Enable the hosted search tool in every discovery request; require tool invocation
   where the selected API supports it. Inspect actual execution metadata regardless.
2. Accept an identifier candidate only when a successful search and supporting source
   citations are present. A generated URL or “I searched” text is not execution evidence.
3. Resolve source/redirect URLs safely when needed, enforce the accepted-domain list,
   and retain the search queries, source URLs, supporting spans where available, provider,
   model, tool version, timestamp and response digest in the enrichment report.
4. The model returns DOI/ISBN candidates plus search evidence, or null. It does not
   author final CSL fields. CiteIndex independently round-trips the identifier through
   Crossref/DataCite/OpenLibrary, validates host identity, and copies supported registry
   values into missing fields under T4. Each fill links to both discovery evidence and
   registry evidence. T1/T2 automatic overwrite policy remains unchanged.
5. Missing search execution, absent/unsupported citations, off-domain evidence, invalid
   identifiers, or failed registry verification produces an unapplied result with a
   reason. Never retry as plain model completion. Ingestion can continue unchanged.
6. Add fixtures for each provider covering search not invoked, fabricated citation URLs,
   tool errors, malformed output, null, valid search-backed identifier, wrong host,
   timeout and quota failure. Bounded live smoke tests must demonstrate actual search
   execution for all three providers before declaring P7 complete. Include an OpenAI
   cache-only request rejection test and reject incomplete/failed search calls.

Add `online_enrich_ai_provider` (`claude`, `gemini`, or `openai`) alongside the existing opt-in/model
settings; obtain provider credentials from environment and redact them everywhere.
Explicitly enabled but unsupported provider/model/tool combinations fail configuration
validation. Keep the existing whole-stage deadline and request budget: reserve time for
registry validation and skip AI discovery if insufficient budget remains. Start with
one hosted request to the selected provider; its internal searches are separately
bounded where supported and measured in the report. No hidden unbounded retry/fallback.
If measured latency requires a larger deadline, expose/document that configuration
rather than silently exceeding the budget.

Official documentation checked for this update:

- [Claude hosted web search](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool)
  performs searches server-side and supports domain controls and search-count limits.
- [Gemini Google Search grounding](https://ai.google.dev/gemini-api/docs/google-search)
  provides online search and cited results; enabling it does not guarantee every
  response invokes search, hence the execution-evidence gate above.
- [OpenAI web search](https://developers.openai.com/api/docs/guides/tools-web-search)
  also provides native hosted search through Responses `web_search`, with live access
  and tool-choice controls. Implement this provider in P7 alongside Claude and Gemini.
  Require web search through tool choice (with only the search tool exposed), explicitly
  enable live access, and request `web_search_call.action.sources`. Plain Chat Completions
  and cache-only web-search requests do not satisfy this integration contract.
- [DeepSeek API documentation](https://api-docs.deepseek.com/) describes compatible
  chat APIs and agent integrations. No native hosted web-search contract was verified
  in this review; API-format compatibility does not establish hosted-tool support.
  Do not accept plain DeepSeek completions as online discovery. A future integration
  needs a verified hosted search capability or an explicitly integrated search tool.

Online search reduces unsupported generation but does not prove correctness. The
search-evidence requirement plus independent registry verification is the acceptance
rule; never claim that switching on a search tool guarantees zero hallucinations.

## 7. Implementation sequence and acceptance criteria

| Step | Files / deliverable | Exit evidence |
|---|---|---|
| P0: Freeze contracts and replay rules | This plan; target-field table and versioned report/proposal fixtures; encode the confirmed policy in §1 | Every gate, tier, field state, skip reason and proposal ordering has an explicit fixture; T1/T2 overwrite source-supported values without confirmation; T3/T4 cannot. |
| P1: Registry transport and exact identity | `metadata_registry.py`, existing `tests/test_metadata_registry.py` | DOI mismatch and ISBN-edition mismatch rejected; Crossref/DataCite routing covered; offline/provider-disable make zero HTTP calls; deadline, retry, redaction and cache tests pass. |
| P2: Catalog search and normalization | Same module, provider response fixtures under `tests/fixtures/online_enrichment/` | Crossref/OpenAlex/OpenLibrary/DataCite each have positive, no-result and malformed fixtures; OpenLibrary work/edition/author hydration stays within budget; credentials absent or rejected degrade predictably. |
| P3: Pure matching and merge | New `ingestion/online_enrichment.py`, `csl.py` only for a reused helper, `tests/test_online_enrichment.py` | Wrong host, chapter/book, translation, reprint, generic-title, near-tie, DOI-in-bibliography, missing-anchor and CJK cases abstain as specified; T1/T2 fills and overwrites follow precedence, T4 cannot overwrite, and unrelated fields/input objects are preserved. |
| P4: Pipeline and artifacts | `models.py`, `cli.py`, `master.py`, `storage.py`, `markdown_export.py`, `citation_style.py`; focused integration tests | Both PDF pipelines enrich before hashing; URL/media/converted inputs skip; states match actual values; all citation copies agree; sidecar cleanup works; repeated identical results have equal ID/hash despite different retrieval times; Chicago output uses final values. |
| P5: Registry proposals and harness | Shared skill/wrappers, enricher instructions, CLI and shared apply function | One digital and one scanned PDF complete proposal → fresh validation → new finalized output; stale/tampered proposals write nothing; repair → T1/T2 overwrite and combined proposals pass with both audit events retained; discovery continues only for remaining missing fields; older outputs remain intact. |
| P6: Evaluation and default-on release | `benchmarks/citations/ingest.py`, new small `benchmarks/citations/enrichment.py`, benchmark README, CLI docs | Frozen fixture evaluation and held-out gates below pass; baseline runner explicitly disables enrichment; default-on and opt-out behavior are tested. |
| P7: Search-required AI identifier fallback | Enrichment module plus Claude, Gemini, and OpenAI native-search integrations and fixtures | All three providers demonstrate actual search execution; no-search/uncited results write nothing; valid candidates pass registry verification; timeout, quota, wrong-work and off-domain tests pass; incremental correct recovery measured per provider. Off unless configured, search mandatory when invoked. |

Dependencies: P0 → P1 → P2 → P3 → P4 → P5 → P6; P7 uses P3/P4 and gets a
separate evaluation before release. Seed matching fixtures and benchmark cases in P0,
not only after coding. P6 releases the deterministic slice; P7 completes the optional
AI scope. T3 write enablement and T5 remain separate decisions, not incidental P7 work.

## 8. Measurement and release gates

Use existing human-reviewed, source-checksummed, work-family splits
(`benchmarks/citations/README.md`, `split.py`, `run.py`). The current development pilot
is insufficient for a production safety claim; preserve it as development data.

Evaluate four complementary tracks:

1. Masked-field recovery on reviewed records. Remove the selected value **and its
   evidence/status/cached candidate leakage**. Include a no-identifier track by masking
   DOI/ISBN as well; do not accidentally measure only exact-ID lookup.
2. Real extraction outputs, including wrong/unverified titles and authors, so the
   title gate and conservative abstention cost are visible.
3. Adversarial near-matches: editions, translated titles, chapter versus collection,
   namesakes, online/print dates, cited-work identifiers, and mirrored catalog records.

4. Automatic correction on independently reviewed conflicts, including valid source
   repairs subsequently superseded by T1/T2. Report correct corrections and harmful
   overwrites separately from missing-field recovery; no per-record approval step.

Keep source-only gold separate from independently adjudicated registry-completion gold.
A field absent from the PDF may be a legitimate catalog fill; it must not be relabeled
source-supported or automatically treated as a false positive by the old source scorer.
Do not use the same provider payload as both prediction and unquestioned gold.

Report counts and denominators for: correct field recovery / masked eligible fields;
correct applied fields / all applied fields; wrong work-or-edition records / records
with any applied change; abstention / eligible inputs; source-value overwrite count;
correct overwrites / all overwrites; harmful overwrites / previously correct fields;
core requirement improvement; request count, cache-hit rate, p50/p95 added latency,
provider error counts, and AI cost when known. Split by digital/scanned, CJK/Latin/mixed,
work type, provider and tier. Keep failures and skips in coverage denominators.

Proposed initial release criteria (policy targets, not measured results):

- T1/T2 source-supported overwrites apply without confirmation; every overwrite has
  old/new values and evidence. Zero T3/T4 overwrites, unprovenanced writes, offline
  requests, unsafe-URL fetches, or silently successful failed proposals.
- Correction precision ≥99%, measured separately from fill precision; publish harmful
  overwrite counts, including source-repair reversals, against independent gold.
- Applied-field precision ≥99%; zero observed wrong-work/edition changes on at least
  300 independently reviewed changed records overall, and publish a 95% confidence
  bound. Zero/300 gives only an approximately 1% upper bound, not proof of zero risk.
- At least 50 changed records in each enabled CJK and Latin cohort, reported separately;
  this is a coverage minimum, not a 1% subgroup guarantee. No default-on safety claim
  for an undersampled cohort. Collect more evidence or leave release gated explicitly.
- At least 10 percentage points of correct masked-field recovery over extraction-only
  on the frozen eligible set, with no decrease in source-supported core accuracy.
- Automatic stage respects the 20-second/12-attempt budget under timeout/429 fixtures;
  report actual live latency separately. All existing affected regression tests pass.

Freeze thresholds on development data before running held-out evaluation. If gates
fail, retain opt-in/report-only development behavior and document the failing cohort;
do not lower thresholds or exclude hard cases after inspecting held-out outcomes.

## 9. Validation commands and review evidence

During this planning review, system `python` could not collect tests because PyMuPDF
was missing. The existing project environment works:

```sh
.venv/bin/python -m pytest -q tests/test_metadata_registry.py tests/test_citation_verification.py
# 28 passed in 0.24s on the reviewed working tree
```

Implementation validation should add the new focused tests, then run the affected
existing repair, PDF metadata and artifact tests before the complete test suite:

```sh
.venv/bin/python -m pytest -q tests/test_metadata_registry.py tests/test_citation_verification.py tests/test_online_enrichment.py tests/test_host_metadata_regressions.py tests/test_multimodal_contracts.py
.venv/bin/python -m pytest -q
```

The focused test files are now implemented. Provider tests use
fixtures, fake clocks and sessions; live smoke tests are separate, bounded and excluded
from normal CI. Real-PDF acceptance must use the explicit citation-verification wrapper
before claiming source accuracy, per AGENTS.md. A green ingestion status alone is not
evidence of bibliographic correctness.

## 10. Changes from the original proposal

- Corrected the description of existing verification and its integration constraints.
- Replaced unspecified trust promotion with explicit discovery, identity and field policy.
- Added Claude, Gemini, and OpenAI discovery with mandatory native online search, actual
  search-execution evidence, citations, and independent registry verification.
- Retained owner-confirmed automatic T1/T2 overwrites, including source repairs.
- Kept enrichment after source repair and allowed both proposal types in one run.
- Resolve explicit proposal identifiers first, then search for remaining missing fields.
- Covered incomplete/stale field states, repair ordering, deterministic IDs and sidecars.
- Added book-edition hydration, missing-anchor rules, near-tie rejection and CJK cases.
- Moved transport bounds/cache/offline support before default-on integration.
- Made Chicago rendering a final output operation and preserved source Merkle identity.
- Added current provider documentation, explicit proposal replay, independent gold,
  quantitative release gates, and a separate AI implementation/evaluation increment.

## 11. Implementation status

The owner authorized implementation after this plan was reviewed. Registry clients,
bounded transport/cache, identity matching, automatic T1/T2 overwrites, T4 fill-only
native-search discovery, proposal validation, shared PDF orchestration, final artifact
regeneration, Chicago rendering, harness separation and benchmark scoring are implemented.
Source repair runs before enrichment, and both decisions remain in the audit trail.
Development enrichment remains opt-in as specified above.

Automated checks cover digital/scanned orchestration using controlled pipeline outputs,
provider response fixtures, offline and transport bounds, stale/invalid proposals,
ambiguous identities and dependent artifact consistency. These checks do not establish
real-PDF citation accuracy or live provider compatibility.

Outstanding release work: live Claude/Gemini/OpenAI checks with credentials; original
digital/scanned PDF ingestion followed by the explicit citation-verification evidence
review; independent registry gold and frozen held-out cohorts; quantitative release
gates. T3 identifier-free writes and T5 semantic enrichment remain deferred. Do not
enable enrichment by default on the strength of fixture tests alone.

Validation on 2026-09-27: full suite **337 passed, 7 skipped**; the sandbox-skipped
loopback slow-drip transport check passed separately outside the sandbox. Three
live AI smoke tests require credentials; the other three skips belong to existing
tests. Pyright passed for the three enrichment modules with the project virtual
environment on its import path; compileall, CLI help and `git diff --check` passed.
The declared build dependencies produced a wheel at
`/tmp/citeindex-enrichment-dist/citeindex-0.13.3-py3-none-any.whl`.

Independent architect review approved the opt-in development implementation after
fixing source/proposal identity precedence, conflicting given names, differing title
numerals and ambiguous AI identifiers. Matching is intentionally conservative; measure
its recovery cost before relaxing these safeguards. The scoped cleanup reused the
bounded response reader, removed unused code and retained explicit rendering-failure
warnings. No new runtime dependency was introduced.
