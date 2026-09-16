"""Supported CSL-JSON fields, export, and source-type evaluation profiles."""
from datetime import date
from typing import Any

NAME_FIELDS = ("author", "editor", "translator", "host", "interviewer", "director",
               "producer", "executive-producer", "narrator", "performer", "composer",
               "guest", "chair", "contributor", "organizer", "script-writer", "series-creator")
DATE_FIELDS = ("issued", "accessed", "event-date", "original-date", "available-date")
NUMBER_FIELDS = ("edition", "volume", "issue", "number", "collection-number", "page")
CSL_FIELDS = ("type", "title", *NAME_FIELDS, *DATE_FIELDS, "publisher", "publisher-place",
              "container-title", "collection-title", *NUMBER_FIELDS, "DOI", "ISBN", "ISSN",
              "URL", "language", "abstract", "medium", "dimensions", "genre", "event-title",
              "event-place", "archive", "archive_location", "source", "version", "note")
HOST_FIELDS = (*CSL_FIELDS, "subtitle")  # subtitle is composed into title on export
CSL_TYPES = {
    "article", "article-journal", "article-magazine", "article-newspaper", "book", "chapter",
    "thesis", "paper-conference", "report", "manuscript", "document", "webpage", "post-weblog",
    "speech", "interview", "broadcast", "motion_picture", "song", "dataset", "entry-encyclopedia",
}
MEDIA_TYPES = {"speech", "interview", "broadcast", "motion_picture", "song"}


def valid_host_value(field: str, value: Any) -> bool:
    if field not in HOST_FIELDS:
        return False
    if field == "type":
        return isinstance(value, str) and value in CSL_TYPES
    if field in NAME_FIELDS:
        return isinstance(value, list) and bool(value) and all(
            isinstance(name, dict) and bool(name.get("literal") or name.get("family"))
            and set(name) <= {"literal", "family", "given", "suffix", "dropping-particle", "non-dropping-particle"}
            and all(isinstance(part, str) and part.strip() for part in name.values())
            for name in value)
    if field in DATE_FIELDS:
        parts = value.get("date-parts") if isinstance(value, dict) and set(value) == {"date-parts"} else None
        if not (isinstance(parts, list) and len(parts) == 1 and isinstance(parts[0], list)
                and 1 <= len(parts[0]) <= 3 and all(type(n) is int for n in parts[0])):
            return False
        try:
            date(*(parts[0] + [1] * (3 - len(parts[0]))))
            return True
        except ValueError:
            return False
    if field in NUMBER_FIELDS and type(value) is int:
        return value >= 0
    return isinstance(value, str) and bool(value.strip())


def evaluation_fields(csl: dict, modality: str = "") -> set[str]:
    """Fields to annotate, not an assertion that every field must be present."""
    fields = {"type", "title", "author", "issued"}
    kind = csl.get("type")
    if kind in MEDIA_TYPES or (kind == "document" and modality == "media"):
        fields |= {"medium", "dimensions", "publisher", "container-title", "URL"}
        if kind == "interview":
            fields |= {"interviewer", "host"}
        elif kind == "broadcast":
            fields |= {"host", "guest", "producer", "number", "collection-number"}
        elif kind == "speech":
            fields |= {"event-title", "event-date", "event-place", "chair"}
        elif kind == "motion_picture":
            fields |= {"director", "producer"}
        elif kind == "song":
            fields |= {"composer", "performer"}
    elif kind in {"webpage", "post-weblog", "article-newspaper", "article-magazine"}:
        fields |= {"container-title", "publisher", "URL", "accessed"}
    elif kind == "article-journal":
        fields |= {"container-title", "volume", "issue", "page", "DOI"}
    else:
        fields |= {"editor", "translator", "edition", "publisher", "ISBN"}
        if kind == "chapter":
            fields |= {"container-title", "page"}
    if csl.get("URL") or modality == "url_article":
        fields |= {"URL", "accessed"}
    return fields


def export_csl(record: dict) -> dict:
    """Return a processor-ready item; keep unsupported/internal data in custom."""
    if not isinstance(record.get("id"), (str, int)) or isinstance(record.get("id"), bool):
        raise ValueError("CSL export requires an id")
    if not valid_host_value("type", record.get("type")):
        raise ValueError("CSL export requires a supported type")
    result = {"id": record["id"]}
    custom = dict(record.get("custom") or {})
    for key, value in record.items():
        if key in {"id", "custom"}:
            continue
        if key in CSL_FIELDS and valid_host_value(key, value):
            result[key] = value
        else:
            custom[key] = value
    subtitle = record.get("subtitle")
    if isinstance(subtitle, str) and subtitle.strip() and subtitle.casefold() not in result.get("title", "").casefold():
        result["title"] = f"{result.get('title', '')}: {subtitle}".strip(": ")
    if custom:
        result["custom"] = custom
    return result
