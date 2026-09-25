"""Supply a real source, atomically submit a decision, and read its owned provenance."""

import argparse
from pathlib import Path
from uuid import uuid4

from kg.evidence import EvidenceAdministration, EvidenceDatabase, EvidenceService
from kg.indexing._passages import produce
from kg.knowledge import KnowledgeAdministration, KnowledgeService
from kg.models.authoring import (
    AuthoredAssertion,
    AuthoredClassification,
    AuthoredEntity,
    AuthoredMention,
    NewIdentity,
    RecordAuthoringDocument,
    RecordAuthoringRequest,
    SelectLocalClaim,
    SourceCapture,
)
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
    Attribution,
    CreateOnly,
    DocumentReceipt,
    ExternalDocument,
    PutDocument,
    Scope,
    SourceMetadata,
    StringObject,
    SuppliedAnchor,
    SuppliedContent,
    WriteRequest,
)
from kg.models.knowledge import RecordProjection
from kg.models.schema import (
    EntityTypeDefinition,
    SchemaDefinition,
    SchemaPredicateDefinition,
    SchemaPresetRequest,
)


def supply(
    path: Path, *, passages: bool = False
) -> tuple[
    EvidenceService,
    Scope,
    Attribution,
    str,
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
    schema_registration = KnowledgeAdministration(database, authority).register_knowledge_schema(
        SchemaPresetRequest(
            corpus_id="knowledge-demo",
            preset_name="example/1",
            preset_rationale="Explicit example vocabulary; no inferred facts.",
            definition=SchemaDefinition(
                entity_types=(
                    EntityTypeDefinition(
                        name="project",
                        description="An explicitly identified project.",
                    ),
                ),
                predicates=(
                    SchemaPredicateDefinition(
                        name="work:decision",
                        description="An explicitly submitted decision about the subject.",
                        subject_types=("project",),
                        object_kind="string",
                        record_projection=RecordProjection(encoding="direct-subject-decision/1"),
                    ),
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
    enriched = KnowledgeService(database, identity).record(
        RecordAuthoringRequest(
            interface_version="record-authoring/1",
            request_id=str(uuid4()),
            retry_key="knowledge/passages/1" if passages else "knowledge/1",
            scope=scope,
            attribution=attribution,
            document=RecordAuthoringDocument(
                interface_version="record-authoring/1",
                expected_schema_revision=schema_registration.revision,
                support={
                    "source": SourceCapture(
                        kind="source",
                        reference=reference.reference,
                        state_version=doc.processing.state_version,
                    )
                },
                entities=(
                    AuthoredEntity(
                        local_id="project",
                        identity=NewIdentity(
                            kind="new",
                            name="Atlas",
                            support=("source",),
                        ),
                        classifications=(
                            AuthoredClassification(
                                local_id="project-type",
                                entity_type="project",
                                interpretation="explicit",
                                support=("source",),
                                select=SelectLocalClaim(
                                    rationale="The source explicitly identifies a project."
                                ),
                            ),
                        ),
                        mentions=(AuthoredMention(local_id="mention", support=("source",)),)
                        if passages
                        else (),
                    ),
                ),
                assertions=(
                    AuthoredAssertion(
                        local_id="decision",
                        subject="project",
                        predicate="work:decision",
                        object=StringObject(
                            kind="string",
                            value="Ship the reviewed release.",
                        ),
                        interpretation="explicit",
                        support=("source",),
                    ),
                ),
            ),
        )
    )
    if enriched.receipt is None:
        raise RuntimeError(enriched.model_dump_json())
    record_id = enriched.receipt.assertions[0].stored_id
    return evidence, scope, attribution, record_id


def run(path: Path, *, passages: bool = False) -> str:
    evidence, scope, _, record_id = supply(path, passages=passages)
    return (
        KnowledgeService(evidence.database, evidence.identity)
        .contribution(
            scope,
            record_id,
        )
        .model_dump_json()
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument(
        "--passages", action="store_true", help="Produce real passages without vectors"
    )
    args = parser.parse_args()
    print(run(args.database, passages=args.passages))
