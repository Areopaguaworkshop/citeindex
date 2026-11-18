# Pure VLM vs DSPy pipeline — 32 records, same scorer

Date: 2026-10-27
Manifest: `benchmarks/sciencemeta/data/manifest-cp30.jsonl` (32 records: textbooks and ebooks, seed 7)
Protocol: pages 1–6, 140 dpi images for the VLM arms; `-p "1-6"`, `--no-layout --no-pageindex --no-online-enrich` for the pipeline; registry fill off; scorer `sciencemeta-v3`.

## Result

| Arm | strict | soft | Levenshtein | title | author | publisher | issued | ISBN |
|---|---|---|---|---|---|---|---|---|
| DSPy pipeline (deepseek-v4.1-flash) | 5/32 | 5/32 | 9/32 | 11/31 | 16/29 | 9/26 | 22/31 | 9/16 |
| Pure VLM — deepseek-v4.1-flash:1017-cloud | 1/32 | 5/32 | 8/32 | 8/31 | 18/29 | 6/26 | 18/31 | 7/16 |
| Pure VLM — gemma4:31b-cloud | 3/32 | 6/32 | 9/32 | 11/31 | 21/29 | 8/26 | 20/31 | 6/16 |
| DSPy pipeline with gemma4:31b-cloud as `--llm` | 5/32 | 5/32 | 8/32 | 11/31 | 13/29 | 10/26 | 20/31 | 9/16 |

Paired exact McNemar on strict:
- DSPy+DeepSeek vs Gemma 4 (vision): 5 baseline-only, 3 other-only, p = 0.727
- DeepSeek (vision) vs Gemma 4 (vision): 0 vs 2, p = 0.500
- DSPy+DeepSeek vs DSPy+Gemma: 1 vs 1, p = 1.000

No difference is statistically detectable at n = 32.

## Per-field reading

- **Author**: both VLMs beat the pipeline (18 and 21 of 29 against 16). Vision reads the author block and its superscript layout directly.
- **Title**: Gemma matches the pipeline exactly (11/31); DeepSeek loses two.
- **Publisher, issued, ISBN**: the pipeline wins on all three. These values live in dense imprint and copyright text, where the pipeline's deterministic seeds and quote gate do better than a single visual reading.
- **Soft vs strict**: Gemma has the best soft score (6/32) while strict stays at 3/32 — it recovers more fields but still misses at least one evaluated field per record.

## The model swap, isolated

Swapping DeepSeek for Gemma inside the same DSPy workflow changed nothing on strict (5/32 both, McNemar 1-vs-1, p = 1.000). Two per-field results are more informative than the tie:

| Field | Gemma as text model (DSPy) | Gemma as vision model | Difference |
|---|---|---|---|
| author | 13/29 | **21/29** | +8 for the same model |
| publisher | **10/26** | 8/26 | +2 for the text workflow |
| title | 11/31 | 11/31 | identical |

Same model, same 32 records. Reading the page as an image finds eight more authors than reading extracted text through the gate. That is the clearest evidence yet that the constraint is the text path — OCR quality, block window and quote gate — and not the choice of model.

It also explains why publisher improves in the text workflow: the deterministic seeds and the gate do work a single visual reading cannot.

## Conclusion

1. Model substitution inside the current workflow is not a lever: identical strict, identical McNemar.
2. Modality is a lever for authors: +8 authors for Gemma when it sees the page.
3. The workflow is a lever for imprint fields: publisher is best in the DSPy+Gemma arm, not in either vision arm.
4. Neither engine alone wins. The next arm must combine them: vision for title and author, deterministic seeds and the gate for publisher, place, date and ISBN, with the two reconciled per field.

## Failure modes observed

| Arm | Failure | Count |
|---|---|---|
| DeepSeek VLM | empty completion, JSON decode error | 3/32 |
| Gemma 4 VLM | JSON wrapped in a ```json fence despite `format: "json"` | 12/12 on first run, 0 after the parser unwraps fences |
| Both | Chinese author strings carry dynasty prefixes and traditional forms (`唐 刘禹锡`, `馮漢鏞` vs `刘禹锡`, `冯汉镛`) | present, scored as misses |

## Caveats

1. n = 32. A five-versus-three gap has p = 0.727 here; no ranking should be drawn from this table.
2. The VLM arms use one call for the entire record. The pipeline uses page location, deterministic seeds, a quote gate, targeted re-ask and confidence. This compares *engines*, not prompts.
3. GROBID was measured on the Latin subset and removed from the project: 0/29 strict against the pipeline's 5/29, with empty or body-text titles in its raw TEI.

## What the numbers support

1. Vision is the better reader for author names; the pipeline is better for imprint fields.
2. The next arm should be **vision-first with a field split**: title and author from the cover image, publisher, place, date and ISBN from the copyright page image, rather than one prompt for the whole record.
3. Any claim beyond that needs at least ~300 records and a repeat per arm, per the merged plan's gates.
