# ScienceMetaBench citations — pass 3b name-normalised

- manifest: `benchmarks/sciencemeta/data/manifest3.jsonl` (sha256 `28a919ccb42a7748c3f1b2f41f62e57258eb5b4c8af7b2ce810548cd75d1c461`)
- predictions: `benchmarks/sciencemeta/data/predictions3.jsonl`
- scorer: `sciencemeta-v1`; Levenshtein threshold 0.80 (`difflib` ratio)
- sources: 20; scored: 20; attempted: 20; completed: 8; failed: 12; not attempted: 0

## Record metrics

| metric | n | hits | rate | interval |
| --- | --- | --- | --- | --- |
| strict record accuracy | n=20 | 1 | 5.0% | 95% CI 0.9–23.6 |
| soft record accuracy | n=20 | 1 | 5.0% | 95% CI 0.9–23.6 |
| levenshtein record accuracy | n=20 | 2 | 10.0% | 95% CI 2.8–30.1 |
| instance recall | n=20 | 1 | 5.0% | 95% CI 0.9–23.6 |

## Per-field (strict matching)

| field | n | precision | recall | f1 |
| --- | --- | --- | --- | --- |
| title | n=20 | 50.0% (95% CI 21.5–78.5) | 20.0% (95% CI 8.1–41.6) | 28.6% (95% CI 15.3–47.1) |
| author | n=18 | 100.0% (95% CI 43.9–100.0) | 16.7% (95% CI 5.8–39.2) | 28.6% (95% CI 13.8–50.0) |
| publisher | n=11 | 100.0% (95% CI 20.7–100.0) | 9.1% (95% CI 1.6–37.7) | 16.7% (95% CI 4.7–44.8) |
| issued | n=15 | 33.3% (95% CI 6.1–79.2) | 6.7% (95% CI 1.2–29.8) | 11.1% (95% CI 3.1–32.8) |
| ISBN | n=6 | 100.0% (95% CI 20.7–100.0) | 16.7% (95% CI 3.0–56.4) | 28.6% (95% CI 8.2–64.1) |

## Excluded gold-absent fields

| field | outside_scope rows |
| --- | --- |
| title | 0 |
| author | 2 |
| publisher | 9 |
| issued | 5 |
| ISBN | 14 |

## Notes

Record metrics count every manifest row with at least one evaluated field; missing or failed predictions are incorrect, never dropped. The F1 interval is an approximate Wilson interval on the point estimate, not a binomial CI.
Fields marked `outside_scope` in the manifest are excluded from every denominator and cannot fail a record; with n=0 the interval is undefined and printed as `95% CI n/a`.
