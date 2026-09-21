"""Closed shared observation vocabulary. No exception text or private work meters."""

from typing import Literal

from kg.models.foundation import ErrorCode, Value

ObservationKind = Literal["captured_execution", "current_inspection", "retained_commit_fact"]
CommitObservation = Literal[
    "not_attempted",
    "acknowledgement_staged",
    "confirmed_committed",
    "confirmed_rolled_back",
    "unknown",
]
SafeReason = (
    ErrorCode
    | Literal[
        "invalid_shape",
        "reference_unavailable",
        "unsupported_capability",
        "invalid_type",
        "resource_budget",
        "deadline",
        "closed",
        "unavailable",
        "lease_lost",
        "provider_unavailable",
        "invalid_provider_output",
        "unsupported_policy",
        "superseded_attempt",
        "claim_lost",
        "processor_not_available",
        "not_processed",
        "storage_failure",
        "inactive",
        "index_not_ready",
        "not_selected_for_reranking",
        "below_return_limit",
        "awaiting_input",
        "dependency_changed",
        "retry_scheduled",
        "retry_exhausted",
        "purge_blocked",
        "plan_disabled",
        "authority_unavailable",
    ]
)
SafeOutcome = Literal[
    "succeeded",
    "applied",
    "unchanged",
    "rejected",
    "conflict",
    "failed",
    "complete",
    "partial",
    "empty",
    "stopped",
]


class CommitEvent(Value):
    kind: Literal["execution.commit"] = "execution.commit"
    observation: CommitObservation
    observation_kind: ObservationKind = "captured_execution"


class EvidenceEvent(Value):
    kind: Literal["evidence.phase"] = "evidence.phase"
    phase: Literal["authorization", "replay", "mutation", "read"]
    decision: Literal[
        "passed",
        "fresh",
        "replayed",
        "retry_conflict",
        "retry_expired",
        "applied",
        "unchanged",
        "observed",
    ]
    observation_kind: ObservationKind = "captured_execution"
