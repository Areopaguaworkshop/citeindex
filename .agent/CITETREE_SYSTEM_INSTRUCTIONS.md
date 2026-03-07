# CiteTree System Instructions

## Objective
Create a deterministic, citation-first academic RAG system with auditable source integrity and no vector database.

## Core Enforcement
1. Every source has valid CSL JSON.
2. Every text unit is attached to a hierarchical node.
3. Default node granularity is paragraph.
4. Primary sources are line-level.
5. Node fields are mandatory: `node_id`, `source_id`, `section_path`, `text`, `sha256`.
6. Document-level Merkle root is required.
7. Retrieval uses metadata filters + BM25 + deterministic tie-breaks only.
8. Answers reference explicit `node_id` values.
9. Citation rendering uses citation adapter with default style `chicago-author-date`.
10. Machine output always includes `document_merkle_root` and per-evidence `merkle_proof`.

## Forbidden
- Fabricated citations
- Synthetic page numbers
- Using non-indexed knowledge in answers
- Missing trace metadata

## Clarification Policy
If evidence is insufficient, ask user clarification questions instead of guessing.

## Integration Boundary
Use `Areopaguaworkshop/citation` for CSL normalization and citation rendering only.
