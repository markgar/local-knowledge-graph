"""Synthetic supplied note -> explicit K1 facts -> typed graph query -> exact quotes.

Requires the optional supported Ladybug runtime. Native failures can terminate
the host; the 256 MiB buffer is not a hard process-memory or crash boundary.
"""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from kg.evidence import EvidenceAdministration, EvidenceDatabase, EvidenceService
from kg.graph import LocalGraphSession
from kg.knowledge import KnowledgeAdministration
from kg.models.evidence import (
    CorpusRegistration,
    KnowledgeWriterBinding,
    LocalAdminAuthority,
    LocalIdentity,
    LocalPolicy,
    PolicyGrant,
    StoredCitation,
    WriterBinding,
)
from kg.models.foundation import (
    AccessContext,
    AddAssertion,
    Attribution,
    ChangeSet,
    ChangeSetReceipt,
    CreateEntity,
    CreateOnly,
    DocumentDependency,
    DocumentReceipt,
    EntityObject,
    ExternalDocument,
    LocalEntity,
    PutDocument,
    Scope,
    SourceMetadata,
    SourceSupport,
    SuppliedAnchor,
    SuppliedContent,
    WriteRequest,
)
from kg.models.graph import GraphEntitySelector, GraphTraversalRequest
from kg.models.knowledge import KnowledgeSchema, PredicateDefinition


def run(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=False)
    database = EvidenceDatabase(output / "canonical.sqlite")
    database.initialize()
    identity = LocalIdentity(principal_id="me")
    admin = LocalAdminAuthority(principal_id="local-app")
    attribution = Attribution(
        owner_id="me", writer_id="example", producer="supplied-facts", producer_version="1",
    )
    policy = LocalPolicy(
        corpus_id="ownership",
        grants=(
            PolicyGrant(principal_id="me", namespace="notes", grant="read"),
            PolicyGrant(principal_id="me", namespace="notes", grant="write_knowledge"),
            PolicyGrant(
                principal_id="me", namespace="notes", grant="write_documents",
                owner_id="me", writer_id="example", synchronization_scope="local",
            ),
        ),
        bindings=(WriterBinding(
            namespace="notes", owner_id="me", writer_id="example", synchronization_scope="local",
        ),),
        knowledge_bindings=(KnowledgeWriterBinding(
            namespace="notes", principal_id="me", owner_id="me", writer_id="example",
        ),),
    )
    registration = EvidenceAdministration(database, admin).register(CorpusRegistration(
        corpus_id="ownership", namespaces=("notes",), policy=policy,
    ))
    KnowledgeAdministration(database, admin).register_knowledge_schema(KnowledgeSchema(
        corpus_id="ownership", schema_version="ownership/1", entity_types=("person", "project"),
        predicates=(PredicateDefinition(
            name="work:owns", subject_types=("person",),
            object_kind="entity", object_types=("project",),
        ),),
    ))
    scope = Scope(corpus_id="ownership", access=AccessContext(
        principal_id="me", policy_version=registration.policy_version, namespaces=("notes",),
        grants=("read", "write_documents", "write_knowledge"),
    ))
    evidence = EvidenceService(database, identity)
    text = "Review note\r\nAlice owns Project Atlas. Cafe\u0301 \U0001f680."
    document = evidence.write(WriteRequest(
        contract_version="foundation/1", request_id=str(uuid4()), retry_key="note",
        scope=scope, attribution=attribution,
        payload=PutDocument(
            operation="put_document", precondition=CreateOnly(kind="create"),
            document=ExternalDocument(
                source_namespace="notes", external_id="review", synchronization_scope="local",
            ),
            content=SuppliedContent(
                text=text, passage_policy="supplied-anchors/1",
                anchors=(SuppliedAnchor(local_id="note", start=0, end=len(text), quote=text),),
            ),
            metadata=SourceMetadata(title="Supplied review", location="example://review"),
        ),
    ))
    if not isinstance(document.receipt, DocumentReceipt):
        raise RuntimeError(document.model_dump_json())
    saved = document.receipt
    reference = evidence.anchors(
        scope, saved.document_id, saved.processing.state_version,
    ).entries[0].reference
    support = SourceSupport(kind="source", evidence=(reference,))
    enriched = evidence.write(WriteRequest(
        contract_version="foundation/1", request_id=str(uuid4()), retry_key="facts",
        scope=scope, attribution=attribution,
        payload=ChangeSet(
            operation="enrich", dependencies=(DocumentDependency(
                source_namespace="notes", document_id=saved.document_id,
                revision_id=saved.revision_id, state_version=saved.processing.state_version,
            ),),
            changes=(
                CreateEntity(kind="entity", local_id="person", name="Alice",
                             entity_type="person", support=support),
                CreateEntity(kind="entity", local_id="project", name="Project Atlas",
                             entity_type="project", support=support),
                AddAssertion(
                    kind="assertion", local_id="ownership", predicate="work:owns",
                    subject=LocalEntity(kind="local", local_id="person"),
                    object=EntityObject(
                        kind="entity", entity=LocalEntity(kind="local", local_id="project"),
                    ),
                    interpretation="explicit", support=support,
                ),
            ),
        ),
    ))
    if not isinstance(enriched.receipt, ChangeSetReceipt):
        raise RuntimeError(enriched.model_dump_json())
    identifiers = {item.local_id: item.stored_id for item in enriched.receipt.mappings}
    with LocalGraphSession(
        database, identity, scope, graph_directory=output / "derived",
    ) as session:
        capabilities = session.capabilities()
        result = session.traverse(GraphTraversalRequest(
            request_id="ownership-query", scope=scope,
            start=GraphEntitySelector(entity_id=identifiers["person"]),
            predicate="work:owns", direction="outgoing",
        ))
        quotes = []
        for proof in result.paths:
            for item in proof.assertion.support:
                capture = item.captured
                # Separately authorized historical hydration, not a renewed graph observation.
                view = evidence.citation(scope, StoredCitation(
                    reference=capture.reference, metadata_snapshot_id=capture.metadata_snapshot_id,
                    state_version=capture.dependency.state_version,
                ))
                quotes.append(view.model_dump(mode="json"))
        return {
            "capabilities": capabilities.model_dump(mode="json"),
            "result": result.model_dump(mode="json"), "historical_citations": quotes,
            "expected_project": identifiers["project"],
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    print(json.dumps(run(parser.parse_args().output), ensure_ascii=False, indent=2))
