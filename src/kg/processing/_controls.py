"""Explicit failure, retry episodes and bounded reason-specific recovery."""

import sqlite3
from datetime import datetime

from kg.evidence._receipts import clock
from kg.evidence._values import canonical, sha, timestamp, token
from kg.evidence.errors import EvidenceServiceError
from kg.models.evidence import LocalIdentity
from kg.models.processing import (
    FailRequest,
    JobsRequest,
    JobView,
    ProcessingCapabilities,
    RecoveryResult,
    RetryReceipt,
    RetryRequest,
)
from kg.models.processing_events import ProcessingDecision, ProcessingRetry
from kg.processing import _authorization as auth
from kg.processing import _core, _receipts


def fail(
    connection: sqlite3.Connection,
    identity: LocalIdentity,
    request: FailRequest,
    supplied: datetime,
    capture: _core.CaptureType,
) -> JobView | EvidenceServiceError:
    plan = auth.selection(connection, identity, request.scope, request.selection)
    capability = ProcessingCapabilities.model_validate_json(plan["capability_json"])
    if request.failure_class not in capability.failure_classes:
        raise EvidenceServiceError("unsupported")
    live = _core.live_claim(connection, identity, request, supplied, capture)
    if isinstance(live, EvidenceServiceError):
        return live
    row, at = live
    if request.failure_class == "transient":
        _core.retry_wait(connection, row, at, capture)
    else:
        _core.transition(connection, row, "failed", None, at)
        with capture.guard():
            capture.append(
                ProcessingRetry(classification="permanent", attempt=row["episode_attempts"])
            )
    connection.execute(
        "UPDATE processing_job SET failure_code=?,diagnostic_id=? WHERE job_id=?",
        (
            request.failure_code,
            token(),
            row["job_id"],
        ),
    )
    return _core.view(
        connection, auth.job(connection, request.scope, request.selection, request.job_id)
    )


def retry(
    connection: sqlite3.Connection,
    identity: LocalIdentity,
    request: RetryRequest,
    supplied: datetime,
    capture: _core.CaptureType,
) -> RetryReceipt | EvidenceServiceError:
    plan = auth.selection(connection, identity, request.scope, request.selection)
    digest = sha(
        canonical(
            {
                "version": "e4-control-digest/1",
                "corpus": request.scope.corpus_id,
                "principal": identity.principal_id,
                "selection": request.selection.model_dump(),
                "job_id": request.job_id,
                "expected_status_version": request.expected_status_version,
            }
        ).encode()
    )
    key = connection.execute(
        "SELECT * FROM processing_key WHERE corpus_id=? AND writer_id=? "
        "AND operation='retry' AND key_hash=?",
        (request.scope.corpus_id, request.selection.writer_id, sha(request.retry_key.encode())),
    ).fetchone()
    if key is not None:
        return _receipts.replay(
            connection, identity, request, key, digest, supplied, capture, RetryReceipt
        )
    row = auth.job(connection, request.scope, request.selection, request.job_id)
    current = auth.document(
        connection, request.scope, request.selection, auth.dependency(connection, row), current=True
    )
    _core.retain(capture, request.job_id)
    at = clock(connection, supplied)
    if (
        row["status_version"] != request.expected_status_version
        or row["status"] != "failed"
        or row["coordination_reason"] != "retry_exhausted"
    ):
        return EvidenceServiceError("state_conflict")
    guard = connection.execute(
        "SELECT * FROM processing_guard WHERE corpus_id=?", (request.scope.corpus_id,)
    ).fetchone()
    if guard is None:
        raise EvidenceServiceError("internal_error")
    if not current:
        return EvidenceServiceError("state_changed")
    if not plan["enabled"] or guard["blocked"] or guard["epoch"] != row["guard_epoch"]:
        return EvidenceServiceError("state_conflict")
    changed = connection.execute(
        "UPDATE processing_job SET status='queued',status_version=status_version+1,"
        "retry_episode=retry_episode+1,episode_attempts=0,claim_fence=claim_fence+1,"
        "coordination_reason=NULL,failure_code=NULL,diagnostic_id=NULL,next_due_at=?,updated_at=? "
        "WHERE job_id=? AND status_version=? AND status='failed' "
        "AND coordination_reason='retry_exhausted'",
        (timestamp(at), timestamp(at), request.job_id, request.expected_status_version),
    ).rowcount
    if changed != 1:
        raise EvidenceServiceError("state_conflict")
    result = RetryReceipt(
        job_id=request.job_id,
        status_version=row["status_version"] + 1,
        retry_episode=row["retry_episode"] + 1,
    )
    _receipts.store(connection, identity, request, "retry", request.retry_key, digest, result, at)
    with capture.guard():
        capture.append(
            ProcessingDecision(
                kind="processing.restart_decision",
                decision="restarted",
                job_id=request.job_id,
                sequence=result.retry_episode,
            )
        )
    return result


def recover(
    connection: sqlite3.Connection,
    identity: LocalIdentity,
    request: JobsRequest,
    supplied: datetime,
    capture: _core.CaptureType,
) -> RecoveryResult:
    s = request.selection
    plan = auth.selection(connection, identity, request.scope, s)
    at = clock(connection, supplied)
    guard = connection.execute(
        "SELECT * FROM processing_guard WHERE corpus_id=?", (request.scope.corpus_id,)
    ).fetchone()
    if guard is None:
        raise EvidenceServiceError("internal_error")
    rows = connection.execute(
        "SELECT * FROM processing_job WHERE corpus_id=? AND namespace=? AND owner_id=? "
        "AND writer_id=? AND principal_id=? AND plan_id=? AND plan_version=? "
        "AND target_kind='document' AND creation_sequence>? "
        "AND status IN ('queued','retry_wait','running','blocked') "
        "ORDER BY creation_sequence LIMIT ?",
        (
            request.scope.corpus_id,
            s.namespace,
            s.owner_id,
            s.writer_id,
            identity.principal_id,
            s.plan_id,
            s.plan_version,
            request.after_sequence,
            request.limit + 1,
        ),
    ).fetchall()
    changed = unsupported = 0
    page = rows[: request.limit]
    for row in page:
        _core.retain(capture, row["job_id"])
        current = auth.document(
            connection, request.scope, s, auth.dependency(connection, row), current=True
        )
        if (
            current
            and row["status"] == "blocked"
            and row["coordination_reason"] not in {"plan_disabled", "authority_unavailable"}
        ):
            # Do not launder an owner-only block through a temporary plan disable.
            unsupported += 1
            with capture.guard():
                capture.append(
                    ProcessingDecision(
                        kind="processing.restart_decision",
                        decision="unavailable",
                        job_id=row["job_id"],
                        reason="unsupported_capability",
                    )
                )
        elif _core.eligible(connection, request, row, plan, guard, at, capture):
            if row["status"] == "running" and timestamp(at) >= row["lease_deadline"]:
                _core.retry_wait(connection, row, at, capture)
                with capture.guard():
                    capture.append(
                        ProcessingDecision(
                            kind="processing.restart_decision",
                            decision="expired",
                            job_id=row["job_id"],
                            reason="lease_lost",
                        )
                    )
            elif row["status"] == "blocked":
                exhausted = row["episode_attempts"] >= 10
                _core.transition(
                    connection,
                    row,
                    "failed" if exhausted else "queued",
                    "retry_exhausted" if exhausted else None,
                    at,
                )
        updated = auth.job(connection, request.scope, s, row["job_id"])
        if updated["status_version"] != row["status_version"]:
            changed += 1
            with capture.guard():
                capture.append(
                    ProcessingDecision(
                        kind="processing.restart_decision",
                        decision=updated["status"],
                        job_id=row["job_id"],
                    )
                )
    return RecoveryResult(
        examined=len(page),
        changed=changed,
        unsupported=unsupported,
        next_after=page[-1]["creation_sequence"] if len(rows) > request.limit else None,
    )
