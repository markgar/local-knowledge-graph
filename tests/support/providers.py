from __future__ import annotations

import re
from collections.abc import Callable, Sequence

import pytest

from kg.retrieval.dense import DEFAULT_EMBEDDING_PROFILE, EmbeddingProfile


class Embeddings:
    name = "controlled/embedding"
    revision = "test-revision"
    license = "MIT"
    pipeline_version = "test-embedding-v1"
    dimensions = 2
    normalization = "l2"
    context_behavior = "test"
    query_encoding = "test-query"
    document_encoding = "test-document"

    def __init__(self, profile: EmbeddingProfile = DEFAULT_EMBEDDING_PROFILE) -> None:
        self.profile = profile
        self.queries: list[str] = []
        self.on_query: Callable[[], None] = lambda: None

    def encode_documents(
        self,
        texts: Sequence[str],
        *,
        batch_size: int,
    ) -> list[list[float]]:
        return [
            [1.0, 0.0]
            if "keyword meaning" in text
            else [0.8, 0.6]
            if "meaning" in text
            else [0.0, 1.0]
            for text in texts
        ]

    def encode_query(self, text: str) -> list[float]:
        self.queries.append(text)
        self.on_query()
        return [1.0, 0.0]


class Reranker:
    name = "controlled/reranker"
    revision = "test-revision"
    license = "MIT"
    pipeline_version = "test-reranker-v1"

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self.on_score: Callable[[], None] = lambda: None

    def score(self, query: str, passages: Sequence[str], *, batch_size: int) -> list[float]:
        self.calls.append((query, list(passages)))
        self.on_score()
        return [self.value(text) for text in passages]

    @staticmethod
    def value(text: str) -> float:
        item = re.search(r"item (\d+)", text)
        return float(item[1]) if item else float(len(text))


def providers(
    monkeypatch: pytest.MonkeyPatch,
    embedding: Embeddings,
    reranker: Reranker,
) -> list[str]:
    loads: list[str] = []

    def load_embedding(profile: EmbeddingProfile) -> Embeddings:
        assert profile == embedding.profile
        loads.append("embedding")
        return embedding

    def load_reranker() -> Reranker:
        loads.append("reranker")
        return reranker

    monkeypatch.setattr("kg.retrieval.dense.SentenceTransformerEmbeddingProvider", load_embedding)
    monkeypatch.setattr(
        "kg.retrieval.rerank.SentenceTransformerCrossEncoderProvider", load_reranker
    )
    return loads
