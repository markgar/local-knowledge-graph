"""Immutable corpus registry on the existing canonical write transaction."""

from pydantic import ValidationError

from kg.evidence._transactions import CanonicalWriteContext
from kg.evidence._values import canonical, sha, validated
from kg.evidence.errors import EvidenceServiceError, storage_error
from kg.models.evidence import LocalAdminAuthority
from kg.models.knowledge import KnowledgeSchema, KnowledgeSchemaRegistration


def definition_json(schema: KnowledgeSchema) -> str:
    value = schema.model_dump(mode="json")
    value["entity_types"] = sorted(schema.entity_types)
    value["identifier_schemes"] = sorted(schema.identifier_schemes)
    value["predicates"] = [
        {
            **predicate.model_dump(mode="json"),
            "subject_types": sorted(predicate.subject_types),
            "object_types": sorted(predicate.object_types),
        }
        for predicate in sorted(schema.predicates, key=lambda item: item.name)
    ]
    return canonical(value)


def register_schema(
    context: CanonicalWriteContext,
    authority: LocalAdminAuthority,
    schema: KnowledgeSchema,
) -> KnowledgeSchemaRegistration:
    context.check_active()
    authority = validated(LocalAdminAuthority, authority)
    schema = validated(KnowledgeSchema, schema)
    if context.identity.principal_id != authority.principal_id:
        raise EvidenceServiceError("forbidden")
    connection = context.connection
    if connection.execute(
        "SELECT 1 FROM corpus WHERE corpus_id=?", (schema.corpus_id,)
    ).fetchone() is None:
        raise EvidenceServiceError("not_found")
    definition = definition_json(schema)
    existing = connection.execute(
        "SELECT * FROM knowledge_schema WHERE corpus_id=?", (schema.corpus_id,)
    ).fetchone()
    if existing is not None:
        try:
            stored = KnowledgeSchema.model_validate_json(existing["definition_json"])
        except ValidationError as error:
            raise storage_error(error) from None
        if (
            stored.corpus_id != schema.corpus_id
            or stored.schema_version != existing["schema_version"]
            or definition_json(stored) != existing["definition_json"]
            or sha(existing["definition_json"].encode("utf-8")) != existing["definition_hash"]
        ):
            raise EvidenceServiceError("internal_error")
        if definition != existing["definition_json"]:
            raise EvidenceServiceError("state_conflict")
        return KnowledgeSchemaRegistration(
            corpus_id=schema.corpus_id, schema_version=schema.schema_version, status="unchanged",
        )
    connection.execute(
        "INSERT INTO knowledge_schema(corpus_id,schema_version,definition_json,definition_hash) "
        "VALUES (?,?,?,?)",
        (schema.corpus_id, schema.schema_version, definition, sha(definition.encode("utf-8"))),
    )
    return KnowledgeSchemaRegistration(
        corpus_id=schema.corpus_id, schema_version=schema.schema_version, status="applied",
    )
