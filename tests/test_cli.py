from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kg.cli import app
from kg.models.contracts import DenseIndexResult, SearchResult
from kg.retrieval.dense import DenseIndexError, EmbeddingProfile

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


def test_status_without_since_includes_old_dated_evidence(tmp_path: Path) -> None:
    manifest = tmp_path / "corpus.yml"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "atlas.md").write_text(
        "---\n"
        "date: 2000-01-01\n"
        "---\n\n"
        "# Project Atlas\n\n"
        "## Decisions\n\n"
        "- Use SQLite for the local index.\n",
        encoding="utf-8",
    )
    manifest.write_text(
        "corpus_id: test\n"
        "display_name: Test\n"
        "vault_root: vault\n"
        "database: index.sqlite3\n"
        "include: ['*.md']\n"
        "seed_entities:\n"
        "  - entity_id: project-atlas\n"
        "    name: Project Atlas\n"
        "    entity_type: project\n"
        "    aliases: [Atlas]\n"
        "metadata_fields:\n"
        "  event_time: date\n",
        encoding="utf-8",
    )
    assert RUNNER.invoke(app, ["ingest", "--manifest", str(manifest)]).exit_code == 0

    result = RUNNER.invoke(
        app,
        [
            "status",
            "Atlas",
            "--manifest",
            str(manifest),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)["decisions"][0]["summary"] == (
        "Use SQLite for the local index."
    )


@pytest.mark.parametrize("use_context", [False, True])
def test_dense_commands_are_available_through_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_context: bool,
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
        def __init__(
            self,
            database: object,
            corpus_id: str,
            *,
            profile: EmbeddingProfile,
            contextual: bool,
        ) -> None:
            assert corpus_id == "test"
            assert profile is EmbeddingProfile.qwen3_embedding_06b
            assert contextual is use_context

        def build_index(self, *, batch_size: int) -> DenseIndexResult:
            assert batch_size == 16
            return DenseIndexResult(
                corpus_id="test",
                embedding_profile=EmbeddingProfile.qwen3_embedding_06b.value,
                projection_id="projection",
                model_name="test/model",
                model_revision="v1",
                model_license="MIT",
                pipeline_version="test-pipeline-v1",
                dimensions=2,
                normalization="l2",
                context_behavior="test-context",
                query_encoding="test-query",
                document_encoding="test-document",
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
            *(["--contextual"] if use_context else []),
            "--manifest",
            str(manifest),
            "--batch-size",
            "16",
            "--embedding-profile",
            "qwen3-embedding-0.6b",
            "--format",
            "json",
        ],
    )
    search_result = RUNNER.invoke(
        app,
        [
            "search",
            "semantic question",
            *(["--contextual"] if use_context else []),
            "--query-mode",
            "dense",
            "--embedding-profile",
            "qwen3-embedding-0.6b",
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


def test_dense_encode_error_is_machine_readable(
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
        def __init__(
            self,
            database: object,
            corpus_id: str,
            *,
            profile: EmbeddingProfile,
            contextual: bool,
        ) -> None:
            pass

        def search(self, query: str, **kwargs: object) -> list[SearchResult]:
            raise DenseIndexError("Could not encode query: model failure")

    monkeypatch.setattr("kg.cli.DenseRetrievalService", StubDenseRetrievalService)

    result = RUNNER.invoke(
        app,
        [
            "search",
            "question",
            "--query-mode",
            "dense",
            "--manifest",
            str(manifest),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 2
    error = json.loads(result.stderr)
    assert error == {
        "error": "dense_index_unavailable",
        "message": "Could not encode query: model failure",
    }


@pytest.mark.parametrize("use_context", [False, True])
def test_hybrid_query_mode_is_available_through_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_context: bool,
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
        def __init__(
            self,
            database: object,
            corpus_id: str,
            *,
            embedding_profile: EmbeddingProfile,
            contextual: bool,
        ) -> None:
            assert corpus_id == "test"
            assert embedding_profile is EmbeddingProfile.qwen3_embedding_06b
            assert contextual is use_context

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
            *(["--contextual"] if use_context else []),
            "--query-mode",
            "hybrid",
            "--embedding-profile",
            "qwen3-embedding-0.6b",
            "--manifest",
            str(manifest),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)[0]["quote"] == "Hybrid evidence."


@pytest.mark.parametrize("use_context", [False, True])
def test_reranked_query_mode_is_available_through_cli(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_context: bool,
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

    class StubRerankedRetrievalService:
        def __init__(
            self,
            database: object,
            corpus_id: str,
            *,
            embedding_profile: EmbeddingProfile,
            contextual: bool,
        ) -> None:
            assert corpus_id == "test"
            assert embedding_profile is EmbeddingProfile.qwen3_embedding_06b
            assert contextual is use_context

        def search(self, query: str, **kwargs: object) -> list[SearchResult]:
            assert query == "reranked question"
            return [
                SearchResult(
                    record_id="passage",
                    record_type="passage",
                    title="Note",
                    source_path="note.md",
                    source_revision_id="revision",
                    anchor_id="anchor",
                    quote="Reranked evidence.",
                    rank=4.2,
                )
            ]

    monkeypatch.setattr(
        "kg.cli.RerankedRetrievalService",
        StubRerankedRetrievalService,
    )
    result = RUNNER.invoke(
        app,
        [
            "search",
            "reranked question",
            *(["--contextual"] if use_context else []),
            "--query-mode",
            "reranked",
            "--embedding-profile",
            "qwen3-embedding-0.6b",
            "--manifest",
            str(manifest),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)[0]["quote"] == "Reranked evidence."
