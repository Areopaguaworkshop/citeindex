---
description: Ingest a source with CiteIndex, then run evidence-backed citation verification
agent: build
---

Ingest `$ARGUMENTS` with CiteIndex. After the command returns successfully,
load the `citation-verification` skill and delegate the generated corpus output
to `@citation-verifier`. Present its quotation-backed source report. Apply requested
repairs through `--repair-proposal`. For requested registry completion or correction,
delegate to `@citation-enricher`, which uses the constrained CLI workflow. Accepted
T1/T2 changes need no per-record confirmation. Report source support separately
from registry completion; raw CLI runs never start these agents.
