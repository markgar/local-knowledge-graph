from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from kg.config import load_manifest
from kg.db import Database
from kg.ingest import IngestService
from kg.models.contracts import SearchResult
from kg.retrieval.dense import DenseIndexError
from kg.retrieval.hybrid import (
    DEFAULT_RECIPROCAL_RANK_FUSION_K,
    HybridRetrievalService,
)


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


def _manifest(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\n\nEvidence.\n", encoding="utf-8")
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


@pytest.mark.service
def test_hybrid_search_fuses_unchanged_rankings_and_forwards_filters(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)

    class StubDenseRetrieval:
        def search(self, query: str, **kwargs: object) -> list[SearchResult]:
            assert query == "question"
            assert kwargs == {
                "subject": "Note",
                "limit": 4,
                "since": datetime(2026, 1, 1, tzinfo=UTC),
                "source_path": "note.md",
            }
            return [_result("b"), _result("d"), _result("a")]

        def warmup(self) -> None:
            pass

    service = HybridRetrievalService(
        database,
        manifest.corpus_id,
        dense_retrieval=StubDenseRetrieval(),
        candidate_limit=4,
    )
    since = datetime(2026, 1, 1, tzinfo=UTC)

    def lexical_search(query: str, **kwargs: object) -> list[SearchResult]:
        assert query == "question"
        assert kwargs == {
            "subject": "Note",
            "limit": 4,
            "since": since,
            "source_path": "note.md",
            "query_mode": "natural",
        }
        return [_result("a"), _result("b"), _result("c")]

    monkeypatch.setattr(service.retrieval, "search", lexical_search)
    results = service.search(
        "question",
        subject="Note",
        limit=4,
        since=since,
        source_path="note.md",
    )

    assert [result.record_id for result in results] == ["a", "b", "c", "d"]
    assert results[0].rank == (
        1 / (DEFAULT_RECIPROCAL_RANK_FUSION_K + 1)
        + 0.5 / (DEFAULT_RECIPROCAL_RANK_FUSION_K + 3)
    )


@pytest.mark.service
def test_hybrid_search_breaks_equal_scores_by_record_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)

    class StubDenseRetrieval:
        def search(self, query: str, **kwargs: object) -> list[SearchResult]:
            return [_result("b")]

        def warmup(self) -> None:
            pass

    service = HybridRetrievalService(
        database,
        manifest.corpus_id,
        dense_retrieval=StubDenseRetrieval(),
        candidate_limit=1,
        dense_weight=1.0,
    )
    monkeypatch.setattr(
        service.retrieval,
        "search",
        lambda *args, **kwargs: [_result("a")],
    )

    assert [result.record_id for result in service.search("question", limit=2)] == [
        "a",
        "b",
    ]


@pytest.mark.service
def test_hybrid_search_rejects_a_corpus_change_during_fusion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)

    class StubDenseRetrieval:
        def search(self, query: str, **kwargs: object) -> list[SearchResult]:
            return [_result("a")]

        def warmup(self) -> None:
            pass

    service = HybridRetrievalService(
        database,
        manifest.corpus_id,
        dense_retrieval=StubDenseRetrieval(),
        candidate_limit=1,
    )
    fingerprints = iter(("before", "after"))
    monkeypatch.setattr(
        service.retrieval,
        "index_fingerprint",
        lambda: next(fingerprints),
    )
    monkeypatch.setattr(
        service.retrieval,
        "search",
        lambda *args, **kwargs: [_result("a")],
    )

    with pytest.raises(DenseIndexError, match="Corpus changed"):
        service.search("question")
