# CiteIndex verification hook

Whenever this harness runs `citeindex` and it succeeds, load the
`citation-verification` skill and delegate the generated corpus output to
`@citation-verifier` before presenting the result. Do this for `/ingest-verified`
without asking. For a manually requested plain `citeindex` run, present the
verification report before proposing any citation correction.

The verifier must provide quotation-backed corrections only. Do not directly
edit persisted CiteIndex artifacts until a repair path can regenerate the
dependent hash, folder, JSON, and Markdown artifacts. The implemented paths are
`--repair-proposal` for source evidence and `--enrich-proposal` for registry changes.
For requested registry enrichment, use `@citation-enricher`; keep its registry
report separate from `@citation-verifier` source support. T1/T2 changes apply
automatically without per-record confirmation. These are explicit harness
instructions; raw CiteIndex CLI processes never load skills or spawn audits.
