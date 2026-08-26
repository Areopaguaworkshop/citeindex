# CiteIndex verification hook

Whenever you run `citeindex` and it succeeds, load the
`citation-verification` skill and delegate the generated corpus output to
`@citation-verifier` before presenting the result. Do this for `/ingest-verified`
without asking. For a manually requested plain `citeindex` run, present the
verification report before proposing any citation correction.

The verifier must provide quotation-backed corrections only. Do not directly
edit persisted CiteIndex artifacts until a repair path can regenerate the
dependent hash, folder, JSON, and Markdown artifacts.
