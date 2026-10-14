"""Explicit, process-isolated plugins for completed CiteIndex corpora."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

ENTRY_POINT_GROUP = "citeindex.plugins.v1"
PROTOCOL_VERSION = 1


def _entry_points() -> dict:
    plugins = {}
    for entry in metadata.entry_points(group=ENTRY_POINT_GROUP):
        if entry.name in plugins:
            raise ValueError(f"Duplicate plugin name: {entry.name}")
        plugins[entry.name] = entry
    return plugins


def _plugin_info(entry) -> dict:
    return {
        "name": entry.name,
        "distribution": entry.dist.metadata["Name"] if entry.dist else None,
        "version": entry.dist.version if entry.dist else None,
    }


def discover_plugins() -> list[dict]:
    """List package registrations without importing plugin code."""
    return [_plugin_info(entry) for _, entry in sorted(_entry_points().items())]


def _read_json(path: Path) -> dict:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def _write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _library_paths(document: Path) -> set[Path]:
    # Include the legacy adapter's fallback locations as well as the normal sibling library.
    return {
        document.parent.parent / "library" / f"{document.name}.md",
        document.parent / "library" / f"{document.name}.md",
        Path.cwd() / "corpus" / "library" / f"{document.name}.md",
        Path.cwd() / "library" / f"{document.name}.md",
    }


def _source_hashes(document: Path) -> dict[str, str]:
    paths = [p for p in document.rglob("*") if p.is_file()]
    paths.extend(p for p in _library_paths(document) if p.is_file())
    return {str(p.resolve()): _digest(p) for p in sorted(set(paths))}


def _output_destination(path: Path, document: Path, sources: dict) -> Path:
    if path.is_symlink():
        raise ValueError("Output destination cannot be a symlink")
    path = path.resolve()
    if path == document or path.is_relative_to(document) or document.is_relative_to(path):
        raise ValueError("Output must be separate from the source corpus")
    if any(Path(source) == path or Path(source).is_relative_to(path) for source in sources):
        raise ValueError("Output overlaps source evidence or library Markdown")
    if path.exists() and (not path.is_dir() or any(path.iterdir())):
        raise ValueError(f"Output destination already exists and is not empty: {path}")
    return path


def _run_worker(name: str, request_path: Path, result_path: Path, timeout: float) -> None:
    # Files carry the protocol so noisy third-party stdout cannot corrupt results.
    with tempfile.TemporaryFile() as logs:
        process = subprocess.Popen(
            [sys.executable, "-m", "citeindex.plugins", "--worker", name,
             str(request_path), str(result_path)],
            stdout=logs, stderr=logs, start_new_session=os.name == "posix",
        )
        try:
            process.wait(timeout=timeout)
        except BaseException as exc:
            if os.name == "posix":
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            else:
                process.kill()
            process.wait()
            if isinstance(exc, subprocess.TimeoutExpired):
                raise TimeoutError(f"Plugin {name!r} exceeded {timeout:g} seconds") from exc
            raise
        if process.returncode:
            # Do not echo provider logs: they may contain credentials or document text.
            raise RuntimeError(f"Plugin {name!r} worker failed (exit {process.returncode})")


def _validate_result(result: dict, staging: Path) -> dict[str, str]:
    if staging.is_symlink() or not staging.is_dir():
        raise RuntimeError("Plugin output directory was replaced or is a symlink")
    if type(result.get("protocol_version")) is not int or result["protocol_version"] != PROTOCOL_VERSION:
        raise RuntimeError("Unsupported plugin result protocol version")
    if not isinstance(result.get("status"), str) or result["status"] not in {"ok", "partial", "failed"}:
        raise RuntimeError("Invalid plugin result status")
    for field in ("errors", "warnings"):
        if not isinstance(result.get(field, []), list) or any(not isinstance(item, dict) for item in result.get(field, [])):
            raise RuntimeError(f"Plugin {field} must be a list of objects")
    if not isinstance(result.get("qa_summary", {}), dict):
        raise RuntimeError("Plugin QA summary must be an object")
    if result["status"] == "ok" and result.get("errors"):
        raise RuntimeError("Plugin reported success with errors")
    artifacts = result.get("artifacts")
    if not isinstance(artifacts, list) or any(not isinstance(p, str) or not p for p in artifacts):
        raise RuntimeError("Plugin artifacts must be relative file paths")
    if result["status"] != "failed" and not artifacts:
        raise RuntimeError("Plugin produced no artifacts")
    if len({Path(name).as_posix() for name in artifacts}) != len(artifacts):
        raise RuntimeError("Duplicate plugin artifact paths")
    for path in staging.rglob("*"):
        if path.is_symlink() or (not path.is_file() and not path.is_dir()):
            raise RuntimeError("Plugin output contains a symlink or unsupported file")
    hashes = {}
    for name in artifacts:
        path = Path(name)
        if path.is_absolute() or ".." in path.parts or path == Path("plugin_result.json"):
            raise RuntimeError("Unsafe plugin artifact path")
        target = staging / path
        if not target.resolve().is_relative_to(staging.resolve()) or not target.is_file():
            raise RuntimeError(f"Missing or unsafe plugin artifact: {name}")
        hashes[name] = _digest(target)
    actual = {p.relative_to(staging).as_posix() for p in staging.rglob("*") if p.is_file()}
    if actual != {Path(p).as_posix() for p in artifacts}:
        raise RuntimeError("Plugin output contains unreported artifacts")
    return hashes


def _safe_options(value):
    sensitive = ("secret", "password", "credential", "key", "token", "authorization")
    if isinstance(value, dict):
        return {key: _safe_options(item) for key, item in value.items()
                if not any(word in key.lower() for word in sensitive)}
    if isinstance(value, list):
        return [_safe_options(item) for item in value]
    return value


def run_plugin(
    name: str,
    document_path: str | Path,
    *,
    output_dir: str | Path,
    options: dict | None = None,
    timeout: float = 3600,
) -> dict:
    """Run one installed plugin; publish derived output without changing ingestion."""
    if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    if options is None:
        options = {}
    if not isinstance(options, dict):
        raise ValueError("Plugin options must be an object")
    json.dumps(options, allow_nan=False)
    plugins = _entry_points()
    if name not in plugins:
        raise ValueError(f"Plugin {name!r} is unavailable; install its package in this environment")
    document = Path(document_path).resolve()
    try:
        csl = _read_json(document / "csl.json")
        ingestion = _read_json(document / "ingestion_output.json")
    except (OSError, ValueError) as exc:
        raise ValueError("Plugin input requires readable csl.json and ingestion_output.json") from exc
    if ingestion.get("status") != "ok":
        raise ValueError("Plugin input must be a completed, successful ingestion")
    sources = _source_hashes(document)
    destination = _output_destination(Path(output_dir), document, sources)
    partial_destination = _output_destination(destination.with_name(destination.name + ".partial"), document, sources)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=f".{destination.name}.", dir=destination.parent) as temporary:
        work = Path(temporary)
        staging = work / "output"
        staging.mkdir()
        request_path, result_path = work / "request.json", work / "result.json"
        _write_json(request_path, {
            "protocol_version": PROTOCOL_VERSION,
            "document_path": str(document),
            "output_dir": str(staging),
            "options": options,
        })
        _run_worker(name, request_path, result_path, timeout)
        try:
            result = _read_json(result_path)
        except (OSError, ValueError) as exc:
            raise RuntimeError("Plugin worker returned missing or malformed JSON") from exc
        hashes = _validate_result(result, staging)
        if _source_hashes(document) != sources:
            raise RuntimeError("Source artifacts changed during translation; output was not published")
        if result["status"] == "failed":
            return result
        target = partial_destination if result["status"] == "partial" else destination
        _output_destination(target, document, sources)
        manifest_options = result.pop("manifest_options", {})
        if not isinstance(manifest_options, dict):
            raise RuntimeError("Plugin manifest_options must be an object")
        manifest = {
            "protocol_version": PROTOCOL_VERSION,
            "plugin": _plugin_info(plugins[name]),
            "source": {"document_path": str(document), "csl_id": csl.get("id"), "files": sources},
            "created_at": datetime.now(UTC).isoformat(),
            "options": _safe_options(manifest_options),
            "result": result,
            "artifacts": hashes,
        }
        _write_json(staging / "plugin_result.json", manifest)
        # Atomic rename publishes the complete directory; a nonempty concurrent output is protected.
        staging.rename(target)
        return {**result, "output_dir": str(target), "manifest_path": str(target / "plugin_result.json")}


def _worker(name: str, request_path: str, result_path: str) -> None:
    request = _read_json(Path(request_path))
    if request.get("protocol_version") != PROTOCOL_VERSION:
        raise ValueError("Unsupported plugin request protocol version")
    entry = _entry_points().get(name)
    if entry is None:
        raise ValueError(f"Plugin {name!r} is unavailable")
    with contextlib.redirect_stdout(sys.stderr):
        result = entry.load()(request)
    if not isinstance(result, dict):
        raise ValueError("Plugin must return an object")
    _write_json(Path(result_path), result)


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["--worker"]:
        if len(args) != 4:
            raise ValueError("Invalid worker arguments")
        _worker(*args[1:])
        return
    parser = argparse.ArgumentParser(prog="citeindex-plugin", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="List installed plugins without loading them")
    run = commands.add_parser("run", help="Process an already ingested corpus")
    run.add_argument("plugin")
    run.add_argument("document_path")
    run.add_argument("--output", required=True)
    run.add_argument("--options", type=Path, help="Plugin options JSON file")
    run.add_argument("--timeout", type=float, default=3600)
    parsed = parser.parse_args(args)
    try:
        if parsed.command == "list":
            output = discover_plugins()
        else:
            output = run_plugin(parsed.plugin, parsed.document_path, output_dir=parsed.output,
                                options=_read_json(parsed.options) if parsed.options else {}, timeout=parsed.timeout)
    except (OSError, ValueError, RuntimeError, TimeoutError) as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False))
        raise SystemExit(1) from exc
    print(json.dumps(output, ensure_ascii=False, indent=2))
    raise SystemExit(0 if parsed.command == "list" or output["status"] == "ok" else 1)


if __name__ == "__main__":
    main()
