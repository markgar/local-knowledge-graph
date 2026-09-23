"""Typed private participants; orchestration implementations remain E4-owned."""

from __future__ import annotations

from typing import Annotated, Literal, Protocol

from pydantic import Field

from kg.evidence._transactions import CanonicalWriteContext
from kg.models.foundation import Failure, Token, Value, WriteOutcome, WriteRequest

WriteOperation = Literal[
    "put_document", "remove_document", "enrich", "replace_seed_set", "withdraw_assertion",
]
IndexMutationPhase = Literal[
    "admission", "passages", "staging", "failure", "cleanup", "publication",
]


class CanonicalKey(Value):
    corpus_id: Token
    writer_id: Token
    operation: WriteOperation
    retry_key_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class UnitIdentity(Value):
    corpus_id: Token
    batch_id: Token
    unit_id: Token
    ordinal: int = Field(ge=0)


class DocumentTarget(Value):
    corpus_id: Token
    namespace: Token
    document_id: Token
    revision_id: Token
    state_version: Token
    namespace_token: Token


class IndexTarget(Value):
    document: DocumentTarget
    configuration_id: Token


class NewWork(Value):
    kind: Literal["new_work"] = "new_work"


class SettledSuccess(Value):
    kind: Literal["settled_success"] = "settled_success"
    canonical_key_id: Token


class SettledFailure(Value):
    kind: Literal["settled_failure"] = "settled_failure"
    failure: Failure


WriteAdmission = Annotated[NewWork | SettledSuccess | SettledFailure, Field(discriminator="kind")]


class WriteCommit(Value):
    key_id: Token
    outcome: WriteOutcome


class ProjectionCommit(Value):
    target: IndexTarget
    projection_id: Token
    attempt_id: Token
    outcome: Literal["published", "verified_unchanged"]


class CoordinatedWriteParticipant(Protocol):
    def classify_unit(
        self, context: CanonicalWriteContext, unit: UnitIdentity, key: CanonicalKey,
    ) -> WriteAdmission: ...

    def guard_new(
        self, context: CanonicalWriteContext, unit: UnitIdentity, request: WriteRequest,
    ) -> None: ...

    def acknowledge_write(
        self, context: CanonicalWriteContext, unit: UnitIdentity, committed: WriteCommit,
    ) -> None: ...


class CoordinatedIndexParticipant(Protocol):
    def check(
        self, context: CanonicalWriteContext, target: IndexTarget, phase: IndexMutationPhase,
    ) -> None: ...

    def acknowledge_projection(
        self, context: CanonicalWriteContext, completion: ProjectionCommit,
    ) -> None: ...
