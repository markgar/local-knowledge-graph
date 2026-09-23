"""K1-owned reader/witness contracts shared with composed readers."""

from __future__ import annotations

from typing import Annotated, Literal, Protocol, Self

from pydantic import Field, model_validator

from kg.models.foundation import (
    DocumentDependency,
    EvidenceRef,
    Failure,
    Label,
    Record,
    Token,
    Value,
)


class EntitySelector(Value):
    name: Label | None = None
    entity_id: Token | None = None

    @model_validator(mode="after")
    def exactly_one(self) -> Self:
        if (self.name is None) == (self.entity_id is None):
            raise ValueError("Exactly one entity selector is required")
        return self


class CapturedEvidence(Value):
    reference: EvidenceRef
    dependency: DocumentDependency
    metadata_snapshot_id: Token
    namespace_token: Token

    @model_validator(mode="after")
    def exact_dependency(self) -> Self:
        if (
            self.reference.source_namespace != self.dependency.source_namespace
            or self.reference.document_id != self.dependency.document_id
            or self.reference.revision_id != self.dependency.revision_id
        ):
            raise ValueError("Captured reference/dependency mismatch")
        return self


class SourceWitness(Value):
    kind: Literal["source"] = "source"
    evidence: tuple[CapturedEvidence, ...] = Field(min_length=1, max_length=200)


class SeedWitness(Value):
    kind: Literal["seed"] = "seed"
    namespace: Token
    owner_id: Token
    writer_id: Token
    seed_set_id: Token
    seed_key: Token
    contribution_id: Token
    membership_event_id: Token
    generation: int = Field(ge=1)


class EntityWitness(Value):
    entity_id: Token
    contribution_id: Token
    contribution_sequence: int = Field(ge=1)
    basis: Annotated[SourceWitness | SeedWitness, Field(discriminator="kind")]

    @model_validator(mode="after")
    def seed_contribution(self) -> Self:
        if (
            isinstance(self.basis, SeedWitness)
            and self.basis.contribution_id != self.contribution_id
        ):
            raise ValueError("Seed witness contribution mismatch")
        return self


class EntitySelectionItem(Value):
    entity_id: Token
    witness: EntityWitness

    @model_validator(mode="after")
    def exact_entity(self) -> Self:
        if self.entity_id != self.witness.entity_id:
            raise ValueError("Entity witness mismatch")
        return self


class DecisionDependencies(Value):
    assertion_support: tuple[CapturedEvidence, ...] = Field(min_length=1, max_length=200)
    subject_witness: EntityWitness


class DecisionSelectionItem(Value):
    record: Record
    subject_id: Token
    schema_version: Token
    encoding: Literal["direct-subject-decision/1"] = "direct-subject-decision/1"
    dependencies: DecisionDependencies

    @model_validator(mode="after")
    def exact_bundle(self) -> Self:
        if (
            self.record.record_type != "decision"
            or self.subject_id != self.dependencies.subject_witness.entity_id
            or self.record.support.evidence != tuple(
                item.reference for item in self.dependencies.assertion_support
            )
        ):
            raise ValueError("Decision dependency bundle mismatch")
        return self


# Retention keeps the producer-selected witness verbatim, not another selection.
RetainedDecisionMember = DecisionSelectionItem


class EligibleEOF(Value):
    kind: Literal["eligible_eof"] = "eligible_eof"


class SelectionStopped(Value):
    kind: Literal["public_budget_stop", "private_resource_stop", "deadline_stop"]


class SelectionFailure(Value):
    kind: Literal["selection_failure"] = "selection_failure"
    failure: Failure


SelectionTerminal = Annotated[
    EligibleEOF | SelectionStopped | SelectionFailure, Field(discriminator="kind"),
]


class SelectionPage[T](Value):
    items: tuple[T, ...] = Field(max_length=200)
    terminal: SelectionTerminal | None

    @model_validator(mode="after")
    def progress_or_terminal(self) -> Self:
        if not self.items and self.terminal is None:
            raise ValueError("An empty page must have an explicit terminal")
        return self


class SelectionCursor[T](Protocol):
    """K1 implementations bind every fetch to their live CanonicalReadContext."""

    def read(self, *, limit: int = 200) -> SelectionPage[T]: ...
    def close(self) -> None: ...


class KnowledgeReader(Protocol):
    def resolve_entity(self, selector: EntitySelector) -> SelectionCursor[EntitySelectionItem]: ...
    def select_decisions(self, subject_id: str) -> SelectionCursor[DecisionSelectionItem]: ...
    def revalidate_member(self, member: RetainedDecisionMember) -> None: ...
