"""Strict standalone indexing and immutable passage values."""

from typing import Literal, Self

from pydantic import Field, model_validator

from kg.models.evidence import EvidenceView
from kg.models.foundation import Token, Value


class PassageEntry(EvidenceView):
    ordinal: int = Field(gt=0)

    @model_validator(mode="after")
    def passage_reference(self) -> Self:
        if self.reference.passage_id is None:
            raise ValueError("A passage entry requires a passage reference")
        return self


class PassagePage(Value):
    interface_version: Literal["indexing/1"] = "indexing/1"
    document_id: Token
    revision_id: Token
    state_version: Token
    passage_policy: Token
    status: Literal["not_processed", "complete"]
    passage_set_id: Token | None
    entries: tuple[PassageEntry, ...] = Field(max_length=200)
    has_more: bool
    next_after_ordinal: int | None

    @model_validator(mode="after")
    def publication(self) -> Self:
        if (self.status == "complete") != (self.passage_set_id is not None):
            raise ValueError("Only a published passage set is complete")
        if self.status == "not_processed" and (self.entries or self.has_more):
            raise ValueError("Unprocessed state has no passages")
        if self.has_more != (self.next_after_ordinal is not None) or (
            self.has_more
            and (not self.entries or self.next_after_ordinal != self.entries[-1].ordinal)
        ):
            raise ValueError("Invalid passage cursor")
        return self


class IndexConfiguration(Value):
    embedding_profile: Literal["gte-modernbert", "qwen3-embedding-0.6b"] = "gte-modernbert"
    contextual: bool = False
    representation: Literal["generic-title-quote/1", "exact-quote/1"] = "exact-quote/1"
    lexical_version: Literal["generic-lexical/1"] = "generic-lexical/1"
    dense_version: Literal["generic-dense-dot/1"] = "generic-dense-dot/1"

    @model_validator(mode="after")
    def representation_matches(self) -> Self:
        if self.contextual != (self.representation == "generic-title-quote/1"):
            raise ValueError("Representation must match contextual mode")
        return self


DEFAULT_CONFIGURATION = IndexConfiguration()
IndexReason = Literal[
    "not_processed",
    "unsupported_policy",
    "provider_unavailable",
    "invalid_provider_output",
    "state_changed",
    "storage_failure",
    "superseded_attempt",
    "claim_lost",
    "inactive",
]
AttemptStatus = Literal[
    "admitted",
    "staging",
    "published",
    "unchanged",
    "failed",
    "stale",
    "superseded",
]


class ExecutionIdentity(Value):
    profile: Literal["gte-modernbert", "qwen3-embedding-0.6b"]
    model: Token
    revision: Token
    pipeline: str = Field(min_length=1, max_length=2048)
    dimensions: int = Field(gt=0, le=1024)
    normalization: Literal["l2"] = "l2"
    vector_encoding: Literal["little-endian-float32/1"] = "little-endian-float32/1"
    query_encoding: str = Field(min_length=1, max_length=1024)
    document_encoding: str = Field(min_length=1, max_length=1024)
    context_behavior: Token


class AttemptView(Value):
    attempt_id: Token
    state_version: Token
    status: AttemptStatus
    reason: IndexReason | None
    execution_identity: ExecutionIdentity | None


class ProcessResult(Value):
    interface_version: Literal["indexing/1"] = "indexing/1"
    document_id: Token
    state_version: Token
    configuration_id: Token
    attempt_id: Token | None
    passage_set_id: Token | None = None
    projection_id: Token | None = None
    outcome: Literal["ready", "unchanged", "stale", "failed"]
    produced_passages: int = Field(default=0, ge=0)
    reused_passages: int = Field(default=0, ge=0)
    produced_vectors: int = Field(default=0, ge=0)
    reused_vectors: int = Field(default=0, ge=0)
    cleanup_pending: bool = False
    reason: IndexReason | None = None
    diagnostic_id: Token | None = None

    @model_validator(mode="after")
    def completion(self) -> Self:
        success = self.outcome in ("ready", "unchanged")
        if success != (self.projection_id is not None) or success != (self.reason is None):
            raise ValueError("Only complete process results carry a projection")
        if success and (self.passage_set_id is None or self.attempt_id is None):
            raise ValueError("A complete projection requires a set and attempt")
        return self


class IndexStatus(Value):
    interface_version: Literal["indexing/1"] = "indexing/1"
    document_id: Token
    state_version: Token
    configuration_id: Token
    source: Literal["active", "inactive"]
    status: Literal["pending", "ready", "failed", "inactive"]
    passages: Literal["not_processed", "complete"]
    lexical: Literal["not_processed", "complete"]
    vectors: Literal["not_processed", "complete"]
    passage_set_id: Token | None
    projection_id: Token | None
    execution_identity: ExecutionIdentity | None
    provider_compatibility: Literal["unverified"] = "unverified"
    latest_attempt: AttemptView | None
    reason: IndexReason | None


class PendingPage(Value):
    interface_version: Literal["indexing/1"] = "indexing/1"
    entries: tuple[IndexStatus, ...] = Field(max_length=200)
    has_more: bool
    next_after_document_id: Token | None


class CleanupResult(Value):
    interface_version: Literal["indexing/1"] = "indexing/1"
    removed: int = Field(ge=0, le=1000)
    has_more: bool


class IndexCapabilities(Value):
    interface_version: Literal["indexing/1"] = "indexing/1"
    supported: tuple[Literal["process", "status", "pending", "cleanup"], ...] = (
        "process",
        "status",
        "pending",
        "cleanup",
    )
    unsupported: tuple[Literal["search", "coordinated_process"], ...] = (
        "search",
        "coordinated_process",
    )
