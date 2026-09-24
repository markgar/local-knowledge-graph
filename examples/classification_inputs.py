"""Explicit synthetic fixture inputs shared by the executable examples."""

from kg.models.foundation import (
    AddClassification,
    CreateEntity,
    LocalClassificationRef,
    LocalEntity,
    SeedSupport,
    SelectClassification,
    Support,
)


def classified_entity(
    *,
    local_id: str,
    name: str,
    entity_type: str,
    support: Support,
) -> tuple[CreateEntity, AddClassification, SelectClassification]:
    """Author three explicit changes, not an implicit runtime type conversion."""
    classification_support = (
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
            support=classification_support,
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
