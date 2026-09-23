"""Supply a document, index it, and execute ranked Q1 search with exact citations.

This executable demonstration deliberately uses controlled deterministic providers
at private factory seams, not real models or a relevance/quality benchmark.
No downloads, inference service, language planning or knowledge extraction.
"""

import argparse
import json
from pathlib import Path
from uuid import uuid4

from kg.evidence import EvidenceAdministration, EvidenceDatabase, EvidenceService
from kg.indexing import IndexService
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
    EvidenceStep,
    ExternalDocument,
    PutDocument,
    QueryBudget,
    QueryRequest,
    RankedResult,
    Scope,
    SearchStep,
    SourceMetadata,
    SuppliedContent,
    WriteRequest,
)
from kg.query import QueryService, _worker
from kg.retrieval.dense import EMBEDDING_PROFILES, ENCODING_PIPELINE_VERSION, EmbeddingProfile
from kg.retrieval.rerank import MODEL_NAME, MODEL_REVISION, SCORING_PIPELINE_VERSION

_RUN_WORKER = _worker.run


class ControlledEmbedding:
    def __init__(self, profile=EmbeddingProfile.gte_modernbert):
        config = EMBEDDING_PROFILES[profile]
        self.profile = profile
        self.name, self.revision, self.license = (
            config.model_name, config.model_revision, config.model_license,
        )
        self.dimensions = config.dimensions
        self.normalization = "l2"
        self.context_behavior = config.context_behavior
        self.query_encoding = config.query_encoding
        self.document_encoding = config.document_encoding
        self.pipeline_version = ENCODING_PIPELINE_VERSION + "|controlled-query-example"

    def encode_query(self, text):
        return [float(len(text) + 1), 1.0] + [0.0] * (self.dimensions - 2)

    def encode_documents(self, texts, *, batch_size):
        return [self.encode_query(text) for text in texts]


class ControlledReranker:
    name, revision = MODEL_NAME, MODEL_REVISION
    license = "Apache-2.0"
    pipeline_version = SCORING_PIPELINE_VERSION + "|controlled-query-example"

    def score(self, query, passages, *, batch_size):
        return [float(sum(text.casefold().count(word) for word in query.casefold().split()))
                for text in passages]


def controlled_worker(*args):
    # Spawn reconstructs trusted providers; no live service/model/context is pickled.
    from kg.indexing import EvidenceSearchService

    original = EvidenceSearchService.__init__

    def initialize(service, database, identity):
        original(service, database, identity)
        service._provider_factory = ControlledEmbedding
        service._reranker_factory = ControlledReranker

    EvidenceSearchService.__init__ = initialize
    _RUN_WORKER(*args)


def run(path: Path, text: str, query: str) -> dict:
    database = EvidenceDatabase(path)
    database.initialize()
    identity = LocalIdentity(principal_id="me")
    policy = LocalPolicy(
        corpus_id="query-search-demo",
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
    ).register(CorpusRegistration(
        corpus_id=policy.corpus_id, namespaces=("notes",), policy=policy,
    ))
    scope = Scope(corpus_id=policy.corpus_id, access=AccessContext(
        principal_id="me", policy_version=registered.policy_version,
        namespaces=("notes",), grants=("read", "write_documents"),
    ))
    attribution = Attribution(
        owner_id="me", writer_id="example", producer="controlled-example", producer_version="1",
    )
    evidence = EvidenceService(database, identity)
    written = evidence.write(WriteRequest(
        contract_version="foundation/1", request_id=str(uuid4()),
        retry_key="query-search-example/1",
        scope=scope, attribution=attribution,
        payload=PutDocument(
            operation="put_document",
            document=ExternalDocument(
                source_namespace="notes", synchronization_scope="local", external_id="supplied",
            ),
            precondition=CreateOnly(kind="create"),
            content=SuppliedContent(text=text, passage_policy="codepoint-window/1"),
            metadata=SourceMetadata(title="Supplied Q1 example", location="example://supplied"),
        ),
    ))
    if not isinstance(written.receipt, DocumentReceipt):
        raise RuntimeError(written.model_dump_json())
    saved = written.receipt
    index = IndexService(database, identity)
    index._provider_factory = ControlledEmbedding
    indexed = index.process(
        scope, attribution, saved.document_id, saved.processing.state_version,
    )
    if indexed.outcome not in ("ready", "unchanged"):
        raise RuntimeError(indexed.model_dump_json())
    original = _worker.run
    _worker.run = controlled_worker
    try:
        with QueryService(database, identity) as service:
            execution = service.execute(QueryRequest(
                contract_version="foundation/1", request_id="search", scope=scope,
                steps=(SearchStep(operation="search", step_id="ranked", text=query),),
                output_step="ranked", budget=QueryBudget(max_milliseconds=30000),
            ))
            if not isinstance(execution.result.data, RankedResult):
                raise RuntimeError(execution.model_dump_json())
            exact = []
            for ordinal, hit in enumerate(execution.result.data.hits):
                view = evidence.evidence(scope, hit.evidence)
                assert text[view.start:view.end] == view.quote
                assert evidence.citation(scope, view.citation) == view
                inspected = service.execute(QueryRequest(
                    contract_version="foundation/1", request_id=f"evidence-{ordinal}", scope=scope,
                    steps=(EvidenceStep(operation="evidence", step_id="e", evidence=hit.evidence),),
                    output_step="e", budget=QueryBudget(max_milliseconds=30000),
                ))
                if inspected.result.error is not None:
                    raise RuntimeError(inspected.model_dump_json())
                exact.append({"score": hit.score, "evidence": view.model_dump(mode="json")})
            return {
                "providers": "controlled-query-example",
                "quality_evidence": False,
                "separate_evidence_requests_are_atomic_with_search": False,
                "execution": execution.model_dump(mode="json"), "exact_hits": exact,
            }
    finally:
        _worker.run = original


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--text-file", type=Path)
    parser.add_argument("--query", default="release")
    args = parser.parse_args()
    supplied = args.text_file.read_bytes().decode("utf-8") if args.text_file else (
        "The release is supported by exact supplied evidence.\r\nCafe\u0301 \U0001f680"
    )
    print(json.dumps(run(args.database, supplied, args.query), ensure_ascii=False, indent=2))
