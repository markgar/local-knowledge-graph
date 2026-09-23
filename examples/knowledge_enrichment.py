"""Supply a real source, atomically submit a decision, and read its owned provenance."""

import argparse
from pathlib import Path
from uuid import uuid4

from kg.evidence import EvidenceAdministration, EvidenceDatabase, EvidenceService
from kg.indexing._passages import produce
from kg.knowledge import KnowledgeAdministration, KnowledgeService
from kg.models.evidence import (
    CorpusRegistration,
    KnowledgeWriterBinding,
    LocalAdminAuthority,
    LocalIdentity,
    LocalPolicy,
    PolicyGrant,
    WriterBinding,
)
from kg.models.foundation import (
    AccessContext,
    AddAssertion,
    AddMention,
    Attribution,
    ChangeSet,
    ChangeSetReceipt,
    CreateEntity,
    CreateOnly,
    DocumentDependency,
    DocumentReceipt,
    ExternalDocument,
    LocalEntity,
    PutDocument,
    Scope,
    SourceMetadata,
    SourceSupport,
    StringObject,
    SuppliedAnchor,
    SuppliedContent,
    WriteRequest,
)
from kg.models.knowledge import KnowledgeSchema, PredicateDefinition, RecordProjection


def supply(path: Path, *, passages: bool = False) -> tuple[
    EvidenceService, Scope, Attribution, str,
]:
    database = EvidenceDatabase(path)
    database.initialize()
    authority = LocalAdminAuthority(principal_id="trusted-local-app")
    policy = LocalPolicy(
        corpus_id="knowledge-demo",
        bindings=(
            WriterBinding(
                namespace="notes",
                owner_id="me",
                writer_id="example",
                synchronization_scope="local",
            ),
        ),
        knowledge_bindings=(
            KnowledgeWriterBinding(
                namespace="notes",
                principal_id="me",
                owner_id="me",
                writer_id="example",
            ),
        ),
        grants=(
            PolicyGrant(principal_id="me", namespace="notes", grant="read"),
            PolicyGrant(principal_id="me", namespace="notes", grant="write_knowledge"),
            PolicyGrant(
                principal_id="me",
                namespace="notes",
                grant="write_documents",
                owner_id="me",
                writer_id="example",
                synchronization_scope="local",
            ),
        ),
    )
    registration = EvidenceAdministration(database, authority).register(
        CorpusRegistration(corpus_id="knowledge-demo", namespaces=("notes",), policy=policy),
    )
    KnowledgeAdministration(database, authority).register_knowledge_schema(
        KnowledgeSchema(
            corpus_id="knowledge-demo",
            schema_version="example/1",
            entity_types=("project",),
            predicates=(
                PredicateDefinition(
                    name="work:decision",
                    subject_types=("project",),
                    object_kind="string",
                    record_projection=RecordProjection(encoding="direct-subject-decision/1"),
                ),
            ),
        )
    )
    scope = Scope(
        corpus_id="knowledge-demo",
        access=AccessContext(
            principal_id="me",
            policy_version=registration.policy_version,
            namespaces=("notes",),
            grants=("read", "write_documents", "write_knowledge"),
        ),
    )
    identity = LocalIdentity(principal_id="me")
    evidence = EvidenceService(database, identity)
    attribution = Attribution(
        owner_id="me",
        writer_id="example",
        producer="example",
        producer_version="1",
    )
    text = "Project Atlas: ship the reviewed release."
    written = evidence.write(
        WriteRequest(
            contract_version="foundation/1",
            request_id=str(uuid4()),
            retry_key="source/1",
            scope=scope,
            attribution=attribution,
            payload=PutDocument(
                operation="put_document",
                document=ExternalDocument(
                    source_namespace="notes",
                    synchronization_scope="local",
                    external_id="decision",
                ),
                precondition=CreateOnly(kind="create"),
                content=SuppliedContent(
                    text=text,
                    anchors=(
                        SuppliedAnchor(
                            local_id="whole",
                            start=0,
                            end=len(text),
                            quote=text,
                        ),
                    ),
                    passage_policy="codepoint-window/1",
                ),
                metadata=SourceMetadata(title="Review decision", location="example://decision"),
            ),
        )
    )
    if not isinstance(written.receipt, DocumentReceipt):
        raise RuntimeError(written.model_dump_json())
    doc = written.receipt
    if passages:
        # Trusted integration with the private E3 producer; no model/vector work.
        produce(
            database, identity, scope, attribution, doc.document_id, doc.processing.state_version
        )
        reference = evidence.passages(scope, doc.document_id, doc.processing.state_version).entries[
            0
        ]
    else:
        reference = evidence.anchors(scope, doc.document_id, doc.processing.state_version).entries[
            0
        ]
    support = SourceSupport(kind="source", evidence=(reference.reference,))
    enriched = evidence.write(
        WriteRequest(
            contract_version="foundation/1",
            request_id=str(uuid4()),
            retry_key="knowledge/passages/1" if passages else "knowledge/1",
            scope=scope,
            attribution=attribution,
            payload=ChangeSet(
                operation="enrich",
                dependencies=(
                    DocumentDependency(
                        source_namespace="notes",
                        document_id=doc.document_id,
                        revision_id=doc.revision_id,
                        state_version=doc.processing.state_version,
                    ),
                ),
                changes=(
                    CreateEntity(
                        kind="entity",
                        local_id="project",
                        name="Atlas",
                        entity_type="project",
                        support=support,
                    ),
                    AddAssertion(
                        kind="assertion",
                        local_id="decision",
                        subject=LocalEntity(kind="local", local_id="project"),
                        predicate="work:decision",
                        object=StringObject(
                            kind="string",
                            value="Ship the reviewed release.",
                        ),
                        interpretation="explicit",
                        support=support,
                    ),
                )
                + (
                    (
                        AddMention(
                            kind="mention",
                            local_id="mention",
                            entity=LocalEntity(kind="local", local_id="project"),
                            support=support,
                        ),
                    )
                    if passages
                    else ()
                ),
            ),
        )
    )
    if not isinstance(enriched.receipt, ChangeSetReceipt):
        raise RuntimeError(enriched.model_dump_json())
    record_id = next(m.stored_id for m in enriched.receipt.mappings if m.local_id == "decision")
    return evidence, scope, attribution, record_id


def run(path: Path, *, passages: bool = False) -> str:
    evidence, scope, _, record_id = supply(path, passages=passages)
    return KnowledgeService(evidence.database, evidence.identity).contribution(
        scope, record_id,
    ).model_dump_json()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--passages", action="store_true", help="Produce real passages without vectors"
    )
    args = parser.parse_args()
    print(run(args.database, passages=args.passages))
