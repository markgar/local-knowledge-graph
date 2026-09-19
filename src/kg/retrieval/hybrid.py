from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from kg.db import Database
from kg.models.contracts import SearchResult
from kg.retrieval.dense import (
    DEFAULT_EMBEDDING_PROFILE,
    DenseIndexError,
    DenseRetrievalService,
    EmbeddingProfile,
)
from kg.retrieval.service import RetrievalService, SearchQueryError

DEFAULT_CANDIDATE_LIMIT = 50
DEFAULT_RECIPROCAL_RANK_FUSION_K = 20
DEFAULT_LEXICAL_WEIGHT = 1.0
DEFAULT_DENSE_WEIGHT = 0.5


class DenseSearchService(Protocol):
    def search(
        self,
        query: str,
        *,
        subject: str | None = None,
        limit: int = 20,
        since: datetime | None = None,
        source_path: str | None = None,
    ) -> list[SearchResult]: ...

    def warmup(self) -> None: ...


class HybridRetrievalService:
    def __init__(
        self,
        database: Database,
        corpus_id: str,
        *,
        dense_retrieval: DenseSearchService | None = None,
        candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
        rrf_k: int = DEFAULT_RECIPROCAL_RANK_FUSION_K,
        lexical_weight: float = DEFAULT_LEXICAL_WEIGHT,
        dense_weight: float = DEFAULT_DENSE_WEIGHT,
        embedding_profile: EmbeddingProfile = DEFAULT_EMBEDDING_PROFILE,
        contextual: bool = False,
    ) -> None:
        if candidate_limit < 1:
            raise ValueError("candidate_limit must be at least 1")
        if rrf_k < 0:
            raise ValueError("rrf_k must not be negative")
        if lexical_weight <= 0 or dense_weight <= 0:
            raise ValueError("fusion weights must be positive")
        self.retrieval = RetrievalService(database, corpus_id)
        self.dense_retrieval = dense_retrieval or DenseRetrievalService(
            database,
            corpus_id,
            profile=embedding_profile,
            contextual=contextual,
        )
        self.candidate_limit = candidate_limit
        self.rrf_k = rrf_k
        self.lexical_weight = lexical_weight
        self.dense_weight = dense_weight

    def search(
        self,
        query: str,
        *,
        subject: str | None = None,
        limit: int = 20,
        since: datetime | None = None,
        source_path: str | None = None,
    ) -> list[SearchResult]:
        if not query.strip():
            raise SearchQueryError("Search text must not be blank")
        if limit < 1:
            raise ValueError("limit must be at least 1")

        source_fingerprint = self.retrieval.index_fingerprint()
        candidate_limit = max(limit, self.candidate_limit)
        lexical = self.retrieval.search(
            query,
            subject=subject,
            limit=candidate_limit,
            since=since,
            source_path=source_path,
            query_mode="natural",
        )
        dense = self.dense_retrieval.search(
            query,
            subject=subject,
            limit=candidate_limit,
            since=since,
            source_path=source_path,
        )
        if self.retrieval.index_fingerprint() != source_fingerprint:
            raise DenseIndexError("Corpus changed while hybrid search was running")
        return _reciprocal_rank_fusion(
            lexical,
            dense,
            limit=limit,
            rrf_k=self.rrf_k,
            lexical_weight=self.lexical_weight,
            dense_weight=self.dense_weight,
        )

    def warmup(self) -> None:
        self.dense_retrieval.warmup()


def _reciprocal_rank_fusion(
    lexical: Sequence[SearchResult],
    dense: Sequence[SearchResult],
    *,
    limit: int,
    rrf_k: int,
    lexical_weight: float,
    dense_weight: float,
) -> list[SearchResult]:
    results_by_id = {
        result.record_id: result
        for ranking in (lexical, dense)
        for result in ranking
    }
    weighted_positions = (
        (lexical_weight, _rank_positions(lexical)),
        (dense_weight, _rank_positions(dense)),
    )
    scores = {
        record_id: sum(
            weight / (rrf_k + ranking[record_id])
            for weight, ranking in weighted_positions
            if record_id in ranking
        )
        for record_id in results_by_id
    }
    ranked_ids = sorted(
        results_by_id,
        key=lambda record_id: (-scores[record_id], record_id),
    )[:limit]
    return [
        results_by_id[record_id].model_copy(update={"rank": scores[record_id]})
        for record_id in ranked_ids
    ]


def _rank_positions(ranking: Sequence[SearchResult]) -> dict[str, int]:
    positions: dict[str, int] = {}
    for position, result in enumerate(ranking, start=1):
        positions.setdefault(result.record_id, position)
    return positions
