from kg.models.knowledge import KnowledgeSchema, PredicateDefinition, RecordProjection


def schema(corpus="work", version="test/1"):
    return KnowledgeSchema(
        corpus_id=corpus,
        schema_version=version,
        entity_types=("person", "project"),
        identifier_schemes=("email", "ticket"),
        predicates=(
            PredicateDefinition(
                name="work:owns", subject_types=("person",),
                object_kind="entity", object_types=("project",),
            ),
            PredicateDefinition(
                name="work:decision", subject_types=("project",), object_kind="string",
                record_projection=RecordProjection(encoding="direct-subject-decision/1"),
            ),
        ),
    )
