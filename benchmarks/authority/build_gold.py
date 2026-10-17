"""Build the authority gold set (P2 phase 3 / D5) — honest labels only.

Every gold row carries ``label_status`` and a ``provenance`` string that
records *how the label was made*:

- ``premodern`` seeds (hand-curated): pre-1900 persons are certain
  ``absent`` labels — ORCID iDs are researcher-registered and historical
  figures cannot hold one.
- library authors / places: dumped as candidates for human review
  (``review/``), then hand-verified into ``seeds/verified_*.jsonl``.
  Unverified names are excluded, never guessed.
- field-sampled persons / works: ORCID asserted in a publisher-deposited
  Crossref record (provenance = that record's DOI); sampled from the
  candidate's field journals, verified against the Crossref registry.
- journals: ISSN from the Crossref journals registry.
- library works: DOI from a Crossref bibliographic search accepted only
  when the title is fuzzy-identical and author/year agree.

Run:  ``.venv/bin/python benchmarks/authority/build_gold.py``
Review pass: read ``review/*.jsonl`` and write ``seeds/verified_*.jsonl``,
then re-run. The first run dumps candidates; the final run emits
``gold.jsonl``. All API traffic is cached under ``cache/`` (gitignored).
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import requests

AUTHORITY_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(AUTHORITY_DIR.parent.parent))

from citeindex.ingestion.authority import normalize_orcid_person  # noqa: E402
from citeindex.ingestion.authority.matching import fuzzy_similarity  # noqa: E402
from citeindex.ingestion.metadata_registry import RegistryClient  # noqa: E402
from citeindex.ingestion.models import IngestionConfig  # noqa: E402

MANIFEST = AUTHORITY_DIR.parent / "citations" / "manifest.jsonl"
SEEDS = AUTHORITY_DIR / "seeds"
REVIEW = AUTHORITY_DIR / "review"
CACHE = AUTHORITY_DIR / "cache"
CROSSREF = "https://api.crossref.org"
#: Real journals in the candidate's field; verified against the Crossref
#: registry at build time — anything not found is dropped with a warning.
FIELD_JOURNALS = [
    "Hugoye: Journal of Syriac Studies",
    "Journal of Early Christian Studies",
    "Vigiliae Christianae",
    "Le Muséon",
    "Ostkirchliche Studien",
    "Journal of Theological Studies",
    "Journal of Ecclesiastical History",
    "Harvard Theological Review",
    "Digital Scholarship in the Humanities",
    "Digital Humanities Quarterly",
]
PERSON_SAMPLE_TARGET = 380
WORK_SAMPLE_CAP = 120
TODAY = "2026-10-17"


def bench_config() -> IngestionConfig:
    return IngestionConfig(
        online_enrich=True,
        online_enrich_providers=("crossref", "orcid", "wikidata", "geonames"),
        online_enrich_timeout=30.0,
        online_enrich_cache_ttl=30 * 86400,
    )


def _client() -> RegistryClient:
    CACHE.mkdir(exist_ok=True)
    return RegistryClient(bench_config(), CACHE)


def _year(csl: dict) -> int | None:
    try:
        value = csl["issued"]["date-parts"][0][0]
        return int(value) if isinstance(value, int) else None
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def library_entities() -> tuple[list[dict], list[dict], list[dict], list[str]]:
    """Extract authors, works, journals and places from the pilot library manifest."""
    authors: list[dict] = []
    works: list[dict] = []
    journals: list[str] = []
    places: list[str] = []
    seen_authors: set[str] = set()
    with open(MANIFEST, encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if row.get("exclusion_reason"):
                continue
            csl = row.get("csl") or {}
            for person in csl.get("author") or []:
                if not isinstance(person, dict):
                    continue
                key = json.dumps(person, sort_keys=True)
                if any(person.get(field) for field in ("family", "literal")) and key not in seen_authors:
                    seen_authors.add(key)
                    authors.append(person)
            if csl.get("title") and len(works) < 60:
                works.append({"title": csl["title"],
                              "author": (csl.get("author") or [{}])[0].get("family", ""),
                              "year": _year(csl), "id": row.get("id")})
            if isinstance(csl.get("container-title"), str) and csl["container-title"] not in journals:
                journals.append(csl["container-title"])
            if isinstance(csl.get("publisher-place"), str) and csl["publisher-place"] not in places:
                places.append(csl["publisher-place"])
    return authors, works, journals, places


def _orcid_family_pool(family: str) -> list[dict]:
    """Wider family-only ORCID pool for human review (fielded, rows=10, cache-bypassing)."""
    for attempt in range(3):
        time.sleep(2.0 * attempt)
        try:
            response = requests.get("https://pub.orcid.org/v3.0/expanded-search/",
                                    params={"q": f'family-name:"{family}"', "rows": 10},
                                    headers={"Accept": "application/json"}, timeout=20)
            if response.status_code == 200:
                items = response.json().get("expanded-result") or []
                if items:
                    return [normalize_orcid_person(item) for item in items[:10]]
        except requests.RequestException:
            continue
    return []


def dump_review_candidates(authors: list[dict], places: list[str]) -> None:
    REVIEW.mkdir(exist_ok=True)
    # one client per query: the RegistryClient budget is per-document (12 requests,
    # one deadline), so a benchmark loop must not share a single instance.
    with open(REVIEW / "orcid_candidates.jsonl", "w", encoding="utf-8") as out:
        for person in authors:
            family = str(person.get("family") or person.get("literal") or "").strip()
            given = str(person.get("given") or "").strip() or None
            results = _client().search("orcid", family, author=given)
            candidates = [r.get("candidate") for r in results if r.get("status") == "found"]
            # Fielded given-name queries are strict; also dump the family-only
            # pool so the human reviewer can judge initials/variants (e.g. "S. P." Brock).
            if not candidates and given and person.get("family"):
                candidates = _orcid_family_pool(person["family"])
            out.write(json.dumps({"person": person, "orcid_candidates": candidates}, ensure_ascii=False) + "\n")
    with open(REVIEW / "place_candidates.jsonl", "w", encoding="utf-8") as out:
        for place in places:
            results = _client().search("wikidata", place)
            candidates = [r.get("candidate") for r in results if r.get("status") == "found"]
            out.write(json.dumps({"place": place, "wikidata_candidates": candidates}, ensure_ascii=False) + "\n")


def label_library_works(works: list[dict]) -> list[dict]:
    """Crossref bibliographic search; accept only fuzzy-title + author/year agreement."""
    labelled: list[dict] = []
    for work in works:
        results = _client().search("crossref", work["title"], author=work["author"] or None, year=work["year"])
        best = None
        for result in results:
            candidate = result.get("candidate") or {}
            if not candidate.get("DOI"):
                continue
            similarity = fuzzy_similarity(work["title"], str(candidate.get("title") or ""))
            families = " ".join(str(a.get("family") or a.get("literal") or "") for a in candidate.get("author") or [])
            year_match = candidate.get("issued", {}).get("date-parts", [[None]])[0][0] == work["year"]
            if similarity >= 0.8 and (work["author"] and work["author"].casefold() in families.casefold() or year_match):
                best = {"doi": candidate["DOI"], "similarity": round(similarity, 4)}
                break
        if best:
            labelled.append({**work, "doi": best["doi"], "similarity": best["similarity"],
                             "provenance": f"crossref-bibliographic-match:{best['doi']} (title fuzzy {best['similarity']}, {TODAY})"})
    return labelled


def verify_journals(titles: list[str]) -> list[dict]:
    """ISSN labels verified via deposited works (the /journals index truncates titles)."""
    verified: list[dict] = []
    for title in titles:
        time.sleep(1.1)
        try:
            response = requests.get(
                f"{CROSSREF}/works",
                params={"query.bibliographic": title, "rows": 5,
                        "select": "DOI,title,container-title,ISSN"},
                timeout=20)
            response.raise_for_status()
            items = response.json().get("message", {}).get("items", [])
        except requests.RequestException:
            items = []
        for item in items:
            container = ((item.get("container-title") or [""]) or [""])[0]
            issns = item.get("ISSN") or []
            if container and issns and fuzzy_similarity(title, container) >= 0.9:
                verified.append({"title": container, "issn": issns[0],
                                 "provenance": f"crossref-container-title:{issns[0]} (work {item.get('DOI')}, {TODAY})"})
                break
        else:
            print(f"  journal not verified in Crossref: {title!r}")
    return verified


def sample_field(journals: list[dict]) -> tuple[list[dict], list[dict]]:
    """Persons labelled by ORCID assertions + works labelled by their own DOI."""
    persons: dict[str, dict] = {}
    works: list[dict] = []
    for journal in journals:
        pages = 0
        offset = 0
        while len(persons) < PERSON_SAMPLE_TARGET and pages < 3:
            time.sleep(1.2)
            try:
                response = requests.get(
                    f"{CROSSREF}/works",
                    params={"filter": f"issn:{journal['issn']},has-orcid:true,from-pub-date:2016-01-01",
                            "rows": 100, "offset": offset,
                            "select": "DOI,title,author,container-title,issued"},
                    timeout=30)
                response.raise_for_status()
                items = response.json().get("message", {}).get("items", [])
            except requests.RequestException as exc:
                print(f"  crossref sampling failed for {journal['title']}: {type(exc).__name__}")
                break
            if not items:
                break
            for record in items:
                doi = record.get("DOI")
                title = (record.get("title") or [""])[0]
                year = ((record.get("issued") or {}).get("date-parts") or [[None]])[0][0]
                if doi and title and len(works) < WORK_SAMPLE_CAP:
                    works.append({"title": title, "author": (record.get("author") or [{}])[0].get("family", ""),
                                  "year": year, "doi": doi,
                                  "provenance": f"crossref-deposit:{doi} ({TODAY})"})
                for author in record.get("author") or []:
                    orcid = str(author.get("ORCID") or "").rsplit("/", 1)[-1] or None
                    if orcid and author.get("family"):
                        persons.setdefault(orcid, {
                            "family": author["family"], "given": author.get("given") or "",
                            "orcid": orcid,
                            "provenance": f"orcid-asserted-in-crossref-deposit:{doi}"
                                          + (" (authenticated)" if author.get("authenticated-orcid") else "")
                                          + f" ({journal['title']})"})
            pages += 1
            offset += 100
        if len(persons) >= PERSON_SAMPLE_TARGET:
            break
    return list(persons.values()), works


def load_verified(filename: str) -> list[dict]:
    path = SEEDS / filename
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def emit(rows: list[dict]) -> None:
    with open(AUTHORITY_DIR / "gold.jsonl", "w", encoding="utf-8") as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")


def build(emit_gold: bool) -> None:
    authors, library_works, library_journals, places = library_entities()
    print(f"library: {len(authors)} authors, {len(library_works)} works, "
          f"{len(library_journals)} journals, {len(places)} places")

    premodern = load_verified("premodern.jsonl")
    verified_orcids = load_verified("verified_orcids.jsonl")
    verified_places = load_verified("verified_places.jsonl")

    dump_review_candidates(authors, places)
    print(f"review candidates written: {REVIEW}/ (verify by hand into seeds/verified_*.jsonl)")

    verified_journal_count = len(library_journals)
    journals = verify_journals(list(dict.fromkeys(library_journals + FIELD_JOURNALS)))
    sampled_persons, sampled_works = sample_field(journals)
    labelled_works = label_library_works(library_works)
    print(f"verified journals: {len(journals)} (library {verified_journal_count} + field)")
    print(f"sampled persons: {len(sampled_persons)}; sampled works: {len(sampled_works)}")
    print(f"labelled library works: {len(labelled_works)}")
    print(f"verified library authors: {len(verified_orcids)}; verified places: {len(verified_places)}")

    rows: list[dict] = []
    for seed in premodern:
        rows.append({"entity_type": "person", "source": {"literal": seed["surface"]},
                     "label_status": "absent", "label": {"system": "orcid", "id": None},
                     "script": seed["script"], "origin": "premodern-seed",
                     "provenance": f"pre-modern person (d. {seed['death_year']}); ORCID registration not expected"})
    for verified in verified_orcids:
        rows.append({"entity_type": "person", "source": verified["person"],
                     "label_status": "present", "label": {"system": "orcid", "id": verified["orcid"]},
                     "origin": "library-author", "provenance": f"hand-verified ORCID profile ({TODAY}): {verified['note']}"})
    for person in sampled_persons:
        rows.append({"entity_type": "person", "source": {"family": person["family"], "given": person["given"]},
                     "label_status": "present", "label": {"system": "orcid", "id": person["orcid"]},
                     "origin": "field-sample", "provenance": person["provenance"]})
    for work in sampled_works + labelled_works:
        rows.append({"entity_type": "work", "source": {"title": work["title"], "author": work["author"],
                                                       "year": work["year"]},
                     "label_status": "present", "label": {"system": "doi", "id": work["doi"]},
                     "origin": "field-sample" if work in sampled_works else "library-work",
                     "provenance": work["provenance"]})
    for journal in journals:
        rows.append({"entity_type": "journal", "source": {"title": journal["title"]},
                     "label_status": "present", "label": {"system": "issn", "id": journal["issn"]},
                     "origin": "journal-registry", "provenance": journal["provenance"]})
    for place in verified_places:
        rows.append({"entity_type": "place", "source": {"literal": place["place"]},
                     "label_status": "present", "label": {"system": "wikidata", "id": place["wikidata"]},
                     "origin": "library-place", "provenance": f"hand-verified Wikidata item ({TODAY}): {place['note']}"})

    counters: dict[str, int] = {}
    for row in rows:
        counters[row["entity_type"]] = counters.get(row["entity_type"], 0) + 1
        row["id"] = f"{row['entity_type']}-{counters[row['entity_type']]:04d}"
    if emit_gold:
        emit(rows)
    total = len(rows)
    print(f"gold rows assembled: {total} ({counters})" + (" → gold.jsonl" if emit_gold else " (dry-run)"))
    if total < 500 and emit_gold:
        print("WARNING: below the 500-row acceptance floor")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--emit", action="store_true", help="write gold.jsonl (default: dry-run)")
    args = parser.parse_args()
    build(emit_gold=args.emit)


if __name__ == "__main__":
    main()