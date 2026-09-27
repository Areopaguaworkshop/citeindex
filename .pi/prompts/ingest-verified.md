---
description: Ingest a source with CiteIndex, then run evidence-backed citation verification
---

Ingest `$ARGUMENTS` with CiteIndex. After the command returns successfully,
load the `citation-verification` skill and review the generated corpus output.
Present a quotation-backed source report. Apply requested repairs through
`--repair-proposal`; perform requested registry enrichment through the shared
skill's constrained CLI workflow. Accepted T1/T2 changes need no per-record
confirmation. Never edit a persisted `csl.json` directly. Report source support
separately from registry completion; a raw CLI run never starts this audit.
