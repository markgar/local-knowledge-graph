from __future__ import annotations

import sqlite3

from kg.evidence.errors import EvidenceServiceError
from kg.models.evidence import Grant, LocalIdentity
from kg.models.foundation import Attribution, ExternalDocument, Scope


def authorize(
    connection: sqlite3.Connection,
    identity: LocalIdentity,
    scope: Scope,
    required: Grant,
    *,
    namespace: str | None = None,
) -> None:
    corpus = connection.execute(
        "SELECT policy_version FROM corpus WHERE corpus_id=?", (scope.corpus_id,)
    ).fetchone()
    if (
        corpus is None
        or identity.principal_id != scope.access.principal_id
        or corpus["policy_version"] != scope.access.policy_version
        or required not in scope.access.grants
        or (namespace is not None and namespace not in scope.access.namespaces)
    ):
        raise EvidenceServiceError("forbidden")
    for declared_namespace in scope.access.namespaces:
        granted = {
            row[0]
            for row in connection.execute(
                "SELECT grant_name FROM policy_grant "
                "WHERE corpus_id=? AND namespace=? AND principal_id=?",
                (scope.corpus_id, declared_namespace, identity.principal_id),
            )
        }
        if not set(scope.access.grants) <= granted:
            raise EvidenceServiceError("forbidden")


def authorize_writer(
    connection: sqlite3.Connection,
    identity: LocalIdentity,
    scope: Scope,
    attribution: Attribution,
    document: ExternalDocument,
) -> None:
    authorize(connection, identity, scope, "write_documents", namespace=document.source_namespace)
    parameters = (
        scope.corpus_id,
        document.source_namespace,
        attribution.owner_id,
        attribution.writer_id,
        document.synchronization_scope,
    )
    binding = connection.execute(
        "SELECT 1 FROM writer_binding WHERE corpus_id=? AND namespace=? "
        "AND owner_id=? AND writer_id=? AND synchronization_scope=?",
        parameters,
    ).fetchone()
    grant = connection.execute(
        "SELECT 1 FROM policy_grant WHERE corpus_id=? AND namespace=? "
        "AND owner_id=? AND writer_id=? AND synchronization_scope=? "
        "AND principal_id=? AND grant_name='write_documents'",
        (*parameters, identity.principal_id),
    ).fetchone()
    if binding is None or grant is None:
        raise EvidenceServiceError("forbidden")
