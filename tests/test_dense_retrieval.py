from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest

from kg.config import load_manifest
from kg.db import Database
from kg.ingest import IngestService
from kg.models.contracts import SearchResult
from kg.retrieval.dense import (
    DEFAULT_EMBEDDING_PROFILE,
    DenseIndexError,
    DenseRetrievalService,
    EmbeddingProfile,
    SentenceTransformerEmbeddingProvider,
)


class FakeEmbeddingProvider:
    profile = DEFAULT_EMBEDDING_PROFILE
    name = "test/embedding"
    revision = "v1"
    license = "MIT"
    pipeline_version = "test-pipeline-v1"
    dimensions = 2
    normalization = "l2"
    context_behavior = "test-context"
    query_encoding = "test-query"
    document_encoding = "test-document"

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


@pytest.mark.parametrize("profile", list(EmbeddingProfile))
def test_contextual_index_is_separate_and_preserves_evidence(
    tmp_path: Path, profile: EmbeddingProfile,
) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    (manifest.vault_root / "storage.md").write_text(
        "# Storage\n\n## Database\n\nIt keeps the canonical evidence.\n", encoding="utf-8"
    )
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    captured: list[str] = []

    class RecordingProvider(FakeEmbeddingProvider):
        def encode_documents(
            self, texts: Sequence[str], *, batch_size: int,
        ) -> list[list[float]]:
            captured.extend(texts)
            return super().encode_documents(texts, batch_size=batch_size)

    provider = RecordingProvider()
    provider.profile = profile
    plain = DenseRetrievalService(database, manifest.corpus_id, provider=provider, profile=profile)
    contextual = DenseRetrievalService(
        database, manifest.corpus_id, provider=provider, profile=profile, contextual=True
    )
    plain_index = plain.build_index()
    assert "It keeps the canonical evidence." in captured
    assert not any(text.startswith("Title:") for text in captured)
    captured.clear()
    with pytest.raises(DenseIndexError, match="--contextual"):
        contextual.search("database")
    contextual_index = contextual.build_index()
    assert (
        "Title: Storage\n\nHeading: Storage / Database\n\nIt keeps the canonical evidence."
        in captured
    )
    assert contextual_index.contextual is True
    assert plain_index.contextual is False
    assert contextual_index.projection_id != plain_index.projection_id
    assert contextual_index.index_path != plain_index.index_path
    assert not contextual.build_index().built
    assert not plain.build_index().built
    for service in (plain, contextual):
        for result in service.search("database", limit=10):
            assert result.quote == service.retrieval.source_range(result.anchor_id).quote

    wrong_mode = DenseRetrievalService(
        database, manifest.corpus_id, provider=provider, profile=profile,
        projection_path=plain.projection_path, contextual=True,
    )
    with pytest.raises(DenseIndexError, match="incompatible"):
        wrong_mode.search("database")

    (manifest.vault_root / "storage.md").write_text(
        "# Changed\n\nNew evidence.\n", encoding="utf-8"
    )
    IngestService(database).ingest(manifest)
    with pytest.raises(DenseIndexError, match="stale"):
        contextual.search("database")


def test_embedding_profile_parsing_is_exact() -> None:
    assert EmbeddingProfile("gte-modernbert") is EmbeddingProfile.gte_modernbert
    assert (
        EmbeddingProfile("qwen3-embedding-0.6b")
        is EmbeddingProfile.qwen3_embedding_06b
    )
    with pytest.raises(ValueError):
        EmbeddingProfile("qwen")


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


def test_sentence_transformer_uses_profile_specific_query_and_document_encoding() -> None:
    class RecordingModel:
        def __init__(self) -> None:
            self.calls: list[tuple[Sequence[str], dict[str, object]]] = []

        def encode(self, texts: Sequence[str], **kwargs: object) -> list[list[float]]:
            self.calls.append((texts, kwargs))
            return [[1.0, 0.0] for _ in texts]

    provider = SentenceTransformerEmbeddingProvider.__new__(
        SentenceTransformerEmbeddingProvider
    )
    provider._model = RecordingModel()
    provider.dimensions = 2
    provider.query_prompt_name = "query"

    provider.encode_documents(["document"], batch_size=1)
    provider.encode_query("question")

    document_call, query_call = provider._model.calls
    assert "prompt_name" not in document_call[1]
    assert query_call[1]["prompt_name"] == "query"


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

    with pytest.raises(DenseIndexError, match="incompatible"):
        service.search("database", provider=changed_provider)


@pytest.mark.parametrize(
    ("property_name", "changed_value"),
    [
        ("query_encoding", "changed-query"),
        ("document_encoding", "changed-document"),
        ("context_behavior", "changed-context"),
        ("normalization", "changed-normalization"),
    ],
)
def test_dense_projection_identity_includes_encoding_behavior(
    tmp_path: Path,
    property_name: str,
    changed_value: str,
) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    projection_path = tmp_path / "dense.sqlite3"
    original = DenseRetrievalService(
        database,
        manifest.corpus_id,
        projection_path=projection_path,
        provider=FakeEmbeddingProvider(),
    ).build_index()
    changed_provider = FakeEmbeddingProvider()
    setattr(changed_provider, property_name, changed_value)
    changed = DenseRetrievalService(
        database,
        manifest.corpus_id,
        projection_path=projection_path,
        provider=changed_provider,
    ).build_index()

    assert changed.projection_id != original.projection_id
    assert changed.built is True
    original_service = DenseRetrievalService(
        database,
        manifest.corpus_id,
        projection_path=projection_path,
        provider=FakeEmbeddingProvider(),
    )
    assert original_service.search("database", limit=1)[0].source_path == "storage.md"


def test_dense_profiles_coexist_with_dynamic_dimensions(tmp_path: Path) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)

    class QwenFakeEmbeddingProvider(FakeEmbeddingProvider):
        profile = EmbeddingProfile.qwen3_embedding_06b
        name = "test/qwen"
        dimensions = 3
        query_encoding = "instructed-query"
        document_encoding = "plain-document"

        @staticmethod
        def _vector(text: str) -> list[float]:
            if "sqlite" in text.casefold() or "database" in text.casefold():
                return [1.0, 0.0, 0.0]
            return [0.0, 1.0, 0.0]

    gte = DenseRetrievalService(
        database,
        manifest.corpus_id,
        provider=FakeEmbeddingProvider(),
    )
    qwen = DenseRetrievalService(
        database,
        manifest.corpus_id,
        profile=EmbeddingProfile.qwen3_embedding_06b,
        provider=QwenFakeEmbeddingProvider(),
    )

    gte_result = gte.build_index()
    qwen_result = qwen.build_index()

    assert gte.projection_path != qwen.projection_path
    assert gte.projection_path.exists()
    assert qwen.projection_path.exists()
    assert gte_result.dimensions == 2
    assert qwen_result.dimensions == 3
    assert gte_result.projection_id != qwen_result.projection_id
    assert gte.search("database", limit=1)[0].source_path == "storage.md"
    assert qwen.search("database", limit=1)[0].source_path == "storage.md"


def test_dense_search_rejects_projection_built_for_another_profile(
    tmp_path: Path,
) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    projection_path = tmp_path / "shared.sqlite3"
    DenseRetrievalService(
        database,
        manifest.corpus_id,
        projection_path=projection_path,
        provider=FakeEmbeddingProvider(),
    ).build_index()

    class QwenFakeEmbeddingProvider(FakeEmbeddingProvider):
        profile = EmbeddingProfile.qwen3_embedding_06b

    service = DenseRetrievalService(
        database,
        manifest.corpus_id,
        projection_path=projection_path,
        profile=EmbeddingProfile.qwen3_embedding_06b,
        provider=QwenFakeEmbeddingProvider(),
    )

    with pytest.raises(DenseIndexError, match="another embedding profile"):
        service.search("database")


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


def test_existing_gte_projection_schema_is_migrated_compatibly(tmp_path: Path) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    projection_path = tmp_path / "dense.sqlite3"
    with sqlite3.connect(projection_path) as connection:
        connection.executescript(
            """
            CREATE TABLE projection_schema (version INTEGER PRIMARY KEY);
            INSERT INTO projection_schema (version) VALUES (2);
            CREATE TABLE dense_projection (
                projection_id TEXT PRIMARY KEY,
                corpus_id TEXT NOT NULL,
                model_name TEXT NOT NULL,
                model_revision TEXT NOT NULL,
                model_license TEXT NOT NULL,
                pipeline_version TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                normalized INTEGER NOT NULL,
                source_text_version TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                vector_table TEXT NOT NULL,
                status TEXT NOT NULL,
                passage_count INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 0
            );
            """
        )
    service = DenseRetrievalService(
        database,
        manifest.corpus_id,
        projection_path=projection_path,
        provider=FakeEmbeddingProvider(),
    )

    result = service.build_index()

    assert result.built is True
    assert result.embedding_profile == "gte-modernbert"
    assert service.search("database", limit=1)[0].source_path == "storage.md"


def test_completed_v2_gte_projection_is_reused_without_reembedding(
    tmp_path: Path,
) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    projection_path = tmp_path / "dense.sqlite3"
    class LegacyCompatibleFakeEmbeddingProvider(FakeEmbeddingProvider):
        context_behavior = "model-native-truncation"
        query_encoding = "plain-text"
        document_encoding = "plain-text"

    provider = LegacyCompatibleFakeEmbeddingProvider()
    service = DenseRetrievalService(
        database,
        manifest.corpus_id,
        projection_path=projection_path,
        provider=provider,
    )
    original = service.build_index()
    source_fingerprint = service.retrieval.index_fingerprint()
    legacy_id = hashlib.sha256(
        "\0".join(
            (
                manifest.corpus_id,
                provider.name,
                provider.revision,
                provider.pipeline_version,
                str(provider.dimensions),
                "passage-text-v1",
                source_fingerprint,
            )
        ).encode()
    ).hexdigest()
    with sqlite3.connect(projection_path) as connection:
        connection.execute(
            "UPDATE dense_embedding SET projection_id = ? WHERE projection_id = ?",
            (legacy_id, original.projection_id),
        )
        connection.execute(
            "UPDATE dense_projection SET projection_id = ? WHERE projection_id = ?",
            (legacy_id, original.projection_id),
        )
        for column in (
            "normalization",
            "context_behavior",
            "query_encoding",
            "document_encoding",
            "profile",
        ):
            connection.execute(f"ALTER TABLE dense_projection DROP COLUMN {column}")
        connection.execute("UPDATE projection_schema SET version = 2")

    assert service.search("database", limit=1)[0].source_path == "storage.md"
    reused = service.build_index()

    assert reused.built is False
    assert reused.projection_id == original.projection_id


def test_search_migrates_v2_schema_before_projection_validation(tmp_path: Path) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    projection_path = tmp_path / "dense.sqlite3"
    with sqlite3.connect(projection_path) as connection:
        connection.executescript(
            """
            CREATE TABLE projection_schema (version INTEGER PRIMARY KEY);
            INSERT INTO projection_schema (version) VALUES (2);
            CREATE TABLE dense_projection (
                projection_id TEXT PRIMARY KEY,
                corpus_id TEXT NOT NULL,
                model_name TEXT NOT NULL,
                model_revision TEXT NOT NULL,
                model_license TEXT NOT NULL,
                pipeline_version TEXT NOT NULL,
                dimensions INTEGER NOT NULL,
                normalized INTEGER NOT NULL,
                source_text_version TEXT NOT NULL,
                source_fingerprint TEXT NOT NULL,
                vector_table TEXT NOT NULL,
                status TEXT NOT NULL,
                passage_count INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 0
            );
            """
        )
    service = DenseRetrievalService(
        database,
        manifest.corpus_id,
        projection_path=projection_path,
        provider=FakeEmbeddingProvider(),
    )

    with pytest.raises(DenseIndexError, match="No dense projection"):
        service.search("database")

    with sqlite3.connect(projection_path) as connection:
        assert connection.execute(
            "SELECT version FROM projection_schema"
        ).fetchone()[0] == 3


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
