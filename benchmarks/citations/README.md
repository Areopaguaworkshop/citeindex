# Citation benchmark

`manifest.jsonl` is deliberately local and untracked: each reviewed row needs
`id`, `source_path`, `source_sha256`, and a human-reviewed `csl` object.
Predictions use the same `id` and a `csl` object. Run:

```sh
python benchmarks/citations/run.py manifest.jsonl predictions.jsonl
```

The runner counts every gold record, including missing predictions. Keep PDFs
out of this directory unless their redistribution rights permit it.
