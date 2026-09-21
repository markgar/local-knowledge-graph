"""Trusted schema provisioning, deliberately outside scoped execution reports."""

from kg.evidence._transactions import writing
from kg.evidence._values import validated
from kg.evidence.database import EvidenceDatabase
from kg.knowledge._registry import register_schema
from kg.models.evidence import LocalAdminAuthority, LocalIdentity
from kg.models.knowledge import KnowledgeSchema, KnowledgeSchemaRegistration


class KnowledgeAdministration:
    def __init__(self, database: EvidenceDatabase, authority: LocalAdminAuthority) -> None:
        self.database = database
        self.authority = validated(LocalAdminAuthority, authority)

    def register_knowledge_schema(self, schema: KnowledgeSchema) -> KnowledgeSchemaRegistration:
        schema = validated(KnowledgeSchema, schema)
        authority = validated(LocalAdminAuthority, self.authority)
        identity = LocalIdentity(principal_id=authority.principal_id)
        with writing(self.database, identity) as context:
            return register_schema(context, authority, schema)
