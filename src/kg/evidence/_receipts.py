from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Literal

from kg.evidence._authorization import authorize_writer
from kg.evidence._coordination import CanonicalKey
from kg.evidence._transactions import CanonicalWriteContext
from kg.evidence._values import request_digest, sha, timestamp, token
from kg.evidence.errors import EvidenceServiceError
from kg.models.evidence import LocalIdentity
from kg.models.foundation import (
    DocumentReceipt,
    ExternalDocument,
    Failure,
    PutDocument,
    RemoveDocument,
    WriteOutcome,
    WriteRequest,
)


@dataclass(frozen=True)
class ReplayMissing:
    pass


@dataclass(frozen=True)
class ReplaySuccess:
    key_id: str
    status: Literal["applied", "unchanged"]
    receipt: DocumentReceipt

    def outcome(self, request_id: str) -> WriteOutcome:
        return WriteOutcome(request_id=request_id, status=self.status, receipt=self.receipt)


@dataclass(frozen=True)
class ReplayExpired:
    failure: Failure


@dataclass(frozen=True)
class ReplayConflict:
    failure: Failure


ReplayResult = ReplayMissing | ReplaySuccess | ReplayExpired | ReplayConflict


def canonical_key(request: WriteRequest) -> CanonicalKey:
    return CanonicalKey(
        corpus_id=request.scope.corpus_id, writer_id=request.attribution.writer_id,
        operation=request.payload.operation, retry_key_hash=sha(request.retry_key.encode()),
    )


def lookup_key(context: CanonicalWriteContext, key: CanonicalKey) -> sqlite3.Row | None:
    row = context.connection.execute(
        "SELECT * FROM write_key WHERE corpus_id=? AND writer_id=? AND operation=? AND key_hash=?",
        (key.corpus_id, key.writer_id, key.operation, key.retry_key_hash),
    ).fetchone()
    if row is not None and not isinstance(row, sqlite3.Row):
        raise EvidenceServiceError("internal_error")
    return row


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


def replay_only(
    context: CanonicalWriteContext, identity: LocalIdentity, request: WriteRequest,
    key_id: str, observed_at: datetime,
) -> ReplayResult:
    connection = context.connection
    if identity != context.identity:
        raise EvidenceServiceError("forbidden")
    key = lookup_key(context, canonical_key(request))
    if key is None:
        return ReplayMissing()
    if key["key_id"] != key_id:
        raise EvidenceServiceError("state_conflict")
    if not isinstance(request.payload, (PutDocument, RemoveDocument)):
        # Knowledge retained manifests and receipt decoding are owned by K1.
        raise EvidenceServiceError("unsupported")
    response = connection.execute(
        "SELECT * FROM write_response WHERE key_id=?", (key_id,)
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
        document = ExternalDocument(
            source_namespace=target["namespace"],
            synchronization_scope=target["synchronization_scope"],
            external_id=target["external_id"],
        )
    else:
        document = request.payload.document
    authorize_writer(connection, identity, request.scope, request.attribution, document)
    if response is None and not key["expired"]:
        raise EvidenceServiceError("internal_error")
    at = clock(connection, observed_at)
    if key["expired"] or timestamp(at) >= key["expires_at"]:
        connection.execute("DELETE FROM write_response WHERE key_id=?", (key_id,))
        connection.execute("UPDATE write_key SET expired=1 WHERE key_id=?", (key_id,))
        return ReplayExpired(EvidenceServiceError("retry_expired").failure)
    if key["digest"] != request_digest(request):
        return ReplayConflict(EvidenceServiceError("retry_conflict").failure)
    assert response is not None
    receipt = DocumentReceipt.model_validate_json(response["receipt_json"])
    if receipt.document_id != response["document_id"]:
        raise EvidenceServiceError("internal_error")
    return ReplaySuccess(key_id, key["status"], receipt)


def save_document(
    context: CanonicalWriteContext, request: WriteRequest, receipt: DocumentReceipt,
    status: Literal["applied", "unchanged"], committed_at: datetime,
) -> str:
    connection = context.connection
    key_id = token()
    connection.execute(
        "INSERT INTO write_key(key_id,corpus_id,writer_id,operation,key_hash,digest,digest_version,"
        "status,committed_at,expires_at,expired) VALUES (?,?,?,?,?,?,?,?,?,?,0)",
        (
            key_id, request.scope.corpus_id, request.attribution.writer_id,
            request.payload.operation, sha(request.retry_key.encode()), request_digest(request),
            "e1-request-digest/1", status, timestamp(committed_at),
            timestamp(committed_at + timedelta(days=30)),
        ),
    )
    connection.execute(
        "INSERT INTO write_response(key_id,corpus_id,document_id,receipt_json) VALUES (?,?,?,?)",
        (key_id, request.scope.corpus_id, receipt.document_id, receipt.model_dump_json()),
    )
    connection.execute(
        "INSERT INTO write_provenance(key_id,corpus_id,document_id,state_version,attribution_json) "
        "VALUES (?,?,?,?,?)",
        (
            key_id, request.scope.corpus_id, receipt.document_id,
            receipt.processing.state_version, request.attribution.model_dump_json(),
        ),
    )
    return key_id
