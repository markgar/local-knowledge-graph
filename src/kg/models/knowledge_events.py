"""K1 event values only; these do not implement knowledge operations."""

from typing import Literal

from kg.models.execution_events import CommitObservation, SafeReason
from kg.models.foundation import Token, Value


class KnowledgeValidation(Value):
    kind: Literal["knowledge.validation"] = "knowledge.validation"
    phase: Literal["envelope", "scope", "binding", "schema", "type", "support", "seed_cas"]
    status: Literal["passed", "rejected", "not_observed"]
    reason: SafeReason | None = None


class KnowledgeEncoding(Value):
    kind: Literal["knowledge.encoding"] = "knowledge.encoding"
    encoding: Literal["ordinary", "direct-subject-decision/1"]
    status: Literal["accepted", "rejected", "not_observed"]
    configuration_id: Token | None = None


class KnowledgeReplay(Value):
    kind: Literal["knowledge.replay"] = "knowledge.replay"
    decision: Literal[
        "fresh",
        "linked_settled",
        "replayed",
        "retry_conflict",
        "retry_expired",
        "linkage_conflict",
    ]
    receipt_id: Token | None = None


class KnowledgeCommit(Value):
    kind: Literal["knowledge.commit"] = "knowledge.commit"
    observation: CommitObservation


class KnowledgeCoordination(Value):
    kind: Literal["knowledge.coordination"] = "knowledge.coordination"
    decision: Literal[
        "standalone",
        "guard_passed",
        "guard_rejected",
        "acknowledgement_staged",
        "confirmed_committed",
        "not_observed",
    ]


class KnowledgeSelection(Value):
    kind: Literal["knowledge.selection"] = "knowledge.selection"
    semantics: Literal["direct-subject-decision/1", "entity", "seed", "contribution"]
    state: Literal["empty", "nonempty", "exhausted", "stopped"]


KnowledgeEvent = (
    KnowledgeValidation
    | KnowledgeEncoding
    | KnowledgeReplay
    | KnowledgeCommit
    | KnowledgeCoordination
    | KnowledgeSelection
)
