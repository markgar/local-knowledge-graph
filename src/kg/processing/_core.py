"""Bounded durable transitions on the service-owned canonical transaction."""

import sqlite3
from datetime import datetime, timedelta

from kg.diagnostics._collector import Capture, CaptureUnavailable
from kg.diagnostics._targets import ProcessingTarget, ReportTargets
from kg.evidence._receipts import clock
from kg.evidence._values import canonical, sha, timestamp, token
from kg.evidence.errors import EvidenceServiceError
from kg.models.evidence import LocalIdentity
from kg.models.processing import (
    Claim,
    ClaimResult,
    CoordinationReason,
    HeartbeatRequest,
    JobStatus,
    JobView,
    ScheduleReceipt,
    ScheduleRequest,
    WorkerRequest,
)
from kg.models.processing_events import ProcessingDecision, ProcessingRetry
from kg.processing import _authorization as auth
from kg.processing import _receipts

CaptureType = Capture | CaptureUnavailable


def retain(capture: CaptureType, job_id: str) -> None:
    with capture.guard():
        capture.retain(ReportTargets(values=(ProcessingTarget(job_id=job_id),)))


def view(connection: sqlite3.Connection, row: sqlite3.Row) -> JobView:
    return JobView(
        job_id=row["job_id"],
        target=auth.dependency(connection, row),
        status=row["status"],
        creation_sequence=row["creation_sequence"],
        status_version=row["status_version"],
        lifetime_attempts=row["lifetime_attempts"],
        retry_episode=row["retry_episode"],
        episode_attempts=row["episode_attempts"],
        claim_fence=row["claim_fence"],
        worker_id=row["worker_id"],
        lease_deadline=datetime.fromisoformat(row["lease_deadline"])
        if row["lease_deadline"]
        else None,
        next_due_at=datetime.fromisoformat(row["next_due_at"]),
        reason=row["coordination_reason"],
        failure_code=row["failure_code"],
        diagnostic_id=row["diagnostic_id"],
    )


def schedule(
    connection: sqlite3.Connection,
    identity: LocalIdentity,
    request: ScheduleRequest,
    supplied: datetime,
    capture: CaptureType,
) -> ScheduleReceipt | EvidenceServiceError:
    s, scope = request.selection, request.scope
    plan = auth.selection(connection, identity, scope, s)
    key = connection.execute(
        "SELECT * FROM processing_key WHERE corpus_id=? AND writer_id=? "
        "AND operation='schedule' AND key_hash=?",
        (scope.corpus_id, s.writer_id, sha(request.retry_key.encode())),
    ).fetchone()
    digest = sha(
        canonical(
            {
                "version": "e4-control-digest/1",
                "corpus": scope.corpus_id,
                "principal": identity.principal_id,
                "selection": s.model_dump(),
                "target": request.target.model_dump(),
            }
        ).encode()
    )
    if key is not None:
        return _receipts.replay(
            connection, identity, request, key, digest, supplied, capture, ScheduleReceipt
        )

    guard = connection.execute(
        "SELECT * FROM processing_guard WHERE corpus_id=?",
        (scope.corpus_id,),
    ).fetchone()
    if guard is None or guard["blocked"] or not plan["enabled"]:
        raise EvidenceServiceError("state_conflict")
    if not auth.document(connection, scope, s, request.target, current=True):
        raise EvidenceServiceError("state_changed")
    at = clock(connection, supplied)
    target_digest = sha(canonical(request.target.model_dump()).encode())
    work = s.model_dump(exclude={"worker_id"})
    logical = sha(
        canonical(
            {
                "selection": work,
                "corpus": scope.corpus_id,
                "principal": identity.principal_id,
                "definition": plan["definition_hash"],
                "target": target_digest,
            }
        ).encode()
    )
    old = connection.execute(
        "SELECT job_id FROM processing_job WHERE corpus_id=? AND logical_key=?",
        (scope.corpus_id, logical),
    ).fetchone()
    job_id = old["job_id"] if old else token()
    result = ScheduleReceipt(job_id=job_id, status="unchanged" if old else "applied")
    if old is None:
        sequence = connection.execute(
            "SELECT COALESCE(MAX(creation_sequence),0)+1 FROM processing_job WHERE corpus_id=?",
            (scope.corpus_id,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO processing_job(job_id,corpus_id,namespace,owner_id,writer_id,principal_id,"
            "plan_id,plan_version,target_kind,document_id,target_state,target_digest,logical_key,"
            "creation_sequence,status,status_version,checkpoint_sequence,lifetime_attempts,"
            "retry_episode,episode_attempts,next_due_at,claim_fence,guard_epoch,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?,?,'document',?,?,?,?,?,'queued',1,0,0,1,0,?,0,?,?,?)",
            (
                job_id,
                scope.corpus_id,
                s.namespace,
                s.owner_id,
                s.writer_id,
                identity.principal_id,
                s.plan_id,
                s.plan_version,
                request.target.document_id,
                request.target.state_version,
                target_digest,
                logical,
                sequence,
                timestamp(at),
                guard["epoch"],
                timestamp(at),
                timestamp(at),
            ),
        )
        t = request.target
        connection.execute(
            "INSERT INTO processing_dependency VALUES (?,?,?,?,?,?,?)",
            (
                scope.corpus_id,
                job_id,
                s.namespace,
                t.document_id,
                t.revision_id,
                t.state_version,
                t.namespace_token,
            ),
        )
    _receipts.store(
        connection, identity, request, "schedule", request.retry_key, digest, result, at
    )
    retain(capture, job_id)
    with capture.guard():
        capture.append(
            ProcessingDecision(
                kind="processing.fence_decision",
                decision="accepted",
                job_id=job_id,
            )
        )
    return result


def transition(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    status: JobStatus,
    reason: CoordinationReason | None,
    at: datetime,
    *,
    delay: int = 0,
) -> None:
    if row["status"] == status and row["coordination_reason"] == reason:
        return
    due = timestamp(at + timedelta(seconds=delay))
    if status in {"blocked", "queued"}:
        due = max(due, row["next_due_at"])
    changed = connection.execute(
        "UPDATE processing_job SET status=?,coordination_reason=?,status_version=status_version+1,"
        "worker_id=NULL,lease_deadline=NULL,claim_fence=claim_fence+1,next_due_at=?,updated_at=? "
        "WHERE job_id=? AND status_version=?",
        (
            status,
            reason,
            due,
            timestamp(at),
            row["job_id"],
            row["status_version"],
        ),
    ).rowcount
    if changed != 1:
        raise EvidenceServiceError("state_conflict")


def retry_wait(
    connection: sqlite3.Connection,
    row: sqlite3.Row,
    at: datetime,
    capture: CaptureType,
) -> None:
    exhausted = row["episode_attempts"] >= 10
    delay = min(300, 2 ** (row["episode_attempts"] - 1))
    transition(
        connection,
        row,
        "failed" if exhausted else "retry_wait",
        "retry_exhausted" if exhausted else "retry_scheduled",
        at,
        delay=0 if exhausted else delay,
    )
    with capture.guard():
        capture.append(
            ProcessingRetry(
                classification="exhausted" if exhausted else "transient",
                due_at=None if exhausted else at + timedelta(seconds=delay),
                attempt=row["episode_attempts"],
            )
        )


def eligible(
    connection: sqlite3.Connection,
    request: WorkerRequest,
    row: sqlite3.Row,
    plan: sqlite3.Row,
    guard: sqlite3.Row,
    at: datetime,
    capture: CaptureType,
) -> bool:
    if not auth.document(
        connection,
        request.scope,
        request.selection,
        auth.dependency(connection, row),
        current=True,
    ):
        transition(connection, row, "superseded", "dependency_changed", at)
        with capture.guard():
            capture.append(
                ProcessingDecision(
                    kind="processing.fence_decision",
                    decision="stale",
                    job_id=row["job_id"],
                    reason="dependency_changed",
                )
            )
        return False
    if not plan["enabled"]:
        transition(connection, row, "blocked", "plan_disabled", at)
        with capture.guard():
            capture.append(
                ProcessingDecision(
                    kind="processing.fence_decision",
                    decision="blocked",
                    job_id=row["job_id"],
                    reason="plan_disabled",
                )
            )
        return False
    if guard["blocked"] or guard["epoch"] != row["guard_epoch"]:
        transition(connection, row, "blocked", "purge_blocked", at)
        with capture.guard():
            capture.append(
                ProcessingDecision(
                    kind="processing.fence_decision",
                    decision="blocked",
                    job_id=row["job_id"],
                    reason="purge_blocked",
                )
            )
        return False
    return True


def claim(
    connection: sqlite3.Connection,
    identity: LocalIdentity,
    request: WorkerRequest,
    supplied: datetime,
    capture: CaptureType,
) -> ClaimResult:
    s, scope = request.selection, request.scope
    plan = auth.selection(connection, identity, scope, s)
    at = clock(connection, supplied)
    guard = connection.execute(
        "SELECT * FROM processing_guard WHERE corpus_id=?",
        (scope.corpus_id,),
    ).fetchone()
    if guard is None:
        raise EvidenceServiceError("internal_error")
    rows = connection.execute(
        "SELECT * FROM processing_job WHERE corpus_id=? AND namespace=? AND owner_id=? "
        "AND writer_id=? AND principal_id=? AND plan_id=? AND plan_version=? "
        "AND target_kind='document' AND ((status IN ('queued','retry_wait') AND next_due_at<=?) "
        "OR (status='running' AND lease_deadline<=?)) "
        "ORDER BY next_due_at,creation_sequence,job_id LIMIT 201",
        (
            scope.corpus_id,
            s.namespace,
            s.owner_id,
            s.writer_id,
            identity.principal_id,
            s.plan_id,
            s.plan_version,
            timestamp(at),
            timestamp(at),
        ),
    ).fetchall()
    for index, row in enumerate(rows[:200], 1):
        retain(capture, row["job_id"])
        if not eligible(connection, request, row, plan, guard, at, capture):
            continue
        if row["status"] == "running":
            retry_wait(connection, row, at, capture)
            with capture.guard():
                capture.append(
                    ProcessingDecision(
                        kind="processing.restart_decision",
                        decision="expired",
                        job_id=row["job_id"],
                        reason="lease_lost",
                    )
                )
            continue
        if row["episode_attempts"] >= 10:
            transition(connection, row, "failed", "retry_exhausted", at)
            continue
        deadline = at + timedelta(seconds=60)
        changed = connection.execute(
            "UPDATE processing_job SET status='running',status_version=status_version+1,"
            "claim_fence=claim_fence+1,worker_id=?,lease_deadline=?,"
            "lifetime_attempts=lifetime_attempts+1,episode_attempts=episode_attempts+1,"
            "coordination_reason=NULL,failure_code=NULL,diagnostic_id=NULL,updated_at=? "
            "WHERE job_id=? AND status_version=?",
            (s.worker_id, timestamp(deadline), timestamp(at), row["job_id"], row["status_version"]),
        ).rowcount
        if changed != 1:
            raise EvidenceServiceError("state_conflict")
        result = Claim(
            job_id=row["job_id"],
            worker_id=s.worker_id,
            claim_fence=row["claim_fence"] + 1,
            lease_deadline=deadline,
            attempt=row["episode_attempts"] + 1,
            target=auth.dependency(connection, row),
        )
        with capture.guard():
            capture.append(
                ProcessingDecision(
                    kind="processing.claim_decision",
                    decision="claimed",
                    job_id=result.job_id,
                    sequence=result.claim_fence,
                )
            )
        return ClaimResult(status="claimed", claim=result, inspected=index)
    with capture.guard():
        capture.append(ProcessingDecision(kind="processing.claim_decision", decision="no_work"))
    return ClaimResult(
        status="no_work", inspected=min(200, len(rows)), scan_truncated=len(rows) > 200
    )


def live_claim(
    connection: sqlite3.Connection,
    identity: LocalIdentity,
    request: HeartbeatRequest,
    supplied: datetime,
    capture: CaptureType,
) -> tuple[sqlite3.Row, datetime] | EvidenceServiceError:
    plan = auth.selection(connection, identity, request.scope, request.selection)
    row = auth.job(connection, request.scope, request.selection, request.job_id)
    auth.document(
        connection,
        request.scope,
        request.selection,
        auth.dependency(connection, row),
        current=False,
    )
    retain(capture, row["job_id"])
    at = clock(connection, supplied)
    if (
        row["status"] != "running"
        or row["claim_fence"] != request.claim_fence
        or row["worker_id"] != request.selection.worker_id
        or timestamp(at) >= row["lease_deadline"]
    ):
        with capture.guard():
            capture.append(
                ProcessingDecision(
                    kind="processing.fence_decision",
                    decision="lease_lost",
                    job_id=request.job_id,
                    reason="lease_lost",
                )
            )
        return EvidenceServiceError("state_conflict")
    guard = connection.execute(
        "SELECT * FROM processing_guard WHERE corpus_id=?",
        (request.scope.corpus_id,),
    ).fetchone()
    if guard is None:
        raise EvidenceServiceError("internal_error")
    if not eligible(connection, request, row, plan, guard, at, capture):
        return EvidenceServiceError("state_changed")
    return row, at


def heartbeat(
    connection: sqlite3.Connection,
    identity: LocalIdentity,
    request: HeartbeatRequest,
    supplied: datetime,
    capture: CaptureType,
) -> Claim | EvidenceServiceError:
    live = live_claim(connection, identity, request, supplied, capture)
    if isinstance(live, EvidenceServiceError):
        return live
    row, at = live
    deadline = at + timedelta(seconds=60)
    changed = connection.execute(
        "UPDATE processing_job SET lease_deadline=?,status_version=status_version+1,updated_at=? "
        "WHERE job_id=? AND status='running' AND claim_fence=? AND worker_id=?",
        (
            timestamp(deadline),
            timestamp(at),
            request.job_id,
            request.claim_fence,
            request.selection.worker_id,
        ),
    ).rowcount
    if changed != 1:
        raise EvidenceServiceError("state_conflict")
    with capture.guard():
        capture.append(
            ProcessingDecision(
                kind="processing.fence_decision",
                decision="accepted",
                job_id=request.job_id,
                sequence=request.claim_fence,
            )
        )
    return Claim(
        job_id=request.job_id,
        worker_id=request.selection.worker_id,
        claim_fence=request.claim_fence,
        lease_deadline=deadline,
        attempt=row["episode_attempts"],
        target=auth.dependency(connection, row),
    )
