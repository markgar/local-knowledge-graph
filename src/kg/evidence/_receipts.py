from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from typing import Literal

from kg.evidence._authorization import authorize_writer
from kg.evidence._values import sha, timestamp, token
from kg.evidence.errors import EvidenceServiceError
from kg.models.evidence import LocalIdentity
from kg.models.foundation import (
    DocumentReceipt,
    ExternalDocument,
    WriteOutcome,
    WriteRequest,
)


def clock(connection: sqlite3.Connection, supplied: datetime) -> datetime:
    value = timestamp(supplied)
    existing = connection.execute(
        "SELECT watermark FROM receipt_clock WHERE singleton=1"
    ).fetchone()
    if existing:
        value = max(value, existing[0])
    connection.execute(
        "INSERT INTO receipt_clock VALUES (1,?) "
        "ON CONFLICT(singleton) DO UPDATE SET watermark=excluded.watermark",
        (value,),
    )
    return datetime.fromisoformat(value)


def replay(
    connection: sqlite3.Connection,
    identity: LocalIdentity,
    request: WriteRequest,
    at: datetime,
    digest: str,
) -> WriteOutcome | None:
    key = connection.execute(
        "SELECT * FROM write_key WHERE corpus_id=? AND writer_id=? AND operation=? AND key_hash=?",
        (
            request.scope.corpus_id,
            request.attribution.writer_id,
            request.payload.operation,
            sha(request.retry_key.encode()),
        ),
    ).fetchone()
    if key is None:
        return None
    response = connection.execute(
        "SELECT * FROM write_response WHERE key_id=?", (key["key_id"],)
    ).fetchone()
    if response:
        target = connection.execute(
            "SELECT * FROM document WHERE document_id=? AND corpus_id=?",
            (response["document_id"], request.scope.corpus_id),
        ).fetchone()
        if target is None:
            raise EvidenceServiceError("internal_error")
        if target["owner_id"] != request.attribution.owner_id:
            raise EvidenceServiceError("forbidden")
        authorize_writer(
            connection,
            identity,
            request.scope,
            request.attribution,
            ExternalDocument(
                source_namespace=target["namespace"],
                synchronization_scope=target["synchronization_scope"],
                external_id=target["external_id"],
            ),
        )
    if key["expired"] or timestamp(at) >= key["expires_at"]:
        connection.execute("DELETE FROM write_response WHERE key_id=?", (key["key_id"],))
        connection.execute("UPDATE write_key SET expired=1 WHERE key_id=?", (key["key_id"],))
        return WriteOutcome(
            request_id=request.request_id,
            status="rejected",
            error=EvidenceServiceError("retry_expired").failure,
        )
    if key["digest"] != digest:
        raise EvidenceServiceError("retry_conflict")
    if response is None:
        raise EvidenceServiceError("internal_error")
    return WriteOutcome(
        request_id=request.request_id,
        status=key["status"],
        receipt=DocumentReceipt.model_validate_json(response["receipt_json"]),
    )


def save(
    connection: sqlite3.Connection,
    request: WriteRequest,
    receipt: DocumentReceipt,
    status: Literal["applied", "unchanged"],
    at: datetime,
    digest: str,
) -> None:
    key_id = token()
    connection.execute(
        "INSERT INTO write_key(key_id,corpus_id,writer_id,operation,key_hash,digest,digest_version,"
        "status,committed_at,expires_at,expired) VALUES (?,?,?,?,?,?,?,?,?,?,0)",
        (
            key_id,
            request.scope.corpus_id,
            request.attribution.writer_id,
            request.payload.operation,
            sha(request.retry_key.encode()),
            digest,
            "e1-request-digest/1",
            status,
            timestamp(at),
            timestamp(at + timedelta(days=30)),
        ),
    )
    connection.execute(
        "INSERT INTO write_response(key_id,corpus_id,document_id,receipt_json) VALUES (?,?,?,?)",
        (key_id, request.scope.corpus_id, receipt.document_id, receipt.model_dump_json()),
    )
    connection.execute(
        "INSERT INTO write_provenance(key_id,corpus_id,document_id,state_version,attribution_json) "
        "VALUES (?,?,?,?,?)",
        (key_id, request.scope.corpus_id, receipt.document_id, receipt.processing.state_version,
         request.attribution.model_dump_json()),
    )
