"""Supply exact content without a parser, source-file reader or model provider.

Trusted bootstrap is outside scoped reporting; the ordinary evidence call below
retains an authorized summary.
"""

from __future__ import annotations

import argparse
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
from kg.models.execution import ExecutionReport, ExplainOptions
from kg.models.foundation import (
    AccessContext,
    Attribution,
    CreateOnly,
    DocumentReceipt,
    ExternalDocument,
    PutDocument,
    Scope,
    SourceMetadata,
    SuppliedAnchor,
    SuppliedContent,
    WriteRequest,
)


def run(path: Path) -> str:
    database = EvidenceDatabase(path)
    database.initialize()
    policy = LocalPolicy(
        corpus_id="demo",
        bindings=(
            WriterBinding(
                namespace="notes",
                owner_id="me",
                writer_id="example",
                synchronization_scope="local",
            ),
        ),
        grants=(
            PolicyGrant(principal_id="me", namespace="notes", grant="read"),
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
    registered = EvidenceAdministration(
        database,
        LocalAdminAuthority(principal_id="trusted-local-app"),
    ).register(CorpusRegistration(corpus_id="demo", namespaces=("notes",), policy=policy))
    scope = Scope(
        corpus_id="demo",
        access=AccessContext(
            principal_id="me",
            policy_version=registered.policy_version,
            namespaces=("notes",),
            grants=("read", "write_documents"),
        ),
    )
    service = EvidenceService(database, LocalIdentity(principal_id="me"))
    text = "A\r\nCafe\u0301 \U0001f680"
    outcome = service.write(
        WriteRequest(
            contract_version="foundation/1",
            request_id=str(uuid4()),
            retry_key="example-v1",
            scope=scope,
            attribution=Attribution(
                owner_id="me",
                writer_id="example",
                producer="example",
                producer_version="1",
            ),
            payload=PutDocument(
                operation="put_document",
                document=ExternalDocument(
                    source_namespace="notes",
                    synchronization_scope="local",
                    external_id="note-1",
                ),
                precondition=CreateOnly(kind="create"),
                content=SuppliedContent(
                    text=text,
                    anchors=(SuppliedAnchor(local_id="body", start=3, end=10, quote=text[3:10]),),
                    passage_policy="not-processed/1",
                ),
                metadata=SourceMetadata(title="Exact supplied text", location="example://note-1"),
            ),
        )
    )
    if not isinstance(outcome.receipt, DocumentReceipt):
        raise RuntimeError(outcome.model_dump_json())
    saved = outcome.receipt
    passages = service.passages(scope, saved.document_id, saved.processing.state_version)
    assert passages.status == "not_processed"  # Intake does not run the private passage kernel.
    headers = service.diagnostics.for_request(scope, outcome.request_id)
    if headers.entries:
        report = service.diagnostics.report(scope, headers.entries[0].report_id)
        if isinstance(report, ExecutionReport):
            assert report.observation_kind == "captured_execution"
    citation = (
        service.anchors(
            scope,
            saved.document_id,
            saved.processing.state_version,
        )
        .entries[0]
        .citation
    )
    explained = service.citation_explained(
        scope, citation, ExplainOptions(detail="detailed", include_quotes=False),
    )
    return explained.outcome.model_dump_json()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    print(run(parser.parse_args().database))
