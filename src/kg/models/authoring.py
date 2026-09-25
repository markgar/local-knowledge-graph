"""Strict public record-authoring request, result, and witness values."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, Self

from pydantic import Field, ValidationError, field_validator, model_validator

from kg.knowledge._selection import CapturedEvidence, ClassificationWitness, SeedWitness
from kg.models.foundation import (
    MAX_BATCH_BYTES,
    MAX_BATCH_ITEMS,
    MAX_CHANGES,
    MAX_REQUEST_BYTES,
    MAX_SUPPORTS,
    Attribution,
    BooleanObject,
    EvidenceRef,
    Failure,
    IntegerObject,
    Label,
    Name,
    SchemaRevisionRef,
    Scope,
    StringObject,
    TimestampObject,
    Token,
    Value,
)


class SourceCapture(Value):
    kind: Literal["source"]
    reference: EvidenceRef
    state_version: Token


class SeedCapture(Value):
    kind: Literal["seed"]
    source_namespace: Token
    seed_set_id: Token
    seed_key: Token


SupportCapture = Annotated[SourceCapture | SeedCapture, Field(discriminator="kind")]
SourceNames = tuple[Token, ...]
SupportNames = SourceNames | Token


class NewIdentity(Value):
    kind: Literal["new"]
    name: Label
    support: SourceNames = Field(min_length=1, max_length=MAX_SUPPORTS)


class ExistingIdentity(Value):
    kind: Literal["existing"]
    entity_id: Token


class SupportExistingIdentity(Value):
    kind: Literal["support_existing"]
    entity_id: Token
    name: Label
    support: SourceNames = Field(min_length=1, max_length=MAX_SUPPORTS)


class SeedSlotIdentity(Value):
    kind: Literal["seed_slot"]
    name: Label
    support: Token


Identity = Annotated[
    NewIdentity | ExistingIdentity | SupportExistingIdentity | SeedSlotIdentity,
    Field(discriminator="kind"),
]


class ClassificationReviewWitness(Value):
    interface_version: Literal["classification-review-witness/1"]
    entity_id: Token
    schema_revision: SchemaRevisionRef
    selection_id: Token
    selected_claim_id: Token | None
    reviewed_claim_ids: tuple[Token, ...] = Field(max_length=MAX_SUPPORTS)
    reviewed_candidates_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    review_coverage: Literal["complete", "selected_subset"]
    accept_incomplete_review: bool

    @field_validator("reviewed_claim_ids")
    @classmethod
    def canonical_claim_ids(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(sorted(value))

    @model_validator(mode="after")
    def valid_review(self) -> Self:
        if len(set(self.reviewed_claim_ids)) != len(self.reviewed_claim_ids):
            raise ValueError("duplicate reviewed claim")
        if self.accept_incomplete_review != (self.review_coverage == "selected_subset"):
            raise ValueError("subset review requires explicit acknowledgement")
        if len(self.model_dump_json().encode()) > 65_536:
            raise ValueError("review witness exceeds serialized byte limit")
        return self


class ClassificationSelectionWitness(Value):
    interface_version: Literal["classification-selection-witness/1"]
    entity_id: Token
    schema_revision: SchemaRevisionRef
    selection_id: Token
    selected_claim_id: Token
    entity_type: Name

    @model_validator(mode="after")
    def bounded_witness(self) -> Self:
        if len(self.model_dump_json().encode()) > 65_536:
            raise ValueError("selection witness exceeds serialized byte limit")
        return self


class SelectLocalClaim(Value):
    rationale: Label
    review_witness: ClassificationReviewWitness | None = None


class CurrentSelection(Value):
    kind: Literal["current"]
    expected_entity_type: Name
    selection_witness: ClassificationSelectionWitness


class StoredClaimSelection(Value):
    kind: Literal["stored_claim"]
    claim_id: Token
    expected_entity_type: Name
    rationale: Label
    review_witness: ClassificationReviewWitness


class ClearSelection(Value):
    kind: Literal["clear"]
    rationale: Label
    review_witness: ClassificationReviewWitness


EntitySelection = Annotated[
    CurrentSelection | StoredClaimSelection | ClearSelection,
    Field(discriminator="kind"),
]


class AuthoredClassification(Value):
    local_id: Token
    entity_type: Name
    interpretation: Literal["explicit", "inferred"]
    support: SupportNames
    select: SelectLocalClaim | None = None


class AuthoredIdentifier(Value):
    local_id: Token
    scheme: Name
    value: Token
    support: SupportNames


class AuthoredAlias(Value):
    local_id: Token
    alias: Label
    support: SupportNames


class AuthoredMention(Value):
    local_id: Token
    support: SourceNames = Field(min_length=1, max_length=MAX_SUPPORTS)


class AuthoredEntity(Value):
    local_id: Token
    identity: Identity
    selection: EntitySelection | None = None
    classifications: tuple[AuthoredClassification, ...] = Field(default=(), max_length=100)
    aliases: tuple[AuthoredAlias, ...] = Field(default=(), max_length=100)
    identifiers: tuple[AuthoredIdentifier, ...] = Field(default=(), max_length=100)
    mentions: tuple[AuthoredMention, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def selection_shape(self) -> Self:
        selected = [item.local_id for item in self.classifications if item.select is not None]
        if len(selected) > 1 or selected and self.selection is not None:
            raise ValueError("invalid classification selection shape")
        if (
            isinstance(self.identity, (NewIdentity, SeedSlotIdentity))
            and self.selection is not None
        ):
            raise ValueError("new identities cannot use an entity-level selection")
        if isinstance(self.identity, (NewIdentity, SeedSlotIdentity)) and selected:
            local_selection = next(
                item.select for item in self.classifications if item.local_id == selected[0]
            )
            if local_selection is not None and local_selection.review_witness is not None:
                raise ValueError("new-entity local selection cannot carry a review witness")
        if selected and isinstance(self.identity, (ExistingIdentity, SupportExistingIdentity)):
            selection = next(
                item.select for item in self.classifications if item.local_id == selected[0]
            )
            if selection is None or selection.review_witness is None:
                raise ValueError("existing-entity local selection requires review witness")
        return self


class AuthoringEntityObject(Value):
    kind: Literal["entity"]
    entity: Token


AuthoringAssertionObject = Annotated[
    AuthoringEntityObject | StringObject | IntegerObject | BooleanObject | TimestampObject,
    Field(discriminator="kind"),
]


class AuthoredAssertion(Value):
    local_id: Token
    subject: Token
    predicate: Name
    object: AuthoringAssertionObject
    interpretation: Literal["explicit", "inferred"]
    support: SourceNames = Field(min_length=1, max_length=MAX_SUPPORTS)


class RecordAuthoringDocument(Value):
    interface_version: Literal["record-authoring/1"]
    expected_schema_revision: SchemaRevisionRef
    support: dict[Token, SupportCapture] = Field(min_length=1, max_length=MAX_SUPPORTS)
    entities: tuple[AuthoredEntity, ...] = Field(min_length=1, max_length=100)
    assertions: tuple[AuthoredAssertion, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def complete_authoring(self) -> Self:
        source_targets = [
            capture.reference
            for capture in self.support.values()
            if isinstance(capture, SourceCapture)
        ]
        seed_targets = [
            (capture.source_namespace, capture.seed_set_id, capture.seed_key)
            for capture in self.support.values()
            if isinstance(capture, SeedCapture)
        ]
        if len(set(source_targets)) != len(source_targets) or len(set(seed_targets)) != len(
            seed_targets
        ):
            raise ValueError("duplicate support declaration")
        entity_ids = [entity.local_id for entity in self.entities]
        if len(set(entity_ids)) != len(entity_ids):
            raise ValueError("duplicate entity local ID")
        authored_ids: list[str] = [*entity_ids]
        derived_ids: list[str] = []
        used_support: list[str] = []
        derived = 0
        for entity in self.entities:
            derived_ids.extend(
                (
                    f"entity/{entity.local_id}",
                    f"entity-support/{entity.local_id}",
                    f"classification-selection/{entity.local_id}",
                )
            )
            if not isinstance(entity.identity, ExistingIdentity):
                derived += 1
            identity_support = getattr(entity.identity, "support", None)
            if identity_support is not None:
                used_support.extend(
                    identity_support if isinstance(identity_support, tuple) else (identity_support,)
                )
                self._check_support_shape(identity_support)
            for collection in (
                entity.classifications,
                entity.aliases,
                entity.identifiers,
                entity.mentions,
            ):
                for item in collection:
                    authored_ids.append(item.local_id)
                    names = item.support
                    used_support.extend(names if isinstance(names, tuple) else (names,))
                    self._check_support_shape(
                        names,
                        source_only=isinstance(item, AuthoredMention),
                    )
                    derived += 1
                    if isinstance(item, AuthoredMention):
                        for name in names:
                            if name not in self.support:
                                continue
                            capture = self.support[name]
                            if (
                                not isinstance(capture, SourceCapture)
                                or capture.reference.passage_id is None
                            ):
                                raise ValueError("mentions require passage evidence")
            for classification in entity.classifications:
                derived_ids.append(f"classification/{entity.local_id}/{classification.local_id}")
                if classification.select is not None:
                    derived += 1
            if isinstance(entity.selection, (StoredClaimSelection, ClearSelection)):
                derived += 1
        for assertion in self.assertions:
            authored_ids.append(assertion.local_id)
            used_support.extend(assertion.support)
            self._check_support_shape(assertion.support, source_only=True)
            derived += 1
            if assertion.subject not in entity_ids or (
                isinstance(assertion.object, AuthoringEntityObject)
                and assertion.object.entity not in entity_ids
            ):
                raise ValueError("assertion endpoint must name a declared entity")
        if len(set(authored_ids)) != len(authored_ids):
            raise ValueError("duplicate authored local ID")
        if any(len(value) > 256 for value in derived_ids):
            raise ValueError("derived local ID exceeds token limit")
        if len(set(derived_ids)) != len(derived_ids) or set(authored_ids) & set(derived_ids):
            raise ValueError("authored local ID collides with a derived local ID")
        if derived == 0:
            raise ValueError("empty authoring")
        if derived > MAX_CHANGES:
            raise ValueError("derived change limit exceeded")
        authored_mapping_count = (
            len(self.entities)
            + len(self.assertions)
            + sum(
                len(entity.classifications)
                + len(entity.aliases)
                + len(entity.identifiers)
                + len(entity.mentions)
                for entity in self.entities
            )
        )
        if authored_mapping_count > MAX_CHANGES:
            raise ValueError("authored receipt mapping limit exceeded")
        unknown = set(used_support) - set(self.support)
        if unknown:
            raise ValueError("unknown support name")
        if set(used_support) != set(self.support):
            raise ValueError("unused support declaration")
        for name, capture in self.support.items():
            if isinstance(capture, SeedCapture) and used_support.count(name) != 1:
                raise ValueError("seed support must identify exactly one contribution")
        if len(self.model_dump_json().encode()) > MAX_REQUEST_BYTES:
            raise ValueError("authoring document exceeds serialized byte limit")
        return self

    def _check_support_shape(
        self,
        names: SupportNames,
        *,
        source_only: bool = False,
    ) -> None:
        selected = names if isinstance(names, tuple) else (names,)
        if len(set(selected)) != len(selected):
            raise ValueError("duplicate support name")
        captures = [self.support[name] for name in selected if name in self.support]
        if len(captures) != len(selected):
            return
        if isinstance(names, tuple):
            if any(not isinstance(capture, SourceCapture) for capture in captures):
                raise ValueError("mixed support kinds")
        elif source_only or not isinstance(captures[0], SeedCapture):
            raise ValueError("mixed support kinds")


class RecordAuthoringRequest(Value):
    interface_version: Literal["record-authoring/1"]
    request_id: Token
    retry_key: Token
    scope: Scope
    attribution: Attribution
    document: RecordAuthoringDocument

    @model_validator(mode="after")
    def declared_scope(self) -> Self:
        if not {"read", "write_knowledge"} <= set(self.scope.access.grants):
            raise ValueError("read and knowledge grants required")
        for capture in self.document.support.values():
            namespace = (
                capture.reference.source_namespace
                if isinstance(capture, SourceCapture)
                else capture.source_namespace
            )
            if namespace not in self.scope.access.namespaces:
                raise ValueError("support outside declared namespaces")
            if isinstance(capture, SourceCapture):
                if capture.reference.corpus_id != self.scope.corpus_id:
                    raise ValueError("cross-corpus evidence")
            elif "seed" not in self.scope.access.grants:
                raise ValueError("seed grant required")
        if len(self.model_dump_json().encode()) > MAX_REQUEST_BYTES:
            raise ValueError("authoring request exceeds serialized byte limit")
        return self


class RecordAuthoringBatch(Value):
    interface_version: Literal["record-authoring-batch/1"]
    batch_id: Token
    items: tuple[RecordAuthoringRequest, ...] = Field(
        min_length=1,
        max_length=MAX_BATCH_ITEMS,
    )

    @model_validator(mode="after")
    def unique_items(self) -> Self:
        if len({item.request_id for item in self.items}) != len(self.items):
            raise ValueError("duplicate request IDs")
        keys = {
            (
                item.scope.corpus_id,
                item.attribution.writer_id,
                item.retry_key,
            )
            for item in self.items
        }
        if len(keys) != len(self.items):
            raise ValueError("duplicate record retry identity")
        if len(self.model_dump_json().encode()) > MAX_BATCH_BYTES:
            raise ValueError("authoring batch exceeds serialized byte limit")
        return self


class SourceCaptureReceipt(Value):
    kind: Literal["source"] = "source"
    name: Token
    evidence: CapturedEvidence
    passage_set_id: Token | None = None


class SeedCaptureReceipt(Value):
    kind: Literal["seed"] = "seed"
    name: Token
    witness: SeedWitness


CaptureReceipt = Annotated[
    SourceCaptureReceipt | SeedCaptureReceipt,
    Field(discriminator="kind"),
]


class AuthoredItemReceipt(Value):
    local_id: Token
    stored_id: Token
    support_names: tuple[Token, ...] = Field(default=(), max_length=MAX_SUPPORTS)
    captures: tuple[CaptureReceipt, ...] = Field(default=(), max_length=MAX_SUPPORTS)
    subject_classification: ClassificationWitness | None = None
    object_classification: ClassificationWitness | None = None


class ClassificationReviewReceipt(Value):
    entity_local_id: Token
    coverage: Literal["complete", "selected_subset"]
    reviewed_count: int = Field(ge=0, le=MAX_SUPPORTS)
    conflicting_types: bool
    selected_local_claim_added: bool


class EntityAuthoringReceipt(Value):
    local_id: Token
    entity_id: Token
    identity_contribution_id: Token | None
    identity_support_names: tuple[Token, ...] = Field(default=(), max_length=MAX_SUPPORTS)
    identity_captures: tuple[CaptureReceipt, ...] = Field(default=(), max_length=MAX_SUPPORTS)
    classification: ClassificationWitness | None
    selection_id: Token
    selected_claim_id: Token | None
    items: tuple[AuthoredItemReceipt, ...] = Field(max_length=MAX_CHANGES)


class DerivedOperationCounts(Value):
    entity: int = Field(ge=0, le=MAX_CHANGES)
    entity_support: int = Field(ge=0, le=MAX_CHANGES)
    classification: int = Field(ge=0, le=MAX_CHANGES)
    classification_selection: int = Field(ge=0, le=MAX_CHANGES)
    alias: int = Field(ge=0, le=MAX_CHANGES)
    identifier: int = Field(ge=0, le=MAX_CHANGES)
    mention: int = Field(ge=0, le=MAX_CHANGES)
    assertion: int = Field(ge=0, le=MAX_CHANGES)


class RecordAuthoringReceipt(Value):
    kind: Literal["record_authoring"] = "record_authoring"
    interface_version: Literal["record-authoring/1"] = "record-authoring/1"
    compiler_version: Literal["record-authoring-compiler/1"] = "record-authoring-compiler/1"
    request_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    schema_revision: SchemaRevisionRef
    receipt_id: Token
    support: tuple[CaptureReceipt, ...] = Field(max_length=MAX_SUPPORTS)
    entities: tuple[EntityAuthoringReceipt, ...] = Field(max_length=100)
    assertions: tuple[AuthoredItemReceipt, ...] = Field(max_length=100)
    derived_operations: DerivedOperationCounts
    classification_reviews: tuple[ClassificationReviewReceipt, ...] = Field(max_length=100)

    @model_validator(mode="after")
    def bounded_receipt(self) -> Self:
        if len(self.model_dump_json().encode()) > MAX_REQUEST_BYTES:
            raise ValueError("authored receipt exceeds serialized byte limit")
        return self


class RecordAuthoringOutcome(Value):
    request_id: Token
    status: Literal["applied", "unchanged", "rejected", "conflict", "failed"]
    receipt: RecordAuthoringReceipt | None = None
    error: Failure | None = None

    @model_validator(mode="after")
    def exclusive_outcome(self) -> Self:
        success = self.status in {"applied", "unchanged"}
        if success != (self.receipt is not None) or success == (self.error is not None):
            raise ValueError("success requires receipt only; failure requires error only")
        if self.error is not None:
            conflict = self.error.code in {"state_conflict", "retry_conflict"}
            if conflict != (self.status == "conflict"):
                raise ValueError("conflict status and code must agree")
        return self


class RecordAuthoringBatchResult(Value):
    interface_version: Literal["record-authoring-batch/1"] = "record-authoring-batch/1"
    batch_id: Token
    status: Literal["complete", "partial", "failed"]
    outcomes: tuple[RecordAuthoringOutcome, ...] = Field(
        min_length=1,
        max_length=MAX_BATCH_ITEMS,
    )

    @model_validator(mode="after")
    def exact_summary(self) -> Self:
        successes = sum(outcome.receipt is not None for outcome in self.outcomes)
        expected = (
            "complete" if successes == len(self.outcomes) else "partial" if successes else "failed"
        )
        if self.status != expected:
            raise ValueError("batch status disagrees with outcomes")
        return self


@dataclass(frozen=True)
class RecordAuthoringValidationError(Exception):
    code: str
    path: tuple[str | int, ...]


def validate_request(value: object) -> RecordAuthoringRequest:
    try:
        return RecordAuthoringRequest.model_validate(value, strict=True)
    except ValidationError as error:
        first = error.errors(include_url=False)[0]
        message = str(first["msg"]).lower()
        codes = (
            ("duplicate authored local id", "duplicate_local_id"),
            ("duplicate entity local id", "duplicate_local_id"),
            ("collides with a derived", "duplicate_local_id"),
            ("unknown support", "unknown_support"),
            ("unused support", "unused_support"),
            ("declared entity", "implicit_endpoint"),
            ("existing-entity local selection", "review_witness_required"),
            ("classification selection shape", "invalid_classification_selection_shape"),
            ("duplicate support name", "duplicate_support"),
            ("empty authoring", "empty_authoring"),
            ("derived change limit", "derived_limit_exceeded"),
            ("derived local id", "derived_limit_exceeded"),
            ("authored receipt mapping", "derived_limit_exceeded"),
        )
        code = next((code for text, code in codes if text in message), "invalid_literal_shape")
        raise RecordAuthoringValidationError(code, tuple(first["loc"])) from None
