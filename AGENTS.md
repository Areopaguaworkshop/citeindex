# CiteIndex agent workflow

After a successful CiteIndex ingestion, load the `citation-verification` skill and run its evidence review before presenting citation accuracy. Never edit a persisted `csl.json` directly: a future repair path must regenerate dependent hash, folder, JSON, and Markdown artifacts.

The independent audit is invoked only by an explicit Codex, Claude Code, OpenCode, or Pi skill/command wrapper; a raw `citeindex` CLI process never detects or starts a harness audit.
