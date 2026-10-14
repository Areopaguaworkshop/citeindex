"""Exercise real entry-point discovery and the subprocess plugin boundary."""

import builtins
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from citeindex import plugins

FAKE_PLUGIN = '''
import builtins
import json
import os
from pathlib import Path
import sys
import time

def run(request):
    builtins._citeindex_plugin_state = "child"
    assert request["protocol_version"] == 1
    assert Path(request["document_path"]).is_absolute()
    assert Path(request["output_dir"]).is_absolute()
    print("third party stdout must not corrupt JSON transport")
    options = request["options"]
    mode = options.get("mode", "ok")
    output = Path(request["output_dir"])
    if mode == "timeout":
        time.sleep(30)
    if mode == "exception":
        raise RuntimeError("fake plugin exploded")
    if mode == "malformed":
        Path(sys.argv[-1]).write_text("{broken JSON")
        os._exit(0)
    if mode == "mutate":
        Path(options["source_file"]).write_text("concurrent update")
    artifact = "translation.mdx"
    (output / artifact).write_text("# Source / Translation\\n")
    if mode == "symlink":
        (output / artifact).unlink()
        (output / artifact).symlink_to(Path(request["document_path"]) / "csl.json")
    if mode == "extra_symlink":
        (output / "unlisted").symlink_to(Path(request["document_path"]) / "csl.json")
    if mode == "traversal":
        artifact = "../escaped.mdx"
    if mode == "absolute":
        artifact = str(output / artifact)
    if mode == "missing":
        artifact = "absent.mdx"
    if mode == "reserved_alias":
        (output / artifact).rename(output / "plugin_result.json")
        artifact = "./plugin_result.json"
    artifacts = [artifact]
    if mode == "duplicate_alias":
        artifacts.append("./" + artifact)
    if mode == "root_symlink":
        (output / artifact).unlink()
        output.rmdir()
        output.symlink_to(Path(request["document_path"]), target_is_directory=True)
        artifacts = [p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()]
    return {
        "protocol_version": 2 if mode == "protocol" else 1,
        "status": mode if mode in ("partial", "failed") else "ok",
        "artifacts": artifacts,
        "qa_summary": {"aligned": 1},
        "errors": [{"message": "chunk failed"}] if mode in ("partial", "failed") else [],
        "warnings": [],
        "manifest_options": {"target_lang": "zh", "api_key": "never-record-me", "token": "never-record-me", "extra": [{"password": "never-record-me"}]},
    }
'''


def _distribution(root, distribution="fake-citeindex-plugin", name="fixture"):
    info = root / (distribution.replace("-", "_") + "-1.2.3.dist-info")
    info.mkdir()
    (info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {distribution}\nVersion: 1.2.3\n"
    )
    (info / "entry_points.txt").write_text(
        f"[citeindex.plugins.v1]\n{name} = fake_citeindex_plugin:run\n"
    )


@pytest.fixture
def installed_plugin(tmp_path, monkeypatch):
    package = tmp_path / "installed"
    package.mkdir()
    (package / "fake_citeindex_plugin.py").write_text(FAKE_PLUGIN)
    _distribution(package)
    monkeypatch.syspath_prepend(str(package))
    paths = [str(package), str(Path(__file__).resolve().parents[1])]
    if os.environ.get("PYTHONPATH"):
        paths.append(os.environ["PYTHONPATH"])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join(paths))
    return package


@pytest.fixture
def corpus(tmp_path):
    document = tmp_path / "corpus" / "paper"
    document.mkdir(parents=True)
    (document / "csl.json").write_text(json.dumps({"id": "paper", "title": "Source"}))
    (document / "ingestion_output.json").write_text('{"status": "ok"}')
    (document / "document.json").write_text('{"text": "Original source"}')
    library = tmp_path / "library"
    library.mkdir()
    (library / "paper.md").write_text("# Original source\n")
    return document


def _digests(root):
    return {
        str(path): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*") if path.is_file()
    }


def test_discovery_uses_metadata_without_importing_plugin(installed_plugin):
    (installed_plugin / "fake_citeindex_plugin.py").write_text(
        'raise AssertionError("discovery imported plugin code")\n'
    )
    assert {"name": "fixture", "distribution": "fake-citeindex-plugin", "version": "1.2.3"} in plugins.discover_plugins()


def test_duplicate_entrypoint_names_fail(installed_plugin):
    _distribution(installed_plugin, distribution="other-plugin")
    with pytest.raises(ValueError, match="(?i)duplicate"):
        plugins.discover_plugins()


def test_discovery_without_plugins_succeeds(monkeypatch):
    monkeypatch.setattr(plugins.metadata, "entry_points", lambda **kwargs: [])
    assert plugins.discover_plugins() == []


def test_success_publishes_hashes_without_mutating_source_or_parent(
    installed_plugin, corpus, tmp_path, monkeypatch
):
    before = _digests(corpus.parent.parent / "corpus") | _digests(corpus.parent.parent / "library")
    monkeypatch.setattr(builtins, "_citeindex_plugin_state", "parent", raising=False)
    output = tmp_path / "translated"
    result = plugins.run_plugin(
        "fixture", corpus, options={"target_lang": "zh", "api_key": "never-record-me"}, output_dir=output
    )
    assert result["status"] == "ok"
    assert Path(result["output_dir"]) == output
    assert (output / "translation.mdx").read_text().startswith("# Source")
    assert builtins._citeindex_plugin_state == "parent"
    assert before == (_digests(corpus.parent.parent / "corpus") | _digests(corpus.parent.parent / "library"))
    manifest = json.loads((output / "plugin_result.json").read_text())
    assert manifest["plugin"] == {"name": "fixture", "distribution": "fake-citeindex-plugin", "version": "1.2.3"}
    assert manifest["source"]["document_path"] == str(corpus)
    assert manifest["source"]["files"] == before
    assert manifest["artifacts"]["translation.mdx"] == hashlib.sha256((output / "translation.mdx").read_bytes()).hexdigest()
    assert "never-record-me" not in json.dumps(manifest)


def test_partial_output_is_separate(installed_plugin, corpus, tmp_path):
    output = tmp_path / "translated"
    result = plugins.run_plugin("fixture", corpus, options={"mode": "partial"}, output_dir=output)
    assert result["status"] == "partial"
    assert not output.exists()
    assert Path(result["output_dir"]) == tmp_path / "translated.partial"
    assert (Path(result["output_dir"]) / "plugin_result.json").is_file()


def test_failed_plugin_does_not_publish(installed_plugin, corpus, tmp_path):
    output = tmp_path / "translated"
    before = _digests(corpus)
    result = plugins.run_plugin("fixture", corpus, options={"mode": "failed"}, output_dir=output)
    assert result["status"] == "failed"
    assert not output.exists()
    assert _digests(corpus) == before


@pytest.mark.parametrize("mode", ["exception", "malformed", "protocol", "traversal", "absolute", "missing", "symlink", "extra_symlink", "root_symlink", "reserved_alias", "duplicate_alias"])
def test_worker_and_artifact_failures_do_not_publish(installed_plugin, corpus, tmp_path, mode):
    output = tmp_path / "translated"
    before = _digests(corpus)
    with pytest.raises((RuntimeError, ValueError)):
        plugins.run_plugin("fixture", corpus, options={"mode": mode}, output_dir=output)
    assert not output.exists()
    assert _digests(corpus) == before


def test_timeout_reaps_worker_without_publication(installed_plugin, corpus, tmp_path):
    output = tmp_path / "translated"
    with pytest.raises(TimeoutError):
        plugins.run_plugin("fixture", corpus, options={"mode": "timeout"}, output_dir=output, timeout=0.1)
    assert not output.exists()


def test_cancellation_kills_and_reaps_worker(monkeypatch, tmp_path):
    events = []

    class Process:
        pid = 12345

        def wait(self, timeout=None):
            events.append(("wait", timeout))
            if timeout is not None:
                raise KeyboardInterrupt

        def kill(self):
            events.append(("kill", self.pid))

    monkeypatch.setattr(plugins.subprocess, "Popen", lambda *args, **kwargs: Process())
    if os.name == "posix":
        monkeypatch.setattr(plugins.os, "killpg", lambda pid, sig: events.append(("kill", pid)))
    with pytest.raises(KeyboardInterrupt):
        plugins._run_worker("fixture", tmp_path / "request.json", tmp_path / "result.json", 10)
    assert events == [("wait", 10), ("kill", 12345), ("wait", None)]


@pytest.mark.parametrize("filename", ["document.json", "../library/paper.md"])
def test_source_change_during_run_prevents_publication(installed_plugin, corpus, tmp_path, filename):
    source = corpus / filename if not filename.startswith("../") else corpus.parent.parent / "library" / "paper.md"
    output = tmp_path / "translated"
    with pytest.raises(RuntimeError, match="(?i)(chang|mutat|source)"):
        plugins.run_plugin("fixture", corpus, options={"mode": "mutate", "source_file": str(source)}, output_dir=output)
    assert not output.exists()


def test_previous_output_is_preserved(installed_plugin, corpus, tmp_path):
    output = tmp_path / "translated"
    output.mkdir()
    (output / "keep.mdx").write_text("previous successful translation")
    with pytest.raises(ValueError):
        plugins.run_plugin("fixture", corpus, output_dir=output)
    assert (output / "keep.mdx").read_text() == "previous successful translation"


@pytest.mark.parametrize("location", ["corpus", "corpus_child", "library"])
def test_output_cannot_overlap_source(installed_plugin, corpus, location):
    output = {"corpus": corpus, "corpus_child": corpus / "translated", "library": corpus.parent.parent / "library"}[location]
    with pytest.raises(ValueError):
        plugins.run_plugin("fixture", corpus, output_dir=output)


@pytest.mark.parametrize("filename, content", [
    ("ingestion_output.json", '{"status":"failed"}'),
    ("ingestion_output.json", "not JSON"),
    ("csl.json", "[]"),
])
def test_invalid_ingestion_is_rejected(installed_plugin, corpus, tmp_path, filename, content):
    (corpus / filename).write_text(content)
    with pytest.raises(ValueError):
        plugins.run_plugin("fixture", corpus, output_dir=tmp_path / "translated")


@pytest.mark.parametrize("filename", ["csl.json", "ingestion_output.json"])
def test_missing_ingestion_artifacts_are_invalid_input(installed_plugin, corpus, tmp_path, filename):
    (corpus / filename).unlink()
    with pytest.raises(ValueError):
        plugins.run_plugin("fixture", corpus, output_dir=tmp_path / "translated")


@pytest.mark.parametrize("options, timeout", [([], 1), ({}, 0), ({}, -1)])
def test_invalid_runner_arguments(installed_plugin, corpus, tmp_path, options, timeout):
    with pytest.raises(ValueError):
        plugins.run_plugin("fixture", corpus, options=options, output_dir=tmp_path / "translated", timeout=timeout)


def test_unknown_plugin_is_actionable(installed_plugin, corpus, tmp_path):
    with pytest.raises((ValueError, RuntimeError), match="(?i)(unknown|not.*(found|installed|available)|unavailable)"):
        plugins.run_plugin("not-installed", corpus, output_dir=tmp_path / "translated")


@pytest.mark.parametrize("status, expected_code", [("ok", 0), ("partial", 1), ("failed", 1)])
def test_cli_returns_nonzero_for_incomplete_output(installed_plugin, corpus, tmp_path, status, expected_code):
    options = tmp_path / "options.json"
    options.write_text(json.dumps({"mode": status}))
    completed = subprocess.run(
        [sys.executable, "-m", "citeindex.plugins", "run", "fixture", str(corpus), "--options", str(options), "--output", str(tmp_path / "translated")],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert completed.returncode == expected_code, completed.stdout + completed.stderr


def test_cli_lists_metadata(installed_plugin):
    completed = subprocess.run(
        [sys.executable, "-m", "citeindex.plugins", "list"],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert "fixture" in completed.stdout
    assert "1.2.3" in completed.stdout
