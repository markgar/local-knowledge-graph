from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kg.cli import app
from kg.models.contracts import DenseIndexResult, SearchResult

RUNNER = CliRunner()


def test_json_manifest_error_is_machine_readable(tmp_path: Path) -> None:
    result = RUNNER.invoke(
        app,
        [
            "search",
            "anything",
            "--manifest",
            str(tmp_path / "missing.yml"),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 2
    error = json.loads(result.stderr)
    assert error["error"] == "invalid_manifest"
    assert "Could not read manifest" in error["message"]


def test_json_query_error_is_machine_readable(tmp_path: Path) -> None:
    manifest = tmp_path / "corpus.yml"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\n", encoding="utf-8")
    manifest.write_text(
        "corpus_id: test\n"
        "display_name: Test\n"
        "vault_root: vault\n"
        "database: index.sqlite3\n"
        "include: ['*.md']\n",
        encoding="utf-8",
    )
    ingest = RUNNER.invoke(app, ["ingest", "--manifest", str(manifest)])
    assert ingest.exit_code == 0

    result = RUNNER.invoke(
        app,
        [
            "search",
            "!!!",
            "--manifest",
            str(manifest),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"] == "invalid_query"


def test_evidence_command_returns_exact_record_anchor(tmp_path: Path) -> None:
    manifest = tmp_path / "corpus.yml"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\n\nEvidence passage.\n", encoding="utf-8")
    manifest.write_text(
        "corpus_id: test\n"
        "display_name: Test\n"
        "vault_root: vault\n"
        "database: index.sqlite3\n"
        "include: ['*.md']\n",
        encoding="utf-8",
    )
    assert RUNNER.invoke(app, ["ingest", "--manifest", str(manifest)]).exit_code == 0
    search = RUNNER.invoke(
        app,
        [
            "search",
            "Evidence",
            "--manifest",
            str(manifest),
            "--format",
            "json",
        ],
    )
    record_id = json.loads(search.stdout)[0]["record_id"]

    evidence = RUNNER.invoke(
        app,
        [
            "evidence",
            record_id,
            "--manifest",
            str(manifest),
            "--format",
            "json",
        ],
    )

    assert evidence.exit_code == 0
    assert json.loads(evidence.stdout)["quote"] == "Evidence passage."


def test_natural_query_mode_is_available_through_cli(tmp_path: Path) -> None:
    manifest = tmp_path / "corpus.yml"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text(
        "# Note\n\nEvidence passage.\n",
        encoding="utf-8",
    )
    manifest.write_text(
        "corpus_id: test\n"
        "display_name: Test\n"
        "vault_root: vault\n"
        "database: index.sqlite3\n"
        "include: ['*.md']\n",
        encoding="utf-8",
    )
    assert RUNNER.invoke(app, ["ingest", "--manifest", str(manifest)]).exit_code == 0

    result = RUNNER.invoke(
        app,
        [
            "search",
            "unrelated evidence",
            "--query-mode",
            "natural",
            "--manifest",
            str(manifest),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)[0]["quote"] == "Evidence passage."


def test_dense_commands_are_available_through_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "corpus.yml"
    vault = tmp_path / "vault"
    vault.mkdir()
    manifest.write_text(
        "corpus_id: test\n"
        "display_name: Test\n"
        "vault_root: vault\n"
        "database: index.sqlite3\n"
        "include: ['*.md']\n",
        encoding="utf-8",
    )

    class StubDenseRetrievalService:
        def __init__(self, database: object, corpus_id: str) -> None:
            assert corpus_id == "test"

        def build_index(self, *, batch_size: int) -> DenseIndexResult:
            assert batch_size == 16
            return DenseIndexResult(
                corpus_id="test",
                projection_id="projection",
                model_name="test/model",
                model_revision="v1",
                model_license="MIT",
                pipeline_version="test-pipeline-v1",
                dimensions=2,
                passage_count=1,
                built=True,
                duration_ms=1.0,
                index_path=str(tmp_path / "index.dense.sqlite3"),
                index_bytes=1024,
            )

        def search(self, query: str, **kwargs: object) -> list[SearchResult]:
            assert query == "semantic question"
            return [
                SearchResult(
                    record_id="passage",
                    record_type="passage",
                    title="Note",
                    source_path="note.md",
                    source_revision_id="revision",
                    anchor_id="anchor",
                    quote="Semantic evidence.",
                    rank=0.1,
                )
            ]

    monkeypatch.setattr("kg.cli.DenseRetrievalService", StubDenseRetrievalService)

    index_result = RUNNER.invoke(
        app,
        [
            "dense-index",
            "--manifest",
            str(manifest),
            "--batch-size",
            "16",
            "--format",
            "json",
        ],
    )
    search_result = RUNNER.invoke(
        app,
        [
            "search",
            "semantic question",
            "--query-mode",
            "dense",
            "--manifest",
            str(manifest),
            "--format",
            "json",
        ],
    )

    assert index_result.exit_code == 0
    assert json.loads(index_result.stdout)["projection_id"] == "projection"
    assert search_result.exit_code == 0
    assert json.loads(search_result.stdout)[0]["quote"] == "Semantic evidence."


def test_hybrid_query_mode_is_available_through_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = tmp_path / "corpus.yml"
    vault = tmp_path / "vault"
    vault.mkdir()
    manifest.write_text(
        "corpus_id: test\n"
        "display_name: Test\n"
        "vault_root: vault\n"
        "database: index.sqlite3\n"
        "include: ['*.md']\n",
        encoding="utf-8",
    )

    class StubHybridRetrievalService:
        def __init__(self, database: object, corpus_id: str) -> None:
            assert corpus_id == "test"

        def search(self, query: str, **kwargs: object) -> list[SearchResult]:
            assert query == "hybrid question"
            return [
                SearchResult(
                    record_id="passage",
                    record_type="passage",
                    title="Note",
                    source_path="note.md",
                    source_revision_id="revision",
                    anchor_id="anchor",
                    quote="Hybrid evidence.",
                    rank=0.03,
                )
            ]

    monkeypatch.setattr(
        "kg.cli.HybridRetrievalService",
        StubHybridRetrievalService,
    )
    result = RUNNER.invoke(
        app,
        [
            "search",
            "hybrid question",
            "--query-mode",
            "hybrid",
            "--manifest",
            str(manifest),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)[0]["quote"] == "Hybrid evidence."
