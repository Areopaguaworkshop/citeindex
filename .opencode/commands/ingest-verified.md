---
description: Ingest a source with CiteIndex, then run evidence-backed citation verification
agent: build
---

Ingest `$ARGUMENTS` with CiteIndex. After the command returns successfully,
load the `citation-verification` skill and delegate the generated corpus output
to `@citation-verifier`. Present its quotation-backed correction plan and ask
for approval before any persisted-artifact repair or re-ingestion.
