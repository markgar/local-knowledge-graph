from kg.knowledge import KnowledgeService
from kg.models.evidence import LocalIdentity
from kg.models.knowledge import KnowledgeSchema, PredicateDefinition, RecordProjection
from kg.models.schema import (
    EntityTypeDefinition,
    IdentifierSchemeDefinition,
    SchemaDefinition,
    SchemaPredicateDefinition,
    SchemaPresetRequest,
)


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


def preset(value=None):
    """Deliberate test-only definitions; not a legacy runtime provisioning adapter."""
    value = schema() if value is None else value
    return SchemaPresetRequest(
        corpus_id=value.corpus_id,
        preset_name=value.schema_version,
        preset_rationale="Explicit synthetic test vocabulary.",
        definition=SchemaDefinition(
            entity_types=tuple(
                EntityTypeDefinition(name=name, description=f"Synthetic {name} identity.")
                for name in value.entity_types
            ),
            identifier_schemes=tuple(
                IdentifierSchemeDefinition(name=name, description=f"Synthetic {name} identifier.")
                for name in value.identifier_schemes
            ),
            predicates=tuple(
                SchemaPredicateDefinition(
                    **p.model_dump(), description=f"Synthetic {p.name} assertion.",
                )
                for p in value.predicates
            ),
        ),
    )


def revision(env):
    return KnowledgeService(
        env.database, LocalIdentity(principal_id=env.scope.access.principal_id),
    ).schema(env.scope).head
