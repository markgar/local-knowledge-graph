"""Installed-facing canonical workflows with controlled providers, never model quality claims."""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from support.index_search import Reranker, SearchProvider
from typer.testing import CliRunner

from kg.cli import app
from kg.client.config import load_profile, profile_path
from kg.client.documents import Documents
from kg.evidence import EvidenceService
from kg.indexing import IndexService
from kg.models.foundation import DocumentDependency, EvidenceRef, SourceSupport

runner = CliRunner()


@pytest.fixture
def configured(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    # Patch provider constructors only. Real canonical save/index/search/read services execute.
    monkeypatch.setattr(
        "kg.indexing.service.SentenceTransformerEmbeddingProvider",
        lambda profile, **kw: SearchProvider(profile),
    )
    monkeypatch.setattr(
        "kg.indexing.search.SentenceTransformerEmbeddingProvider",
        lambda profile, **kw: SearchProvider(profile),
    )
    monkeypatch.setattr(
        "kg.indexing.search.SentenceTransformerCrossEncoderProvider",
        lambda **kw: Reranker(),
    )
    result = runner.invoke(app, ["setup", "--yes", "--approve-models", "--json"])
    assert result.exit_code == 0, result.output
    return tmp_path


def call(*args, code=0):
    result = runner.invoke(app, [*map(str, args), "--json"])
    assert result.exit_code == code, (result.output, result.exception)
    return json.loads(result.stdout)


def test_document_journey_exact_evidence_history_and_search(configured):
    file = configured / "note.md"
    text = "\ufeffAtlas\r\nCafe\u0301 \U0001f680\0"
    file.write_bytes(text.encode())
    added = call("add", file)
    target, state = added["result"]["target"], added["result"]["state"]
    assert added["result"]["preparation"]["outcome"] == "ready"
    read = call("read", target)
    assert read["message"] == text
    entry = read["result"]["entries"][0]
    assert call("read", entry["target"])["result"]["evidence"]["quote"] == text
    # Read's support is directly constructible as canonical grounding input.
    support = entry["support"]
    ref = EvidenceRef.model_validate(support["reference"])
    assert SourceSupport(kind="source", evidence=(ref,)).evidence == (ref,)
    assert (
        DocumentDependency(
            source_namespace=ref.source_namespace,
            document_id=ref.document_id,
            revision_id=ref.revision_id,
            state_version=support["state_version"],
        ).state_version
        == state
    )
    found = call("find", "documents", "Atlas")
    assert found["result"]["entries"][0]["document"] == target
    old_metadata = read["result"]["document"]["metadata"]["metadata"]
    file.write_text("Atlas revised")
    changed = call("update", target, file, "--expect", state)
    new_state = changed["result"]["state"]
    assert new_state != state
    assert call("read", target)["result"]["document"]["metadata"]["metadata"] == old_metadata
    assert call("read", f"{target}@{state}")["message"] == text
    first = call("read", target, "--history", "--limit", "1")
    assert first["result"]["page"]["has_more"]
    next_page = call(
        "read",
        target,
        "--history",
        "--after",
        first["result"]["page"]["next_after_sequence"],
    )
    assert len(next_page["result"]["page"]["entries"]) == 1
    call("update", target, file, "--expect", state, code=2)
    call("remove", target, "--expect", new_state, code=2)
    removed = call("remove", target, "--expect", new_state, "--confirm")
    assert removed["status"] == "complete"
    assert call("read", target)["result"]["document"]["processing"]["source"] == "inactive"
    assert call("find", "documents", "Atlas")["status"] == "empty"


@pytest.mark.parametrize("failure", ["typed", "exception", "interrupt"])
def test_saved_receipt_survives_preparation_failure(configured, monkeypatch, failure):
    def broken(*args, **kwargs):
        if failure == "exception":
            raise RuntimeError("sensitive model exception")
        if failure == "interrupt":
            raise KeyboardInterrupt
        from kg.models.indexing import ProcessResult

        return ProcessResult(
            document_id=args[3],
            state_version=args[4],
            configuration_id="test",
            outcome="failed",
            reason="provider_unavailable",
            attempt_id=None,
        )

    monkeypatch.setattr(IndexService, "process", broken)
    file = configured / "note.md"
    file.write_text("still saved")
    result = call("add", file, code=5)
    assert result["status"] == "partial"
    assert result["result"]["write"]["receipt"]["document_id"]
    assert call("read", result["result"]["target"])["message"] == "still saved"
    assert "sensitive model exception" not in json.dumps(result)


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_unknown_write_does_not_claim_rollback(configured, monkeypatch, error):
    original = EvidenceService.write

    def lost(self, request):
        original(self, request)
        raise error("lost delivery")

    monkeypatch.setattr(EvidenceService, "write", lost)
    file = configured / "note.md"
    file.write_text("text")
    result = call("add", file, code=7)
    assert result["status"] == "uncertain"
    assert "duplicates" in result["message"]


@pytest.mark.parametrize(
    "args",
    [
        ["read", "document:any", "--limit", "0"],
        ["add"],
        ["find", "documents"],
        ["find", "unknown"],
        ["unknown"],
        ["read", "document:any", "--unknown"],
    ],
)
def test_parser_errors_use_json_envelope(args):
    result = call(*args, code=2)
    assert result["interface_version"] == "client/1"
    assert result["status"] == "rejected"
    assert result["code"] == "invalid_arguments"
    assert result["exit_code"] == 2


def test_parser_errors_remain_readable_without_json():
    result = runner.invoke(app, ["add"])
    assert result.exit_code == 2
    assert "Missing argument" in result.output


def test_direct_evidence_warns_when_no_longer_current(configured):
    file = configured / "note.md"
    file.write_text("original")
    added = call("add", file)["result"]
    citation = call("read", added["target"])["result"]["entries"][0]["target"]
    warning = "Historical/inactive support; not a current fact basis."
    assert warning not in runner.invoke(app, ["read", citation]).stdout
    file.write_text("revised")
    changed = call("update", added["target"], file, "--expect", added["state"])["result"]
    old_read = runner.invoke(app, ["read", citation])
    assert old_read.exit_code == 0
    assert warning in old_read.stdout and "original" in old_read.stdout
    citation = call("read", changed["target"])["result"]["entries"][0]["target"]
    call("remove", changed["target"], "--expect", changed["state"], "--confirm")
    inactive_read = runner.invoke(app, ["read", citation])
    assert inactive_read.exit_code == 0
    assert warning in inactive_read.stdout and "revised" in inactive_read.stdout


def test_paging_and_invalid_input(configured):
    file = configured / "note.md"
    file.write_text("a" * 2050)
    target = call("add", file)["result"]["target"]
    first = call("read", target, "--limit", "1")["result"]
    assert first["has_more"] and first["next_after"] == 1
    second = call("read", target, "--after", "1")["result"]
    assert len(second["entries"]) == 2
    call("read", "note.md", code=2)
    call("read", "evidence:!!!", code=2)
    call("update", target, file, code=2)
    file.write_bytes(b"\xff")
    call("add", file, code=2)
    file.unlink()
    file.symlink_to(configured / "missing")
    call("add", file, code=2)


def test_model_approval_is_required_before_save(configured):
    profile = load_profile()
    profile_path().write_text(
        profile.model_copy(update={"models_approved": False}).model_dump_json()
    )
    file = configured / "note.md"
    file.write_text("text")
    assert call("add", file, code=2)["code"] == "models_not_approved"
    assert call("find", "documents", "text", code=2)["code"] == "models_not_approved"


def test_setup_refuses_existing_and_attach_never_initializes(configured, monkeypatch):
    profile = load_profile()
    original = profile_path()
    assert call("setup", "--yes", code=2)["code"] == "already_configured"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(configured / "second-config"))
    attached = call("setup", "--attach", original, "--yes")
    assert attached["result"]["profile"]["store"] == profile.store
    monkeypatch.setenv("XDG_CONFIG_HOME", str(configured / "third-config"))
    assert call("setup", "--store", profile.store, "--yes", code=2)["code"] == "store_exists"
    Path(profile.store).unlink()
    assert call("setup", "--attach", original, "--yes", code=2)["code"] == "invalid_store"
    assert not Path(profile.store).exists()


@pytest.mark.parametrize(
    "args",
    [
        [],
        ["setup"],
        ["add"],
        ["find"],
        ["find", "documents"],
        ["read"],
        ["update"],
        ["remove"],
    ],
)
def test_every_help_level_is_model_free(tmp_path, monkeypatch, args):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    result = runner.invoke(app, [*args, "--help"])
    assert result.exit_code == 0, result.output
    assert not (tmp_path / "config").exists()


def test_module_entrypoint_outside_checkout(configured):
    env = dict(os.environ)
    # Interpreter imports the installed/editable project, never this test's cwd.
    result = subprocess.run(
        [sys.executable, "-m", "kg", "find", "--help"],
        cwd=configured,
        env=env,
        text=True,
        capture_output=True,
    )
    assert result.returncode == 0, result.stderr
    assert "documents" in result.stdout
    assert "dense-index" not in result.stdout


def test_workflow_uses_public_offline_service_options(configured, monkeypatch):
    calls = []

    def offline(profile, **kw):
        calls.append(kw)
        return SearchProvider(profile)

    monkeypatch.setattr("kg.indexing.service.SentenceTransformerEmbeddingProvider", offline)
    file = configured / "note.md"
    file.write_text("approved")
    Documents(load_profile()).save(file)
    assert calls == [{"local_files_only": True, "cache_folder": None}]


def test_guided_setup_and_cancel(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setattr("kg.cli.interactive", lambda: True)
    result = runner.invoke(app, ["setup"], input="\nn\nn\n")
    assert result.exit_code == 2
    assert not profile_path().exists()
    result = runner.invoke(app, ["setup"], input="\nn\ny\n")
    assert result.exit_code == 0
    assert "Local profile configured" in result.stdout


def test_existing_incompatible_store_is_not_changed(configured, monkeypatch):
    profile = load_profile()
    invalid = configured / "incompatible.db"
    invalid.write_bytes(b"not a canonical sqlite database")
    imported = configured / "attach.json"
    imported.write_text(profile.model_copy(update={"store": str(invalid)}).model_dump_json())
    monkeypatch.setenv("XDG_CONFIG_HOME", str(configured / "other-config"))
    original = invalid.read_bytes()
    call("setup", "--attach", imported, "--yes", code=3)
    assert invalid.read_bytes() == original
    assert not profile_path().exists()


def test_providers_forward_local_only_without_model_execution(monkeypatch):
    import types

    from kg.retrieval.dense import SentenceTransformerEmbeddingProvider
    from kg.retrieval.rerank import SentenceTransformerCrossEncoderProvider

    received = []

    def unavailable(*args, **kwargs):
        received.append(kwargs)
        raise OSError("not in local cache")

    module = types.ModuleType("sentence_transformers")
    module.SentenceTransformer = unavailable
    module.CrossEncoder = unavailable
    monkeypatch.setitem(sys.modules, "sentence_transformers", module)
    from kg.retrieval.dense import DenseIndexError
    from kg.retrieval.rerank import RerankerError

    with pytest.raises(DenseIndexError):
        SentenceTransformerEmbeddingProvider(local_files_only=True, cache_folder="/approved")
    with pytest.raises(RerankerError):
        SentenceTransformerCrossEncoderProvider(local_files_only=True, cache_folder="/approved")
    assert len(received) == 2
    assert all(item["local_files_only"] is True for item in received)
    assert all(item["cache_folder"] == "/approved" for item in received)
