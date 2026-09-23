from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kg.config import load_manifest
from kg.db import Database
from kg.ingest import IngestService
from kg.models.contracts import SearchResult
from kg.retrieval.rerank import RerankedRetrievalService, RerankerError


def _result(record_id: str) -> SearchResult:
    return SearchResult(
        record_id=record_id,
        record_type="passage",
        title=record_id,
        source_path=f"{record_id}.md",
        source_revision_id=f"{record_id}-revision",
        anchor_id=f"{record_id}-anchor",
        quote=f"{record_id} evidence",
        rank=0.0,
    )


def _database(tmp_path: Path) -> Database:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\n\nEvidence.\n", encoding="utf-8")
    manifest_path = tmp_path / "corpus.yml"
    manifest_path.write_text(
        "corpus_id: test\n"
        "display_name: Test\n"
        "vault_root: vault\n"
        "database: index.sqlite3\n"
        "include: ['*.md']\n",
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    return database


@pytest.mark.parametrize("contextual", [False, True])
def test_reranked_search_scores_unchanged_hybrid_candidates_and_forwards_filters(
    tmp_path: Path,
    contextual: bool,
) -> None:
    since = datetime(2026, 1, 1, tzinfo=UTC)

    class StubHybridRetrieval:
        def search(self, query: str, **kwargs: object) -> list[SearchResult]:
            assert query == "question"
            assert kwargs == {
                "subject": "Note",
                "limit": 3,
                "since": since,
                "source_path": "note.md",
            }
            return [_result("a"), _result("b"), _result("c")]

        def warmup(self) -> None:
            pass

    class StubReranker:
        name = "stub"
        revision = "revision"
        license = "Apache-2.0"
        pipeline_version = "stub-v1"

        def score(
            self,
            query: str,
            passages: Sequence[str],
            *,
            batch_size: int,
        ) -> list[float]:
            assert query == "question"
            assert passages == [
                f"Title: {name}\n\n{name} evidence" if contextual else f"{name} evidence"
                for name in ("a", "b", "c")
            ]
            assert batch_size == 2
            return [0.1, 0.9, 0.4]

    service = RerankedRetrievalService(
        _database(tmp_path),
        "test",
        hybrid_retrieval=StubHybridRetrieval(),
        reranker=StubReranker(),
        candidate_limit=3,
        batch_size=2,
        contextual=contextual,
    )
    results = service.search(
        "question",
        subject="Note",
        limit=2,
        since=since,
        source_path="note.md",
    )

    assert [result.record_id for result in results] == ["b", "c"]
    assert [result.rank for result in results] == [0.9, 0.4]
    assert [result.quote for result in results] == ["b evidence", "c evidence"]


def test_reranked_search_breaks_equal_scores_by_record_id(tmp_path: Path) -> None:
    class StubHybridRetrieval:
        def search(self, query: str, **kwargs: object) -> list[SearchResult]:
            return [_result("b"), _result("a")]

        def warmup(self) -> None:
            pass

    class StubReranker:
        name = "stub"
        revision = "revision"
        license = "Apache-2.0"
        pipeline_version = "stub-v1"

        def score(
            self,
            query: str,
            passages: Sequence[str],
            *,
            batch_size: int,
        ) -> list[float]:
            return [0.5, 0.5]

    service = RerankedRetrievalService(
        _database(tmp_path),
        "test",
        hybrid_retrieval=StubHybridRetrieval(),
        reranker=StubReranker(),
        candidate_limit=2,
    )

    assert [result.record_id for result in service.search("question")] == ["a", "b"]


def test_reranked_search_expands_candidates_to_the_requested_limit(
    tmp_path: Path,
) -> None:
    class StubHybridRetrieval:
        def search(self, query: str, **kwargs: object) -> list[SearchResult]:
            assert kwargs["limit"] == 100
            return [_result(f"{index:03}") for index in range(100)]

        def warmup(self) -> None:
            pass

    class StubReranker:
        name = "stub"
        revision = "revision"
        license = "Apache-2.0"
        pipeline_version = "stub-v1"

        def score(
            self,
            query: str,
            passages: Sequence[str],
            *,
            batch_size: int,
        ) -> list[float]:
            return [1.0] * len(passages)

    service = RerankedRetrievalService(
        _database(tmp_path),
        "test",
        hybrid_retrieval=StubHybridRetrieval(),
        reranker=StubReranker(),
        candidate_limit=2,
    )

    results = service.search("question", limit=100)

    assert len(results) == 100
    assert [result.record_id for result in results[:3]] == ["000", "001", "002"]


def test_reranked_search_rejects_invalid_scores(tmp_path: Path) -> None:
    class StubHybridRetrieval:
        def search(self, query: str, **kwargs: object) -> list[SearchResult]:
            return [_result("a")]

        def warmup(self) -> None:
            pass

    class StubReranker:
        name = "stub"
        revision = "revision"
        license = "Apache-2.0"
        pipeline_version = "stub-v1"

        def score(
            self,
            query: str,
            passages: Sequence[str],
            *,
            batch_size: int,
        ) -> list[float]:
            return [float("nan")]

    service = RerankedRetrievalService(
        _database(tmp_path),
        "test",
        hybrid_retrieval=StubHybridRetrieval(),
        reranker=StubReranker(),
    )

    with pytest.raises(RerankerError, match="non-finite"):
        service.search("question")


def test_reranked_search_rejects_a_corpus_change(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class StubHybridRetrieval:
        def search(self, query: str, **kwargs: object) -> list[SearchResult]:
            return [_result("a")]

        def warmup(self) -> None:
            pass

    class StubReranker:
        name = "stub"
        revision = "revision"
        license = "Apache-2.0"
        pipeline_version = "stub-v1"

        def score(
            self,
            query: str,
            passages: Sequence[str],
            *,
            batch_size: int,
        ) -> list[float]:
            return [0.5]

    service = RerankedRetrievalService(
        _database(tmp_path),
        "test",
        hybrid_retrieval=StubHybridRetrieval(),
        reranker=StubReranker(),
    )
    fingerprints = iter(("before", "after"))
    monkeypatch.setattr(
        service.retrieval,
        "index_fingerprint",
        lambda: next(fingerprints),
    )

    with pytest.raises(RerankerError, match="Corpus changed"):
        service.search("question")
