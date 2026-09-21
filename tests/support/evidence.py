from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from kg.evidence import EvidenceAdministration, EvidenceDatabase, EvidenceService
from kg.models.evidence import (
    CorpusRegistration,
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
    ExpectedState,
    ExternalDocument,
    PutDocument,
    Scope,
    SourceMetadata,
    SuppliedAnchor,
    SuppliedContent,
    WriteOutcome,
    WriteRequest,
)


@dataclass
class Environment:
    database: EvidenceDatabase
    admin: EvidenceAdministration
    service: EvidenceService
    scope: Scope
    policy: LocalPolicy
    registration: CorpusRegistration


def environment(path: Path, *, corpus: str = "work") -> Environment:
    database = EvidenceDatabase(path)
    database.initialize()
    admin = EvidenceAdministration(database, LocalAdminAuthority(principal_id="admin"))
    bindings = tuple(
        WriterBinding(
            namespace=namespace,
            owner_id="owner",
            writer_id="writer",
            synchronization_scope="all",
        )
        for namespace in ("markdown", "email")
    )
    grants = tuple(
        grant
        for namespace in ("markdown", "email")
        for grant in (
            PolicyGrant(principal_id="principal", namespace=namespace, grant="read"),
            PolicyGrant(
                principal_id="principal",
                namespace=namespace,
                grant="write_documents",
                owner_id="owner",
                writer_id="writer",
                synchronization_scope="all",
            ),
        )
    )
    policy = LocalPolicy(corpus_id=corpus, bindings=bindings, grants=grants)
    registration = CorpusRegistration(
        corpus_id=corpus,
        namespaces=("markdown", "email"),
        policy=policy,
    )
    registered = admin.register(registration)
    scope = Scope(
        corpus_id=corpus,
        access=AccessContext(
            principal_id="principal",
            policy_version=registered.policy_version,
            namespaces=("markdown", "email"),
            grants=("read", "write_documents"),
        ),
    )
    service = EvidenceService(database, LocalIdentity(principal_id="principal"))
    return Environment(database, admin, service, scope, policy, registration)


def put(
    scope: Scope,
    *,
    external: str = "doc",
    namespace: str = "markdown",
    text: str = "A\r\nCafe\u0301 \U0001f680",
    state: str | None = None,
    title: str = "Title",
    retry: str | None = None,
) -> WriteRequest:
    return WriteRequest(
        contract_version="foundation/1",
        request_id=str(uuid4()),
        retry_key=retry or str(uuid4()),
        scope=scope,
        attribution=Attribution(
            owner_id="owner",
            writer_id="writer",
            producer="synthetic",
            producer_version="1",
        ),
        payload=PutDocument(
            operation="put_document",
            document=ExternalDocument(
                source_namespace=namespace,
                synchronization_scope="all",
                external_id=external,
            ),
            precondition=ExpectedState(kind="match", state_version=state)
            if state
            else CreateOnly(
                kind="create",
            ),
            content=SuppliedContent(
                text=text,
                anchors=(
                    SuppliedAnchor(
                        local_id="whole",
                        start=0,
                        end=len(text),
                        quote=text,
                    ),
                )
                if text
                else (),
                passage_policy="future/1",
            ),
            metadata=SourceMetadata(title=title, location="source://doc"),
        ),
    )


def receipt(outcome: WriteOutcome) -> DocumentReceipt:
    assert outcome.error is None, outcome.error
    assert isinstance(outcome.receipt, DocumentReceipt)
    return outcome.receipt
