"""Provision an immutable registry, not entities, decisions or a query service.

The embedding application provisions LocalAdminAuthority; never deserialize it
from an untrusted request. Trusted provisioning does not capture scoped reports.
"""

import argparse
from pathlib import Path

from kg.evidence import EvidenceAdministration, EvidenceDatabase
from kg.knowledge import KnowledgeAdministration
from kg.models.evidence import CorpusRegistration, LocalAdminAuthority, LocalPolicy
from kg.models.knowledge import (
    KnowledgeSchema,
    KnowledgeSchemaRegistration,
    PredicateDefinition,
    RecordProjection,
)


def run(path: Path) -> KnowledgeSchemaRegistration:
    database = EvidenceDatabase(path)
    database.initialize()
    authority = LocalAdminAuthority(principal_id="trusted-local-app")
    EvidenceAdministration(database, authority).register(
        CorpusRegistration(
            corpus_id="registry-demo",
            namespaces=("notes",),
            policy=LocalPolicy(corpus_id="registry-demo"),
        ),
    )
    return KnowledgeAdministration(database, authority).register_knowledge_schema(
        KnowledgeSchema(
            corpus_id="registry-demo",
            schema_version="example/1",
            entity_types=("person", "project"),
            identifier_schemes=("email",),
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
        ),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    print(run(parser.parse_args().database).model_dump_json(indent=2))
