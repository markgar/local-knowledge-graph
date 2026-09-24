"""Boundary values for described schema protocol, with actual-service atomic rejection."""

import pytest
from pydantic import ValidationError
from test_schema_evolution import counts, proposal, request
from test_schema_evolution import env as env

from kg.evidence import EvidenceServiceError
from kg.models.schema import (
    EntityTypeDefinition,
    SchemaDefinition,
    SchemaPredicateDefinition,
    SchemaView,
)


@pytest.mark.unit
def test_term_text_and_registry_size_boundaries():
    assert len(EntityTypeDefinition(name="kind", description="x" * 4096).description) == 4096
    for description in (" ", "x" * 4097, "\u00e9" * 2049):
        with pytest.raises(ValidationError):
            EntityTypeDefinition(name="kind", description=description)
    types = tuple(
        EntityTypeDefinition(name=f"type{i}", description="Description.") for i in range(1001)
    )
    assert len(SchemaDefinition(entity_types=types[:1000]).entity_types) == 1000
    for collection in ((), types, (types[0], types[0])):
        with pytest.raises(ValidationError):
            SchemaDefinition(entity_types=collection)
    with pytest.raises(ValidationError, match="byte limit"):
        SchemaDefinition(entity_types=tuple(
            entry.model_copy(update={"description": "x" * 4096}) for entry in types[:300]
        ))


@pytest.mark.unit
def test_predicate_endpoint_and_encoding_boundaries():
    names = tuple(f"type{i}" for i in range(101))
    assert len(SchemaPredicateDefinition(
        name="relation", description="An explicit relation.",
        subject_types=names[:100], object_kind="entity", object_types=names[:100],
    ).subject_types) == 100
    for updates in (
        {"subject_types": names}, {"object_types": ()}, {"object_types": names},
        {"object_kind": "boolean"}, {"record_projection": {"encoding": "unknown"}},
    ):
        with pytest.raises(ValidationError):
            SchemaPredicateDefinition.model_validate({
                "name": "relation", "description": "An explicit relation.",
                "subject_types": ("type0",), "object_kind": "entity", "object_types": ("type0",),
                **updates,
            })


@pytest.mark.service
@pytest.mark.parametrize("limit", [0, 101, True])
def test_history_limit_is_explicit_and_has_no_partial_result(env, limit):
    with pytest.raises(EvidenceServiceError, match="invalid_request"):
        env.knowledge.schema_history(env.scope, limit=limit)


@pytest.mark.service
def test_oversize_forged_proposal_rejected_atomically(env):
    value = proposal(env).model_copy(update={"rationale": "x" * 4097})
    before = counts(env)
    with pytest.raises(EvidenceServiceError, match="invalid_request"):
        env.schema_admin.apply_schema(request(env, value))
    assert counts(env) == before


@pytest.mark.service
def test_unconfigured_view_cannot_claim_a_configured_schema(env):
    configured = env.knowledge.schema(env.scope)
    assert SchemaView.model_validate_json(configured.model_dump_json()) == configured
    for mutation in ({"status": "unconfigured"}, {"head": None}, {"definition": None}):
        with pytest.raises(ValidationError):
            SchemaView.model_validate({**configured.model_dump(), **mutation})
