from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest

from kg.config import load_manifest
from kg.db import Database
from kg.ingest import IngestService
from kg.models.contracts import SearchResult
from kg.retrieval.dense import (
    DenseIndexError,
    DenseRetrievalService,
    SentenceTransformerEmbeddingProvider,
)


class FakeEmbeddingProvider:
    name = "test/embedding"
    revision = "v1"
    license = "MIT"
    pipeline_version = "test-pipeline-v1"
    dimensions = 2

    def encode_documents(
        self,
        texts: Sequence[str],
        *,
        batch_size: int,
    ) -> list[list[float]]:
        assert batch_size > 0
        return [self._vector(text) for text in texts]

    def encode_query(self, text: str) -> list[float]:
        return self._vector(text)

    @staticmethod
    def _vector(text: str) -> list[float]:
        normalized = text.casefold()
        if "sqlite" in normalized or "database" in normalized:
            return [1.0, 0.0]
        return [0.0, 1.0]


def _manifest(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "storage.md").write_text(
        "# Storage\n\nSQLite keeps the canonical evidence.\n",
        encoding="utf-8",
    )
    (vault / "garden.md").write_text(
        "# Garden\n\nWildflowers bloom beside the path.\n",
        encoding="utf-8",
    )
    path = tmp_path / "corpus.yml"
    path.write_text(
        "corpus_id: test\n"
        "display_name: Test\n"
        "vault_root: vault\n"
        "database: index.sqlite3\n"
        "include: ['*.md']\n",
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("failure", [OSError("disk"), RuntimeError("model"), ValueError("input")])
@pytest.mark.parametrize("method", ["encode_documents", "encode_query"])
def test_sentence_transformer_encode_failures_are_dense_index_errors(
    failure: Exception,
    method: str,
) -> None:
    class FailingModel:
        def encode(self, *args: object, **kwargs: object) -> object:
            raise failure

    provider = SentenceTransformerEmbeddingProvider.__new__(
        SentenceTransformerEmbeddingProvider
    )
    provider._model = FailingModel()

    with pytest.raises(DenseIndexError, match="Could not encode"):
        if method == "encode_documents":
            provider.encode_documents(["passage"], batch_size=1)
        else:
            provider.encode_query("query")


@pytest.mark.parametrize("method", ["encode_documents", "encode_query"])
def test_sentence_transformer_conversion_failures_are_dense_index_errors(
    method: str,
) -> None:
    class InvalidModel:
        def encode(self, *args: object, **kwargs: object) -> object:
            return [["not-a-number"]]

    provider = SentenceTransformerEmbeddingProvider.__new__(
        SentenceTransformerEmbeddingProvider
    )
    provider._model = InvalidModel()

    with pytest.raises(DenseIndexError, match="convert embedding vector"):
        if method == "encode_documents":
            provider.encode_documents(["passage"], batch_size=1)
        else:
            provider.encode_query("query")


def test_dense_projection_is_versioned_and_returns_canonical_evidence(
    tmp_path: Path,
) -> None:
    manifest_path = _manifest(tmp_path)
    manifest = load_manifest(manifest_path)
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    projection_path = tmp_path / "dense.sqlite3"
    service = DenseRetrievalService(
        database,
        manifest.corpus_id,
        projection_path=projection_path,
        provider=FakeEmbeddingProvider(),
    )

    first = service.build_index()
    second = service.build_index()
    results = service.search("Which database stores evidence?", limit=1)

    assert first.built is True
    assert second.built is False
    assert second.projection_id == first.projection_id
    assert results[0].quote == "SQLite keeps the canonical evidence."
    evidence = service.retrieval.evidence(results[0].record_id)
    assert evidence.anchor_id == results[0].anchor_id

    with sqlite3.connect(projection_path) as connection:
        stored = connection.execute(
            """
            SELECT passage_id, anchor_id, revision_id
            FROM dense_embedding
            WHERE projection_id = ?
            """,
            (first.projection_id,),
        ).fetchall()
    assert len(stored) == first.passage_count
    assert all(
        passage_id and anchor_id and revision_id
        for passage_id, anchor_id, revision_id in stored
    )


def test_dense_search_rejects_a_stale_projection(tmp_path: Path) -> None:
    manifest_path = _manifest(tmp_path)
    manifest = load_manifest(manifest_path)
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    service = DenseRetrievalService(
        database,
        manifest.corpus_id,
        projection_path=tmp_path / "dense.sqlite3",
        provider=FakeEmbeddingProvider(),
    )
    service.build_index()

    (manifest.vault_root / "storage.md").write_text(
        "# Storage\n\nSQLite preserves revised canonical evidence.\n",
        encoding="utf-8",
    )
    IngestService(database).ingest(manifest)

    with pytest.raises(DenseIndexError, match="stale"):
        service.search("database")


def test_dense_search_honors_source_filter(tmp_path: Path) -> None:
    manifest_path = _manifest(tmp_path)
    manifest = load_manifest(manifest_path)
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    service = DenseRetrievalService(
        database,
        manifest.corpus_id,
        projection_path=tmp_path / "dense.sqlite3",
        provider=FakeEmbeddingProvider(),
    )
    service.build_index()

    results = service.search("database", source_path="garden.md")

    assert results
    assert {result.source_path for result in results} == {"garden.md"}


def test_dense_search_rejects_a_different_embedding_pipeline(tmp_path: Path) -> None:
    manifest_path = _manifest(tmp_path)
    manifest = load_manifest(manifest_path)
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    service = DenseRetrievalService(
        database,
        manifest.corpus_id,
        projection_path=tmp_path / "dense.sqlite3",
        provider=FakeEmbeddingProvider(),
    )
    service.build_index()
    changed_provider = FakeEmbeddingProvider()
    changed_provider.pipeline_version = "test-pipeline-v2"

    with pytest.raises(DenseIndexError, match="different embedding model"):
        service.search("database", provider=changed_provider)


def test_dense_subject_filter_treats_sql_wildcards_literally(tmp_path: Path) -> None:
    manifest_path = _manifest(tmp_path)
    manifest = load_manifest(manifest_path)
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    service = DenseRetrievalService(
        database,
        manifest.corpus_id,
        projection_path=tmp_path / "dense.sqlite3",
        provider=FakeEmbeddingProvider(),
    )
    service.build_index()

    assert service.search("database", subject="%") == []


def test_dense_build_rolls_back_when_corpus_changes_during_activation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = _manifest(tmp_path)
    manifest = load_manifest(manifest_path)
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    projection_path = tmp_path / "dense.sqlite3"
    service = DenseRetrievalService(
        database,
        manifest.corpus_id,
        projection_path=projection_path,
        provider=FakeEmbeddingProvider(),
    )
    fingerprints = iter(("before", "before", "before", "after"))
    monkeypatch.setattr(
        service.retrieval,
        "index_fingerprint",
        lambda *_args: next(fingerprints),
    )

    with pytest.raises(DenseIndexError, match="activated"):
        service.build_index()

    with sqlite3.connect(projection_path) as connection:
        projection_count = connection.execute(
            "SELECT count(*) FROM dense_projection"
        ).fetchone()[0]
        vector_tables = connection.execute(
            """
            SELECT count(*)
            FROM sqlite_master
            WHERE type = 'table' AND name LIKE 'dense_vec_%'
            """
        ).fetchone()[0]
    assert projection_count == 0
    assert vector_tables == 0


def test_dense_projection_rejects_an_older_schema(tmp_path: Path) -> None:
    manifest_path = _manifest(tmp_path)
    manifest = load_manifest(manifest_path)
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    projection_path = tmp_path / "dense.sqlite3"
    with sqlite3.connect(projection_path) as connection:
        connection.executescript(
            """
            CREATE TABLE projection_schema (version INTEGER PRIMARY KEY);
            INSERT INTO projection_schema (version) VALUES (1);
            """
        )
    service = DenseRetrievalService(
        database,
        manifest.corpus_id,
        projection_path=projection_path,
        provider=FakeEmbeddingProvider(),
    )

    with pytest.raises(DenseIndexError, match="delete the dense database"):
        service.build_index()


def test_dense_build_reuses_projection_created_by_a_concurrent_builder(
    tmp_path: Path,
) -> None:
    manifest_path = _manifest(tmp_path)
    manifest = load_manifest(manifest_path)
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    projection_path = tmp_path / "dense.sqlite3"
    competitor = DenseRetrievalService(
        database,
        manifest.corpus_id,
        projection_path=projection_path,
        provider=FakeEmbeddingProvider(),
    )

    class ConcurrentEmbeddingProvider(FakeEmbeddingProvider):
        def encode_documents(
            self,
            texts: Sequence[str],
            *,
            batch_size: int,
        ) -> list[list[float]]:
            competitor.build_index(batch_size=batch_size)
            return super().encode_documents(texts, batch_size=batch_size)

    service = DenseRetrievalService(
        database,
        manifest.corpus_id,
        projection_path=projection_path,
        provider=ConcurrentEmbeddingProvider(),
    )

    result = service.build_index()

    assert result.built is False
    assert service.search("database", limit=1)[0].source_path == "storage.md"


def test_dense_search_validates_fingerprint_after_materializing_results(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = _manifest(tmp_path)
    manifest = load_manifest(manifest_path)
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    service = DenseRetrievalService(
        database,
        manifest.corpus_id,
        projection_path=tmp_path / "dense.sqlite3",
        provider=FakeEmbeddingProvider(),
    )
    service.build_index()
    materialized = False
    original_passage_results = service.retrieval.passage_results

    def passage_results(
        ranked_passage_ids: Sequence[tuple[str, float]],
    ) -> list[SearchResult]:
        nonlocal materialized
        materialized = True
        return original_passage_results(ranked_passage_ids)

    original_fingerprint = service.retrieval.index_fingerprint
    fingerprint_calls = 0

    def index_fingerprint(
        connection: sqlite3.Connection | None = None,
    ) -> str:
        nonlocal fingerprint_calls
        fingerprint_calls += 1
        if fingerprint_calls == 2:
            assert materialized
        return original_fingerprint(connection)

    monkeypatch.setattr(service.retrieval, "passage_results", passage_results)
    monkeypatch.setattr(service.retrieval, "index_fingerprint", index_fingerprint)

    service.search("database", limit=1)

    assert materialized is True
    assert fingerprint_calls == 2


def test_empty_dense_search_still_validates_the_final_fingerprint(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest_path = _manifest(tmp_path)
    manifest = load_manifest(manifest_path)
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    service = DenseRetrievalService(
        database,
        manifest.corpus_id,
        projection_path=tmp_path / "dense.sqlite3",
        provider=FakeEmbeddingProvider(),
    )
    service.build_index()
    original_fingerprint = service.retrieval.index_fingerprint
    fingerprint_calls = 0

    def index_fingerprint(
        connection: sqlite3.Connection | None = None,
    ) -> str:
        nonlocal fingerprint_calls
        fingerprint_calls += 1
        if fingerprint_calls == 2:
            return "changed"
        return original_fingerprint(connection)

    monkeypatch.setattr(service.retrieval, "index_fingerprint", index_fingerprint)

    with pytest.raises(DenseIndexError, match="Corpus changed"):
        service.search("database", source_path="missing.md")

    assert fingerprint_calls == 2
