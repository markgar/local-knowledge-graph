"""Private canonical knowledge write-plan and receipt values."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from kg.models.foundation import (
    MAX_CHANGES,
    MAX_SUPPORTS,
    BooleanObject,
    DocumentDependency,
    EvidenceRef,
    IntegerObject,
    Label,
    Name,
    SchemaRevisionRef,
    SeedSupport,
    SourceSupport,
    StoredClassificationRef,
    StoredEntity,
    StoredSelectionRef,
    StringObject,
    TimestampObject,
    Token,
    Value,
)

Support = Annotated[SourceSupport | SeedSupport, Field(discriminator="kind")]


class LocalEntity(Value):
    kind: Literal["local"]
    local_id: Token


EntityRef = Annotated[LocalEntity | StoredEntity, Field(discriminator="kind")]


class LocalClassificationRef(Value):
    kind: Literal["local"]
    local_id: Token


class LocalSelectionRef(Value):
    kind: Literal["local"]
    local_id: Token


SelectionRef = Annotated[
    LocalSelectionRef | StoredSelectionRef,
    Field(discriminator="kind"),
]


class EntityObject(Value):
    kind: Literal["entity"]
    entity: EntityRef


AssertionObject = Annotated[
    EntityObject | StringObject | IntegerObject | BooleanObject | TimestampObject,
    Field(discriminator="kind"),
]


class CreateEntity(Value):
    kind: Literal["entity"]
    local_id: Token
    name: Label
    support: Support


class AddEntitySupport(Value):
    kind: Literal["entity_support"]
    local_id: Token
    entity: StoredEntity
    name: Label
    support: Support


class AddAlias(Value):
    kind: Literal["alias"]
    local_id: Token
    entity: EntityRef
    alias: Label
    support: Support


class AddIdentifier(Value):
    kind: Literal["identifier"]
    local_id: Token
    entity: EntityRef
    scheme: Name
    value: Token
    support: Support


class AddMention(Value):
    kind: Literal["mention"]
    local_id: Token
    entity: EntityRef
    support: SourceSupport

    @model_validator(mode="after")
    def passage_required(self) -> Self:
        if any(ref.passage_id is None for ref in self.support.evidence):
            raise ValueError("mentions require passage evidence")
        return self


class AddClassification(Value):
    kind: Literal["classification"]
    local_id: Token
    entity: EntityRef
    entity_type: Name
    interpretation: Literal["explicit", "inferred"]
    support: Support


class SelectClassification(Value):
    kind: Literal["classification_selection"]
    local_id: Token
    entity: EntityRef
    claim: (
        Annotated[
            LocalClassificationRef | StoredClassificationRef,
            Field(discriminator="kind"),
        ]
        | None
    )
    expected_selection_id: Token | None
    reviewed_candidates_digest: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    reviewed_claim_ids: tuple[Token, ...] = Field(max_length=200)
    review_coverage: Literal["complete", "selected_subset"]
    accept_incomplete_review: bool
    rationale: Label

    @model_validator(mode="after")
    def bounded_review(self) -> Self:
        if len(self.rationale.encode("utf-8")) > 4096:
            raise ValueError("Selection rationale exceeds 4096 UTF-8 bytes")
        if len(set(self.reviewed_claim_ids)) != len(self.reviewed_claim_ids):
            raise ValueError("Duplicate reviewed claim")
        if self.accept_incomplete_review != (self.review_coverage == "selected_subset"):
            raise ValueError("Subset review requires explicit incomplete-review acknowledgement")
        return self


class AddAssertion(Value):
    kind: Literal["assertion"]
    local_id: Token
    subject: EntityRef
    predicate: Name
    object: AssertionObject
    interpretation: Literal["explicit", "inferred"]
    support: SourceSupport
    subject_classification: SelectionRef | None = None
    object_classification: SelectionRef | None = None


Change = Annotated[
    CreateEntity
    | AddEntitySupport
    | AddAlias
    | AddIdentifier
    | AddMention
    | AddAssertion
    | AddClassification
    | SelectClassification,
    Field(discriminator="kind"),
]


class ChangeSet(Value):
    operation: Literal["enrich"]
    expected_schema_revision: SchemaRevisionRef | None = None
    dependencies: tuple[DocumentDependency, ...] = Field(default=(), max_length=MAX_SUPPORTS)
    changes: tuple[Change, ...] = Field(min_length=1, max_length=MAX_CHANGES)

    @model_validator(mode="after")
    def references_and_dependencies(self) -> Self:
        local_ids = [change.local_id for change in self.changes]
        if len(set(local_ids)) != len(local_ids):
            raise ValueError("change local IDs must be unique")
        entities = {change.local_id for change in self.changes if isinstance(change, CreateEntity)}
        claims = {c.local_id for c in self.changes if isinstance(c, AddClassification)}
        selections = {c.local_id for c in self.changes if isinstance(c, SelectClassification)}
        evidence: list[EvidenceRef] = []
        for change in self.changes:
            refs: list[EntityRef] = []
            if isinstance(
                change,
                (
                    AddAlias,
                    AddIdentifier,
                    AddMention,
                    AddClassification,
                    SelectClassification,
                ),
            ):
                refs.append(change.entity)
            elif isinstance(change, AddAssertion):
                refs.append(change.subject)
                if change.object.kind == "entity":
                    refs.append(change.object.entity)
            if any(isinstance(ref, LocalEntity) and ref.local_id not in entities for ref in refs):
                raise ValueError("unresolved request-local entity")
            if isinstance(change, SelectClassification):
                if (
                    isinstance(change.claim, LocalClassificationRef)
                    and change.claim.local_id not in claims
                ):
                    raise ValueError("Unresolved local classification")
                continue
            if isinstance(change, AddAssertion):
                if change.subject_classification is None:
                    raise ValueError("Assertion requires subject classification selection")
                if (change.object.kind == "entity") != (change.object_classification is not None):
                    raise ValueError("Entity objects require an object classification selection")
                for selection in (change.subject_classification, change.object_classification):
                    if (
                        isinstance(selection, LocalSelectionRef)
                        and selection.local_id not in selections
                    ):
                        raise ValueError("Unresolved local classification selection")
            if isinstance(change.support, SourceSupport):
                evidence.extend(change.support.evidence)
        if len(evidence) > MAX_SUPPORTS:
            raise ValueError("change set exceeds total evidence reference limit")
        dependencies = {
            (dependency.source_namespace, dependency.document_id): dependency
            for dependency in self.dependencies
        }
        if len(dependencies) != len(self.dependencies):
            raise ValueError("duplicate supporting document dependency")
        used: set[tuple[str, str]] = set()
        for reference in evidence:
            key = (reference.source_namespace, reference.document_id)
            dependency = dependencies.get(key)
            if dependency is None or dependency.revision_id != reference.revision_id:
                raise ValueError("every evidence revision requires a matching document dependency")
            used.add(key)
        if used != set(dependencies):
            raise ValueError("unused supporting document dependency")
        return self


class IDMapping(Value):
    local_id: Token
    stored_id: Token


class EntityClassificationReceipt(Value):
    entity_id: Token
    selection_id: Token
    selected_claim_id: Token | None


class ChangeSetReceipt(Value):
    kind: Literal["enrichment"]
    mappings: tuple[IDMapping, ...] = Field(min_length=1, max_length=MAX_CHANGES)
    entity_classifications: tuple[EntityClassificationReceipt, ...] = Field(
        default=(),
        max_length=MAX_CHANGES,
    )

    @model_validator(mode="after")
    def unique_local_ids(self) -> Self:
        if len({mapping.local_id for mapping in self.mappings}) != len(self.mappings):
            raise ValueError("duplicate local ID mapping")
        return self
