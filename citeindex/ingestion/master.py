import hashlib
import json
import logging
import os
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

from .citation_verification import build_quotation_locators, verify_citation_metadata
from .csl import normalize_cataloguing_fields
from .deterministic import hash_payload
from .markdown_export import write_library_markdown
from .models import OUTPUT_MODES, IngestionConfig, IngestionFailure, IngestionLogEntry, PipelineResult
from .online_enrichment import build_authority_links, enrich_metadata
from .pipelines.common import parse_author_from_filename, prompt_author_interactively, validate_authors
from .registry_fill import canonical_isbn, fill_from_registry, find_identifiers
from .storage import (
    append_jsonl,
    csl_folder_name,
    ensure_dir,
    publish_staged_record,
    store_corpus_artifacts,
    write_json,
)
from .title_guard import reject_filename_title
from .url_security import UnsafeUrlError, validate_public_url

logger = logging.getLogger(__name__)

# Extensions supported for conversion (merged from legacy utils.py)
_OFFICE_EXTENSIONS = {".docx", ".doc", ".rtf", ".odt", ".pptx", ".ppt", ".odp"}
_DJVU_EXTENSIONS = {".djvu"}
_MEDIA_EXTENSIONS = {".mp3", ".wav", ".m4a", ".mp4", ".mkv", ".webm"}

# Payload keys a mode does not persist.  The values are still computed (the
# Merkle tree and nodes feed ``csl.json``'s identity); storage.py writes only
# the keys it receives, so a mode is expressed by dropping keys here.
_MODE_REMOVED_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "full": (),
    "csl_evidence": ("document_json", "merkle_tree", "pageindex_tree"),
    "csl_only": (
        "document_json",
        "merkle_tree",
        "pageindex_tree",
        "source_blocks",
        "layout_features",
    ),
}


def apply_output_mode(artifacts: dict[str, Any], mode: str) -> dict[str, Any]:
    """Return the artifact payload a mode persists, leaving ``artifacts`` intact.

    ``full`` persists everything.  ``csl_evidence`` keeps the CSL plus the
    evidence sidecars and drops the document model, its Merkle proof and the
    PageIndex tree.  ``csl_only`` keeps only the CSL and the run summary.
    """
    if mode not in OUTPUT_MODES:
        raise ValueError(f"output_mode must be one of {', '.join(OUTPUT_MODES)}; got {mode!r}")
    removed = _MODE_REMOVED_ARTIFACTS[mode]
    if not removed:
        return dict(artifacts)
    return {key: value for key, value in artifacts.items() if key not in removed}


# A scoped run (``pages`` set) reads only a page window, so the page Merkle root
# no longer describes the whole file.  Identity moves to the source bytes plus
# the canonical page window instead; the pipeline therefore builds no nodes and
# no Merkle tree at all.


def file_sha256(path: str) -> str:
    """Stream a file through SHA-256 in 1 MB chunks."""
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def resolve_page_scope(pdf_path: str, pages: str) -> str:
    """Render a page selection against this document as a canonical range."""
    import fitz

    from ..utils import canonical_page_range, parse_page_range

    with fitz.open(pdf_path) as doc:
        total_pages = doc.page_count
    selected = parse_page_range(pages, total_pages)
    if not selected:
        raise ValueError(
            f"pages {pages!r} selects no page in a {total_pages}-page document"
        )
    return canonical_page_range(selected)



class _RegistryFillClient:
    """One-shot, short-timeout adapter over ``RegistryClient`` for registry fill.

    The fill path is not a research loop: each identifier gets exactly one
    attempt inside a five-second budget, and any miss or error yields ``None``
    so the extracted record is left as printed.
    """

    def __init__(self, config: IngestionConfig, *, cache_dir: str | None = None, timeout: float = 5.0):
        from .metadata_registry import RegistryClient

        settings = {
            "online_enrich_timeout": min(5.0, float(timeout)),
            "online_enrich_providers": tuple(config.online_enrich_providers),
            "online_enrich_cache_ttl": float(config.online_enrich_cache_ttl),
            "crossref_enabled": bool(config.crossref_enabled),
            "openlibrary_enabled": bool(config.openlibrary_enabled),
            "offline_verification": bool(config.offline_verification),
            "registry_contact_email": config.registry_contact_email,
        }
        self._client = RegistryClient(settings, cache_dir=cache_dir)

    def _lookup(self, provider: str, identifier: str) -> dict[str, Any] | None:
        result = self._client.lookup(provider, identifier)
        if not isinstance(result, Mapping) or result.get("status") != "found":
            return None
        candidate = result.get("candidate")
        return dict(candidate) if isinstance(candidate, Mapping) else None

    def lookup_isbn(self, isbn: str) -> dict[str, Any] | None:
        return self._lookup("openlibrary", canonical_isbn(isbn) or isbn)

    def lookup_doi(self, doi: str) -> dict[str, Any] | None:
        return self._lookup("crossref", doi)


class CiteIndexIngestionOrchestrator:
    def __init__(self, corpus_root: str = "corpus", schema_version: str = "1.0.0"):
        self.corpus_root = os.path.abspath(corpus_root)
        self.schema_version = schema_version
        ensure_dir(self.corpus_root)

    def ingest(
        self,
        input_ref: str,
        config: IngestionConfig | None = None,
    ) -> dict[str, Any]:
        cfg = config or IngestionConfig()
        cleanup_paths: list[str] = []

        def cleanup_temporary_outputs() -> None:
            for path in cleanup_paths:
                try:
                    if os.path.isdir(path):
                        shutil.rmtree(path, ignore_errors=True)
                    elif os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    logger.warning("Failed to clean temporary output: %s", path, exc_info=True)

        try:
            cfg.__post_init__()
            logger.info("Ingesting: %s", input_ref)
            resource_type, normalized = self.detect_resource_type(input_ref, config=cfg)
            logger.info("Detected resource type: %s", resource_type)
            if resource_type == "unsupported":
                return self._failure(
                    stage="detect_resource_type",
                    source_id="unknown",
                    error_code="unsupported_input",
                    error_message=f"Unsupported input: {input_ref}",
                    next_action="Provide PDF, URL, or media file",
                )

            original_sha256 = file_sha256(input_ref) if os.path.isfile(input_ref) else None
            # Phase 1: Office/DJVU → convert to PDF before routing
            temp_pdf = None
            if resource_type == "office_document":
                temp_pdf = self._convert_office_to_pdf(normalized, cfg)
                if not temp_pdf:
                    return self._failure(
                        stage="office_conversion",
                        source_id="unknown",
                        error_code="conversion_failed",
                        error_message=f"Failed to convert Office document: {input_ref}",
                        next_action="Ensure LibreOffice is installed",
                    )
                resource_type = self._pdf_kind(
                    temp_pdf,
                    force_kind=cfg.force_pdf_kind,
                    strip_existing_ocr=cfg.strip_existing_ocr,
                )
                normalized = temp_pdf

            elif resource_type == "djvu_document":
                temp_pdf = self._convert_djvu_to_pdf(normalized, cfg)
                if not temp_pdf:
                    return self._failure(
                        stage="djvu_conversion",
                        source_id="unknown",
                        error_code="conversion_failed",
                        error_message=f"Failed to convert DJVU document: {input_ref}",
                        next_action="Ensure djvulibre-bin is installed",
                    )
                resource_type = self._pdf_kind(
                    temp_pdf,
                    force_kind=cfg.force_pdf_kind,
                    strip_existing_ocr=cfg.strip_existing_ocr,
                )
                normalized = temp_pdf

            try:
                logger.info("Routing to pipeline: %s", resource_type)
                # A scoped run reads only a page window, so its identity is the
                # source bytes plus that window: the digest is taken once here
                # and the canonical range is resolved against this document.
                page_scope = None
                source_sha256 = None
                if cfg.pages and resource_type in {"digital_pdf", "scanned_pdf"}:
                    page_scope = resolve_page_scope(normalized, cfg.pages)
                    source_sha256 = file_sha256(normalized)
                    logger.info(
                        "Scoped page ingestion: pages=%s (of %s), source_sha256=%s",
                        page_scope,
                        cfg.pages,
                        source_sha256[:12],
                    )
                # PageIndex is the largest LLM cost a reduced mode removes.  A
                # scoped run never needs the section tree either.  The caller's
                # config is never mutated; routing gets a copy with the stage
                # switched off.  The Merkle tree and nodes are unaffected.
                keep_pageindex = cfg.output_mode == "full" and not cfg.pages
                pipeline_cfg = cfg if keep_pageindex else replace(cfg, use_pageindex=False)
                sub_result = self.route_to_pipeline(resource_type, normalized, pipeline_cfg)
                # Stage 1 layout cues: captured next to the text stage and
                # persisted as a sidecar, never fed back into extraction.
                layout_features = self._layout_features(normalized, resource_type, sub_result)
                for key in ("source_snapshot_path", "_images_tmpdir"):
                    path = sub_result.extra.get(key)
                    if (
                        isinstance(path, str)
                        and sub_result.extra.get("cleanup_source_snapshot" if key == "source_snapshot_path" else key)
                        is not None
                    ):
                        cleanup_paths.append(path)
            finally:
                # Clean up temp PDF from Office/DJVU conversion
                if temp_pdf and os.path.exists(temp_pdf):
                    os.remove(temp_pdf)
                    logger.info("Removed temporary conversion file: %s", temp_pdf)

            # ── Author enrichment fallback ────────────────────────────
            # First validate LLM host-metadata authors (detect garbage extraction)
            candidate_csl = dict(sub_result.csl_json)
            existing_authors = candidate_csl.get("author", [])
            validated_authors = validate_authors(existing_authors, input_ref) if existing_authors else None
            if resource_type in {"url_article", "media"}:
                from .csl import valid_host_value

                validated_authors = existing_authors if valid_host_value("author", existing_authors) else None

            if validated_authors:
                # Keep the cleaned authors
                candidate_csl["author"] = validated_authors
                logger.info("Validated authors: %s", validated_authors)
            elif not validated_authors and candidate_csl.get("author"):
                # All authors were garbage — clear them and try fallback
                candidate_csl.pop("author", None)

            if not candidate_csl.get("author") and resource_type in {"digital_pdf", "scanned_pdf"}:
                # 1. Try parsing from filename
                author_from_filename = parse_author_from_filename(input_ref)
                if author_from_filename:
                    candidate_csl["author"] = [author_from_filename]
                    logger.info("Author extracted from filename: %s", author_from_filename)
                else:
                    # 2. Interactive prompt
                    author_from_prompt = prompt_author_interactively()
                    if author_from_prompt:
                        candidate_csl["author"] = author_from_prompt
                        logger.info("Author provided interactively: %s", author_from_prompt)

            registry_client = None
            if (
                (cfg.online_enrich or cfg.enrich_proposal)
                and resource_type in {"digital_pdf", "scanned_pdf"}
                and input_ref.lower().endswith(".pdf")
            ):
                from .metadata_registry import RegistryClient

                registry_client = RegistryClient(cfg, cache_dir=os.path.join(self.corpus_root, ".registry-cache"))
            citation_verification = None
            if cfg.verify_citations:
                registry_kwargs = {"registry_client": registry_client} if registry_client is not None else {}
                candidate_csl, citation_verification = verify_citation_metadata(
                    candidate_csl,
                    sub_result.document_json,
                    resource_type,
                    sub_result.extra,
                    cfg,
                    **registry_kwargs,
                )
                for correction in citation_verification.get("applied_corrections", []):
                    candidate_csl.setdefault("_field_evidence", {})[correction["field"]] = {
                        "quote": correction["quote"],
                        "locator": correction["locator"],
                        **(
                            {"block_id": correction["locator"]["block_id"]}
                            if correction["locator"].get("block_id")
                            else {}
                        ),
                    }

            if cfg.repair_proposal:
                candidate_csl = self._apply_repair_proposal(
                    candidate_csl,
                    cfg.repair_proposal,
                    input_ref,
                    sub_result,
                )
                if citation_verification:
                    citation_verification["status"] = "needs_review"
                    citation_verification["verified"] = False
            candidate_csl, online_enrichment = enrich_metadata(
                candidate_csl,
                sub_result.document_json,
                resource_type,
                sub_result.extra,
                cfg,
                input_ref,
                client=registry_client,
            )
            sub_result.extra["online_enrichment"] = online_enrichment
            authority_links = build_authority_links(online_enrichment)
            if authority_links is not None:
                sub_result.extra["authority_links"] = authority_links
            if online_enrichment["decisions"]:
                if citation_verification:
                    citation_verification["status"] = "needs_review"
                    citation_verification["verified"] = False
                    citation_verification["superseded_by_enrichment"] = [
                        d["field"] for d in online_enrichment["decisions"]
                    ]
                for repair in sub_result.extra.get("citation_repair", {}).get("applied", []):
                    changes = [d for d in online_enrichment["decisions"] if d["field"] == repair["field"]]
                    if changes:
                        repair["superseded_by_enrichment"] = changes[-1]
            # Identifier-first fill runs last so it can only add what the
            # source and enrichment left empty (off by default).
            candidate_csl = self._apply_registry_fill(candidate_csl, sub_result, cfg, resource_type)
            standardized_csl = self.standardize_csl_json(
                candidate_csl,
                sub_result.merkle_tree or {},
                resource_type,
                source_sha256=source_sha256,
                page_scope=page_scope,
            )
            # A filename is not a title: drop a fallback title (e.g. a
            # ``<sha256>.pdf`` corpus filename) before the record is persisted.
            reject_filename_title(standardized_csl, input_ref)
            logger.info("Standardized CSL JSON for: %s", standardized_csl.get("title", "unknown"))

            # Finalize every embedded citation copy before any artifact is persisted.
            sub_result.csl_json = standardized_csl
            snapshot = sub_result.extra.get("source_snapshot_path")
            if isinstance(snapshot, str) and os.path.isfile(snapshot):
                sub_result.extra["source_identity"] = {
                    "sha256": file_sha256(snapshot),
                    "artifact": "source.html" if resource_type == "url_article" else "source.media",
                }
            elif original_sha256:
                if file_sha256(input_ref) != original_sha256:
                    raise ValueError("Original source changed during ingestion")
                sub_result.extra["source_identity"] = {"sha256": original_sha256}
            for quotation in sub_result.extra.get("quotation_locators", []):
                quotation["citation_item"]["id"] = standardized_csl["id"]
            if sub_result.document_json:
                sub_result.document_json.setdefault("metadata", {})["title"] = standardized_csl.get("title")
            tree = sub_result.extra.get("pageindex_tree")
            if tree:
                from .pipelines.pageindex_tree import _build_level0

                tree["level_0"] = _build_level0(
                    standardized_csl, sub_result.source_id, (sub_result.merkle_tree or {}).get("root")
                )
            redirect_html = []
            if resource_type == "url_article":
                for index, redirect in enumerate(sub_result.extra.get("redirect_snapshots", []), start=1):
                    redirect_html.append((f"source-redirect-{index}.html", redirect.pop("html")))
                    redirect["artifact"] = f"source-redirect-{index}.html"
            artifacts = sub_result.to_dict()
            artifacts["csl_json"] = standardized_csl
            if cfg.output_mode != "csl_only":
                quotation_locators = build_quotation_locators(
                    standardized_csl["id"], sub_result.extra.get("source_identity", {}).get("sha256"),
                    sub_result.document_json, resource_type, sub_result.extra.get("source_blocks", []),
                )
                if quotation_locators:
                    existing = artifacts.get("quotation_locators", []) if resource_type != "media" else []
                    artifacts["quotation_locators"] = [*existing, *quotation_locators]
            if layout_features is not None:
                artifacts["layout_features"] = layout_features
            if citation_verification:
                artifacts["citation_verification"] = citation_verification
            if cfg.pages:
                # A scoped record has neither a page Merkle proof nor a
                # section tree: its identity is the file digest and the window.
                artifacts.pop("merkle_tree", None)
                artifacts.pop("pageindex_tree", None)
            # Only the persistence of an artifact depends on the mode.
            artifacts = apply_output_mode(artifacts, cfg.output_mode)

            folder_name = csl_folder_name(standardized_csl)
            document_path = os.path.join(self.corpus_root, folder_name)
            staging_root = tempfile.mkdtemp(prefix=".citeindex-record-", dir=self.corpus_root)
            cleanup_paths.append(staging_root)
            staged_document = store_corpus_artifacts(staging_root, folder_name, artifacts)
            snapshot_path = sub_result.extra.get("source_snapshot_path")
            if resource_type == "url_article" and isinstance(snapshot_path, str) and os.path.isfile(snapshot_path):
                shutil.copy2(snapshot_path, os.path.join(staged_document, "source.html"))
                for filename, html in redirect_html:
                    with open(os.path.join(staged_document, filename), "w", encoding="utf-8") as sidecar:
                        sidecar.write(html)
            elif resource_type == "media" and isinstance(snapshot_path, str) and os.path.isfile(snapshot_path):
                shutil.copy2(snapshot_path, os.path.join(staged_document, "source.media"))
            log_entry = self.log_ingestion(input_ref, resource_type, standardized_csl, sub_result)

            # ── Copy extracted images to corpus dir ─────────────────
            images_tmpdir = sub_result.extra.get("_images_tmpdir")
            images_list = sub_result.extra.get("_images_list", [])
            if cfg.output_mode == "full" and images_tmpdir and images_list:
                try:
                    dest_images_dir = os.path.join(staged_document, "images")
                    if os.path.isdir(images_tmpdir):
                        for img_info in images_list:
                            # Images are in images_tmpdir/images/, not images_tmpdir/
                            src = os.path.join(images_tmpdir, "images", img_info["filename"])
                            if os.path.isfile(src):
                                os.makedirs(dest_images_dir, exist_ok=True)
                                shutil.copy2(src, os.path.join(dest_images_dir, img_info["filename"]))
                        logger.info("Copied %d images to %s", len(images_list), dest_images_dir)
                    # Clean up temp dir
                    shutil.rmtree(images_tmpdir, ignore_errors=True)
                except Exception:
                    logger.warning("Failed to copy extracted images", exc_info=True)
            elif images_tmpdir:
                # A reduced mode copies no images; the extraction temp dir must
                # not survive the run either.
                shutil.rmtree(images_tmpdir, ignore_errors=True)

            # Remove internal paths before serializing persisted pipeline output.
            for key in ("_images_tmpdir", "_images_list", "source_snapshot_path", "cleanup_source_snapshot"):
                sub_result.extra.pop(key, None)

            output = {
                "schema_version": self.schema_version,
                "status": "ok",
                "document_path": document_path,
                "standardized_csl_json": standardized_csl,
                "sub_pipeline_outputs": sub_result.to_dict(),
                "ingestion_log_entry": log_entry,
            }
            if citation_verification:
                output["citation_verification"] = citation_verification
            output["online_enrichment"] = online_enrichment

            rendered_citation = None
            if resource_type in {"digital_pdf", "scanned_pdf"}:
                from ..citation_style import format_bibliography
                from .csl import export_csl

                render_item = export_csl(standardized_csl)
                render_item.pop("custom", None)
                rendered_citation, _ = format_bibliography([render_item], cfg.citation_style)
                if not rendered_citation or rendered_citation.startswith("Error"):
                    rendered_citation = None
                    output["citation_render_warning"] = "CSL rendering unavailable; used deterministic fallback"

            if cfg.output_mode == "full":
                output["library_md_path"] = write_library_markdown(
                    corpus_root=self.corpus_root,
                    csl_json=standardized_csl,
                    document_json=sub_result.document_json,
                    transcript_json=sub_result.transcript_json,
                    resource_type=resource_type,
                    verification_status=citation_verification["status"] if citation_verification else None,
                    rendered_citation=rendered_citation,
                    authority_links=authority_links,
                    staged_path=os.path.join(staging_root, "library.md"),
                )
            write_json(os.path.join(staged_document, "ingestion_output.json"), output)
            try:
                publish_staged_record(
                    self.corpus_root, folder_name, staging_root,
                    remove_markdown=cfg.output_mode != "full",
                )
            except OSError:
                # Retain the staged backup if publication could not restore it.
                if os.path.lexists(os.path.join(staging_root, "previous-record")):
                    cleanup_paths.remove(staging_root)
                raise
            cleanup_temporary_outputs()
            return output
        except Exception as e:
            cleanup_temporary_outputs()
            return self._failure(
                stage="master_ingestion",
                source_id="unknown",
                error_code="ingestion_exception",
                error_message=str(e),
                next_action="Inspect stack trace and input file health",
            )

    def _apply_repair_proposal(
        self,
        csl: dict[str, Any],
        proposal_path: str,
        input_ref: str,
        sub_result: PipelineResult,
    ) -> dict[str, Any]:
        """Apply only quote-backed proposals, then use normal finalization."""
        with open(proposal_path, encoding="utf-8") as handle:
            payload = json.load(handle)
        from .citation_verification import _find_evidence, _text_items, valid_host_value, validate_block_evidence

        if not isinstance(payload, dict) or not isinstance(payload.get("proposals"), list):
            raise ValueError("repair proposal must contain a proposals array")
        source_path = sub_result.extra.get("source_snapshot_path") or input_ref
        if not os.path.isfile(source_path):
            raise ValueError("repair requires an available original source or URL snapshot")
        digest = hashlib.sha256()
        with open(source_path, "rb") as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(chunk)
        if payload.get("source_sha256") != digest.hexdigest():
            raise ValueError("repair proposal source digest does not match input")
        repaired, applied = dict(csl), []
        blocks = sub_result.extra.get("source_blocks", [])
        for proposal in payload["proposals"]:
            if not isinstance(proposal, dict) or proposal.get("status") != "proposed":
                raise ValueError("each repair entry must have status=proposed")
            field: Any = proposal.get("field")
            value: Any = proposal.get("proposed_value")
            if not valid_host_value(field, value):
                raise ValueError("invalid host metadata field or value")
            if "old_value" not in proposal or proposal["old_value"] != csl.get(field):
                raise ValueError("repair old_value does not match current extraction")
            locator, quote = proposal.get("locator"), proposal.get("quote")
            if not isinstance(locator, dict) or not isinstance(quote, str) or not quote.strip():
                raise ValueError("repair requires a quote and exact locator")
            if locator.get("block_id"):
                evidence = validate_block_evidence(
                    field, value, {"block_id": locator["block_id"], "quote": quote}, blocks
                )
                supported = evidence is not None and evidence["locator"] == locator
            else:
                # URL/document locators must resolve to actual text, never metadata or summaries.
                supported = any(
                    item["quote"] == quote and item["locator"] == locator and _find_evidence(value, [item])
                    for item in _text_items(sub_result.document_json, sub_result.resource_type)
                )
            if not supported:
                raise ValueError("repair quote/value/locator does not match original source evidence")
            if not proposal.get("reason"):
                raise ValueError("repair requires a reason identifying the host source")
            repaired[field] = value
            if "_field_status" in repaired:
                repaired["_field_status"] = {**repaired["_field_status"], field: "source-supported"}
            repaired.setdefault("_field_evidence", {})
            repaired["_field_evidence"] = dict(repaired["_field_evidence"])
            repaired["_field_evidence"][field] = {"quote": quote, "locator": locator}
            if locator.get("block_id"):
                repaired["_field_evidence"][field]["block_id"] = locator["block_id"]
            applied.append(proposal)
        sub_result.extra["citation_repair"] = {"source_sha256": digest.hexdigest(), "applied": applied}
        return repaired

    def _apply_registry_fill(
        self,
        csl: dict[str, Any],
        result: PipelineResult,
        config: IngestionConfig,
        resource_type: str,
    ) -> dict[str, Any]:
        """Fill source-empty fields from the authority behind a printed identifier.

        Off by default.  Registry failures are diagnostic only: the extracted
        record always survives so the artifact still persists.
        """
        if not config.registry_fill:
            return csl
        try:
            identifiers = find_identifiers(result.extra.get("source_blocks") or [])
        except Exception:
            logger.warning("Registry fill identifier scan failed; keeping extracted metadata", exc_info=True)
            return csl
        if not identifiers:
            logger.info("Registry fill: no ISBN or DOI printed in the front matter")
            return csl
        try:
            client = _RegistryFillClient(config, cache_dir=os.path.join(self.corpus_root, ".registry-cache"))
            record, provenance = fill_from_registry(
                csl,
                identifiers,
                client=client,
                doc_type=csl.get("type") or config.doc_type_override or resource_type,
            )
        except Exception:
            logger.warning("Registry fill failed; keeping extracted metadata", exc_info=True)
            return csl
        if not provenance and not record.get("_field_conflicts"):
            logger.info("Registry fill: no empty fields matched the registry record for %s", identifiers)
            return csl
        if provenance:
            record["_field_provenance"] = {**(csl.get("_field_provenance") or {}), **provenance}
        if record.get("_field_conflicts"):
            record["_field_conflicts"] = {
                **(csl.get("_field_conflicts") or {}),
                **record["_field_conflicts"],
            }
        logger.info("Registry fill added %d field(s): %s", len(provenance), ", ".join(sorted(provenance)))
        return record

    def detect_resource_type(self, input_ref: str, config: IngestionConfig | None = None) -> tuple[str, str]:
        parsed = urlparse(input_ref)
        if parsed.scheme in {"http", "https"} and parsed.netloc:
            try:
                # Lexical checks keep obvious private targets out of every route;
                # network fetches perform DNS-aware validation immediately before I/O.
                validate_public_url(input_ref, resolve=False)
            except UnsafeUrlError:
                return "unsupported", input_ref
            host = parsed.netloc.lower()
            if any(token in host for token in ["youtube", "youtu.be", "vimeo", "podcast", "soundcloud"]):
                return "media", input_ref
            return "url_article", input_ref

        if not os.path.exists(input_ref):
            return "unsupported", input_ref

        ext = os.path.splitext(input_ref.lower())[1]
        if ext == ".pdf":
            force_kind = config.force_pdf_kind if config else None
            return self._pdf_kind(
                input_ref,
                force_kind=force_kind,
                strip_existing_ocr=config.strip_existing_ocr if config else False,
            ), os.path.abspath(input_ref)
        if ext in _OFFICE_EXTENSIONS:
            return "office_document", os.path.abspath(input_ref)
        if ext in _DJVU_EXTENSIONS:
            return "djvu_document", os.path.abspath(input_ref)
        if ext in _MEDIA_EXTENSIONS:
            return "media", os.path.abspath(input_ref)
        return "unsupported", input_ref

    def _pdf_kind(
        self,
        pdf_path: str,
        force_kind: str | None = None,
        strip_existing_ocr: bool = False,
    ) -> str:
        """Classify a PDF as digital, scanned, or mixed.

        Reuses usable text layers, including searchable scans, and tolerates
        occasional image-only pages. Doubtful text falls back to OCR.

        Parameters
        ----------
        pdf_path : str
            Path to the PDF file.
        force_kind : str, optional
            Override classification. One of 'force_ocr', 'force_digital',
            'digital_pdf', 'scanned_pdf', 'mixed_pdf'.
        strip_existing_ocr : bool
            Reject existing OCR layers instead of reusing them.

        Returns
        -------
        str
            One of 'digital_pdf', 'scanned_pdf', or 'mixed_pdf'.
        """
        from .pdf_classifier import DocumentKind, classify_pdf

        logger.info("Classifying PDF: %s (force_kind=%s)", os.path.basename(pdf_path), force_kind)
        force_doc_kind = None
        if force_kind == "force_ocr":
            force_doc_kind = DocumentKind.SCANNED_PDF
        elif force_kind == "force_digital":
            force_doc_kind = DocumentKind.DIGITAL_PDF
        elif force_kind in ("digital_pdf", "scanned_pdf", "mixed_pdf"):
            force_doc_kind = DocumentKind(force_kind)

        classification = classify_pdf(
            pdf_path,
            force_kind=force_doc_kind,
            strip_existing_ocr=strip_existing_ocr,
        )
        kind = classification.document_kind.value
        # For mixed_pdf, route to scanned pipeline (OCR handles both text and images)
        if classification.document_kind == DocumentKind.MIXED_PDF:
            logger.info(
                "Mixed PDF detected (%d digital, %d scanned, %d mixed pages) — routing to scanned pipeline",
                classification.digital_page_count,
                classification.scanned_page_count,
                classification.mixed_page_count,
            )
            kind = "scanned_pdf"
        else:
            logger.info(
                "PDF classified as %s (%d digital, %d scanned, %d mixed, %d empty pages)",
                kind,
                classification.digital_page_count,
                classification.scanned_page_count,
                classification.mixed_page_count,
                classification.empty_page_count,
            )
        return kind

    def _convert_office_to_pdf(self, doc_path: str, config: IngestionConfig) -> str | None:
        """Convert Office document to PDF using legacy file_converter."""
        from ..file_converter import convert_to_pdf

        return convert_to_pdf(doc_path)

    def _convert_djvu_to_pdf(self, doc_path: str, config: IngestionConfig) -> str | None:
        """Convert DJVU document to PDF using legacy file_converter."""
        from ..file_converter import convert_to_pdf

        return convert_to_pdf(doc_path)

    def route_to_pipeline(
        self,
        resource_type: str,
        normalized_input: str,
        config: IngestionConfig,
    ) -> PipelineResult:
        if resource_type == "digital_pdf":
            from .pipelines import digital_pdf

            return digital_pdf.run(normalized_input, source_type="digital_pdf", config=config)
        if resource_type == "scanned_pdf":
            from .pipelines import scanned_pdf

            return scanned_pdf.run(normalized_input, config=config)
        if resource_type == "url_article":
            from .pipelines import url_article

            return url_article.run(normalized_input, config=config)
        if resource_type == "media":
            from .pipelines import media

            return media.run(normalized_input, config=config)
        raise ValueError(f"No route for resource type: {resource_type}")

    def _layout_features(
        self,
        pdf_path: str,
        resource_type: str,
        result: PipelineResult,
    ) -> dict[str, Any] | None:
        """Capture layout cues for every page of a PDF lane.

        Missing geometry is a non-event: the sidecar is optional, so any
        failure logs a warning and the ingestion continues unchanged.
        """
        if resource_type not in {"digital_pdf", "scanned_pdf"}:
            return None
        # Private OCR geometry handed over by the scanned pipeline; dropping it
        # here keeps it out of every later artifact and payload.
        content_list = result.extra.pop("_layout_content_list", None)
        try:
            from .pipelines.layout_features import extract_layout_features

            return extract_layout_features(
                pdf_path,
                pages=None,
                content_list=content_list if isinstance(content_list, list) else None,
            )
        except Exception:
            logger.warning("Layout feature capture failed; continuing without layout.json", exc_info=True)
            return None

    def standardize_csl_json(
        self,
        csl_json: dict[str, Any],
        merkle_tree: dict[str, Any],
        source_type: str,
        *,
        source_sha256: str | None = None,
        page_scope: str | None = None,
    ) -> dict[str, Any]:
        # Cataloguing semantics (publisher-place, subtitle, date precision) run
        # before hashing so the digest describes the CSL that is persisted.
        standardized = normalize_cataloguing_fields(csl_json)
        merkle_root = merkle_tree.get("root")
        if page_scope is not None:
            # Scoped run: the Merkle root covers every page and no longer
            # describes what was read, so identity is the file digest plus the
            # page window that was read from it.
            if not source_sha256:
                raise ValueError("a scoped page record needs the source file digest")
            content_hash = hash_payload(
                {
                    "csl": standardized,
                    "source_sha256": source_sha256,
                    "page_scope": page_scope,
                    "source_type": source_type,
                }
            )
            merkle_root = None
        else:
            # Artifact identity must follow the source content as well as its
            # citation metadata, otherwise a revised file overwrites its evidence.
            content_hash = hash_payload({"csl": standardized, "merkle_root": merkle_root, "source_type": source_type})

        standardized["id"] = content_hash[:16]
        standardized["content_hash"] = content_hash
        standardized["merkle_root"] = merkle_root
        standardized["source_type"] = source_type
        if page_scope is not None:
            standardized["source_sha256"] = source_sha256
            standardized["page_scope"] = page_scope
        standardized["ingestion_timestamp"] = datetime.now(UTC).replace(microsecond=0).isoformat()
        return standardized

    def log_ingestion(
        self,
        input_ref: str,
        resource_type: str,
        standardized_csl: dict[str, Any],
        result: PipelineResult,
    ) -> dict[str, Any]:
        entry = IngestionLogEntry(
            input_ref=input_ref,
            resource_type=resource_type,
            csl_id=standardized_csl.get("id", ""),
            merkle_root=(result.merkle_tree or {}).get("root", ""),
            ingestion_timestamp=standardized_csl.get("ingestion_timestamp", ""),
        ).to_dict()
        append_jsonl(os.path.join(self.corpus_root, "ingestion_log.jsonl"), entry)
        return entry

    # ------------------------------------------------------------------
    # Batch URL article ingestion (--all-url-article / --update-url-article)
    # ------------------------------------------------------------------

    _CONTENT_HASHES_FILE = "_url_content_hashes.json"

    def _load_content_hashes(self) -> dict[str, str]:
        """Load URL → content-hash mapping from corpus root."""
        path = os.path.join(self.corpus_root, self._CONTENT_HASHES_FILE)
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_content_hashes(self, hashes: dict[str, str]) -> None:
        """Persist URL → content-hash mapping to corpus root."""
        write_json(os.path.join(self.corpus_root, self._CONTENT_HASHES_FILE), hashes)

    def ingest_all_urls(
        self,
        root_url: str,
        config: IngestionConfig | None = None,
        update: bool = False,
        max_depth: int = 2,
        max_pages: int = 100,
    ) -> dict[str, Any]:
        """Crawl *root_url*, discover article links, and ingest each one.

        Parameters
        ----------
        root_url : str
            Starting page for the crawl (e.g. a site index or homepage).
        config : IngestionConfig, optional
            Shared ingestion config forwarded to each URL pipeline run.
        update : bool
            When True, fetch each page and compare its content hash with
            the stored hash.  Skip if unchanged; re-ingest if changed or
            new.  When False, ingest every discovered URL unconditionally.
        max_depth : int
            BFS crawl depth (default 2).
        max_pages : int
            Maximum pages the crawler will visit (default 100).

        Returns
        -------
        dict  Summary with ``discovered``, ``ingested``, ``skipped``,
              ``updated``, ``failed`` counts and per-URL results.
        """
        from .url_crawler import discover_article_urls, fetch_content_hash

        logger.info("Discovering article URLs from %s (depth=%d, max_pages=%d)", root_url, max_depth, max_pages)
        discovered = discover_article_urls(root_url, max_depth=max_depth, max_pages=max_pages)

        stored_hashes: dict[str, str] = {}
        if update:
            stored_hashes = self._load_content_hashes()
            logger.info("Update mode: %d stored content hashes", len(stored_hashes))

        results: list[dict[str, Any]] = []
        ingested_count = 0
        updated_count = 0
        skipped_count = 0
        failed_count = 0

        for idx, url in enumerate(discovered, start=1):
            # ── Update mode: compare content hash before ingesting ──
            if update:
                new_hash = fetch_content_hash(url)
                old_hash = stored_hashes.get(url)

                if new_hash and old_hash and new_hash == old_hash:
                    logger.info("[%d/%d] SKIP (unchanged): %s", idx, len(discovered), url)
                    skipped_count += 1
                    results.append({"url": url, "status": "unchanged"})
                    continue

                is_update = old_hash is not None
            else:
                new_hash = None
                is_update = False

            logger.info("[%d/%d] %s: %s", idx, len(discovered), "Updating" if is_update else "Ingesting", url)
            try:
                output = self.ingest(url, config=config)
                status = output.get("status", "unknown")
                if status == "ok":
                    if is_update:
                        updated_count += 1
                        results.append({"url": url, "status": "updated"})
                    else:
                        ingested_count += 1
                        results.append({"url": url, "status": status})

                    # Store content hash (compute now if not in update mode)
                    if new_hash is None:
                        new_hash = fetch_content_hash(url)
                    if new_hash:
                        stored_hashes[url] = new_hash
                else:
                    failed_count += 1
                    results.append({"url": url, "status": status})
            except Exception as exc:
                logger.error("Failed to ingest %s: %s", url, exc, exc_info=True)
                results.append({"url": url, "status": "error", "error": str(exc)})
                failed_count += 1

        # Persist content hashes after batch
        self._save_content_hashes(stored_hashes)

        summary = {
            "status": "blocked" if failed_count else "ok",
            "root_url": root_url,
            "discovered": len(discovered),
            "ingested": ingested_count,
            "updated": updated_count,
            "skipped": skipped_count,
            "failed": failed_count,
            "results": results,
        }
        logger.info(
            "Batch complete: discovered=%d ingested=%d updated=%d skipped=%d failed=%d",
            len(discovered),
            ingested_count,
            updated_count,
            skipped_count,
            failed_count,
        )
        return summary

    # ------------------------------------------------------------------

    def _failure(
        self,
        stage: str,
        source_id: str,
        error_code: str,
        error_message: str,
        next_action: str,
    ) -> dict[str, Any]:
        return IngestionFailure(
            status="blocked",
            stage=stage,
            source_id=source_id,
            error_code=error_code,
            error_message=error_message,
            next_action=next_action,
        ).to_dict()
