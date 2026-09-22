"""Supply, index and search one document in a fresh canonical store.

Uses real pinned local providers, not keyword fallback. Prepare approved model
caches first; HF_HUB_OFFLINE=1 prevents downloads. This is not a quality benchmark.
"""

import argparse
from pathlib import Path
from uuid import uuid4

from kg.evidence import EvidenceAdministration, EvidenceDatabase, EvidenceService
from kg.indexing import EvidenceSearchService, IndexService
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
    ExternalDocument,
    PutDocument,
    Scope,
    SourceMetadata,
    SuppliedContent,
    WriteRequest,
)
from kg.models.indexing import CanonicalSearchResult


def run(path: Path, text: str, query: str) -> CanonicalSearchResult:
    database = EvidenceDatabase(path)
    database.initialize()
    identity = LocalIdentity(principal_id="me")
    policy = LocalPolicy(
        corpus_id="search-demo",
        bindings=(WriterBinding(
            namespace="notes", owner_id="me", writer_id="example", synchronization_scope="local",
        ),),
        grants=(
            PolicyGrant(principal_id="me", namespace="notes", grant="read"),
            PolicyGrant(
                principal_id="me", namespace="notes", grant="write_documents",
                owner_id="me", writer_id="example", synchronization_scope="local",
            ),
        ),
    )
    registered = EvidenceAdministration(
        database, LocalAdminAuthority(principal_id="trusted-local-app"),
    ).register(CorpusRegistration(corpus_id="search-demo", namespaces=("notes",), policy=policy))
    scope = Scope(corpus_id="search-demo", access=AccessContext(
        principal_id="me", policy_version=registered.policy_version,
        namespaces=("notes",), grants=("read", "write_documents"),
    ))
    attribution = Attribution(
        owner_id="me", writer_id="example", producer="example", producer_version="1",
    )
    evidence = EvidenceService(database, identity)
    written = evidence.write(WriteRequest(
        contract_version="foundation/1", request_id=str(uuid4()), retry_key="search-example/1",
        scope=scope, attribution=attribution,
        payload=PutDocument(
            operation="put_document",
            document=ExternalDocument(
                source_namespace="notes", synchronization_scope="local", external_id="supplied",
            ),
            precondition=CreateOnly(kind="create"),
            content=SuppliedContent(text=text, passage_policy="codepoint-window/1"),
            metadata=SourceMetadata(title="Supplied search example", location="example://supplied"),
        ),
    ))
    if not isinstance(written.receipt, DocumentReceipt):
        raise RuntimeError(written.model_dump_json())
    saved = written.receipt
    indexed = IndexService(database, identity).process(
        scope, attribution, saved.document_id, saved.processing.state_version,
    )
    if indexed.outcome not in ("ready", "unchanged"):
        raise RuntimeError(indexed.model_dump_json())
    result = EvidenceSearchService(database, identity).search(scope, query)
    for hit in result.hits:
        assert evidence.citation(scope, hit.evidence.citation) == hit.evidence
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--text", default="The release is supported by the supplied evidence.")
    parser.add_argument("--query", default="release evidence")
    args = parser.parse_args()
    print(run(args.database, args.text, args.query).model_dump_json(indent=2))
