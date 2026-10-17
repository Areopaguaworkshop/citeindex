"""Entity similarity and the ranked no-match policy (P2 phase 2, D2/D3).

Policy (D3): every authority comparison produces a scored decision with
named components — fuzzy string similarity, positive-evidence anchor
agreement, and an optional embedding similarity — evaluated against a
threshold per entity type. Accepted decisions record their component
scores; rejected decisions are explicit ``no_match`` with a reason, and
the no-match rate is a headline number, never a hidden failure.

Documented outcomes (not bugs):

- ``ambiguous_identity`` — two distinct authority identities score
  within :data:`AMBIGUITY_MARGIN` of each other (homonyms, e.g. a city
  and a film sharing a name). No link is emitted; the runner-ups are
  listed for audit.
- ``script_mismatch`` — the source surface and the candidate surface
  use different scripts (e.g. CJK vs Latin). Token-based fuzzy scores
  and English MiniLM embeddings are unreliable across scripts, so the
  embedding component is skipped, the flag is recorded, and the decision
  rests on the (usually low) fuzzy score. Transliteration mismatches
  (Antakya vs Antioch) are expected no-matches here.

Tiers (D2): with no optional extra installed, the fuzzy component uses
stdlib ``SequenceMatcher`` (the behaviour the benchmark baseline calls
"today's path"); with rapidfuzz installed the fuzzy component uses
``WRatio`` and ``token_set_ratio``; with the ``.[authority]`` extra the
optional MiniLM embedding component is added. The pure-fuzzy tiers are
fully supported — embeddings never gate the pipeline.

Thresholds and weights are initial values, chosen conservatively; the
phase 3 gold set (500–1,000 hand-labelled entities) is the tuning
reference, not anecdotes. ``work`` matching is not implemented here: it
lives in ``online_enrichment.match_candidate`` behind the configurable
``online_enrich_min_score`` (0.90 default) and is listed only so the
policy table is complete.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from difflib import SequenceMatcher
from typing import Any, Protocol

try:
    from rapidfuzz import fuzz as _fuzz
except ImportError:  # pragma: no cover - stdlib tier via engine="stdlib" is tested explicitly
    _fuzz = None  # type: ignore[assignment]

_CJK_RE = re.compile(r"[\u3040-\u30ff\u3400-\u9fff\uac00-\ud7af]")
#: Initial policy values; tune on the phase 3 gold set.
ENTITY_THRESHOLDS: dict[str, float] = {"person": 0.85, "place": 0.80, "journal": 0.90, "work": 0.90}
AMBIGUITY_MARGIN = 0.05
#: (fuzzy, embedding, anchor) weights, with and without an embedder.
_WEIGHTS: dict[str, tuple[tuple[float, float, float], tuple[float, float, float]]] = {
    "person": ((0.5, 0.3, 0.2), (0.7, 0.0, 0.3)),
    "place": ((0.6, 0.25, 0.15), (0.85, 0.0, 0.15)),
    "journal": ((0.7, 0.3, 0.0), (1.0, 0.0, 0.0)),
}


def _normalized(value: Any) -> str:
    return " ".join(re.findall(r"\w+", unicodedata.normalize("NFKC", str(value or "")).casefold()))


def fuzzy_similarity(left: str, right: str, *, engine: str | None = None) -> float:
    """Fuzzy similarity in [0, 1]: rapidfuzz when installed, stdlib otherwise.

    ``engine`` is ``None`` (auto-detect), ``"rapidfuzz"`` or ``"stdlib"``;
    explicit ``"rapidfuzz"`` without the package raises ``ImportError``.
    """
    left, right = _normalized(left), _normalized(right)
    if not left or not right:
        return 0.0
    if engine is None:
        engine = "rapidfuzz" if _fuzz is not None else "stdlib"
    if engine == "rapidfuzz":
        if _fuzz is None:
            raise ImportError("rapidfuzz is not installed; use engine='stdlib'")
        return max(_fuzz.WRatio(left, right), _fuzz.token_set_ratio(left, right)) / 100.0
    return SequenceMatcher(None, left, right, autojunk=False).ratio()


class SimilarityEmbedder(Protocol):
    def similarity(self, left: str, right: str) -> float: ...


class MiniLMEmbedder:
    """all-MiniLM-L6-v2 cosine similarity; heavy (torch) — load only when installed."""

    def __init__(self, model_name: str = "sentence-transformers/all-MiniLM-L6-v2") -> None:
        from sentence_transformers import SentenceTransformer  # heavy import, on purpose

        self._model = SentenceTransformer(model_name)

    def similarity(self, left: str, right: str) -> float:
        vectors = self._model.encode([str(left), str(right)], normalize_embeddings=True)
        return float(vectors[0] @ vectors[1])


_EMBEDDER: MiniLMEmbedder | None = None
_EMBEDDER_TRIED = False


def get_embedder() -> MiniLMEmbedder | None:
    """Process-wide lazy embedder; ``None`` when sentence-transformers is absent."""
    global _EMBEDDER, _EMBEDDER_TRIED
    if not _EMBEDDER_TRIED:
        _EMBEDDER_TRIED = True
        try:
            _EMBEDDER = MiniLMEmbedder()
        except ImportError:
            _EMBEDDER = None
    return _EMBEDDER


def _full_name(person: Mapping[str, Any]) -> str:
    family = _normalized(person.get("family"))
    given = _normalized(person.get("given"))
    if family and given:
        return given + " " + family
    return _normalized(person.get("literal")) or family or given


def _candidate_strings(candidate: Mapping[str, Any]) -> list[str]:
    strings = [_full_name(candidate)]
    if isinstance(candidate.get("other-names"), list):
        strings.extend(str(item) for item in candidate["other-names"] if str(item).strip())
    if _normalized(candidate.get("literal")):
        strings.append(str(candidate["literal"]))
    return [item for item in strings if item]


def _given_agreement(left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
    """Missing given names never contradict; otherwise initials or exact tokens agree."""
    left_given, right_given = _normalized(left.get("given")), _normalized(right.get("given"))
    if not left_given or not right_given:
        return True
    left_first, right_first = left_given.split()[0], right_given.split()[0]
    return left_first == right_first or (
        left_first[0] == right_first[0] and min(len(left_first), len(right_first)) == 1
    )


def _person_components(source: Mapping[str, Any], candidate: Mapping[str, Any]) -> tuple[float, int]:
    source_strings = [_full_name(source)]
    fuzzy = max(
        (fuzzy_similarity(left, right) for left in source_strings for right in _candidate_strings(candidate)),
        default=0.0,
    )
    source_family = _normalized(source.get("family") or source.get("literal"))
    candidate_family = _normalized(candidate.get("family") or candidate.get("literal"))
    family_agree = bool(source_family and candidate_family) and (
        source_family == candidate_family or fuzzy_similarity(source_family, candidate_family) >= 0.9
    )
    anchor = 1 if family_agree and _given_agreement(source, candidate) else 0
    return fuzzy, anchor


def _place_components(source_place: str, candidate: Mapping[str, Any]) -> tuple[float, int]:
    fuzzy = fuzzy_similarity(source_place, str(candidate.get("label") or ""))
    source_text = " " + _normalized(source_place) + " "
    anchor = (
        1
        if any(
            _normalized(candidate.get(key)) and f" {_normalized(candidate[key])} " in source_text
            for key in ("country", "admin1")
        )
        else 0
    )
    return fuzzy, anchor


def _script_mismatch(*texts: str) -> bool:
    flags = {bool(_CJK_RE.search(text)) for text in texts if text}
    return len(flags) > 1


def _source_snapshot(entity_type: str, source: Any) -> Any:
    if entity_type == "person" and isinstance(source, Mapping):
        return {key: source.get(key) for key in ("family", "given", "literal") if source.get(key) is not None}
    return str(source)


def match_entity(
    entity_type: str,
    source: Any,
    candidate: Mapping[str, Any],
    *,
    embedder: SimilarityEmbedder | None = None,
    threshold: float | None = None,
) -> dict[str, Any]:
    """Score one authority candidate against a source entity (the D3 policy)."""
    if entity_type == "person":
        if not isinstance(source, Mapping):
            raise ValueError("person matching requires a source name mapping")
        fuzzy, anchor = _person_components(source, candidate)
        source_text, candidate_text = _full_name(source), _full_name(candidate)
        weights_key = "person"
    elif entity_type == "place":
        fuzzy, anchor = _place_components(str(source), candidate)
        source_text, candidate_text = str(source), str(candidate.get("label") or "")
        weights_key = "place"
    elif entity_type == "journal":
        fuzzy = fuzzy_similarity(str(source), str(candidate.get("label") or candidate.get("container-title") or ""))
        anchor = 0
        source_text, candidate_text = str(source), str(candidate.get("label") or "")
        weights_key = "journal"
    else:
        raise ValueError("unsupported entity type: " + str(entity_type))
    fuzzy = round(fuzzy, 4)
    script_mismatch = _script_mismatch(source_text, candidate_text)
    embedding = None
    if embedder is not None and not script_mismatch:
        embedding = round(max(0.0, embedder.similarity(source_text, candidate_text)), 4)
    with_embedding, without_embedding = _WEIGHTS[weights_key]
    fuzzy_weight, embedding_weight, anchor_weight = with_embedding if embedding is not None else without_embedding
    score = round(fuzzy_weight * fuzzy + embedding_weight * (embedding or 0.0) + anchor_weight * anchor, 4)
    threshold = ENTITY_THRESHOLDS.get(entity_type, 0.90) if threshold is None else float(threshold)
    return {
        "score": score,
        "accepted": score >= threshold,
        "components": {"fuzzy": fuzzy, "anchor": anchor, "embedding": embedding, "script_mismatch": script_mismatch},
    }


def _audit_rows(scored: list[tuple[float, dict[str, Any]]]) -> list[dict[str, Any]]:
    return [
        {
            "system": row["candidate"]["authority"].get("system"),
            "id": row["candidate"]["authority"].get("id"),
            "score": score,
        }
        for score, row in scored
    ]


def select_authority_candidate(
    entity_type: str,
    source: Any,
    results: list[dict],
    *,
    embedder: SimilarityEmbedder | None = None,
    field: str | None = None,
) -> dict[str, Any]:
    """Ranked decision for one entity query across pooled authority results.

    ``linked`` requires a unique top candidate at or above the threshold
    and clear of the runner-up by :data:`AMBIGUITY_MARGIN`; everything
    else is an explicit ``no_match`` (``below_threshold``,
    ``ambiguous_identity``, ``no_candidates``) or ``skipped``
    (``budget_exhausted``). Non-authority (work-shaped) results are
    ignored so authority and work candidate pools cannot be confused.
    """
    decision: dict[str, Any] = {"entity_type": entity_type, "source": _source_snapshot(entity_type, source)}
    if field:
        decision["field"] = field
    scored: list[tuple[float, dict[str, Any]]] = []
    seen: set[tuple[str, str]] = set()
    for result in results or []:
        if not isinstance(result, Mapping) or result.get("status") != "found":
            continue
        candidate = result.get("candidate")
        if not isinstance(candidate, Mapping) or not isinstance(candidate.get("authority"), Mapping):
            continue
        authority = candidate["authority"]
        key = (str(authority.get("system")), str(authority.get("id")))
        if key in seen:
            continue
        seen.add(key)
        if embedder is None:
            embedder = get_embedder()  # lazy; stays None on the pure-fuzzy tiers
        match = match_entity(entity_type, source, candidate, embedder=embedder)
        scored.append((match["score"], {"result": dict(result), "candidate": dict(candidate), "match": match}))
    if not scored:
        budget = any(
            isinstance(result, Mapping) and result.get("error") == "budget_exhausted" for result in results or []
        )
        if budget:
            decision.update(status="skipped", reason="budget_exhausted")
        else:
            decision.update(status="no_match", reason="no_candidates", candidates=[])
        return decision
    scored.sort(key=lambda pair: pair[0], reverse=True)
    top_score, top = scored[0]
    threshold = ENTITY_THRESHOLDS.get(entity_type, 0.90)
    if top_score < threshold:
        decision.update(status="no_match", reason="below_threshold", score=top_score, candidates=_audit_rows(scored))
        return decision
    if len(scored) > 1 and top_score - scored[1][0] < AMBIGUITY_MARGIN:
        decision.update(status="no_match", reason="ambiguous_identity", score=top_score, candidates=_audit_rows(scored))
        return decision
    decision.update(
        status="linked",
        score=top_score,
        authority=dict(top["candidate"]["authority"]),
        components=top["match"]["components"],
        provenance=dict(top["result"].get("provenance") or {}),
        alternatives=len(scored) - 1,
    )
    return decision
