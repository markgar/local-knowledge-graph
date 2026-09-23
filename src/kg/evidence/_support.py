"""Current evidence proofs for trusted K1 code inside its canonical write unit."""

from __future__ import annotations

from dataclasses import dataclass

from kg.evidence import _store
from kg.evidence._authorization import authorize
from kg.evidence._reads import evidence_view, scoped_document
from kg.evidence._transactions import CanonicalWriteContext
from kg.evidence._values import validated
from kg.evidence.errors import EvidenceServiceError
from kg.models.foundation import DocumentDependency, EvidenceRef, Scope


@dataclass(frozen=True)
class ValidatedSupport:
    reference: EvidenceRef
    state_version: str
    metadata_snapshot_id: str
    start: int
    end: int
    quote: str
    quote_hash: str


class TransactionEvidence:
    def __init__(self, context: CanonicalWriteContext) -> None:
        self.context = context

    def validate_current(
        self,
        scope: Scope,
        dependencies: tuple[DocumentDependency, ...],
        references: tuple[EvidenceRef, ...],
    ) -> tuple[ValidatedSupport, ...]:
        self.context.check_active()
        scope = validated(Scope, scope)
        if (
            type(dependencies) is not tuple
            or type(references) is not tuple
            or len(dependencies) > 200
            or len(references) > 200
        ):
            raise EvidenceServiceError("invalid_request")
        dependencies = tuple(validated(DocumentDependency, item) for item in dependencies)
        references = tuple(validated(EvidenceRef, item) for item in references)
        by_document = {item.document_id: item for item in dependencies}
        if len(by_document) != len(dependencies) or len(set(references)) != len(references):
            raise EvidenceServiceError("invalid_request")
        connection = self.context.connection
        authorize(connection, self.context.identity, scope, "write_knowledge")
        for dependency in dependencies:
            doc = scoped_document(connection, scope, dependency.document_id)
            if doc["namespace"] != dependency.source_namespace:
                raise EvidenceServiceError("not_found")
            current = _store.head(connection, dependency.document_id)
            namespace = connection.execute(
                "SELECT policy_token FROM source_namespace WHERE corpus_id=? AND namespace=?",
                (scope.corpus_id, dependency.source_namespace),
            ).fetchone()
            if (
                current["source"] != "active"
                or current["revision_id"] != dependency.revision_id
                or current["state_version"] != dependency.state_version
                or namespace is None
                or current["namespace_token"] != namespace[0]
            ):
                raise EvidenceServiceError("state_conflict")
        results = []
        for reference in references:
            linked = by_document.get(reference.document_id)
            if (
                linked is None
                or linked.source_namespace != reference.source_namespace
                or linked.revision_id != reference.revision_id
            ):
                raise EvidenceServiceError("invalid_request")
            evidence = evidence_view(connection, scope, reference, linked.state_version)
            if not evidence.is_current_support:
                raise EvidenceServiceError("state_conflict")
            results.append(
                ValidatedSupport(
                    reference=reference,
                    state_version=linked.state_version,
                    metadata_snapshot_id=evidence.metadata.metadata_snapshot_id,
                    start=evidence.start,
                    end=evidence.end,
                    quote=evidence.quote,
                    quote_hash=evidence.quote_hash,
                )
            )
        return tuple(results)
