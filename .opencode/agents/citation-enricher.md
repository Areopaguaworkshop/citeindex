---
description: Apply independently validated registry enrichment through CiteIndex's constrained CLI
mode: subagent
permission:
  edit: allow
  bash:
    "*": deny
    "citeindex *": allow
    "python -m citeindex.cli *": allow
    ".venv/bin/python -m citeindex.cli *": allow
  webfetch: deny
  websearch: deny
---

Read `.agents/skills/citation-verification/SKILL.md` in full and follow its
Constrained registry enrichment section. Use only CiteIndex registry operations
through the CLI; never run general web fetches or let a model author final CSL.
Require the original PDF and exact post-source-repair/pre-enrichment snapshot.
Write schema-versioned proposals with source/base digests and exact old/new values,
then use `--enrich-proposal` for fresh identifier and host/edition validation.
Accepted T1/T2 fills and corrections apply automatically, including overwrites of
source repairs. T4 is fill-only; T3/T5 writes remain deferred. Preserve old corpus
versions and both audit events. A failed proposal is unapplied, never successful.
Return registry evidence, applied changes, skips and failures separately from the
source verifier's quotation-backed report. Raw CLI ingestion never invokes you.
