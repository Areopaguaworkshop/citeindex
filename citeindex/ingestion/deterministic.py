import hashlib
import json
from typing import Any, Dict, List


def canonicalize_text(text: str) -> str:
    """Normalize text for deterministic hashing."""
    if text is None:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [" ".join(line.split()) for line in text.split("\n")]
    return "\n".join(lines).strip()


def canonical_json_dumps(data: Any) -> str:
    """Stable JSON serialization for hashing and artifact persistence."""
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_hex(data: str) -> str:
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def hash_payload(payload: Any) -> str:
    if isinstance(payload, str):
        normalized = canonicalize_text(payload)
    else:
        normalized = canonical_json_dumps(payload)
    return sha256_hex(normalized)


def build_merkle_tree(leaf_hashes: List[str]) -> Dict[str, Any]:
    """
    Build a deterministic Merkle tree.
    Odd nodes are duplicated at each level for deterministic pairing.
    """
    if not leaf_hashes:
        leaf_hashes = [sha256_hex("")]

    levels: List[List[str]] = [leaf_hashes[:]]
    while len(levels[-1]) > 1:
        current = levels[-1]
        nxt: List[str] = []
        for i in range(0, len(current), 2):
            left = current[i]
            right = current[i + 1] if i + 1 < len(current) else current[i]
            nxt.append(sha256_hex(left + right))
        levels.append(nxt)

    return {
        "algorithm": "sha256",
        "leaf_count": len(leaf_hashes),
        "levels": levels,
        "root": levels[-1][0],
    }


def build_merkle_proof(tree: Dict[str, Any], leaf_index: int) -> List[Dict[str, str]]:
    levels = tree.get("levels", [])
    if not levels or leaf_index < 0 or leaf_index >= len(levels[0]):
        return []

    proof: List[Dict[str, str]] = []
    idx = leaf_index
    for level in levels[:-1]:
        sibling_idx = idx ^ 1
        if sibling_idx >= len(level):
            sibling_idx = idx
        position = "right" if sibling_idx > idx else "left"
        proof.append({"position": position, "hash": level[sibling_idx]})
        idx //= 2
    return proof
