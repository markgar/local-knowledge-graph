"""Explicit classification inputs for synthetic typed-knowledge fixtures."""

from kg.knowledge import KnowledgeService
from kg.models.foundation import (
    AddAssertion,
    AddClassification,
    CreateEntity,
    EntityObject,
    LocalClassificationRef,
    LocalEntity,
    LocalSelectionRef,
    SeedSupport,
    SelectClassification,
    StoredSelectionRef,
)


def typed_entity(*, kind="entity", local_id, name, entity_type, support):
    assert kind == "entity"
    claim_support = (
        support.model_copy(update={"seed_key": f"classification:{support.seed_key}"})
        if isinstance(support, SeedSupport)
        else support
    )
    return (
        CreateEntity(kind="entity", local_id=local_id, name=name, support=support),
        AddClassification(
            kind="classification",
            local_id=f"{local_id}:classification",
            entity=LocalEntity(kind="local", local_id=local_id),
            entity_type=entity_type,
            interpretation="explicit",
            support=claim_support,
        ),
        SelectClassification(
            kind="classification_selection",
            local_id=f"{local_id}:selection",
            entity=LocalEntity(kind="local", local_id=local_id),
            claim=LocalClassificationRef(kind="local", local_id=f"{local_id}:classification"),
            expected_selection_id=None,
            reviewed_candidates_digest=None,
            reviewed_claim_ids=(),
            review_coverage="complete",
            accept_incomplete_review=False,
            rationale="Explicit synthetic fixture classification.",
        ),
    )


def fixture_changes(env, changes):
    """Capture fixture choices when preparing input, never during service execution."""
    flat = tuple(
        item for value in changes for item in (value if isinstance(value, tuple) else (value,))
    )
    service = KnowledgeService(env.database, env.service.identity)
    captured = {}

    def capture(ref):
        if isinstance(ref, LocalEntity):
            return LocalSelectionRef(kind="local", local_id=f"{ref.local_id}:selection")
        if ref.entity_id not in captured:
            captured[ref.entity_id] = StoredSelectionRef(
                kind="stored",
                event_id=service.entity(
                    env.scope,
                    ref.entity_id,
                    mode="history",
                ).classification.selection_id,
            )
        return captured[ref.entity_id]

    result = []
    for change in flat:
        if isinstance(change, AddAssertion):
            fields = {}
            if change.subject_classification is None:
                fields["subject_classification"] = capture(change.subject)
            if isinstance(change.object, EntityObject) and change.object_classification is None:
                fields["object_classification"] = capture(change.object.entity)
            change = change.model_copy(update=fields)
        result.append(change)
    return tuple(result)
