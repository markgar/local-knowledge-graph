"""Exact current authority and retained document identity, not capability tokens."""

import sqlite3

from kg.evidence._authorization import authorize
from kg.evidence.errors import EvidenceServiceError
from kg.models.evidence import LocalIdentity
from kg.models.foundation import Scope
from kg.models.processing import DocumentTarget, WorkerSelection


def selection(
    connection: sqlite3.Connection,
    identity: LocalIdentity,
    scope: Scope,
    worker: WorkerSelection,
    *,
    enabled: bool = False,
) -> sqlite3.Row:
    authorize(connection, identity, scope, "read", namespace=worker.namespace)
    row = connection.execute(
        "SELECT p.* FROM processing_worker_binding w JOIN processing_plan p "
        "USING(corpus_id,plan_id,plan_version) WHERE w.corpus_id=? "
        "AND w.plan_id=? AND w.plan_version=? AND w.namespace=? AND w.principal_id=? "
        "AND w.worker_id=? AND w.owner_id=? AND w.writer_id=?",
        (
            scope.corpus_id,
            worker.plan_id,
            worker.plan_version,
            worker.namespace,
            identity.principal_id,
            worker.worker_id,
            worker.owner_id,
            worker.writer_id,
        ),
    ).fetchone()
    if not isinstance(row, sqlite3.Row) or (enabled and not row["enabled"]):
        raise EvidenceServiceError("forbidden")
    return row


def job(
    connection: sqlite3.Connection,
    scope: Scope,
    worker: WorkerSelection,
    job_id: str,
) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM processing_job WHERE corpus_id=? AND job_id=? "
        "AND namespace=? AND owner_id=? AND writer_id=? AND principal_id=? "
        "AND plan_id=? AND plan_version=? AND target_kind='document'",
        (
            scope.corpus_id,
            job_id,
            worker.namespace,
            worker.owner_id,
            worker.writer_id,
            scope.access.principal_id,
            worker.plan_id,
            worker.plan_version,
        ),
    ).fetchone()
    if not isinstance(row, sqlite3.Row):
        raise EvidenceServiceError("not_found")
    return row


def document(
    connection: sqlite3.Connection,
    scope: Scope,
    worker: WorkerSelection,
    target: DocumentTarget,
    *,
    current: bool,
) -> bool:
    row = connection.execute(
        "SELECT d.current_state,s.source,s.namespace_token,n.policy_token "
        "FROM document d JOIN document_state s USING(document_id) "
        "JOIN source_namespace n ON n.corpus_id=d.corpus_id AND n.namespace=d.namespace "
        "WHERE d.corpus_id=? AND d.namespace=? AND d.owner_id=? AND d.document_id=? "
        "AND s.revision_id=? AND s.state_version=? AND s.namespace_token=?",
        (
            scope.corpus_id,
            worker.namespace,
            worker.owner_id,
            target.document_id,
            target.revision_id,
            target.state_version,
            target.namespace_token,
        ),
    ).fetchone()
    if row is None:
        raise EvidenceServiceError("not_found")
    return not current or (
        row["current_state"] == target.state_version
        and row["source"] == "active"
        and row["policy_token"] == target.namespace_token
    )


def dependency(connection: sqlite3.Connection, row: sqlite3.Row) -> DocumentTarget:
    dep = connection.execute(
        "SELECT * FROM processing_dependency WHERE corpus_id=? AND job_id=? AND document_id=?",
        (row["corpus_id"], row["job_id"], row["document_id"]),
    ).fetchone()
    if dep is None or dep["state_version"] != row["target_state"]:
        raise EvidenceServiceError("internal_error")
    return DocumentTarget(
        document_id=dep["document_id"],
        revision_id=dep["revision_id"],
        state_version=dep["state_version"],
        namespace_token=dep["namespace_token"],
    )
