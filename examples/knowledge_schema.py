"""Provision an immutable registry, not entities, decisions or a query service.

The embedding application provisions LocalAdminAuthority; never deserialize it
from an untrusted request. Trusted provisioning does not capture scoped reports.
"""

import argparse
from pathlib import Path

from kg.evidence import EvidenceAdministration, EvidenceDatabase
from kg.knowledge import KnowledgeAdministration
from kg.models.evidence import CorpusRegistration, LocalAdminAuthority, LocalPolicy
from kg.models.knowledge import RecordProjection
from kg.models.schema import (
    EntityTypeDefinition,
    IdentifierSchemeDefinition,
    SchemaDefinition,
    SchemaPredicateDefinition,
    SchemaPresetRegistration,
    SchemaPresetRequest,
)


def run(path: Path) -> SchemaPresetRegistration:
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
        SchemaPresetRequest(
            corpus_id="registry-demo",
            preset_name="example/1",
            preset_rationale="Explicit example vocabulary, not sampled-document inference.",
            definition=SchemaDefinition(
                entity_types=(
                    EntityTypeDefinition(name="person", description="An individual person."),
                    EntityTypeDefinition(name="project", description="An identified project."),
                ),
                identifier_schemes=(
                    IdentifierSchemeDefinition(name="email", description="An email address."),
                ),
                predicates=(
                    SchemaPredicateDefinition(
                        name="work:owns", description="The subject is accountable for the object.",
                        subject_types=("person",), object_kind="entity", object_types=("project",),
                    ),
                    SchemaPredicateDefinition(
                        name="work:decision", description="An explicit decision about the subject.",
                        subject_types=("project",), object_kind="string",
                        record_projection=RecordProjection(encoding="direct-subject-decision/1"),
                    ),
                ),
            ),
        ),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    print(run(parser.parse_args().database).model_dump_json(indent=2))
