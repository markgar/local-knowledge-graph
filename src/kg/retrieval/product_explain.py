"""Version-2 product search telemetry; legacy lexical reports remain separate."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field, SerializerFunctionWrapHandler, model_serializer

from kg.models.contracts import ContractModel
from kg.retrieval.explain import ExplainedSearchHit, ScopeEntity, SearchFilters


class ModelIdentity(ContractModel):
    name: str
    revision: str
    pipeline_version: str


class SearchConfiguration(ContractModel):
    projection_id: str
    projection_path: str
    embedding_profile: str
    embedding_model: ModelIdentity
    reranker_model: ModelIdentity
    contextual: bool
    source_text_version: str
    lexical_candidate_limit: int
    dense_candidate_limit: int
    reranker_candidate_limit: int
    lexical_weight: float
    dense_weight: float
    rrf_k: int


class StageScore(ContractModel):
    position: int = Field(ge=1)
    score: float


class FusionScore(StageScore):
    lexical_contribution: float | None
    dense_contribution: float | None
    selected_for_reranking: bool


class CandidateTrace(ContractModel):
    record_id: str
    lexical: StageScore | None
    dense: StageScore | None
    fusion: FusionScore
    reranker: StageScore | None
    final_position: int | None


class SearchStageCounts(ContractModel):
    lexical_candidates: int
    dense_candidates: int
    deduplicated_union: int
    fused_shortlist: int
    reranked_candidates: int
    returned_hits: int


class ProductSearchExplanation(ContractModel):
    report_version: Literal["2"] = "2"
    corpus_id: str
    corpus_fingerprint: str
    query: str
    filters: SearchFilters
    lexical_expression: str
    configuration: SearchConfiguration
    score_semantics: dict[str, str] = Field(default_factory=lambda: {
        "lexical": "SQLite FTS5 BM25; lower is better; not confidence",
        "dense": "sqlite-vec L2 distance between normalized embeddings; lower is better",
        "fusion": "Weighted reciprocal-rank sum; higher is better; not confidence",
        "reranker": (
            "Raw cross-encoder score; higher is better; not confidence or comparable "
            "across queries, models, or historical BM25 scores; returned order is authoritative"
        ),
    })
    tie_breakers: dict[str, list[str]] = Field(default_factory=lambda: {
        "lexical": ["source_path", "start_offset", "passage_id"],
        "dense": ["passage_id"],
        "fusion": ["record_id"],
        "reranker": ["record_id"],
    })
    stage_counts: SearchStageCounts
    subject_scope: list[ScopeEntity]
    max_graph_distance: Literal[2] = 2
    graph_direction: Literal["undirected"] = "undirected"
    graph_path_policy: str = "One deterministic shortest supporting path per scoped entity"
    active_current_revisions_only: Literal[True] = True
    supersession_filter_applied: Literal[False] = False
    quotes_included: bool
    hits: list[ExplainedSearchHit]
    candidates: list[CandidateTrace]
    trace_limit: int = Field(ge=1, le=200)
    total_candidates: int
    displayed_candidates: int
    truncated: bool

    @model_serializer(mode="wrap")
    def _serialize(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if not self.quotes_included:
            for hit in data.get("hits", []):
                hit.pop("quote", None)
        return data
