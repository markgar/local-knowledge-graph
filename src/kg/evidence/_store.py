from __future__ import annotations

import sqlite3
from datetime import datetime

from kg.evidence._values import sha, timestamp, token
from kg.evidence.errors import EvidenceServiceError
from kg.models.foundation import DocumentReceipt, ProcessingState


def head(connection: sqlite3.Connection, document_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT s.*, m.metadata_json FROM document d "
        "JOIN document_state s ON s.state_version=d.current_state AND s.document_id=d.document_id "
        "JOIN metadata_snapshot m ON m.snapshot_id=s.metadata_snapshot_id WHERE d.document_id=?",
        (document_id,),
    ).fetchone()
    if not isinstance(row, sqlite3.Row):
        raise EvidenceServiceError("internal_error")
    return row


def document(connection: sqlite3.Connection, corpus: str, document_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM document WHERE corpus_id=? AND document_id=?", (corpus, document_id)
    ).fetchone()
    if not isinstance(row, sqlite3.Row):
        raise EvidenceServiceError("not_found")
    return row


def content_bytes(connection: sqlite3.Connection, document_id: str, revision_id: str) -> bytes:
    row = connection.execute(
        "SELECT content, content_hash, byte_length FROM revision "
        "WHERE document_id=? AND revision_id=?",
        (document_id, revision_id),
    ).fetchone()
    if row is None:
        raise EvidenceServiceError("not_found")
    value = row["content"]
    if not isinstance(value, bytes) or sha(value) != row["content_hash"] or len(value) != row[2]:
        raise EvidenceServiceError("internal_error")
    try:
        value.decode("utf-8", errors="strict")
    except UnicodeError:
        raise EvidenceServiceError("internal_error") from None
    return value


def add_state(
    connection: sqlite3.Connection,
    *,
    document_id: str,
    revision_id: str,
    set_id: str,
    metadata: str,
    source: str,
    namespace_token: str,
    passage_policy: str,
    kind: str,
    at: datetime,
    state_version: str | None = None,
    previous: sqlite3.Row | None = None,
) -> str:
    state_version = state_version or token()
    snapshot = token()
    sequence = int(previous["sequence"]) + 1 if previous else 1
    previous_id = previous["state_version"] if previous else None
    connection.execute(
        "INSERT INTO document_state VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            state_version,
            document_id,
            sequence,
            previous_id,
            revision_id,
            set_id,
            snapshot,
            source,
            namespace_token,
            passage_policy,
            kind,
            timestamp(at),
        ),
    )
    connection.execute(
        "INSERT INTO metadata_snapshot VALUES (?,?,?,?,?)",
        (snapshot, document_id, revision_id, state_version, metadata),
    )
    connection.execute(
        "INSERT INTO processing_state VALUES (?, 'pending', 'pending', 'processor_not_available')",
        (state_version,),
    )
    if previous is not None:
        cursor = connection.execute(
            "UPDATE document SET current_state=? WHERE document_id=? AND current_state=?",
            (state_version, document_id, previous_id),
        )
        if cursor.rowcount != 1:
            raise EvidenceServiceError("state_conflict")
    return state_version


def receipt(connection: sqlite3.Connection, document_id: str) -> DocumentReceipt:
    row = head(connection, document_id)
    processing = connection.execute(
        "SELECT * FROM processing_state WHERE state_version=?", (row["state_version"],)
    ).fetchone()
    if processing is None:
        raise EvidenceServiceError("internal_error")
    return DocumentReceipt(
        kind="document",
        document_id=document_id,
        revision_id=row["revision_id"],
        metadata_snapshot_id=row["metadata_snapshot_id"],
        processing=ProcessingState(
            state_version=row["state_version"],
            source=row["source"],
            indexing=processing["indexing"],
            enrichment=processing["enrichment"],
        ),
    )
