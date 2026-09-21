"""Content-free lifecycle bookkeeping in the evidence owner's existing transaction."""

from __future__ import annotations

import sqlite3

from kg.evidence.errors import EvidenceServiceError


def state_created(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    revision_id: str,
    state_version: str,
    change_kind: str,
    committed_at: str,
) -> None:
    if not connection.in_transaction:
        raise RuntimeError("State bookkeeping requires the canonical write transaction")
    document = connection.execute(
        "SELECT corpus_id,namespace,owner_id,synchronization_scope "
        "FROM document WHERE document_id=?",
        (document_id,),
    ).fetchone()
    if document is None:
        raise EvidenceServiceError("internal_error")
    connection.execute(
        "INSERT INTO sync_scope(corpus_id,namespace,owner_id,synchronization_scope,"
        "generation,mutation_epoch) VALUES (?,?,?,?,0,1) "
        "ON CONFLICT(corpus_id,namespace,owner_id,synchronization_scope) "
        "DO UPDATE SET mutation_epoch=mutation_epoch+1",
        tuple(document),
    )
    connection.execute(
        "INSERT INTO state_intent(corpus_id,namespace,document_id,revision_id,state_version,"
        "change_kind,created_at) VALUES (?,?,?,?,?,?,?)",
        (
            document[0],
            document[1],
            document_id,
            revision_id,
            state_version,
            change_kind,
            committed_at,
        ),
    )
