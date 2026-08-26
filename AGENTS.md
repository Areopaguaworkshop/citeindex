# CiteIndex agent workflow

After a successful CiteIndex ingestion, load the `citation-verification` skill and run its evidence review before presenting citation accuracy. Never edit a persisted `csl.json` directly: a future repair path must regenerate dependent hash, folder, JSON, and Markdown artifacts.
