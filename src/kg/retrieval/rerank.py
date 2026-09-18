from __future__ import annotations

import importlib.metadata
import math
from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from kg.db import Database
from kg.models.contracts import SearchResult
from kg.retrieval.hybrid import HybridRetrievalService
from kg.retrieval.service import RetrievalService, SearchQueryError

MODEL_NAME = "cross-encoder/ms-marco-MiniLM-L6-v2"
MODEL_REVISION = "233902d25c440f23af6f7d6e94d2946bac0bee0a"
MODEL_LICENSE = "Apache-2.0"
SCORING_PIPELINE_VERSION = "sentence-transformers-cross-encoder-raw-score-v1"
DEFAULT_CANDIDATE_LIMIT = 50
DEFAULT_BATCH_SIZE = 32


class RerankerError(RuntimeError):
    pass


class HybridSearchService(Protocol):
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


class RerankerProvider(Protocol):
    name: str
    revision: str
    license: str
    pipeline_version: str

    def score(
        self,
        query: str,
        passages: Sequence[str],
        *,
        batch_size: int,
    ) -> list[float]: ...


class SentenceTransformerCrossEncoderProvider:
    name = MODEL_NAME
    revision = MODEL_REVISION
    license = MODEL_LICENSE

    def __init__(self) -> None:
        try:
            self.pipeline_version = "|".join(
                (
                    SCORING_PIPELINE_VERSION,
                    "sentence-transformers="
                    f"{importlib.metadata.version('sentence-transformers')}",
                    f"transformers={importlib.metadata.version('transformers')}",
                    f"torch={importlib.metadata.version('torch')}",
                )
            )
            from sentence_transformers import CrossEncoder

            self._model = CrossEncoder(
                self.name,
                revision=self.revision,
                trust_remote_code=False,
            )
        except (ImportError, OSError, RuntimeError) as exc:
            raise RerankerError(f"Could not load reranker model: {exc}") from exc

    def score(
        self,
        query: str,
        passages: Sequence[str],
        *,
        batch_size: int,
    ) -> list[float]:
        try:
            scores = self._model.predict(
                [(query, passage) for passage in passages],
                batch_size=batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
            )
        except (OSError, RuntimeError, ValueError) as exc:
            raise RerankerError(f"Could not score reranking candidates: {exc}") from exc
        values = scores.tolist() if hasattr(scores, "tolist") else scores
        if not isinstance(values, list):
            raise RerankerError("Reranker model returned an invalid score collection")
        return [_coerce_score(value) for value in values]


class RerankedRetrievalService:
    def __init__(
        self,
        database: Database,
        corpus_id: str,
        *,
        hybrid_retrieval: HybridSearchService | None = None,
        reranker: RerankerProvider | None = None,
        candidate_limit: int = DEFAULT_CANDIDATE_LIMIT,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        if candidate_limit < 1:
            raise ValueError("candidate_limit must be at least 1")
        if batch_size < 1:
            raise ValueError("batch_size must be at least 1")
        self.retrieval = RetrievalService(database, corpus_id)
        self.hybrid_retrieval = hybrid_retrieval or HybridRetrievalService(
            database,
            corpus_id,
        )
        self._reranker = reranker
        self.candidate_limit = candidate_limit
        self.batch_size = batch_size

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
        candidates = self.hybrid_retrieval.search(
            query,
            subject=subject,
            limit=self.candidate_limit,
            since=since,
            source_path=source_path,
        )
        if not candidates:
            return []
        scores = self._provider().score(
            query,
            [candidate.quote for candidate in candidates],
            batch_size=self.batch_size,
        )
        if len(scores) != len(candidates):
            raise RerankerError(
                "Reranker model returned a different number of scores than candidates"
            )
        if self.retrieval.index_fingerprint() != source_fingerprint:
            raise RerankerError("Corpus changed while reranking search results")

        ranked = sorted(
            zip(candidates, scores, strict=True),
            key=lambda item: (-_validate_score(item[1]), item[0].record_id),
        )
        return [
            candidate.model_copy(update={"rank": score})
            for candidate, score in ranked[:limit]
        ]

    def warmup(self) -> None:
        self.hybrid_retrieval.warmup()
        self._provider()

    def _provider(self) -> RerankerProvider:
        if self._reranker is None:
            self._reranker = SentenceTransformerCrossEncoderProvider()
        return self._reranker


def _coerce_score(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RerankerError("Reranker model returned a non-numeric score")
    return _validate_score(float(value))


def _validate_score(value: float) -> float:
    if not math.isfinite(value):
        raise RerankerError("Reranker model returned a non-finite score")
    return value
