"""Strict knowledge/1 registry values, not knowledge-write or query services."""

from typing import Literal, Self

from pydantic import Field, model_validator

from kg.knowledge._selection import CapturedEvidence, EntityWitness
from kg.models.foundation import Attribution, Change, Label, Name, SchemaRevisionRef, Token, Value


class KnowledgeValue(Value):
    interface_version: Literal["knowledge/1"] = "knowledge/1"


class RecordProjection(Value):
    encoding: Literal["direct-subject-decision/1"]


class PredicateDefinition(Value):
    name: Name
    subject_types: tuple[Name, ...] = Field(min_length=1, max_length=100)
    object_kind: Literal["entity", "string", "integer", "boolean", "timestamp"]
    object_types: tuple[Name, ...] = Field(default=(), max_length=100)
    record_projection: RecordProjection | None = None

    @model_validator(mode="after")
    def typed_definition(self) -> Self:
        if len(set(self.subject_types)) != len(self.subject_types) or len(
            set(self.object_types)
        ) != len(self.object_types):
            raise ValueError("Duplicate predicate types")
        if (self.object_kind == "entity") != bool(self.object_types):
            raise ValueError("Only entity predicates require object types")
        if self.record_projection is not None and self.object_kind != "string":
            raise ValueError("Decision projection requires a string predicate")
        return self


class KnowledgeSchema(KnowledgeValue):
    corpus_id: Token
    schema_version: Token
    entity_types: tuple[Name, ...] = Field(min_length=1, max_length=1000)
    identifier_schemes: tuple[Name, ...] = Field(default=(), max_length=1000)
    predicates: tuple[PredicateDefinition, ...] = Field(default=(), max_length=1000)

    @model_validator(mode="after")
    def registry_references(self) -> Self:
        names = tuple(predicate.name for predicate in self.predicates)
        if any(
            len(set(items)) != len(items)
            for items in (self.entity_types, self.identifier_schemes, names)
        ):
            raise ValueError("Duplicate registry entries")
        known_types = set(self.entity_types)
        if any(
            not set((*predicate.subject_types, *predicate.object_types)) <= known_types
            for predicate in self.predicates
        ):
            raise ValueError("Predicate refers to an unknown entity type")
        return self


class KnowledgeSchemaRegistration(KnowledgeValue):
    corpus_id: Token
    schema_version: Token
    status: Literal["applied", "unchanged"]


class KnowledgeCapabilities(KnowledgeValue):
    schema_status: Literal["configured", "unconfigured"] = "configured"
    schema_revision: SchemaRevisionRef | None = None
    enrichment_revision: Literal["exact_head"] = "exact_head"
    withdrawal: Literal["owned_assertion"] | None = "owned_assertion"
    change_kinds: tuple[str, ...] = (
        "entity", "entity_support", "alias", "identifier", "mention", "assertion",
    )
    support: Literal["anchors_passages_and_seed_add"] = "anchors_passages_and_seed_add"
    reads: tuple[str, ...] = ("entity", "entities", "contribution", "contributions")
    unsupported: tuple[str, ...] = (
        "replace_seed_set",
        "retraction",
        "traversal",
    )
    decision_encoding: Literal["direct-subject-decision/1"] | None


class EntityView(KnowledgeValue):
    entity_id: Token
    name: Label
    entity_type: Name
    sequence: int = Field(ge=1)
    is_current: bool
    witness: EntityWitness
    has_more_support: bool


class AssertionWithdrawal(KnowledgeValue):
    withdrawal_id: Token
    committed_at: str
    attribution: Attribution


class ContributionView(KnowledgeValue):
    contribution_id: Token
    sequence: int = Field(ge=1)
    schema_version: Token
    attribution: Attribution
    committed_at: str
    payload: Change
    evidence: tuple[CapturedEvidence, ...] = Field(max_length=200)
    is_current: bool
    witnesses: tuple[EntityWitness, ...] = Field(max_length=2)
    withdrawal: AssertionWithdrawal | None = None


class KnowledgePage[T](KnowledgeValue):
    entries: tuple[T, ...] = Field(max_length=200)
    has_more: bool
    next_after_sequence: int | None = Field(default=None, ge=1)
