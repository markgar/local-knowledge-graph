"""Strict knowledge/1 registry and immutable contribution-read values."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from kg.knowledge._selection import (
    CapturedEvidence,
    ClassificationWitness,
    EntityWitness,
    SeedWitness,
    SourceWitness,
)
from kg.models.authoring import ClassificationReviewWitness, ClassificationSelectionWitness
from kg.models.foundation import (
    Attribution,
    BooleanObject,
    IntegerObject,
    Label,
    Name,
    SchemaRevisionRef,
    SeedSupport,
    SourceSupport,
    StoredEntity,
    StoredSelectionRef,
    StringObject,
    TimestampObject,
    Token,
    Value,
)


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
    authoring: Literal["record-authoring/1"] | None = "record-authoring/1"
    authoring_batch: Literal["record-authoring-batch/1"] | None = "record-authoring-batch/1"
    withdrawal: Literal["owned_assertion"] | None = "owned_assertion"
    support: Literal["anchors_passages_and_seed_add"] = "anchors_passages_and_seed_add"
    reads: tuple[str, ...] = ("entity", "entities", "contribution", "contributions")
    unsupported: tuple[str, ...] = (
        "replace_seed_set",
        "retraction",
        "traversal",
    )
    decision_encoding: Literal["direct-subject-decision/1"] | None
    classification_withdrawal: Literal["owned_classification"] | None = "owned_classification"
    classification_semantics: Literal["explicit_selected_claim"] = "explicit_selected_claim"


class ClassificationSummary(Value):
    selection_id: Token
    status: Literal["selected", "unresolved"]
    selected: ClassificationWitness | None
    alternatives_scope: Literal["authorized_only"] = "authorized_only"


class EntityView(KnowledgeValue):
    entity_id: Token
    name: Label
    entity_type: Name | None
    sequence: int = Field(ge=1)
    is_current: bool
    witness: EntityWitness
    has_more_support: bool
    classification: ClassificationSummary
    selection_witness: ClassificationSelectionWitness | None


class AssertionWithdrawal(KnowledgeValue):
    withdrawal_id: Token
    committed_at: str
    attribution: Attribution


Eligibility = Literal[
    "current",
    "assertion_withdrawn",
    "source_stale",
    "identity_unsupported",
    "classification_changed",
    "classification_withdrawn",
    "classification_stale",
]


ContributionSupport = Annotated[
    SourceSupport | SeedSupport,
    Field(discriminator="kind"),
]


class EntitySupportPayload(Value):
    kind: Literal["entity_support"]
    local_id: Token
    entity: StoredEntity
    name: Label
    support: ContributionSupport


class AliasPayload(Value):
    kind: Literal["alias"]
    local_id: Token
    entity: StoredEntity
    alias: Label
    support: ContributionSupport


class IdentifierPayload(Value):
    kind: Literal["identifier"]
    local_id: Token
    entity: StoredEntity
    scheme: Name
    value: Token
    support: ContributionSupport


class MentionPayload(Value):
    kind: Literal["mention"]
    local_id: Token
    entity: StoredEntity
    support: SourceSupport


class ClassificationPayload(Value):
    kind: Literal["classification"]
    local_id: Token
    entity: StoredEntity
    entity_type: Name
    interpretation: Literal["explicit", "inferred"]
    support: ContributionSupport


class ContributionEntityObject(Value):
    kind: Literal["entity"]
    entity: StoredEntity


ContributionAssertionObject = Annotated[
    ContributionEntityObject | StringObject | IntegerObject | BooleanObject | TimestampObject,
    Field(discriminator="kind"),
]


class AssertionPayload(Value):
    kind: Literal["assertion"]
    local_id: Token
    subject: StoredEntity
    predicate: Name
    object: ContributionAssertionObject
    interpretation: Literal["explicit", "inferred"]
    support: SourceSupport
    subject_classification: StoredSelectionRef
    object_classification: StoredSelectionRef | None = None


KnowledgeContributionPayload = Annotated[
    EntitySupportPayload
    | AliasPayload
    | IdentifierPayload
    | MentionPayload
    | ClassificationPayload
    | AssertionPayload,
    Field(discriminator="kind"),
]


class ContributionView(KnowledgeValue):
    contribution_id: Token
    sequence: int = Field(ge=1)
    schema_version: Token
    attribution: Attribution
    committed_at: str
    payload: KnowledgeContributionPayload
    evidence: tuple[CapturedEvidence, ...] = Field(max_length=200)
    is_current: bool
    witnesses: tuple[EntityWitness, ...] = Field(max_length=2)
    withdrawal: AssertionWithdrawal | None = None
    classification_witnesses: tuple[ClassificationWitness, ...] = Field(default=(), max_length=2)
    eligibility: Eligibility = "current"


class ClassificationClaim(Value):
    claim_id: Token
    entity_id: Token
    entity_type: Name
    schema_version: Token
    interpretation: Literal["explicit", "inferred"]
    basis: SourceWitness | SeedWitness
    is_current: bool
    withdrawn: bool


class ClassificationReview(KnowledgeValue):
    entity_id: Token
    selection_id: Token
    selected: ClassificationClaim | None
    claims: tuple[ClassificationClaim, ...] = Field(max_length=200)
    reviewed_claim_ids: tuple[Token, ...] = Field(max_length=200)
    review_coverage: Literal["complete", "selected_subset"]
    conflicting_types: bool
    reviewed_candidates_digest: str
    review_witness: ClassificationReviewWitness


class ClassificationEvent(KnowledgeValue):
    event_id: Token
    entity_id: Token
    selected: ClassificationClaim | None
    rationale: Label
    attribution: Attribution
    schema_version: Token
    committed_at: str
    is_head: bool


class ClassificationHistory(KnowledgeValue):
    entries: tuple[ClassificationEvent, ...] = Field(max_length=200)
    has_more: bool
    next_after_event_id: Token | None


class KnowledgePage[T](KnowledgeValue):
    entries: tuple[T, ...] = Field(max_length=200)
    has_more: bool
    next_after_sequence: int | None = Field(default=None, ge=1)
