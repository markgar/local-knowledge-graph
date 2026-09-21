"""Historical control receipts share the canonical durable clock and key ledger."""

import sqlite3
from datetime import datetime, timedelta
from typing import Literal

from kg.diagnostics._collector import Capture, CaptureUnavailable
from kg.diagnostics._targets import ProcessingSelectionTarget, ProcessingTarget, ReportTargets
from kg.evidence._receipts import clock
from kg.evidence._values import sha, timestamp, token
from kg.evidence.errors import EvidenceServiceError
from kg.models.evidence import LocalIdentity
from kg.models.processing import RetryReceipt, ScheduleReceipt, WorkerRequest, WorkerSelection
from kg.models.processing_events import ProcessingDecision
from kg.processing import _authorization as auth

Receipt = ScheduleReceipt | RetryReceipt


def replay[T: (ScheduleReceipt, RetryReceipt)](
    connection: sqlite3.Connection,
    identity: LocalIdentity,
    request: WorkerRequest,
    key: sqlite3.Row,
    digest: str,
    supplied: datetime,
    capture: Capture | CaptureUnavailable,
    kind: type[T],
) -> T | EvidenceServiceError:
    if (key["principal_id"], key["owner_id"]) != (
        identity.principal_id,
        request.selection.owner_id,
    ):
        raise EvidenceServiceError("forbidden")
    response = connection.execute(
        "SELECT * FROM processing_response WHERE key_id=?", (key["key_id"],)
    ).fetchone()
    retained = (
        WorkerSelection.model_validate_json(response["authorization_json"])
        if response is not None
        else request.selection
    )
    auth.selection(connection, identity, request.scope, retained)
    with capture.guard():
        capture.retain(ReportTargets(values=(ProcessingSelectionTarget(**retained.model_dump()),)))
    if response is None and not key["expired"]:
        raise EvidenceServiceError("internal_error")
    receipt: T | None = None
    if response is not None:
        receipt = kind.model_validate_json(response["response_json"])
        row = auth.job(connection, request.scope, retained, receipt.job_id)
        auth.document(
            connection, request.scope, retained, auth.dependency(connection, row), current=False
        )
        with capture.guard():
            capture.retain(ReportTargets(values=(ProcessingTarget(job_id=receipt.job_id),)))
    at = clock(connection, supplied)
    if key["expired"] or timestamp(at) >= key["expires_at"]:
        connection.execute("DELETE FROM processing_response WHERE key_id=?", (key["key_id"],))
        connection.execute("UPDATE processing_key SET expired=1 WHERE key_id=?", (key["key_id"],))
        with capture.guard():
            capture.append(
                ProcessingDecision(kind="processing.settled_retrieval", decision="expired")
            )
        return EvidenceServiceError("retry_expired")
    if digest != key["digest"]:
        return EvidenceServiceError("retry_conflict")
    assert receipt is not None
    with capture.guard():
        capture.append(
            ProcessingDecision(
                kind="processing.settled_retrieval", decision="replayed", job_id=receipt.job_id
            )
        )
        capture.append(
            ProcessingDecision(
                kind="processing.settled_retrieval",
                decision="accepted",
                job_id=receipt.job_id,
                observation_kind="retained_commit_fact",
            )
        )
    return receipt


def store(
    connection: sqlite3.Connection,
    identity: LocalIdentity,
    request: WorkerRequest,
    operation: Literal["schedule", "retry"],
    retry_key: str,
    digest: str,
    result: Receipt,
    at: datetime,
) -> None:
    key_id = token()
    connection.execute(
        "INSERT INTO processing_key VALUES (?,?,?,?,?,?,?,?,'e4-control-digest/1',?,?,?,0)",
        (
            key_id,
            request.scope.corpus_id,
            identity.principal_id,
            request.selection.owner_id,
            request.selection.writer_id,
            operation,
            sha(retry_key.encode()),
            digest,
            result.status,
            timestamp(at),
            timestamp(at + timedelta(days=30)),
        ),
    )
    connection.execute(
        "INSERT INTO processing_response VALUES (?,?,?,?,?)",
        (
            key_id,
            request.scope.corpus_id,
            "job" if operation == "schedule" else "retry",
            result.model_dump_json(),
            request.selection.model_dump_json(),
        ),
    )
