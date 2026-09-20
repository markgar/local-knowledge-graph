"""Execution-local instrumentation without changing component call signatures."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field

from kg.models.contracts import SearchResult
from kg.retrieval.product_explain import FusionScore, ModelIdentity


@dataclass
class SearchExecution:
    lexical: list[SearchResult] = field(default_factory=list)
    dense: list[SearchResult] = field(default_factory=list)
    fusion: dict[str, FusionScore] = field(default_factory=dict)
    reranked: list[SearchResult] = field(default_factory=list)
    projection_id: str | None = None
    projection_path: str | None = None
    embedding_profile: str | None = None
    embedding_model: ModelIdentity | None = None
    source_text_version: str | None = None
    candidate_limit: int = 0
    reranker_candidate_limit: int = 0
    lexical_weight: float = 0.0
    dense_weight: float = 0.0
    rrf_k: int = 0


execution_trace: ContextVar[SearchExecution | None] = ContextVar("search_execution", default=None)


@contextmanager
def capture_execution() -> Iterator[SearchExecution]:
    trace = SearchExecution()
    token = execution_trace.set(trace)
    try:
        yield trace
    finally:
        execution_trace.reset(token)
