"""Deterministic compilation of concise authored records into canonical plans."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Literal, cast

from kg._execution_budget import PrivateBudget
from kg.evidence._transactions import CanonicalWriteContext
from kg.evidence.errors import EvidenceServiceError
from kg.knowledge import _classification
from kg.knowledge._authoring_models import (
    AuthoredEntity,
    AuthoringEntityObject,
    ClassificationReviewReceipt,
    ClassificationReviewWitness,
    ClearSelection,
    CurrentSelection,
    ExistingIdentity,
    NewIdentity,
    RecordAuthoringRequest,
    SeedCapture,
    SeedSlotIdentity,
    SourceCapture,
    StoredClaimSelection,
    SupportCapture,
    SupportExistingIdentity,
)
from kg.knowledge._store import Store
from kg.knowledge._write_models import (
    AddAlias,
    AddAssertion,
    AddClassification,
    AddEntitySupport,
    AddIdentifier,
    AddMention,
    Change,
    ChangeSet,
    CreateEntity,
    SelectClassification,
)
from kg.models.foundation import (
    DocumentDependency,
    EntityObject,
    LocalClassificationRef,
    LocalEntity,
    LocalSelectionRef,
    SeedSupport,
    SourceSupport,
    StoredClassificationRef,
    StoredEntity,
    StoredSelectionRef,
)
from kg.models.knowledge import ClassificationReview


@dataclass(frozen=True)
class CompiledAuthoring:
    plan: ChangeSet
    support_names: dict[str, tuple[str, ...]]
    authored_ids: dict[str, str]
    entity_plan_ids: dict[str, str | None]
    entity_item_ids: dict[str, tuple[str, ...]]
    entity_selection_refs: dict[str, LocalSelectionRef | StoredSelectionRef | None]
    review_receipts: tuple[ClassificationReviewReceipt, ...]
    operation_counts: Counter[str]


@dataclass(frozen=True)
class ReviewFields:
    expected_selection_id: str
    reviewed_candidates_digest: str
    reviewed_claim_ids: tuple[str, ...]
    review_coverage: Literal["complete", "selected_subset"]
    accept_incomplete_review: bool


def _source_key(capture: SourceCapture) -> tuple[str, ...]:
    reference = capture.reference
    return (
        reference.corpus_id,
        reference.source_namespace,
        reference.document_id,
        reference.revision_id,
        reference.anchor_id,
        reference.passage_id or "",
        capture.state_version,
    )


def _capture_key(capture: SupportCapture) -> tuple[str, ...]:
    if isinstance(capture, SourceCapture):
        return _source_key(capture)
    return capture.source_namespace, capture.seed_set_id, capture.seed_key


class Compiler:
    def __init__(
        self,
        context: CanonicalWriteContext,
        request: RecordAuthoringRequest,
        budget: PrivateBudget,
    ) -> None:
        self.context = context
        self.request = request
        self.document = request.document
        self.store = Store(context.connection, request.scope, budget)
        self.support_names: dict[str, tuple[str, ...]] = {}
        self.authored_ids: dict[str, str] = {}
        self.dependencies: dict[tuple[str, str], DocumentDependency] = {}
        self.source_occurrences = 0

    def close(self) -> None:
        self.store.close()

    def support(
        self,
        names: tuple[str, ...] | str,
        *,
        source_only: bool = False,
    ) -> SourceSupport | SeedSupport:
        selected = names if isinstance(names, tuple) else (names,)
        captures = [self.document.support[name] for name in selected]
        if len(set(selected)) != len(selected):
            raise EvidenceServiceError("invalid_request")
        if all(isinstance(capture, SourceCapture) for capture in captures):
            if not isinstance(names, tuple):
                raise EvidenceServiceError("invalid_request")
            sources = cast(list[SourceCapture], captures)
            ordered = sorted(sources, key=_source_key)
            if len({capture.reference for capture in ordered}) != len(ordered):
                raise EvidenceServiceError("invalid_request")
            self.source_occurrences += len(ordered)
            if self.source_occurrences > 200:
                raise EvidenceServiceError("invalid_request")
            for capture in ordered:
                reference = capture.reference
                key = reference.source_namespace, reference.document_id
                dependency = DocumentDependency(
                    source_namespace=reference.source_namespace,
                    document_id=reference.document_id,
                    revision_id=reference.revision_id,
                    state_version=capture.state_version,
                )
                existing = self.dependencies.get(key)
                if existing is not None and existing != dependency:
                    raise EvidenceServiceError("invalid_request")
                self.dependencies[key] = dependency
            return SourceSupport(
                kind="source",
                evidence=tuple(capture.reference for capture in ordered),
            )
        if source_only or isinstance(names, tuple) or len(captures) != 1:
            raise EvidenceServiceError("invalid_request")
        seed_capture = captures[0]
        if not isinstance(seed_capture, SeedCapture):
            raise EvidenceServiceError("invalid_request")
        return SeedSupport(
            kind="seed",
            source_namespace=seed_capture.source_namespace,
            seed_set_id=seed_capture.seed_set_id,
            seed_key=seed_capture.seed_key,
        )

    def canonical_names(self, names: tuple[str, ...] | str) -> tuple[str, ...]:
        selected = names if isinstance(names, tuple) else (names,)
        return tuple(
            sorted(
                selected,
                key=lambda name: _capture_key(self.document.support[name]),
            )
        )

    def review_fields(
        self,
        witness: ClassificationReviewWitness,
        entity_id: str,
    ) -> ReviewFields:
        if (
            witness.entity_id != entity_id
            or witness.schema_revision != self.document.expected_schema_revision
        ):
            raise EvidenceServiceError("state_conflict")
        return ReviewFields(
            expected_selection_id=witness.selection_id,
            reviewed_candidates_digest=witness.reviewed_candidates_digest,
            reviewed_claim_ids=tuple(sorted(witness.reviewed_claim_ids)),
            review_coverage=witness.review_coverage,
            accept_incomplete_review=witness.accept_incomplete_review,
        )

    def validate_review(
        self,
        witness: ClassificationReviewWitness,
        review: ClassificationReview,
    ) -> None:
        selected = review.selected
        if (
            review.selection_id != witness.selection_id
            or (selected.claim_id if selected is not None else None)
            != witness.selected_claim_id
            or tuple(review.reviewed_claim_ids)
            != tuple(sorted(witness.reviewed_claim_ids))
            or review.reviewed_candidates_digest != witness.reviewed_candidates_digest
            or review.review_coverage != witness.review_coverage
        ):
            raise EvidenceServiceError("state_conflict")

    def current_selection(
        self,
        entity: AuthoredEntity,
        selection: CurrentSelection,
    ) -> StoredSelectionRef:
        identity = entity.identity
        if not isinstance(identity, (ExistingIdentity, SupportExistingIdentity)):
            raise EvidenceServiceError("invalid_request")
        witness = selection.selection_witness
        if (
            witness.entity_id != identity.entity_id
            or witness.schema_revision != self.document.expected_schema_revision
            or witness.entity_type != selection.expected_entity_type
        ):
            raise EvidenceServiceError("state_conflict")
        selected = _classification.selected(self.store, identity.entity_id)
        if (
            selected is None
            or selected.selection_id != witness.selection_id
            or selected.claim_id != witness.selected_claim_id
            or selected.entity_type != witness.entity_type
        ):
            raise EvidenceServiceError("state_conflict")
        return StoredSelectionRef(kind="stored", event_id=witness.selection_id)

    def compile(self) -> CompiledAuthoring:
        if self.document.expected_schema_revision != self.store.registry.head():
            raise EvidenceServiceError("state_conflict")
        identities: list[Change] = []
        classifications: list[Change] = []
        selections: list[Change] = []
        nested: list[Change] = []
        assertions: list[Change] = []
        entity_refs: dict[str, LocalEntity | StoredEntity] = {}
        entity_plan_ids: dict[str, str | None] = {}
        entity_item_ids: dict[str, list[str]] = {}
        selection_refs: dict[str, LocalSelectionRef | StoredSelectionRef | None] = {}
        reviews: list[ClassificationReviewReceipt] = []

        for entity in self.document.entities:
            entity_item_ids[entity.local_id] = []
            identity = entity.identity
            if isinstance(identity, ExistingIdentity):
                entity_ref: LocalEntity | StoredEntity = StoredEntity(
                    kind="stored", entity_id=identity.entity_id,
                )
                self.store.require_entity(identity.entity_id)
                entity_plan_ids[entity.local_id] = None
            elif isinstance(identity, SupportExistingIdentity):
                entity_ref = StoredEntity(kind="stored", entity_id=identity.entity_id)
                local_id = f"entity-support/{entity.local_id}"
                identities.append(
                    AddEntitySupport(
                        kind="entity_support",
                        local_id=local_id,
                        entity=entity_ref,
                        name=identity.name,
                        support=self.support(identity.support),
                    )
                )
                self.support_names[local_id] = self.canonical_names(identity.support)
                self.authored_ids[local_id] = entity.local_id
                entity_plan_ids[entity.local_id] = local_id
            else:
                entity_ref = LocalEntity(kind="local", local_id=f"entity/{entity.local_id}")
                local_id = entity_ref.local_id
                support = (
                    self.support(identity.support)
                    if isinstance(identity, (NewIdentity, SeedSlotIdentity))
                    else None
                )
                assert support is not None
                identities.append(
                    CreateEntity(
                        kind="entity",
                        local_id=local_id,
                        name=identity.name,
                        support=support,
                    )
                )
                self.support_names[local_id] = self.canonical_names(identity.support)
                self.authored_ids[local_id] = entity.local_id
                entity_plan_ids[entity.local_id] = local_id
            entity_refs[entity.local_id] = entity_ref

            selected_local = next(
                (
                    classification
                    for classification in entity.classifications
                    if classification.select is not None
                ),
                None,
            )
            for classification in entity.classifications:
                local_id = f"classification/{entity.local_id}/{classification.local_id}"
                classifications.append(
                    AddClassification(
                        kind="classification",
                        local_id=local_id,
                        entity=entity_ref,
                        entity_type=classification.entity_type,
                        interpretation=classification.interpretation,
                        support=self.support(classification.support),
                    )
                )
                self.support_names[local_id] = self.canonical_names(classification.support)
                self.authored_ids[local_id] = classification.local_id
                entity_item_ids[entity.local_id].append(local_id)
            if selected_local is not None:
                selection_id = f"classification-selection/{entity.local_id}"
                select = selected_local.select
                assert select is not None
                fields: ReviewFields | None
                selected_added = False
                if isinstance(entity_ref, LocalEntity):
                    fields = None
                    reviews.append(
                        ClassificationReviewReceipt(
                            entity_local_id=entity.local_id,
                            coverage="complete",
                            reviewed_count=len(entity.classifications),
                            conflicting_types=(
                                len(
                                    {
                                        classification.entity_type
                                        for classification in entity.classifications
                                    }
                                )
                                > 1
                            ),
                            selected_local_claim_added=False,
                        )
                    )
                else:
                    if select.review_witness is None:
                        raise EvidenceServiceError("invalid_request")
                    fields = self.review_fields(select.review_witness, entity_ref.entity_id)
                    selected_added = (
                        select.review_witness.review_coverage == "selected_subset"
                    )
                    review = _classification.review(
                        self.store,
                        entity_ref.entity_id,
                        select.review_witness.reviewed_claim_ids
                        if select.review_witness.review_coverage == "selected_subset"
                        else None,
                    )
                    self.validate_review(select.review_witness, review)
                    reviews.append(
                        ClassificationReviewReceipt(
                            entity_local_id=entity.local_id,
                            coverage=review.review_coverage,
                            reviewed_count=len(review.reviewed_claim_ids),
                            conflicting_types=(
                                len(
                                    {
                                        *(
                                            claim.entity_type
                                            for claim in review.claims
                                        ),
                                        *(
                                            classification.entity_type
                                            for classification in entity.classifications
                                        ),
                                    }
                                )
                                > 1
                            ),
                            selected_local_claim_added=selected_added,
                        )
                    )
                selections.append(
                    SelectClassification(
                        kind="classification_selection",
                        local_id=selection_id,
                        entity=entity_ref,
                        claim=LocalClassificationRef(
                            kind="local",
                            local_id=(
                                f"classification/{entity.local_id}/{selected_local.local_id}"
                            ),
                        ),
                        rationale=select.rationale,
                        expected_selection_id=(
                            fields.expected_selection_id if fields is not None else None
                        ),
                        reviewed_candidates_digest=(
                            fields.reviewed_candidates_digest if fields is not None else None
                        ),
                        reviewed_claim_ids=(
                            fields.reviewed_claim_ids if fields is not None else ()
                        ),
                        review_coverage=(
                            fields.review_coverage if fields is not None else "complete"
                        ),
                        accept_incomplete_review=(
                            fields.accept_incomplete_review if fields is not None else False
                        ),
                    )
                )
                selection_refs[entity.local_id] = LocalSelectionRef(
                    kind="local", local_id=selection_id,
                )
            elif isinstance(entity.selection, CurrentSelection):
                selection_refs[entity.local_id] = self.current_selection(
                    entity, entity.selection,
                )
            elif isinstance(entity.selection, (StoredClaimSelection, ClearSelection)):
                identity = entity.identity
                if not isinstance(identity, (ExistingIdentity, SupportExistingIdentity)):
                    raise EvidenceServiceError("invalid_request")
                selection = entity.selection
                fields = self.review_fields(selection.review_witness, identity.entity_id)
                claim = None
                if isinstance(selection, StoredClaimSelection):
                    value = _classification.claim(
                        self.store, selection.claim_id, entity_id=identity.entity_id,
                    )
                    if (
                        not value.is_current
                        or value.entity_type != selection.expected_entity_type
                    ):
                        raise EvidenceServiceError("state_conflict")
                    claim = StoredClassificationRef(
                        kind="stored", contribution_id=selection.claim_id,
                    )
                review = _classification.review(
                    self.store,
                    identity.entity_id,
                    selection.review_witness.reviewed_claim_ids
                    if selection.review_witness.review_coverage == "selected_subset"
                    else None,
                )
                self.validate_review(selection.review_witness, review)
                reviews.append(
                    ClassificationReviewReceipt(
                        entity_local_id=entity.local_id,
                        coverage=review.review_coverage,
                        reviewed_count=len(review.reviewed_claim_ids),
                        conflicting_types=(
                            len(
                                {
                                    *(claim.entity_type for claim in review.claims),
                                    *(
                                        classification.entity_type
                                        for classification in entity.classifications
                                    ),
                                }
                            )
                            > 1
                        ),
                        selected_local_claim_added=False,
                    )
                )
                selection_id = f"classification-selection/{entity.local_id}"
                selections.append(
                    SelectClassification(
                        kind="classification_selection",
                        local_id=selection_id,
                        entity=entity_ref,
                        claim=claim,
                        rationale=selection.rationale,
                        expected_selection_id=fields.expected_selection_id,
                        reviewed_candidates_digest=fields.reviewed_candidates_digest,
                        reviewed_claim_ids=fields.reviewed_claim_ids,
                        review_coverage=fields.review_coverage,
                        accept_incomplete_review=fields.accept_incomplete_review,
                    )
                )
                selection_refs[entity.local_id] = LocalSelectionRef(
                    kind="local", local_id=selection_id,
                )
            else:
                selection_refs[entity.local_id] = None

            for alias in entity.aliases:
                nested.append(
                    AddAlias(
                        kind="alias",
                        local_id=alias.local_id,
                        entity=entity_ref,
                        alias=alias.alias,
                        support=self.support(alias.support),
                    )
                )
                self.support_names[alias.local_id] = self.canonical_names(alias.support)
                self.authored_ids[alias.local_id] = alias.local_id
                entity_item_ids[entity.local_id].append(alias.local_id)
            for identifier in entity.identifiers:
                nested.append(
                    AddIdentifier(
                        kind="identifier",
                        local_id=identifier.local_id,
                        entity=entity_ref,
                        scheme=identifier.scheme,
                        value=identifier.value,
                        support=self.support(identifier.support),
                    )
                )
                self.support_names[identifier.local_id] = self.canonical_names(
                    identifier.support,
                )
                self.authored_ids[identifier.local_id] = identifier.local_id
                entity_item_ids[entity.local_id].append(identifier.local_id)
            for mention in entity.mentions:
                mention_support = self.support(mention.support, source_only=True)
                if not isinstance(mention_support, SourceSupport):
                    raise EvidenceServiceError("invalid_request")
                nested.append(
                    AddMention(
                        kind="mention",
                        local_id=mention.local_id,
                        entity=entity_ref,
                        support=mention_support,
                    )
                )
                self.support_names[mention.local_id] = self.canonical_names(mention.support)
                self.authored_ids[mention.local_id] = mention.local_id
                entity_item_ids[entity.local_id].append(mention.local_id)

        for assertion in self.document.assertions:
            subject_selection = selection_refs[assertion.subject]
            object_selection = (
                selection_refs[assertion.object.entity]
                if isinstance(assertion.object, AuthoringEntityObject)
                else None
            )
            if subject_selection is None or (
                isinstance(assertion.object, AuthoringEntityObject)
                and object_selection is None
            ):
                raise EvidenceServiceError("invalid_request")
            obj = (
                EntityObject(kind="entity", entity=entity_refs[assertion.object.entity])
                if isinstance(assertion.object, AuthoringEntityObject)
                else assertion.object
            )
            assertion_support = self.support(assertion.support, source_only=True)
            if not isinstance(assertion_support, SourceSupport):
                raise EvidenceServiceError("invalid_request")
            assertions.append(
                AddAssertion(
                    kind="assertion",
                    local_id=assertion.local_id,
                    subject=entity_refs[assertion.subject],
                    predicate=assertion.predicate,
                    object=obj,
                    interpretation=assertion.interpretation,
                    support=assertion_support,
                    subject_classification=subject_selection,
                    object_classification=object_selection,
                )
            )
            self.support_names[assertion.local_id] = self.canonical_names(assertion.support)
            self.authored_ids[assertion.local_id] = assertion.local_id

        changes = tuple((*identities, *classifications, *selections, *nested, *assertions))
        if not 1 <= len(changes) <= 100:
            raise EvidenceServiceError("invalid_request")
        counts: Counter[str] = Counter(change.kind for change in changes)
        plan = ChangeSet(
            operation="enrich",
            expected_schema_revision=self.document.expected_schema_revision,
            dependencies=tuple(
                self.dependencies[key]
                for key in sorted(self.dependencies)
            ),
            changes=changes,
        )
        return CompiledAuthoring(
            plan=plan,
            support_names=self.support_names,
            authored_ids=self.authored_ids,
            entity_plan_ids=entity_plan_ids,
            entity_item_ids={
                local_id: tuple(items) for local_id, items in entity_item_ids.items()
            },
            entity_selection_refs=selection_refs,
            review_receipts=tuple(reviews),
            operation_counts=counts,
        )


def compile_authoring(
    context: CanonicalWriteContext,
    request: RecordAuthoringRequest,
    budget: PrivateBudget,
) -> CompiledAuthoring:
    compiler = Compiler(context, request, budget)
    try:
        return compiler.compile()
    finally:
        compiler.close()
