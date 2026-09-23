"""Supported product search: the complete, unchanged reranked hybrid pipeline."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime

from kg.db import Database
from kg.lexical import corpus_fingerprint
from kg.models.contracts import SearchResult
from kg.retrieval._telemetry import SearchExecution, capture_execution
from kg.retrieval.dense import DEFAULT_EMBEDDING_PROFILE, DenseIndexError, EmbeddingProfile
from kg.retrieval.explain import (
    SearchExplanationError,
    SearchFilters,
    _explain_hit,
    _scope_paths,
)
from kg.retrieval.product_explain import (
    CandidateTrace,
    ModelIdentity,
    ProductSearchExplanation,
    SearchConfiguration,
    SearchStageCounts,
    StageScore,
)
from kg.retrieval.rerank import RerankedRetrievalService, RerankerError
from kg.retrieval.service import SearchQueryError, _fts_expression


class SearchStateChangedError(SearchQueryError):
    code = "search_state_changed"

    def __init__(self) -> None:
        super().__init__("The indexed database changed during search; retry the query.")


class SearchService:
    """Product-facing search with mandatory index/model readiness and no fallback.

    Providers are lazily cached by the composed components. Projection freshness
    is checked on every execution, including empty results. The component service
    classes remain low-level evaluation APIs, not alternate product search modes.
    """

    def __init__(
        self,
        database: Database,
        corpus_id: str,
        *,
        embedding_profile: EmbeddingProfile = DEFAULT_EMBEDDING_PROFILE,
        contextual: bool = False,
    ) -> None:
        embedding_profile = EmbeddingProfile(embedding_profile)
        if not isinstance(contextual, bool):
            raise ValueError("contextual must be a boolean")
        self.database = database
        self.corpus_id = corpus_id
        self._pipeline = RerankedRetrievalService(
            database, corpus_id, embedding_profile=embedding_profile, contextual=contextual
        )

    def search(
        self,
        query: str,
        *,
        subject: str | None = None,
        limit: int = 20,
        since: datetime | None = None,
        source_path: str | None = None,
    ) -> list[SearchResult]:
        _validate_arguments(query, subject, limit, since, source_path)
        with self._observe(explained=False):
            return self._search(query, subject, limit, since, source_path)

    def explain_search(
        self,
        query: str,
        *,
        subject: str | None = None,
        limit: int = 20,
        since: datetime | None = None,
        source_path: str | None = None,
        include_quotes: bool = False,
        trace_limit: int = 50,
    ) -> ProductSearchExplanation:
        expression = _validate_arguments(query, subject, limit, since, source_path)
        if isinstance(trace_limit, bool) or not isinstance(trace_limit, int) or not (
            1 <= trace_limit <= 200
        ):
            raise ValueError("trace_limit must be between 1 and 200")
        if not isinstance(include_quotes, bool):
            raise ValueError("include_quotes must be a boolean")
        with self._observe(explained=True) as connection, capture_execution() as trace:
            results = self._search(query, subject, limit, since, source_path)
            connection.execute("BEGIN")
            try:
                fingerprint = corpus_fingerprint(connection, self.corpus_id)
                scope = (
                    _scope_paths(self._pipeline.retrieval, connection, subject) if subject else {}
                )
                hits = [
                    _explain_hit(connection, result, position, subject, scope, include_quotes)
                    for position, result in enumerate(results, start=1)
                ]
                candidates = _candidate_trace(trace, results)
                report = ProductSearchExplanation(
                    corpus_id=self.corpus_id,
                    corpus_fingerprint=fingerprint,
                    query=query,
                    filters=SearchFilters(
                        subject=subject, limit=limit, since=since, source_path=source_path
                    ),
                    lexical_expression=expression,
                    configuration=self._configuration(trace),
                    stage_counts=SearchStageCounts(
                        lexical_candidates=len(trace.lexical),
                        dense_candidates=len(trace.dense),
                        deduplicated_union=len(trace.fusion),
                        fused_shortlist=sum(
                            entry.selected_for_reranking for entry in trace.fusion.values()
                        ),
                        reranked_candidates=len(trace.reranked),
                        returned_hits=len(results),
                    ),
                    subject_scope=sorted(
                        scope.values(), key=lambda entity: (entity.distance, entity.entity_id)
                    ),
                    quotes_included=include_quotes,
                    hits=hits,
                    candidates=candidates[:trace_limit],
                    trace_limit=trace_limit,
                    total_candidates=len(candidates),
                    displayed_candidates=min(trace_limit, len(candidates)),
                    truncated=len(candidates) > trace_limit,
                )
            finally:
                connection.rollback()
            return report

    def _search(
        self, query: str, subject: str | None, limit: int,
        since: datetime | None, source_path: str | None,
    ) -> list[SearchResult]:
        try:
            results = self._pipeline.search(
                query, subject=subject, limit=limit, since=since, source_path=source_path
            )
            # The component intentionally skips this for empty candidates; the product cannot.
            self._pipeline._provider()
            return results
        except DenseIndexError as exc:
            raise DenseIndexError(
                f"{exc} Prepare the selected embedding model using an approved local cache "
                "or download source, then run 'kg dense-index' with matching "
                "--embedding-profile and --contextual settings."
            ) from exc
        except RerankerError as exc:
            raise RerankerError(
                f"{exc} Ensure the pinned reranker model is available from an approved "
                "local cache or download source, then retry search."
            ) from exc

    @contextmanager
    def _observe(self, *, explained: bool) -> Iterator[sqlite3.Connection]:
        with self.database.connection() as connection:
            version = connection.execute("PRAGMA data_version").fetchone()[0]
            try:
                yield connection
            finally:
                if connection.execute("PRAGMA data_version").fetchone()[0] != version:
                    error = SearchStateChangedError()
                    if explained:
                        raise SearchExplanationError(error.code, str(error))
                    raise error

    def _configuration(self, trace: SearchExecution) -> SearchConfiguration:
        if (
            trace.projection_id is None or trace.projection_path is None
            or trace.embedding_profile is None or trace.embedding_model is None
            or trace.source_text_version is None
        ):
            raise SearchExplanationError(
                "search_explanation_mismatch", "Dense execution did not record its projection."
            )
        provider = self._pipeline._provider()
        return SearchConfiguration(
            projection_id=trace.projection_id,
            projection_path=trace.projection_path,
            embedding_profile=trace.embedding_profile,
            embedding_model=trace.embedding_model,
            reranker_model=ModelIdentity(
                name=provider.name, revision=provider.revision,
                pipeline_version=provider.pipeline_version,
            ),
            contextual=self._pipeline.contextual,
            source_text_version=trace.source_text_version,
            lexical_candidate_limit=trace.candidate_limit,
            dense_candidate_limit=trace.candidate_limit,
            reranker_candidate_limit=trace.reranker_candidate_limit,
            lexical_weight=trace.lexical_weight,
            dense_weight=trace.dense_weight,
            rrf_k=trace.rrf_k,
        )


def _validate_arguments(
    query: str, subject: str | None, limit: int,
    since: datetime | None, source_path: str | None,
) -> str:
    if not isinstance(query, str):
        raise SearchQueryError("Search text must be a string")
    expression = _fts_expression(query, "natural")
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be an integer of at least 1")
    if subject is not None and not isinstance(subject, str):
        raise SearchQueryError("subject must be a string or None")
    if source_path is not None and not isinstance(source_path, str):
        raise SearchQueryError("source_path must be a string or None")
    if since is not None and not isinstance(since, datetime):
        raise SearchQueryError("since must be a datetime or None")
    return expression


def _stage_scores(results: Sequence[SearchResult]) -> dict[str, StageScore]:
    scores: dict[str, StageScore] = {}
    for position, result in enumerate(results, start=1):
        scores.setdefault(result.record_id, StageScore(position=position, score=result.rank))
    return scores


def _candidate_trace(
    trace: SearchExecution, results: Sequence[SearchResult],
) -> list[CandidateTrace]:
    lexical, dense, reranker = (
        _stage_scores(ranking) for ranking in (trace.lexical, trace.dense, trace.reranked)
    )
    final = {result.record_id: position for position, result in enumerate(results, start=1)}
    ordered_ids = dict.fromkeys([
        *final,
        *reranker,
        *trace.fusion,
    ])
    return [
        CandidateTrace(
            record_id=record_id,
            lexical=lexical.get(record_id),
            dense=dense.get(record_id),
            fusion=trace.fusion[record_id],
            reranker=reranker.get(record_id),
            final_position=final.get(record_id),
        )
        for record_id in ordered_ids
    ]
