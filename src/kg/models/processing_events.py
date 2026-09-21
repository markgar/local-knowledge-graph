"""E4 closed coordination observations; never source or provider state dumps."""

from typing import Literal

from pydantic import AwareDatetime, Field

from kg.models.execution_events import CommitObservation, ObservationKind, SafeReason
from kg.models.foundation import Token, Value


class ProcessingDecision(Value):
    kind: Literal[
        "processing.claim_decision",
        "processing.fence_decision",
        "processing.checkpoint_decision",
        "processing.restart_decision",
        "processing.settled_retrieval",
        "processing.status_observation",
    ]
    decision: Literal[
        "claimed",
        "no_work",
        "denied",
        "stale",
        "lease_lost",
        "accepted",
        "rejected",
        "unchanged",
        "restarted",
        "discarded",
        "reused",
        "replayed",
        "expired",
        "conflict",
        "unavailable",
        "pending",
        "running",
        "succeeded",
        "failed",
        "blocked",
    ]
    observation_kind: ObservationKind = "captured_execution"
    job_id: Token | None = None
    attempt_id: Token | None = None
    sequence: int | None = Field(default=None, ge=0)
    reason: SafeReason | None = None


class ProcessingRetry(Value):
    kind: Literal["processing.retry_decision"] = "processing.retry_decision"
    classification: Literal["transient", "permanent", "blocked", "exhausted"]
    due_at: AwareDatetime | None = None
    attempt: int | None = Field(default=None, ge=1)


class ProcessingAck(Value):
    kind: Literal["processing.ack_decision"] = "processing.ack_decision"
    observation: CommitObservation
    unit_id: Token | None = None
    publication: Literal["published", "verified_unchanged"] | None = None


ProcessingEvent = ProcessingDecision | ProcessingRetry | ProcessingAck
