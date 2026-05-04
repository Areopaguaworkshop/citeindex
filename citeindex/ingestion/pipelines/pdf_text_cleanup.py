import math
import re
from typing import Dict, List


_MARGIN_PAGE_NUMBER_RE = re.compile(
    r"^\s*(?:[-–—·•]*\s*)?(?:\d{1,4}|[ivxlcdm]{1,8})(?:\s*[-–—·•]*)?\s*$",
    re.IGNORECASE,
)


def clean_page_texts(page_texts: List[str], window: int = 4) -> List[str]:
    """Strip repeated running headers/footers and standalone page numbers.

    Only strips short lines that repeat in the first/last few lines across
    multiple pages. The earliest occurrence of a repeated header/footer is kept
    so genuine section openers on their first page survive.
    """
    repeated_lines = _find_repeated_margin_lines(page_texts, window=window)
    return [
        _strip_page_margins(text, repeated_lines, page_idx, window=window)
        for page_idx, text in enumerate(page_texts)
    ]


def _find_repeated_margin_lines(page_texts: List[str], window: int) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    first_seen: Dict[str, int] = {}

    for page_idx, text in enumerate(page_texts):
        seen_on_page = set()
        for line in _margin_lines(text, window=window):
            key = _normalize_margin_line(line)
            if not _is_candidate_margin_key(key, line):
                continue
            seen_on_page.add(key)
            first_seen.setdefault(key, page_idx)
        for key in seen_on_page:
            counts[key] = counts.get(key, 0) + 1

    min_repeats = min(5, max(2, math.ceil(len(page_texts) * 0.15)))
    return {
        key: first_seen[key]
        for key, count in counts.items()
        if count >= min_repeats
    }


def _margin_lines(text: str, window: int) -> List[str]:
    nonempty_lines = [line.strip() for line in text.splitlines() if line.strip()]
    if len(nonempty_lines) <= window * 2:
        return nonempty_lines
    return nonempty_lines[:window] + nonempty_lines[-window:]


def _normalize_margin_line(line: str) -> str:
    normalized = re.sub(r"\d+", " ", line.casefold())
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def _is_candidate_margin_key(key: str, original_line: str) -> bool:
    if not key:
        return False
    if _MARGIN_PAGE_NUMBER_RE.match(original_line):
        return False
    if len(original_line.strip()) > 80 or len(key) > 60:
        return False
    if len(key.split()) > 8:
        return False
    return True


def _strip_page_margins(
    text: str,
    repeated_lines: Dict[str, int],
    page_idx: int,
    window: int,
) -> str:
    lines = text.splitlines()
    start = 0
    end = len(lines)

    while start < end:
        line = lines[start]
        if not line.strip():
            start += 1
            continue
        if _should_strip_margin_line(line, repeated_lines, page_idx):
            start += 1
            continue
        break

    while end > start:
        line = lines[end - 1]
        if not line.strip():
            end -= 1
            continue
        if _should_strip_margin_line(line, repeated_lines, page_idx):
            end -= 1
            continue
        break

    cleaned_lines = lines[start:end]
    cleaned = "\n".join(cleaned_lines).strip()
    return cleaned or text.strip()


def _should_strip_margin_line(
    line: str,
    repeated_lines: Dict[str, int],
    page_idx: int,
) -> bool:
    if _MARGIN_PAGE_NUMBER_RE.match(line):
        return True
    key = _normalize_margin_line(line)
    if not key:
        return False
    first_seen = repeated_lines.get(key)
    return first_seen is not None and page_idx > first_seen
