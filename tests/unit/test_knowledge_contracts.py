import json

import pytest
from pydantic import ValidationError
from support.knowledge import schema

from kg.models.knowledge import (
    KnowledgeSchema,
    KnowledgeSchemaRegistration,
    PredicateDefinition,
    RecordProjection,
)


def test_registry_roundtrip_schema_and_frozen_values():
    value = schema()
    assert KnowledgeSchema.model_validate_json(value.model_dump_json()) == value
    assert value.interface_version == "knowledge/1"
    definitions = KnowledgeSchema.model_json_schema()["$defs"]
    assert {"PredicateDefinition", "RecordProjection"} <= definitions.keys()
    with pytest.raises(ValidationError, match="frozen"):
        value.schema_version = "other"
    with pytest.raises(ValidationError, match="frozen"):
        value.predicates[0].name = "other"
    for status in ("applied", "unchanged"):
        result = KnowledgeSchemaRegistration(
            corpus_id="work", schema_version="test/1", status=status,
        )
        assert KnowledgeSchemaRegistration.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("kind", ["entity", "string", "integer", "boolean", "timestamp"])
def test_all_predicate_object_kinds(kind):
    predicate = PredicateDefinition(
        name="test:value", subject_types=("person",),
        object_kind=kind, object_types=("project",) if kind == "entity" else (),
    )
    value = schema().model_dump()
    value["predicates"] = (predicate,)
    assert KnowledgeSchema.model_validate(value).predicates == (predicate,)


@pytest.mark.parametrize(
    "field,value",
    [
        ("name", "Upper"),
        ("name", "a" * 129),
        ("name", ""),
        ("subject_types", ()),
        ("subject_types", ("person", "person")),
        ("subject_types", ["person"]),
        ("object_kind", "float"),
        ("object_kind", True),
        ("object_types", ("project",)),
        ("extra", "unknown"),
    ],
)
def test_invalid_predicate_fields(field, value):
    fields = dict(name="test:value", subject_types=("person",), object_kind="string")
    fields[field] = value
    with pytest.raises(ValidationError):
        PredicateDefinition.model_validate(fields)


def test_entity_object_types_and_known_type_references():
    with pytest.raises(ValidationError):
        PredicateDefinition(name="edge", subject_types=("person",), object_kind="entity")
    with pytest.raises(ValidationError):
        PredicateDefinition(
            name="edge", subject_types=("person",), object_kind="entity",
            object_types=("project", "project"),
        )
    for side in ("subject_types", "object_types"):
        fields = schema().model_dump()
        fields["predicates"] = (
            schema().predicates[0].model_copy(update={side: ("unknown",)}),
        )
        with pytest.raises(ValidationError, match="unknown entity type"):
            KnowledgeSchema.model_validate(fields)


@pytest.mark.parametrize("kind", ["entity", "integer", "boolean", "timestamp"])
def test_decision_descriptor_only_on_string(kind):
    with pytest.raises(ValidationError, match="string predicate"):
        PredicateDefinition(
            name="test:decision", subject_types=("project",),
            object_kind=kind, object_types=("person",) if kind == "entity" else (),
            record_projection=RecordProjection(encoding="direct-subject-decision/1"),
        )


@pytest.mark.parametrize("fields", [
    {}, {"encoding": "decision/2"}, {"encoding": "action"},
    {"encoding": "direct-subject-decision/1", "record_type": "action"},
])
def test_closed_record_descriptor(fields):
    with pytest.raises(ValidationError):
        RecordProjection.model_validate(fields)


@pytest.mark.parametrize("field,value", [
    ("corpus_id", ""), ("schema_version", " "), ("schema_version", True),
    ("schema_version", "x" * 257), ("corpus_id", "\ud800"),
    ("interface_version", "knowledge/2"), ("extra", "unknown"),
    ("entity_types", ()), ("entity_types", ("person", "person")),
    ("entity_types", ("Upper",)), ("entity_types", ["person"]),
    ("identifier_schemes", ("email", "email")), ("identifier_schemes", ("invalid scheme",)),
])
def test_invalid_registry_fields(field, value):
    fields = schema().model_dump()
    fields[field] = value
    with pytest.raises(ValidationError):
        KnowledgeSchema.model_validate(fields)


def test_duplicate_predicates_even_with_distinct_definitions():
    fields = schema().model_dump()
    fields["predicates"] = (
        schema().predicates[0],
        PredicateDefinition(name="work:owns", subject_types=("project",), object_kind="string"),
    )
    with pytest.raises(ValidationError, match="Duplicate"):
        KnowledgeSchema.model_validate(fields)


@pytest.mark.parametrize("field", ["entity_types", "identifier_schemes", "predicates"])
def test_exact_registry_entry_limits(field):
    def entries(n):
        if field == "predicates":
            return tuple(
                PredicateDefinition(name=f"p{i}", subject_types=("person",), object_kind="string")
                for i in range(n)
            )
        return tuple(f"v{i}" for i in range(n))
    fields = dict(corpus_id="work", schema_version="v1", entity_types=("person",))
    fields[field] = entries(1000)
    assert len(getattr(KnowledgeSchema.model_validate(fields), field)) == 1000
    fields[field] = entries(1001)
    with pytest.raises(ValidationError):
        KnowledgeSchema.model_validate(fields)


@pytest.mark.parametrize("side", ["subject_types", "object_types"])
def test_exact_predicate_side_limit(side):
    fields = dict(
        name="edge", subject_types=("person",), object_kind="entity", object_types=("person",),
    )
    fields[side] = tuple(f"type{i}" for i in range(100))
    assert len(getattr(PredicateDefinition.model_validate(fields), side)) == 100
    fields[side] += ("one_more",)
    with pytest.raises(ValidationError):
        PredicateDefinition.model_validate(fields)


def test_optional_registries_and_explicit_null_projection():
    value = KnowledgeSchema(corpus_id="work", schema_version="v1", entity_types=("person",))
    assert value.identifier_schemes == value.predicates == ()
    assert json.loads(schema().model_dump_json())["predicates"][0]["record_projection"] is None
