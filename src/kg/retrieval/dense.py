from __future__ import annotations

import hashlib
import importlib.metadata
import math
import sqlite3
import time
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Protocol, cast

import sqlite_vec  # type: ignore[import-untyped]

from kg.db import Database
from kg.models.contracts import DenseIndexResult, SearchResult
from kg.retrieval.service import RetrievalService, SearchQueryError

SOURCE_TEXT_VERSION = "passage-text-v1"
ENCODING_PIPELINE_VERSION = "sentence-transformers-normalized-float32-v1"
NORMALIZATION = "l2"
PROJECTION_SCHEMA_VERSION = 3


class EmbeddingProfile(StrEnum):
    gte_modernbert = "gte-modernbert"
    qwen3_embedding_06b = "qwen3-embedding-0.6b"


@dataclass(frozen=True)
class EmbeddingProfileConfig:
    profile: EmbeddingProfile
    model_name: str
    model_revision: str
    model_license: str
    dimensions: int
    context_behavior: str
    query_encoding: str
    document_encoding: str
    query_prompt_name: str | None
    trust_remote_code: bool
    projection_suffix: str


GTE_QUERY_ENCODING = "plain-text"
GTE_DOCUMENT_ENCODING = "plain-text"
QWEN_QUERY_PROMPT = (
    "Instruct: Given a web search query, retrieve relevant passages that answer the query\n"
    "Query:"
)
QWEN_QUERY_ENCODING = f"sentence-transformers-prompt-name=query:{QWEN_QUERY_PROMPT}"
QWEN_DOCUMENT_ENCODING = "plain-text-no-instruction"

EMBEDDING_PROFILES = {
    EmbeddingProfile.gte_modernbert: EmbeddingProfileConfig(
        profile=EmbeddingProfile.gte_modernbert,
        model_name="Alibaba-NLP/gte-modernbert-base",
        model_revision="752e76f479f37e13f5e956c0a277bd7ccea80714",
        model_license="Apache-2.0",
        dimensions=768,
        context_behavior="model-native-truncation",
        query_encoding=GTE_QUERY_ENCODING,
        document_encoding=GTE_DOCUMENT_ENCODING,
        query_prompt_name=None,
        trust_remote_code=True,
        projection_suffix=".dense.sqlite3",
    ),
    EmbeddingProfile.qwen3_embedding_06b: EmbeddingProfileConfig(
        profile=EmbeddingProfile.qwen3_embedding_06b,
        model_name="Qwen/Qwen3-Embedding-0.6B",
        model_revision="97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3",
        model_license="Apache-2.0",
        dimensions=1024,
        context_behavior="model-native-32k-context-with-truncation",
        query_encoding=QWEN_QUERY_ENCODING,
        document_encoding=QWEN_DOCUMENT_ENCODING,
        query_prompt_name="query",
        trust_remote_code=False,
        projection_suffix=".dense.qwen3-embedding-0.6b.sqlite3",
    ),
}
DEFAULT_EMBEDDING_PROFILE = EmbeddingProfile.gte_modernbert


class DenseIndexError(RuntimeError):
    pass


class EmbeddingProvider(Protocol):
    profile: EmbeddingProfile
    name: str
    revision: str
    license: str
    pipeline_version: str
    dimensions: int
    normalization: str
    context_behavior: str
    query_encoding: str
    document_encoding: str

    def encode_documents(
        self,
        texts: Sequence[str],
        *,
        batch_size: int,
    ) -> list[list[float]]: ...

    def encode_query(self, text: str) -> list[float]: ...


class SentenceTransformerEmbeddingProvider:
    profile = DEFAULT_EMBEDDING_PROFILE
    name = EMBEDDING_PROFILES[DEFAULT_EMBEDDING_PROFILE].model_name
    revision = EMBEDDING_PROFILES[DEFAULT_EMBEDDING_PROFILE].model_revision
    license = EMBEDDING_PROFILES[DEFAULT_EMBEDDING_PROFILE].model_license
    dimensions = EMBEDDING_PROFILES[DEFAULT_EMBEDDING_PROFILE].dimensions
    normalization = NORMALIZATION
    context_behavior = EMBEDDING_PROFILES[DEFAULT_EMBEDDING_PROFILE].context_behavior
    query_encoding = EMBEDDING_PROFILES[DEFAULT_EMBEDDING_PROFILE].query_encoding
    document_encoding = EMBEDDING_PROFILES[DEFAULT_EMBEDDING_PROFILE].document_encoding
    query_prompt_name: str | None = None

    def __init__(
        self,
        profile: EmbeddingProfile = DEFAULT_EMBEDDING_PROFILE,
    ) -> None:
        config = EMBEDDING_PROFILES[profile]
        self.profile = profile
        self.name = config.model_name
        self.revision = config.model_revision
        self.license = config.model_license
        self.dimensions = config.dimensions
        self.context_behavior = config.context_behavior
        self.query_encoding = config.query_encoding
        self.document_encoding = config.document_encoding
        self.query_prompt_name = config.query_prompt_name
        try:
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(
                self.name,
                revision=self.revision,
                trust_remote_code=config.trust_remote_code,
            )
            self.pipeline_version = "|".join(
                (
                    ENCODING_PIPELINE_VERSION,
                    "sentence-transformers="
                    f"{importlib.metadata.version('sentence-transformers')}",
                    f"transformers={importlib.metadata.version('transformers')}",
                    f"torch={importlib.metadata.version('torch')}",
                    f"sqlite-vec={importlib.metadata.version('sqlite-vec')}",
                    f"device={self._model.device}",
                    f"dtype={next(self._model.parameters()).dtype}",
                )
            )
        except (ImportError, OSError, RuntimeError) as exc:
            raise DenseIndexError(f"Could not load embedding model: {exc}") from exc
        dimensions = self._model.get_embedding_dimension()
        if dimensions != self.dimensions:
            raise DenseIndexError(
                f"Expected {self.dimensions} embedding dimensions, got {dimensions}"
            )

    def encode_documents(
        self,
        texts: Sequence[str],
        *,
        batch_size: int,
    ) -> list[list[float]]:
        try:
            encoded = self._model.encode(
                texts,
                batch_size=batch_size,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            return _coerce_vectors(encoded)
        except DenseIndexError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError, OverflowError) as exc:
            raise DenseIndexError(f"Could not encode documents: {exc}") from exc

    def encode_query(self, text: str) -> list[float]:
        try:
            encoded = self._model.encode(
                [text],
                normalize_embeddings=True,
                show_progress_bar=False,
                prompt_name=self.query_prompt_name,
            )
            vectors = _coerce_vectors(encoded)
        except DenseIndexError:
            raise
        except (OSError, RuntimeError, TypeError, ValueError, OverflowError) as exc:
            raise DenseIndexError(f"Could not encode query: {exc}") from exc
        if len(vectors) != 1:
            raise DenseIndexError("Embedding model returned an invalid query vector")
        return vectors[0]


class DenseRetrievalService:
    def __init__(
        self,
        database: Database,
        corpus_id: str,
        *,
        projection_path: Path | None = None,
        provider: EmbeddingProvider | None = None,
        profile: EmbeddingProfile = DEFAULT_EMBEDDING_PROFILE,
    ) -> None:
        self.database = database
        self.corpus_id = corpus_id
        self.profile = profile
        self.projection_path = projection_path or _default_projection_path(
            database.path,
            profile,
        )
        self.retrieval = RetrievalService(database, corpus_id)
        self._embedding_provider = provider
        if provider is not None and provider.profile != profile:
            raise DenseIndexError(
                f"Embedding provider profile {provider.profile.value!r} does not match "
                f"selected profile {profile.value!r}"
            )

    def build_index(
        self,
        provider: EmbeddingProvider | None = None,
        *,
        batch_size: int = 32,
    ) -> DenseIndexResult:
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        embedding_provider = provider or self._provider()
        self._validate_provider_profile(embedding_provider)
        source_fingerprint = self.retrieval.index_fingerprint()
        projection_id = _projection_id(
            self.corpus_id,
            embedding_provider,
            source_fingerprint,
        )
        started = time.perf_counter()
        try:
            self.projection_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise DenseIndexError(
                f"Could not create dense projection directory: {exc}"
            ) from exc

        with self._projection_connection() as connection:
            _initialize_projection_schema(connection)
            existing = connection.execute(
                """
                SELECT passage_count
                FROM dense_projection
                WHERE projection_id = ? AND status = 'completed'
                """,
                (projection_id,),
            ).fetchone()
            if existing:
                with self.database.transaction() as canonical_connection:
                    if (
                        self.retrieval.index_fingerprint(canonical_connection)
                        != source_fingerprint
                    ):
                        raise DenseIndexError(
                            "Corpus changed while the dense projection was selected"
                        )
                    with connection:
                        self._activate_projection(connection, projection_id)
                return self._index_result(
                    projection_id,
                    embedding_provider,
                    int(existing["passage_count"]),
                    False,
                    started,
                )

        passages = self.retrieval.current_passages()
        if self.retrieval.index_fingerprint() != source_fingerprint:
            raise DenseIndexError("Corpus changed while passages were being selected")
        vectors: list[list[float]] = []
        texts = [passage.quote for passage in passages]
        for start in range(0, len(texts), batch_size):
            batch = embedding_provider.encode_documents(
                texts[start : start + batch_size],
                batch_size=batch_size,
            )
            vectors.extend(
                _validate_vector(vector, embedding_provider.dimensions)
                for vector in batch
            )
        if len(vectors) != len(passages):
            raise DenseIndexError(
                "Embedding model returned a different number of vectors than passages"
            )
        if self.retrieval.index_fingerprint() != source_fingerprint:
            raise DenseIndexError("Corpus changed while the dense projection was building")

        built = True
        passage_count = len(passages)
        with self.database.transaction() as canonical_connection:
            if (
                self.retrieval.index_fingerprint(canonical_connection)
                != source_fingerprint
            ):
                raise DenseIndexError(
                    "Corpus changed while the dense projection was activated"
                )
            with self._projection_connection() as connection:
                _initialize_projection_schema(connection)
                with connection:
                    existing = connection.execute(
                        """
                        SELECT passage_count
                        FROM dense_projection
                        WHERE projection_id = ? AND status = 'completed'
                        """,
                        (projection_id,),
                    ).fetchone()
                    if existing:
                        built = False
                        passage_count = int(existing["passage_count"])
                    else:
                        vector_table = _vector_table_name(projection_id)
                        connection.execute(
                            f"""
                            CREATE VIRTUAL TABLE {vector_table} USING vec0(
                                document_id TEXT PARTITION KEY,
                                embedding FLOAT[{embedding_provider.dimensions}]
                            )
                            """
                        )
                        connection.execute(
                            """
                            INSERT INTO dense_projection (
                                projection_id, corpus_id, model_name, model_revision,
                                model_license, pipeline_version, dimensions, normalized,
                                normalization, source_text_version, context_behavior,
                                query_encoding, document_encoding, source_fingerprint,
                                vector_table, status, passage_count, created_at, profile
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?,
                                'completed', ?, datetime('now'), ?)
                            """,
                            (
                                projection_id,
                                self.corpus_id,
                                embedding_provider.name,
                                embedding_provider.revision,
                                embedding_provider.license,
                                embedding_provider.pipeline_version,
                                embedding_provider.dimensions,
                                embedding_provider.normalization,
                                SOURCE_TEXT_VERSION,
                                embedding_provider.context_behavior,
                                embedding_provider.query_encoding,
                                embedding_provider.document_encoding,
                                source_fingerprint,
                                vector_table,
                                len(passages),
                                embedding_provider.profile.value,
                            ),
                        )
                        connection.executemany(
                            """
                            INSERT INTO dense_embedding (
                                projection_id, vector_rowid, passage_id, anchor_id,
                                revision_id, document_id
                            ) VALUES (?, ?, ?, ?, ?, ?)
                            """,
                            [
                                (
                                    projection_id,
                                    rowid,
                                    passage.passage_id,
                                    passage.anchor_id,
                                    passage.revision_id,
                                    passage.document_id,
                                )
                                for rowid, passage in enumerate(passages, start=1)
                            ],
                        )
                        connection.executemany(
                            f"""
                            INSERT INTO {vector_table} (rowid, document_id, embedding)
                            VALUES (?, ?, ?)
                            """,
                            [
                                (
                                    rowid,
                                    passage.document_id,
                                    _serialize_vector(vector),
                                )
                                for rowid, (passage, vector) in enumerate(
                                    zip(passages, vectors, strict=True),
                                    start=1,
                                )
                            ],
                        )
                    self._activate_projection(connection, projection_id)
        return self._index_result(
            projection_id,
            embedding_provider,
            passage_count,
            built,
            started,
        )

    def search(
        self,
        query: str,
        *,
        provider: EmbeddingProvider | None = None,
        subject: str | None = None,
        limit: int = 20,
        since: datetime | None = None,
        source_path: str | None = None,
    ) -> list[SearchResult]:
        if not query.strip():
            raise SearchQueryError("Search text must not be blank")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        current_fingerprint = self.retrieval.index_fingerprint()

        with self._projection_connection(read_only=True) as connection:
            _initialize_projection_schema(connection)
            embedding_provider = provider or self._provider()
            self._validate_provider_profile(embedding_provider)
            projection_id = _projection_id(
                self.corpus_id,
                embedding_provider,
                current_fingerprint,
            )
            projection = connection.execute(
                """
                SELECT *
                FROM dense_projection
                WHERE projection_id = ? AND status = 'completed'
                """,
                (projection_id,),
            ).fetchone()
            if not projection:
                self._raise_unavailable_projection(
                    connection,
                    current_fingerprint,
                )
            _validate_projection_model(projection, embedding_provider)
            query_vector = _validate_vector(
                embedding_provider.encode_query(query),
                embedding_provider.dimensions,
            )
            eligible = self.retrieval.eligible_passages(
                subject=subject,
                since=since,
                source_path=source_path,
            )
            if not eligible:
                if self.retrieval.index_fingerprint() != current_fingerprint:
                    raise DenseIndexError(
                        "Corpus changed while dense search was running"
                    )
                return []
            mapping_rows = connection.execute(
                """
                SELECT vector_rowid, passage_id, document_id
                FROM dense_embedding
                WHERE projection_id = ?
                """,
                (projection["projection_id"],),
            ).fetchall()
            mapping = {
                row["vector_rowid"]: (row["passage_id"], row["document_id"])
                for row in mapping_rows
            }
            vector_table = _checked_vector_table(projection["vector_table"])
            query_blob = _serialize_vector(query_vector)
            if subject is None and since is None and source_path is None:
                neighbors = connection.execute(
                    f"""
                    SELECT rowid, distance
                    FROM {vector_table}
                    WHERE embedding MATCH ? AND k = ?
                    ORDER BY distance
                    """,
                    (query_blob, limit),
                ).fetchall()
            else:
                eligible_by_document: dict[str, set[str]] = {}
                for passage_id, document_id in eligible.items():
                    eligible_by_document.setdefault(document_id, set()).add(passage_id)
                neighbors = []
                for document_id, eligible_ids in eligible_by_document.items():
                    document_size = sum(
                        mapped_document_id == document_id
                        for _, mapped_document_id in mapping.values()
                    )
                    candidate_count = (
                        min(limit, document_size)
                        if len(eligible_ids) == document_size
                        else document_size
                    )
                    document_neighbors = connection.execute(
                        f"""
                        SELECT rowid, distance
                        FROM {vector_table}
                        WHERE embedding MATCH ?
                          AND k = ?
                          AND document_id = ?
                        ORDER BY distance
                        """,
                        (query_blob, candidate_count, document_id),
                    ).fetchall()
                    neighbors.extend(
                        row
                        for row in document_neighbors
                        if mapping[row["rowid"]][0] in eligible_ids
                    )

        ranked = sorted(
            (
                (mapping[row["rowid"]][0], float(row["distance"]))
                for row in neighbors
            ),
            key=lambda item: (item[1], item[0]),
        )[:limit]
        results = self.retrieval.passage_results(ranked)
        if self.retrieval.index_fingerprint() != current_fingerprint:
            raise DenseIndexError("Corpus changed while dense search was running")
        return results

    def _raise_unavailable_projection(
        self,
        connection: sqlite3.Connection,
        current_fingerprint: str,
    ) -> None:
        projections = connection.execute(
            """
            SELECT profile, source_fingerprint
            FROM dense_projection
            WHERE corpus_id = ? AND status = 'completed'
            """,
            (self.corpus_id,),
        ).fetchall()
        if not projections:
            raise DenseIndexError(
                f"No dense projection is available for embedding profile "
                f"{self.profile.value!r}; run 'kg dense-index --embedding-profile "
                f"{self.profile.value}' first."
            )
        matching_profile = [
            projection
            for projection in projections
            if projection["profile"] == self.profile.value
        ]
        if not matching_profile:
            raise DenseIndexError(
                f"The dense projection database was built for another embedding "
                f"profile; run 'kg dense-index --embedding-profile "
                f"{self.profile.value}'."
            )
        if all(
            projection["source_fingerprint"] != current_fingerprint
            for projection in matching_profile
        ):
            raise DenseIndexError(
                f"The dense projection for embedding profile {self.profile.value!r} "
                "is stale; run 'kg dense-index' again."
            )
        raise DenseIndexError(
            f"The dense projection is incompatible with embedding profile "
            f"{self.profile.value!r}; run 'kg dense-index --embedding-profile "
            f"{self.profile.value}'."
        )

    def warmup(self) -> None:
        provider = self._provider()
        _validate_vector(provider.encode_query("warmup"), provider.dimensions)

    def _provider(self) -> EmbeddingProvider:
        if self._embedding_provider is None:
            self._embedding_provider = SentenceTransformerEmbeddingProvider(self.profile)
        return self._embedding_provider

    def _validate_provider_profile(self, provider: EmbeddingProvider) -> None:
        if provider.profile != self.profile:
            raise DenseIndexError(
                f"Embedding provider profile {provider.profile.value!r} does not match "
                f"selected profile {self.profile.value!r}"
            )

    @contextmanager
    def _projection_connection(
        self,
        *,
        read_only: bool = False,
    ) -> Iterator[sqlite3.Connection]:
        if read_only and not self.projection_path.exists():
            raise DenseIndexError(
                "No dense projection is available; run 'kg dense-index' first."
            )
        try:
            connection = sqlite3.connect(self.projection_path)
        except (OSError, sqlite3.Error) as exc:
            raise DenseIndexError(
                f"Could not open dense projection database: {exc}"
            ) from exc
        try:
            connection.row_factory = sqlite3.Row
            connection.enable_load_extension(True)
            sqlite_vec.load(connection)
            connection.enable_load_extension(False)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            yield connection
        except sqlite3.Error as exc:
            raise DenseIndexError(f"Dense projection database error: {exc}") from exc
        finally:
            connection.close()

    def _activate_projection(
        self,
        connection: sqlite3.Connection,
        projection_id: str,
    ) -> None:
        connection.execute(
            "UPDATE dense_projection SET is_active = 0 WHERE corpus_id = ?",
            (self.corpus_id,),
        )
        connection.execute(
            "UPDATE dense_projection SET is_active = 1 WHERE projection_id = ?",
            (projection_id,),
        )

    def _index_result(
        self,
        projection_id: str,
        provider: EmbeddingProvider,
        passage_count: int,
        built: bool,
        started: float,
    ) -> DenseIndexResult:
        try:
            index_bytes = self.projection_path.stat().st_size
        except OSError as exc:
            raise DenseIndexError(
                f"Could not inspect dense projection database: {exc}"
            ) from exc
        return DenseIndexResult(
            corpus_id=self.corpus_id,
            projection_id=projection_id,
            model_name=provider.name,
            model_revision=provider.revision,
            model_license=provider.license,
            pipeline_version=provider.pipeline_version,
            dimensions=provider.dimensions,
            passage_count=passage_count,
            built=built,
            duration_ms=(time.perf_counter() - started) * 1000,
            index_path=str(self.projection_path),
            index_bytes=index_bytes,
            embedding_profile=provider.profile.value,
            normalization=provider.normalization,
            context_behavior=provider.context_behavior,
            query_encoding=provider.query_encoding,
            document_encoding=provider.document_encoding,
        )


def _initialize_projection_schema(connection: sqlite3.Connection) -> None:
    schema_table = connection.execute(
        """
        SELECT 1
        FROM sqlite_master
        WHERE type = 'table' AND name = 'projection_schema'
        """
    ).fetchone()
    if schema_table:
        versions = connection.execute(
            "SELECT version FROM projection_schema ORDER BY version"
        ).fetchall()
        version_values = [row["version"] for row in versions]
        if version_values == [2]:
            _migrate_projection_schema_v2(connection)
        elif version_values != [PROJECTION_SCHEMA_VERSION]:
            raise DenseIndexError(
                "Unsupported dense projection schema version; delete the dense "
                "database and rebuild it."
            )
    connection.executescript(
        f"""
        CREATE TABLE IF NOT EXISTS projection_schema (
            version INTEGER PRIMARY KEY
        );
        INSERT OR IGNORE INTO projection_schema (version)
        VALUES ({PROJECTION_SCHEMA_VERSION});

        CREATE TABLE IF NOT EXISTS dense_projection (
            projection_id TEXT PRIMARY KEY,
            corpus_id TEXT NOT NULL,
            model_name TEXT NOT NULL,
            model_revision TEXT NOT NULL,
            model_license TEXT NOT NULL,
            pipeline_version TEXT NOT NULL,
            dimensions INTEGER NOT NULL,
            normalized INTEGER NOT NULL CHECK (normalized IN (0, 1)),
            normalization TEXT NOT NULL,
            source_text_version TEXT NOT NULL,
            context_behavior TEXT NOT NULL,
            query_encoding TEXT NOT NULL,
            document_encoding TEXT NOT NULL,
            source_fingerprint TEXT NOT NULL,
            vector_table TEXT NOT NULL,
            status TEXT NOT NULL,
            passage_count INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            profile TEXT NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 0 CHECK (is_active IN (0, 1))
        );

        CREATE TABLE IF NOT EXISTS dense_embedding (
            projection_id TEXT NOT NULL
                REFERENCES dense_projection(projection_id) ON DELETE CASCADE,
            vector_rowid INTEGER NOT NULL,
            passage_id TEXT NOT NULL,
            anchor_id TEXT NOT NULL,
            revision_id TEXT NOT NULL,
            document_id TEXT NOT NULL,
            PRIMARY KEY (projection_id, passage_id)
        );

        CREATE INDEX IF NOT EXISTS dense_projection_corpus_active_idx
        ON dense_projection(corpus_id, is_active);
        """
    )


def _migrate_projection_schema_v2(connection: sqlite3.Connection) -> None:
    connection.executescript(
        f"""
        ALTER TABLE dense_projection ADD COLUMN normalization TEXT NOT NULL
            DEFAULT '{NORMALIZATION}';
        ALTER TABLE dense_projection ADD COLUMN context_behavior TEXT NOT NULL
            DEFAULT '{EMBEDDING_PROFILES[DEFAULT_EMBEDDING_PROFILE].context_behavior}';
        ALTER TABLE dense_projection ADD COLUMN query_encoding TEXT NOT NULL
            DEFAULT '{GTE_QUERY_ENCODING}';
        ALTER TABLE dense_projection ADD COLUMN document_encoding TEXT NOT NULL
            DEFAULT '{GTE_DOCUMENT_ENCODING}';
        ALTER TABLE dense_projection ADD COLUMN profile TEXT NOT NULL
            DEFAULT '{DEFAULT_EMBEDDING_PROFILE.value}';
        UPDATE projection_schema SET version = {PROJECTION_SCHEMA_VERSION};
        """
    )
    projections = connection.execute(
        """
        SELECT *
        FROM dense_projection
        WHERE status = 'completed'
        """
    ).fetchall()
    for projection in projections:
        canonical_id = _projection_id_values(
            corpus_id=str(projection["corpus_id"]),
            profile=str(projection["profile"]),
            model_name=str(projection["model_name"]),
            model_revision=str(projection["model_revision"]),
            pipeline_version=str(projection["pipeline_version"]),
            dimensions=int(projection["dimensions"]),
            normalization=str(projection["normalization"]),
            source_text_version=str(projection["source_text_version"]),
            context_behavior=str(projection["context_behavior"]),
            query_encoding=str(projection["query_encoding"]),
            document_encoding=str(projection["document_encoding"]),
            source_fingerprint=str(projection["source_fingerprint"]),
        )
        legacy_id = str(projection["projection_id"])
        if canonical_id == legacy_id:
            continue
        connection.execute(
            """
            INSERT INTO dense_projection (
                projection_id, corpus_id, model_name, model_revision, model_license,
                pipeline_version, dimensions, normalized, normalization,
                source_text_version, context_behavior, query_encoding,
                document_encoding, source_fingerprint, vector_table, status,
                passage_count, created_at, profile, is_active
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                canonical_id,
                projection["corpus_id"],
                projection["model_name"],
                projection["model_revision"],
                projection["model_license"],
                projection["pipeline_version"],
                projection["dimensions"],
                projection["normalized"],
                projection["normalization"],
                projection["source_text_version"],
                projection["context_behavior"],
                projection["query_encoding"],
                projection["document_encoding"],
                projection["source_fingerprint"],
                projection["vector_table"],
                projection["status"],
                projection["passage_count"],
                projection["created_at"],
                projection["profile"],
                projection["is_active"],
            ),
        )
        connection.execute(
            """
            UPDATE dense_embedding
            SET projection_id = ?
            WHERE projection_id = ?
            """,
            (canonical_id, legacy_id),
        )
        connection.execute(
            "DELETE FROM dense_projection WHERE projection_id = ?",
            (legacy_id,),
        )


def _default_projection_path(
    database_path: Path,
    profile: EmbeddingProfile,
) -> Path:
    return database_path.with_name(
        f"{database_path.stem}{EMBEDDING_PROFILES[profile].projection_suffix}"
    )


def _projection_id(
    corpus_id: str,
    provider: EmbeddingProvider,
    source_fingerprint: str,
) -> str:
    return _projection_id_values(
        corpus_id=corpus_id,
        profile=provider.profile.value,
        model_name=provider.name,
        model_revision=provider.revision,
        pipeline_version=provider.pipeline_version,
        dimensions=provider.dimensions,
        normalization=provider.normalization,
        source_text_version=SOURCE_TEXT_VERSION,
        context_behavior=provider.context_behavior,
        query_encoding=provider.query_encoding,
        document_encoding=provider.document_encoding,
        source_fingerprint=source_fingerprint,
    )


def _projection_id_values(
    *,
    corpus_id: str,
    profile: str,
    model_name: str,
    model_revision: str,
    pipeline_version: str,
    dimensions: int,
    normalization: str,
    source_text_version: str,
    context_behavior: str,
    query_encoding: str,
    document_encoding: str,
    source_fingerprint: str,
) -> str:
    value = "\0".join(
        (
            corpus_id,
            profile,
            model_name,
            model_revision,
            pipeline_version,
            str(dimensions),
            normalization,
            source_text_version,
            context_behavior,
            query_encoding,
            document_encoding,
            source_fingerprint,
        )
    )
    return hashlib.sha256(value.encode()).hexdigest()


def _coerce_vectors(value: object) -> list[list[float]]:
    try:
        tolist = getattr(value, "tolist", None)
        raw = tolist() if callable(tolist) else value
    except (OSError, RuntimeError, TypeError, ValueError, OverflowError) as exc:
        raise DenseIndexError(f"Could not convert embedding vectors: {exc}") from exc
    if not isinstance(raw, list):
        raise DenseIndexError("Embedding model returned an unsupported vector type")
    vectors: list[list[float]] = []
    for vector in raw:
        if not isinstance(vector, list):
            raise DenseIndexError("Embedding model returned an unsupported vector shape")
        try:
            vectors.append([float(component) for component in vector])
        except (TypeError, ValueError, OverflowError) as exc:
            raise DenseIndexError(
                f"Could not convert embedding vector components: {exc}"
            ) from exc
    return vectors


def _validate_vector(vector: Sequence[float], dimensions: int) -> list[float]:
    if len(vector) != dimensions:
        raise DenseIndexError(
            f"Expected an embedding with {dimensions} dimensions, got {len(vector)}"
        )
    values = [float(component) for component in vector]
    if any(not math.isfinite(component) for component in values):
        raise DenseIndexError("Embedding contains a non-finite value")
    magnitude = math.sqrt(sum(component * component for component in values))
    if magnitude == 0:
        raise DenseIndexError("Embedding model returned a zero vector")
    return [component / magnitude for component in values]


def _serialize_vector(vector: Sequence[float]) -> bytes:
    return cast(bytes, sqlite_vec.serialize_float32(list(vector)))


def _vector_table_name(projection_id: str) -> str:
    return f"dense_vec_{projection_id[:24]}"


def _checked_vector_table(value: object) -> str:
    if not isinstance(value, str) or not value.startswith("dense_vec_"):
        raise DenseIndexError("Dense projection contains an invalid vector table name")
    suffix = value.removeprefix("dense_vec_")
    if len(suffix) != 24 or any(
        character not in "0123456789abcdef" for character in suffix
    ):
        raise DenseIndexError("Dense projection contains an invalid vector table name")
    return value


def _validate_projection_model(
    projection: sqlite3.Row,
    provider: EmbeddingProvider,
) -> None:
    actual = (
        projection["model_name"],
        projection["model_revision"],
        projection["model_license"],
        projection["pipeline_version"],
        projection["dimensions"],
        projection["normalization"],
        projection["source_text_version"],
        projection["context_behavior"],
        projection["query_encoding"],
        projection["document_encoding"],
        projection["profile"],
    )
    expected = (
        provider.name,
        provider.revision,
        provider.license,
        provider.pipeline_version,
        provider.dimensions,
        provider.normalization,
        SOURCE_TEXT_VERSION,
        provider.context_behavior,
        provider.query_encoding,
        provider.document_encoding,
        provider.profile.value,
    )
    if actual != expected:
        raise DenseIndexError(
            f"The dense projection is incompatible with embedding profile "
            f"{provider.profile.value!r}; run 'kg dense-index --embedding-profile "
            f"{provider.profile.value}'."
        )
