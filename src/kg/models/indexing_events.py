"""E3 event values only, not a search/index implementation."""

from typing import Annotated, Literal

from pydantic import Field

from kg.models.execution_events import SafeReason
from kg.models.foundation import EvidenceRef, Token, Value

Score = Annotated[float, Field(allow_inf_nan=False)]
Rank = Annotated[int, Field(ge=1)]


class IndexPhase(Value):
    kind: Literal["indexing.phase"] = "indexing.phase"
    phase: Literal[
        "admission",
        "readiness",
        "passage",
        "vector",
        "lexical",
        "dense",
        "fusion",
        "rerank",
        "publication",
        "acknowledgement",
    ]
    status: Literal[
        "started",
        "complete",
        "ready",
        "not_ready",
        "produced",
        "reused",
        "rebuilt",
        "published",
        "unchanged",
        "fenced",
        "failed",
        "staged",
    ]
    logical_configuration_id: Token | None = None
    observed_configuration_id: Token | None = None
    passage_set_id: Token | None = None
    reason: SafeReason | None = None


class IndexCandidate(Value):
    kind: Literal["indexing.candidate"] = "indexing.candidate"
    reference: EvidenceRef
    lexical_member: bool | None = None
    lexical_rank: Rank | None = None
    lexical_score: Score | None = None
    dense_member: bool | None = None
    dense_rank: Rank | None = None
    dense_score: Score | None = None
    lexical_contribution: Score | None = None
    dense_contribution: Score | None = None
    fused_rank: Rank | None = None
    fused_score: Score | None = None
    shortlisted: bool | None = None
    rerank_member: bool | None = None
    rerank_rank: Rank | None = None
    rerank_score: Score | None = None
    final_rank: Rank | None = None
    exclusion: Literal["not_selected_for_reranking", "below_return_limit"] | None = None


IndexEvent = IndexPhase | IndexCandidate
