from __future__ import annotations

from concurrent.futures import ProcessPoolExecutor
from datetime import UTC, datetime, timedelta
from multiprocessing import get_context
from pathlib import Path

import pytest
from pydantic import ValidationError
from support.evidence import Environment, environment, put, receipt

from kg.diagnostics._targets import ProcessingSelectionTarget, ReportTargets
from kg.evidence import EvidenceDatabase
from kg.evidence.errors import EvidenceServiceError
from kg.models.evidence import LocalAdminAuthority, LocalIdentity
from kg.models.execution import ExecutionReport, ExplainOptions
from kg.models.processing import (
    ClaimResult,
    DocumentTarget,
    FailRequest,
    HeartbeatRequest,
    JobRequest,
    JobsRequest,
    PlanRegistration,
    PlanStateRequest,
    RetryReceipt,
    RetryRequest,
    ScheduleRequest,
    WorkerRegistration,
    WorkerRequest,
    WorkerSelection,
)
from kg.processing import ProcessingAdministration, ProcessingService

AT = datetime(2026, 9, 21, tzinfo=UTC)
SELECTION = WorkerSelection(
    namespace="markdown",
    owner_id="owner",
    writer_id="writer",
    plan_id="index",
    plan_version="1",
    worker_id="worker",
)


def setup(path: Path) -> tuple[Environment, ProcessingService, ScheduleRequest]:
    env = environment(path)
    admin = ProcessingAdministration(env.database, LocalAdminAuthority(principal_id="admin"))
    admin.register_plan(
        PlanRegistration(
            corpus_id="work",
            plan_id="index",
            plan_version="1",
            producer="test",
            producer_version="1",
        )
    )
    admin.register_worker(
        WorkerRegistration(
            corpus_id="work",
            principal_id="principal",
            selection=SELECTION,
        )
    )
    accepted = receipt(env.service.write(put(env.scope)))
    with env.database.connection() as connection:
        state = connection.execute(
            "SELECT namespace_token FROM document_state WHERE state_version=?",
            (accepted.processing.state_version,),
        ).fetchone()
    request = ScheduleRequest(
        scope=env.scope,
        selection=SELECTION,
        request_id="schedule",
        retry_key="retry",
        target=DocumentTarget(
            document_id=accepted.document_id,
            revision_id=accepted.revision_id,
            state_version=accepted.processing.state_version,
            namespace_token=state[0],
        ),
    )
    service = ProcessingService(env.database, env.service.identity)
    # E1 writes share the same durable clock.
    with env.database.connection() as connection:
        watermark = connection.execute("SELECT watermark FROM receipt_clock").fetchone()[0]
    service._clock = lambda: max(AT, datetime.fromisoformat(watermark))
    return env, service, request


def worker(request: ScheduleRequest) -> WorkerRequest:
    return WorkerRequest(
        scope=request.scope,
        selection=request.selection,
        request_id="claim",
    )


def job_request(request: ScheduleRequest, job_id: str) -> JobRequest:
    return JobRequest(**worker(request).model_dump(), job_id=job_id)


def heartbeat(request: ScheduleRequest, claim: ClaimResult) -> HeartbeatRequest:
    assert claim.claim
    return HeartbeatRequest(
        **job_request(request, claim.claim.job_id).model_dump(),
        claim_fence=claim.claim.claim_fence,
    )


def failure(
    request: ScheduleRequest, claim: ClaimResult, *, permanent: bool = False
) -> FailRequest:
    return FailRequest(
        **heartbeat(request, claim).model_dump(),
        failure_class="permanent" if permanent else "transient",
        failure_code="invalid_request" if permanent else "budget_exceeded",
    )


def exhaust(service: ProcessingService, request: ScheduleRequest) -> RetryRequest:
    scheduled = service.schedule(request)
    for attempt in range(1, 11):
        claimed = service.claim(worker(request))
        assert claimed.claim and claimed.claim.attempt == attempt
        state = service.fail(failure(request, claimed))
        assert state.lifetime_attempts >= attempt
        service._clock = lambda due=state.next_due_at: due
    assert state.status == "failed" and state.reason == "retry_exhausted"
    return RetryRequest(
        **job_request(request, scheduled.job_id).model_dump(),
        expected_status_version=state.status_version,
        retry_key="operator-retry",
    )


def enable_plan(env: Environment, enabled: bool) -> None:
    ProcessingAdministration(env.database, env.admin.authority).set_plan_enabled(
        PlanStateRequest(
            corpus_id="work",
            plan_id="index",
            plan_version="1",
            expected_enabled=not enabled,
            enabled=enabled,
        )
    )


@pytest.mark.service
def test_explicit_fail_delay_exhaustion_and_idempotent_retry(tmp_path: Path) -> None:
    env, service, request = setup(tmp_path / "retry.db")
    retry = exhaust(service, request)
    before = service.job(job_request(request, retry.job_id))
    assert before.episode_attempts == before.lifetime_attempts == 10
    assert before.failure_code == "budget_exceeded" and before.diagnostic_id
    assert service.recover(JobsRequest(**worker(request).model_dump())).changed == 0
    result = service.retry_explained(retry)
    assert result.outcome.retry_episode == 2
    assert isinstance(result.report, ExecutionReport)
    claimed = service.claim(worker(request))
    assert claimed.claim and claimed.claim.attempt == 1
    current = service.job(job_request(request, retry.job_id))
    assert current.lifetime_attempts == 11 and current.retry_episode == 2
    assert current.failure_code is current.diagnostic_id is None
    replay = service.retry_explained(retry.model_copy(update={"request_id": "replay"}))
    assert replay.outcome == result.outcome
    assert isinstance(replay.report, ExecutionReport)
    assert any(
        event.event.kind == "processing.settled_retrieval"
        and event.event.observation_kind == "retained_commit_fact"
        for event in replay.report.events
    )
    assert service.job(job_request(request, retry.job_id)) == current
    restarted = ProcessingService(EvidenceDatabase(env.database.path), env.service.identity)
    assert restarted.retry(retry) == result.outcome
    with pytest.raises(EvidenceServiceError, match="retry_conflict"):
        service.retry(retry.model_copy(update={"expected_status_version": current.status_version}))
    with pytest.raises(EvidenceServiceError, match="state_conflict"):
        service.retry(retry.model_copy(update={"retry_key": "another-key"}))
    assert service.job(job_request(request, retry.job_id)) == current


@pytest.mark.service
def test_failure_due_order_rollback_and_disabled_plan_preserve_not_before(tmp_path: Path) -> None:
    env, service, request = setup(tmp_path / "due.db")
    service.schedule(request)
    claimed = service.claim(worker(request))
    assert claimed.claim
    failed = service.fail(failure(request, claimed))
    assert failed.next_due_at == service._clock() + timedelta(seconds=1)
    enable_plan(env, False)
    page = JobsRequest(**worker(request).model_dump())
    assert service.recover(page).changed == 1
    enable_plan(env, True)
    assert service.recover(page).changed == 1
    assert service.job(job_request(request, failed.job_id)).next_due_at == failed.next_due_at
    assert service.claim(worker(request)).status == "no_work"
    service._clock = lambda: failed.next_due_at
    second = service.claim(worker(request))
    assert second.claim and second.claim.attempt == 2
    with pytest.raises(EvidenceServiceError, match="state_conflict"):
        service.fail(failure(request, claimed))
    next_failure = service.fail(failure(request, second))
    assert next_failure.next_due_at == failed.next_due_at + timedelta(seconds=2)
    service._clock = lambda: failed.next_due_at - timedelta(days=1)
    assert service.claim(worker(request)).status == "no_work"
    assert service.recover(page).changed == 0
    assert service.job(job_request(request, failed.job_id)) == next_failure


@pytest.mark.service
@pytest.mark.parametrize("terminal", ["failed", "cancelled", "superseded", "succeeded"])
def test_control_calls_cannot_resurrect_terminal_outcomes(tmp_path: Path, terminal: str) -> None:
    env, service, request = setup(tmp_path / "terminal.db")
    service.schedule(request)
    claimed = service.claim(worker(request))
    assert claimed.claim
    permanent = service.fail(failure(request, claimed, permanent=True))
    if terminal != "failed":
        with env.database.transaction() as connection:
            # Success is a storage fixture only: no public acknowledgement exists in this slice.
            connection.execute(
                "UPDATE processing_job SET status=?,result_kind=?,result_id=?,result_outcome=?",
                (
                    terminal,
                    "projection" if terminal == "succeeded" else None,
                    "owner-result" if terminal == "succeeded" else None,
                    "published" if terminal == "succeeded" else None,
                ),
            )
    before = service.job(job_request(request, permanent.job_id))
    retry = RetryRequest(
        **job_request(request, permanent.job_id).model_dump(),
        expected_status_version=before.status_version,
        retry_key="denied",
    )
    with pytest.raises(EvidenceServiceError, match="state_conflict"):
        service.retry(retry)
    with pytest.raises(EvidenceServiceError, match="state_conflict"):
        service.fail(failure(request, claimed))
    assert service.recover(JobsRequest(**worker(request).model_dump())).changed == 0
    assert service.claim(worker(request)).status == "no_work"
    assert service.schedule(request).job_id == permanent.job_id
    assert service.job(job_request(request, permanent.job_id)) == before
    with env.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM processing_key WHERE operation='retry'"
            ).fetchone()[0]
            == 0
        )


@pytest.mark.service
def test_retry_receipt_expiry_precedes_changed_input_and_survives_rollback(tmp_path: Path) -> None:
    env, service, request = setup(tmp_path / "expiry.db")
    retry = exhaust(service, request)
    original = service.retry(retry)
    service.claim(worker(request))
    env.service.write(put(env.scope, state=request.target.state_version, text="Changed"))
    assert service.retry(retry) == original
    forged = retry.model_copy(update={"job_id": "unknown", "expected_status_version": 999})
    with pytest.raises(EvidenceServiceError, match="retry_conflict"):
        service.retry(forged)
    with env.database.connection() as connection:
        expiry = datetime.fromisoformat(
            connection.execute(
                "SELECT expires_at FROM processing_key WHERE operation='retry'"
            ).fetchone()[0]
        )
    service._clock = lambda: expiry
    with pytest.raises(EvidenceServiceError, match="retry_expired"):
        service.retry(forged)
    service._clock = lambda: expiry - timedelta(days=40)
    with pytest.raises(EvidenceServiceError, match="retry_expired"):
        service.retry(retry)
    with env.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM processing_response r JOIN processing_key k USING(key_id) "
                "WHERE k.operation='retry'"
            ).fetchone()[0]
            == 0
        )
        assert (
            connection.execute(
                "SELECT expired FROM processing_key WHERE operation='retry'"
            ).fetchone()[0]
            == 1
        )


@pytest.mark.service
@pytest.mark.parametrize("change", ["source", "guard", "plan", "worker", "grant", "replacement"])
def test_fail_checks_actual_current_authority_and_claim(tmp_path: Path, change: str) -> None:
    env, service, request = setup(tmp_path / "fail-guards.db")
    service.schedule(request)
    claimed = service.claim(worker(request))
    assert claimed.claim
    before = service.job(job_request(request, claimed.claim.job_id))
    if change == "source":
        env.service.write(put(env.scope, state=request.target.state_version, text="Changed"))
    elif change == "plan":
        enable_plan(env, False)
    elif change == "replacement":
        service._clock = lambda: claimed.claim.lease_deadline
        assert service.recover(JobsRequest(**worker(request).model_dump())).changed == 1
        due = service.job(job_request(request, claimed.claim.job_id)).next_due_at
        service._clock = lambda: due
        assert service.claim(worker(request)).claim
    else:
        with env.database.transaction() as connection:
            connection.execute(
                {
                    "guard": "UPDATE processing_guard SET epoch=epoch+1",
                    "worker": "DELETE FROM processing_worker_binding",
                    "grant": "DELETE FROM policy_grant WHERE grant_name='read'",
                }[change]
            )
    with pytest.raises(EvidenceServiceError) as error:
        service.fail(failure(request, claimed))
    assert error.value.failure.code == (
        "forbidden"
        if change in {"worker", "grant"}
        else "state_conflict"
        if change == "replacement"
        else "state_changed"
    )
    if change not in {"worker", "grant"}:
        after = service.job(job_request(request, claimed.claim.job_id))
        assert after.status == (
            "superseded"
            if change == "source"
            else "running"
            if change == "replacement"
            else "blocked"
        )
        assert after.lifetime_attempts == before.lifetime_attempts + (change == "replacement")


@pytest.mark.service
def test_inclusive_failure_expiry_and_clock_watermark(tmp_path: Path) -> None:
    _, service, request = setup(tmp_path / "expired-fail.db")
    service.schedule(request)
    claimed = service.claim(worker(request))
    assert claimed.claim
    service._clock = lambda: claimed.claim.lease_deadline
    with pytest.raises(EvidenceServiceError, match="state_conflict"):
        service.fail(failure(request, claimed))
    service._clock = lambda: claimed.claim.lease_deadline - timedelta(days=1)
    with pytest.raises(EvidenceServiceError, match="state_conflict"):
        service.fail(failure(request, claimed))
    recovered = service.recover(JobsRequest(**worker(request).model_dump()))
    assert recovered.examined == recovered.changed == 1
    assert service.job(job_request(request, claimed.claim.job_id)).status == "retry_wait"


@pytest.mark.service
@pytest.mark.parametrize("reason", ["processor_unavailable", "purge_blocked", "awaiting_input"])
def test_recovery_cannot_launder_owner_only_block(tmp_path: Path, reason: str) -> None:
    env, service, request = setup(tmp_path / "owner-only.db")
    scheduled = service.schedule(request)
    with env.database.transaction() as connection:
        connection.execute(
            "UPDATE processing_job SET status='blocked',coordination_reason=?", (reason,)
        )
    page = JobsRequest(**worker(request).model_dump())
    before = service.job(job_request(request, scheduled.job_id))
    for enabled in (False, True):
        enable_plan(env, enabled)
        result = service.recover(page)
        assert result.changed == 0 and result.unsupported == 1
        assert service.job(job_request(request, scheduled.job_id)) == before
    env.service.write(put(env.scope, state=request.target.state_version, text="Changed"))
    assert service.recover(page).changed == 1
    assert service.job(job_request(request, scheduled.job_id)).status == "superseded"


@pytest.mark.service
def test_recovery_plan_cas_counter_preservation_and_exhaustion(tmp_path: Path) -> None:
    env, service, request = setup(tmp_path / "plan.db")
    service.schedule(request)
    claimed = service.claim(worker(request))
    assert claimed.claim
    enable_plan(env, False)
    with pytest.raises(EvidenceServiceError, match="state_conflict"):
        enable_plan(env, False)
    page = JobsRequest(**worker(request).model_dump())
    assert service.recover(page).changed == 1
    blocked = service.job(job_request(request, claimed.claim.job_id))
    assert blocked.reason == "plan_disabled"
    assert service.recover(page).changed == 0
    assert service.job(job_request(request, claimed.claim.job_id)) == blocked
    enable_plan(env, True)
    assert service.recover(page).changed == 1
    after = service.job(job_request(request, claimed.claim.job_id))
    assert after.lifetime_attempts == after.episode_attempts == 1
    assert after.claim_fence > blocked.claim_fence
    assert after.retry_episode == 1
    assert service.recover(page).changed == 0
    for attempt in range(2, 11):
        claimed = service.claim(worker(request))
        assert claimed.claim and claimed.claim.attempt == attempt
        if attempt < 10:
            failed = service.fail(failure(request, claimed))
            service._clock = lambda due=failed.next_due_at: due
    enable_plan(env, False)
    assert service.recover(page).changed == 1
    enable_plan(env, True)
    assert service.recover(page).changed == 1
    final = service.job(job_request(request, after.job_id))
    assert final.status == "failed" and final.reason == "retry_exhausted"
    assert final.lifetime_attempts == final.episode_attempts == 10


def _process_control(path: str, operation: str, payload: str, at: str) -> str:
    service = ProcessingService(
        EvidenceDatabase(Path(path)), LocalIdentity(principal_id="principal")
    )
    service._clock = lambda: datetime.fromisoformat(at)
    try:
        if operation == "retry":
            return service.retry(RetryRequest.model_validate_json(payload)).model_dump_json()
        if operation == "recover":
            return service.recover(JobsRequest.model_validate_json(payload)).model_dump_json()
        return service.fail(FailRequest.model_validate_json(payload)).model_dump_json()
    except EvidenceServiceError as error:
        return error.failure.code


@pytest.mark.process
def test_two_process_retry_controls_start_only_one_episode(tmp_path: Path) -> None:
    env, service, request = setup(tmp_path / "retry-race.db")
    retry = exhaust(service, request)
    with ProcessPoolExecutor(max_workers=2, mp_context=get_context("spawn")) as pool:
        futures = [
            pool.submit(
                _process_control,
                str(env.database.path),
                "retry",
                retry.model_dump_json(),
                service._clock().isoformat(),
            )
            for _ in range(2)
        ]
        results = [RetryReceipt.model_validate_json(f.result(timeout=30)) for f in futures]
    assert results[0] == results[1]
    assert service.job(job_request(request, retry.job_id)).retry_episode == 2


@pytest.mark.process
def test_two_process_failure_cas_records_only_one_outcome(tmp_path: Path) -> None:
    env, service, request = setup(tmp_path / "fail-race.db")
    service.schedule(request)
    claimed = service.claim(worker(request))
    assert claimed.claim
    with ProcessPoolExecutor(max_workers=2, mp_context=get_context("spawn")) as pool:
        futures = [
            pool.submit(
                _process_control,
                str(env.database.path),
                "fail",
                failure(request, claimed, permanent=permanent).model_dump_json(),
                service._clock().isoformat(),
            )
            for permanent in (False, True)
        ]
        results = [f.result(timeout=30) for f in futures]
    assert results.count("state_conflict") == 1
    assert service.job(job_request(request, claimed.claim.job_id)).status_version == 3


@pytest.mark.process
def test_recovery_and_late_failure_race_cannot_overwrite_reclaimed_attempt(tmp_path: Path) -> None:
    env, service, request = setup(tmp_path / "recover-race.db")
    service.schedule(request)
    claimed = service.claim(worker(request))
    assert claimed.claim
    at = claimed.claim.lease_deadline.isoformat()
    with ProcessPoolExecutor(max_workers=2, mp_context=get_context("spawn")) as pool:
        recovery = pool.submit(
            _process_control,
            str(env.database.path),
            "recover",
            JobsRequest(**worker(request).model_dump()).model_dump_json(),
            at,
        )
        late = pool.submit(
            _process_control,
            str(env.database.path),
            "fail",
            failure(request, claimed, permanent=True).model_dump_json(),
            at,
        )
        assert late.result(timeout=30) == "state_conflict"
        assert '"changed":1' in recovery.result(timeout=30)
    state = service.job(job_request(request, claimed.claim.job_id))
    assert state.status == "retry_wait" and state.episode_attempts == 1


@pytest.mark.service
def test_recovery_pages_and_due_selection_order(tmp_path: Path) -> None:
    env, service, request = setup(tmp_path / "ordering.db")
    first = service.schedule(request)
    accepted = receipt(env.service.write(put(env.scope, external="second")))
    second_request = request.model_copy(
        update={
            "retry_key": "second",
            "target": request.target.model_copy(
                update={
                    "document_id": accepted.document_id,
                    "revision_id": accepted.revision_id,
                    "state_version": accepted.processing.state_version,
                }
            ),
        }
    )
    second = service.schedule(second_request)
    claimed = service.claim(worker(request))
    assert claimed.claim and claimed.claim.job_id == first.job_id
    failed = service.fail(failure(request, claimed))
    next_claim = service.claim(worker(request))
    assert next_claim.claim and next_claim.claim.job_id == second.job_id
    assert service.claim(worker(request)).status == "no_work"
    service._clock = lambda: failed.next_due_at
    assert service.claim(worker(request)).claim.job_id == first.job_id
    enable_plan(env, False)
    page = JobsRequest(**worker(request).model_dump(), limit=1)
    recovered = service.recover(page)
    assert recovered.examined == recovered.changed == 1 and recovered.next_after == 1
    last = service.recover(page.model_copy(update={"after_sequence": recovered.next_after}))
    assert last.examined == last.changed == 1 and last.next_after is None
    enable_plan(env, True)
    assert service.recover(JobsRequest(**worker(request).model_dump())).changed == 2
    assert service.claim(worker(request)).claim.job_id == first.job_id
    assert service.claim(worker(request)).claim.job_id == second.job_id


@pytest.mark.service
@pytest.mark.parametrize("reason", ["plan_disabled", "authority_unavailable"])
def test_actual_policy_rotation_supersedes_recoverable_block(tmp_path: Path, reason: str) -> None:
    env, service, request = setup(tmp_path / "policy.db")
    scheduled = service.schedule(request)
    with env.database.transaction() as connection:
        connection.execute(
            "UPDATE processing_job SET status='blocked',coordination_reason=?", (reason,)
        )
    revoked = env.policy.model_copy(
        update={
            "grants": tuple(g for g in env.policy.grants if g.grant != "read"),
        }
    )
    changed = env.admin.replace_policy(revoked, env.scope.access.policy_version)
    with pytest.raises(EvidenceServiceError, match="forbidden"):
        service.recover(JobsRequest(**worker(request).model_dump()))
    restored = env.admin.replace_policy(env.policy, changed.policy_version)
    scope = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(
                update={"policy_version": restored.policy_version}
            ),
        }
    )
    current = request.model_copy(update={"scope": scope})
    assert service.recover(JobsRequest(**worker(current).model_dump())).changed == 1
    assert service.job(job_request(current, scheduled.job_id)).status == "superseded"


@pytest.mark.service
def test_retry_replay_requires_original_worker_and_all_retained_targets(tmp_path: Path) -> None:
    env, service, request = setup(tmp_path / "retained.db")
    retry = exhaust(service, request)
    result = service.retry_explained(retry)
    assert isinstance(result.report, ExecutionReport)
    second = request.selection.model_copy(update={"worker_id": "replacement"})
    ProcessingAdministration(env.database, env.admin.authority).register_worker(
        WorkerRegistration(corpus_id="work", principal_id="principal", selection=second)
    )
    with env.database.transaction() as connection:
        connection.execute("DELETE FROM processing_worker_binding WHERE worker_id=?", ("worker",))
    with pytest.raises(EvidenceServiceError, match="forbidden"):
        service.retry(retry.model_copy(update={"selection": second}))
    assert not isinstance(
        service.diagnostics.report(request.scope, result.report.report_id), ExecutionReport
    )
    assert (
        service.job(
            job_request(request.model_copy(update={"selection": second}), retry.job_id)
        ).retry_episode
        == 2
    )


@pytest.mark.service
@pytest.mark.parametrize("operation", ["fail", "retry", "recover"])
def test_new_control_business_rollback_and_diagnostic_failure_independence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    from kg.diagnostics._collector import Capture
    from kg.processing import _controls

    env, service, request = setup(tmp_path / "rollback.db")
    if operation == "retry":
        control = exhaust(service, request)
        job_id = control.job_id
    else:
        job_id = service.schedule(request).job_id
        claimed = service.claim(worker(request))
        assert claimed.claim
        if operation == "fail":
            control = failure(request, claimed)
        else:
            service._clock = lambda: claimed.claim.lease_deadline
            control = JobsRequest(**worker(request).model_dump())
    before = service.job(job_request(request, job_id))
    original = getattr(_controls, operation)

    def fail_after_mutation(*args, **kwargs):
        original(*args, **kwargs)
        raise MemoryError("business failure")

    monkeypatch.setattr(_controls, operation, fail_after_mutation)
    with pytest.raises(MemoryError, match="business failure"):
        getattr(service, operation)(control)
    assert service.job(job_request(request, job_id)) == before
    with env.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM processing_key WHERE operation='retry'"
            ).fetchone()[0]
            == 0
        )
    monkeypatch.setattr(_controls, operation, original)

    def failed_append(*args, **kwargs):
        raise MemoryError

    monkeypatch.setattr(Capture, "append", failed_append)
    getattr(service, operation)(control)
    assert service.job(job_request(request, job_id)).status_version == before.status_version + 1


@pytest.mark.service
def test_control_request_validation_rejects_unapproved_failure_classes(tmp_path: Path) -> None:
    _, service, request = setup(tmp_path / "strict.db")
    service.schedule(request)
    claimed = service.claim(worker(request))
    transient = failure(request, claimed)
    for update in (
        {"failure_code": "internal_error"},
        {"failure_class": "permanent"},
        {"failure_class": "provider_exception"},
        {"claim_fence": True},
    ):
        with pytest.raises(EvidenceServiceError, match="invalid_request"):
            service.fail(transient.model_copy(update=update))
    with pytest.raises(EvidenceServiceError, match="invalid_request"):
        service.recover(
            JobsRequest(**worker(request).model_dump()).model_copy(update={"limit": 201})
        )


@pytest.mark.service
@pytest.mark.parametrize("explained", [False, True])
def test_failure_diagnostic_correlates_actual_capture_only(tmp_path: Path, explained: bool) -> None:
    env, service, request = setup(tmp_path / "failure-report.db")
    service.schedule(request)
    claimed = service.claim(worker(request))
    if explained:
        result = service.fail_explained(failure(request, claimed))
        state = result.outcome
        assert isinstance(result.report, ExecutionReport)
        assert result.report.diagnostic_id == state.diagnostic_id
    else:
        state = service.fail(failure(request, claimed))
    assert state.diagnostic_id is not None
    report = service.diagnostics.report(request.scope, state.diagnostic_id)
    assert isinstance(report, ExecutionReport)
    assert report.operation == "fail" and report.observation_kind == "captured_execution"
    assert report.diagnostic_id == state.diagnostic_id
    inspected = service.job_explained(job_request(request, state.job_id))
    assert inspected.outcome == state
    assert isinstance(inspected.report, ExecutionReport)
    assert inspected.report.diagnostic_id is None
    assert service.diagnostics.report(request.scope, state.diagnostic_id) == report
    restarted = ProcessingService(EvidenceDatabase(env.database.path), env.service.identity)
    assert not isinstance(
        restarted.diagnostics.report(request.scope, state.diagnostic_id), ExecutionReport
    )
    enable_plan(env, False)
    assert not isinstance(
        service.diagnostics.report(request.scope, state.diagnostic_id), ExecutionReport
    )
    enable_plan(env, True)
    assert not isinstance(
        service.diagnostics.report(request.scope, state.diagnostic_id), ExecutionReport
    )


@pytest.mark.service
def test_schedule_registration_and_restart(tmp_path: Path) -> None:
    env, service, request = setup(tmp_path / "store.db")
    admin = ProcessingAdministration(env.database, LocalAdminAuthority(principal_id="admin"))
    plan = PlanRegistration(
        corpus_id="work",
        plan_id="index",
        plan_version="1",
        producer="test",
        producer_version="1",
    )
    assert admin.register_plan(plan).status == "unchanged"
    assert (
        admin.register_worker(
            WorkerRegistration(
                corpus_id="work",
                principal_id="principal",
                selection=SELECTION,
            )
        ).status
        == "unchanged"
    )
    with pytest.raises(EvidenceServiceError, match="state_conflict"):
        admin.register_plan(plan.model_copy(update={"producer_version": "2"}))
    with pytest.raises(EvidenceServiceError, match="not_found"):
        admin.register_plan(
            plan.model_copy(update={"plan_version": "2", "configuration_id": "absent"})
        )
    first = service.schedule(request)
    assert first.status == "applied"
    assert service.schedule(request.model_copy(update={"request_id": "again"})) == first
    duplicate = service.schedule(request.model_copy(update={"retry_key": "new-key"}))
    assert duplicate.job_id == first.job_id and duplicate.status == "unchanged"
    reopened = ProcessingService(EvidenceDatabase(env.database.path), env.service.identity)
    result = reopened.claim(worker(request))
    assert result.claim and result.claim.job_id == first.job_id
    assert service.claim(worker(request)).status == "no_work"
    current = service.job(job_request(request, first.job_id))
    assert current.status == "running" and current.episode_attempts == 1
    assert not service.capabilities().executes_work
    with env.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM processing_job").fetchone()[0] == 1
        assert (
            connection.execute("SELECT indexing FROM processing_state").fetchone()[0] == "pending"
        )
        assert connection.execute("SELECT COUNT(*) FROM write_key").fetchone()[0] == 1


@pytest.mark.service
def test_inclusive_expiry_backoff_fence_and_attempt_cap(tmp_path: Path) -> None:
    env, service, request = setup(tmp_path / "store.db")
    scheduled = service.schedule(request)
    claimed = service.claim(worker(request))
    first_heartbeat = heartbeat(request, claimed)
    assert claimed.claim
    for attempt in range(1, 11):
        assert claimed.claim and claimed.claim.attempt == attempt
        deadline = claimed.claim.lease_deadline
        service._clock = lambda deadline=deadline: deadline
        with pytest.raises(EvidenceServiceError, match="state_conflict"):
            service.heartbeat(heartbeat(request, claimed))
        recovery = service.claim(worker(request))
        assert recovery.status == "no_work"
        current = service.job(job_request(request, scheduled.job_id))
        assert current.status == ("failed" if attempt == 10 else "retry_wait")
        assert current.reason == ("retry_exhausted" if attempt == 10 else "retry_scheduled")
        if attempt == 10:
            break
        due = deadline + timedelta(seconds=min(300, 2 ** (attempt - 1)))
        assert current.next_due_at == due
        service._clock = lambda due=due: due - timedelta(microseconds=1)
        assert service.claim(worker(request)).status == "no_work"
        service._clock = lambda due=due: due
        claimed = service.claim(worker(request))
        with pytest.raises(EvidenceServiceError, match="state_conflict"):
            service.heartbeat(first_heartbeat)
    service._clock = lambda: due + timedelta(days=100)
    assert service.claim(worker(request)).status == "no_work"
    assert service.job(job_request(request, scheduled.job_id)).lifetime_attempts == 10


@pytest.mark.service
def test_backward_clock_never_revives_and_heartbeat_renews(tmp_path: Path) -> None:
    _, service, request = setup(tmp_path / "store.db")
    service.schedule(request)
    claimed = service.claim(worker(request))
    assert claimed.claim
    at = claimed.claim.lease_deadline - timedelta(seconds=40)
    service._clock = lambda: at
    updated = service.heartbeat(heartbeat(request, claimed))
    assert updated.lease_deadline == at + timedelta(seconds=60)
    service._clock = lambda: updated.lease_deadline
    with pytest.raises(EvidenceServiceError, match="state_conflict"):
        service.heartbeat(heartbeat(request, claimed))
    service._clock = lambda: at - timedelta(days=1)
    with pytest.raises(EvidenceServiceError, match="state_conflict"):
        service.heartbeat(heartbeat(request, claimed))
    assert service.claim(worker(request)).status == "no_work"


@pytest.mark.service
def test_schedule_retained_receipt_expiry_precedes_digest(tmp_path: Path) -> None:
    env, service, request = setup(tmp_path / "store.db")
    original = service.schedule(request)
    # Currentness belongs to NEW scheduling, not historical control receipt retrieval.
    env.service.write(put(env.scope, state=request.target.state_version, text="Changed"))
    assert service.schedule(request) == original
    forged = request.model_copy(
        update={
            "target": request.target.model_copy(update={"state_version": "not-a-state"}),
        }
    )
    with pytest.raises(EvidenceServiceError, match="retry_conflict"):
        service.schedule(forged)
    with env.database.connection() as connection:
        expiry = datetime.fromisoformat(
            connection.execute(
                "SELECT expires_at FROM processing_key",
            ).fetchone()[0]
        )
    service._clock = lambda: expiry
    with pytest.raises(EvidenceServiceError, match="retry_expired"):
        service.schedule(forged)
    with env.database.connection() as connection:
        assert connection.execute("SELECT expired FROM processing_key").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM processing_response").fetchone()[0] == 0
    service._clock = lambda: expiry - timedelta(days=40)
    with pytest.raises(EvidenceServiceError, match="retry_expired"):
        service.schedule(request)


@pytest.mark.service
@pytest.mark.parametrize("change", ["source", "guard", "plan"])
def test_live_claim_guards(tmp_path: Path, change: str) -> None:
    env, service, request = setup(tmp_path / "store.db")
    scheduled = service.schedule(request)
    claimed = service.claim(worker(request))
    if change == "source":
        env.service.write(put(env.scope, state=request.target.state_version, text="Changed"))
    else:
        with env.database.transaction() as connection:
            connection.execute(
                "UPDATE processing_guard SET epoch=epoch+1"
                if change == "guard"
                else "UPDATE processing_plan SET enabled=0",
            )
    with pytest.raises(EvidenceServiceError, match="state_changed"):
        service.heartbeat(heartbeat(request, claimed))
    with env.database.connection() as connection:
        row = connection.execute(
            "SELECT status,worker_id,lease_deadline FROM processing_job WHERE job_id=?",
            (scheduled.job_id,),
        ).fetchone()
        assert row[0] == ("superseded" if change == "source" else "blocked")
        assert row[1] is None and row[2] is None


@pytest.mark.service
@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_id", "other"),
        ("worker_id", "other"),
        ("plan_version", "other"),
        ("namespace", "email"),
        ("writer_id", "other"),
    ],
)
def test_exact_selection_and_no_result_reports(
    tmp_path: Path,
    field: str,
    value: str,
) -> None:
    _, service, request = setup(tmp_path / "store.db")
    denied = worker(request).model_copy(
        update={
            "selection": SELECTION.model_copy(update={field: value}),
        }
    )
    with pytest.raises(EvidenceServiceError, match="forbidden"):
        service.claim(denied)
    assert not service.diagnostics.for_request(request.scope, denied.request_id).entries


@pytest.mark.service
@pytest.mark.parametrize("operation", ["claim", "jobs"])
@pytest.mark.parametrize("revocation", ["worker", "grant", "plan"])
def test_empty_report_fresh_authorization(
    tmp_path: Path,
    operation: str,
    revocation: str,
) -> None:
    env, service, request = setup(tmp_path / "store.db")
    report = (
        service.claim_explained(worker(request)).report
        if operation == "claim"
        else service.jobs_explained(JobsRequest(**worker(request).model_dump())).report
    )
    assert isinstance(report, ExecutionReport)
    assert service.diagnostics.report(request.scope, report.report_id) == report
    with env.database.transaction() as connection:
        if revocation == "worker":
            connection.execute("DELETE FROM processing_worker_binding")
        elif revocation == "grant":
            connection.execute("DELETE FROM policy_grant WHERE grant_name='read'")
        else:
            connection.execute("UPDATE processing_plan SET enabled=0")
    assert not isinstance(
        service.diagnostics.report(request.scope, report.report_id), ExecutionReport
    )
    assert not service.diagnostics.recent(request.scope).entries


@pytest.mark.service
def test_reports_observe_real_execution_and_current_status(tmp_path: Path) -> None:
    env, service, request = setup(tmp_path / "store.db")
    result = service.schedule_explained(request)
    assert isinstance(result.report, ExecutionReport)
    assert any(
        item.event.kind == "execution.commit" and item.event.observation == "confirmed_committed"
        for item in result.report.events
    )
    replay = service.schedule_explained(request)
    assert isinstance(replay.report, ExecutionReport)
    assert any(
        item.event.observation_kind == "retained_commit_fact"
        for item in replay.report.events
        if hasattr(item.event, "observation_kind")
    )
    with env.database.connection() as connection:
        before = connection.execute("SELECT watermark FROM receipt_clock").fetchone()[0]
    inspected = service.job_explained(job_request(request, result.outcome.job_id))
    assert isinstance(inspected.report, ExecutionReport)
    assert inspected.report.observation_kind == "current_inspection"
    assert all(item.event.kind != "execution.commit" for item in inspected.report.events)
    service.diagnostics.recent(request.scope)
    with env.database.connection() as connection:
        assert connection.execute("SELECT watermark FROM receipt_clock").fetchone()[0] == before
    forged_scope = request.scope.model_copy(
        update={
            "access": request.scope.access.model_copy(update={"principal_id": "other"}),
        }
    )
    assert not isinstance(
        service.diagnostics.report(forged_scope, result.report.report_id),
        ExecutionReport,
    )
    stranger = ProcessingService(env.database, LocalIdentity(principal_id="other"))
    with pytest.raises(EvidenceServiceError, match="forbidden"):
        stranger.claim(worker(request))


@pytest.mark.service
def test_strict_values_and_selection_target(tmp_path: Path) -> None:
    _, service, request = setup(tmp_path / "store.db")
    target = ProcessingSelectionTarget(**SELECTION.model_dump())
    values = ReportTargets(values=(target,))
    assert ReportTargets.model_validate_json(values.model_dump_json()) == values
    with pytest.raises(ValidationError):
        ProcessingSelectionTarget(**SELECTION.model_dump(), invented=True)
    with pytest.raises(ValidationError):
        ProcessingSelectionTarget(**(SELECTION.model_dump() | {"worker_id": 1}))
    with pytest.raises(EvidenceServiceError, match="invalid_request"):
        service.claim(worker(request).model_copy(update={"request_id": 123}))
    with pytest.raises(EvidenceServiceError, match="invalid_request"):
        service.heartbeat(
            HeartbeatRequest.model_construct(
                **job_request(request, "unknown").model_dump(),
                claim_fence=True,
            )
        )
    assert service.jobs(JobsRequest(**worker(request).model_dump())).entries == ()


def _process_claim(path: str, payload: str) -> str:
    service = ProcessingService(
        EvidenceDatabase(Path(path)), LocalIdentity(principal_id="principal")
    )
    return service.claim(WorkerRequest.model_validate_json(payload)).model_dump_json()


@pytest.mark.process
def test_two_processes_cannot_claim_same_job(tmp_path: Path) -> None:
    env, service, request = setup(tmp_path / "store.db")
    service.schedule(request)
    with ProcessPoolExecutor(max_workers=2, mp_context=get_context("spawn")) as pool:
        futures = [
            pool.submit(_process_claim, str(env.database.path), worker(request).model_dump_json())
            for _ in range(2)
        ]
        results = [ClaimResult.model_validate_json(f.result(timeout=30)) for f in futures]
    assert sorted(r.status for r in results) == ["claimed", "no_work"]


@pytest.mark.service
def test_diagnostic_allocation_failure_does_not_suppress_business(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kg.diagnostics._collector import Capture

    _, service, request = setup(tmp_path / "store.db")

    def failed_append(*args: object, **kwargs: object) -> bool:
        raise MemoryError

    monkeypatch.setattr(Capture, "append", failed_append)
    result = service.schedule(request)
    assert service.job(job_request(request, result.job_id)).status == "queued"


@pytest.mark.service
def test_ordinary_summary_and_opt_in_details(tmp_path: Path) -> None:
    _, service, request = setup(tmp_path / "store.db")
    service.claim(worker(request))
    headers = service.diagnostics.for_request(request.scope, "claim")
    assert len(headers.entries) == 1
    detailed = service.claim_explained(worker(request), ExplainOptions(detail="detailed"))
    assert isinstance(detailed.report, ExecutionReport)
    assert detailed.report.capture_level == "detailed"


@pytest.mark.service
def test_restore_new_state_is_distinct_work_and_pages_are_bounded(tmp_path: Path) -> None:
    env, service, request = setup(tmp_path / "store.db")
    first = service.schedule(request)
    changed = receipt(
        env.service.write(
            put(env.scope, state=request.target.state_version, text="different"),
        )
    )
    restored = receipt(env.service.write(put(env.scope, state=changed.processing.state_version)))
    assert restored.revision_id == request.target.revision_id
    second_request = request.model_copy(
        update={
            "retry_key": "restored",
            "target": request.target.model_copy(
                update={"state_version": restored.processing.state_version}
            ),
        }
    )
    second = service.schedule(second_request)
    assert first.job_id != second.job_id
    assert service.claim(worker(request)).claim.job_id == second.job_id
    assert service.job(job_request(request, first.job_id)).status == "superseded"
    page = service.jobs(JobsRequest(**worker(request).model_dump(), limit=1))
    assert len(page.entries) == 1 and page.next_after == page.entries[0].creation_sequence
    last = service.jobs(
        JobsRequest(
            **worker(request).model_dump(),
            after_sequence=page.next_after,
            limit=1,
        )
    )
    assert last.entries[0].job_id == second.job_id and last.next_after is None


@pytest.mark.service
def test_cross_corpus_document_and_job_isolation(tmp_path: Path) -> None:
    env, service, request = setup(tmp_path / "store.db")
    other = environment(env.database.path, corpus="other")
    foreign = receipt(other.service.write(put(other.scope)))
    wrong = request.model_copy(
        update={
            "target": request.target.model_copy(
                update={
                    "document_id": foreign.document_id,
                    "revision_id": foreign.revision_id,
                    "state_version": foreign.processing.state_version,
                }
            )
        }
    )
    with pytest.raises(EvidenceServiceError, match="not_found"):
        service.schedule(wrong)
    result = service.schedule(request)
    with pytest.raises(EvidenceServiceError, match="forbidden"):
        service.job(job_request(request, result.job_id).model_copy(update={"scope": other.scope}))
    with env.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM processing_job").fetchone()[0] == 1


@pytest.mark.service
def test_inherited_budget_and_business_failure_roll_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import time

    from kg._execution_budget import Deadline, PrivateBudget
    from kg.processing import _core

    env, service, request = setup(tmp_path / "store.db")
    exhausted = PrivateBudget(Deadline(time.monotonic() + 5)).limited(max_visits=1)
    with pytest.raises(EvidenceServiceError, match="budget_exceeded"):
        service._write("schedule", request, _core.schedule, budget=exhausted)
    with env.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM processing_job").fetchone()[0] == 0
    service.schedule(request)
    original = _core.claim

    def fail_after_claim(*args, **kwargs):
        original(*args, **kwargs)
        raise MemoryError("business failure")

    monkeypatch.setattr(_core, "claim", fail_after_claim)
    with pytest.raises(MemoryError, match="business failure"):
        service.claim(worker(request))
    with env.database.connection() as connection:
        row = connection.execute("SELECT status,episode_attempts FROM processing_job").fetchone()
        assert tuple(row) == ("queued", 0)


@pytest.mark.service
def test_immediate_report_revocation_and_irreversible_redaction(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kg.diagnostics._collector import Capture

    env, service, request = setup(tmp_path / "store.db")
    saved = service.claim_explained(worker(request)).report
    assert isinstance(saved, ExecutionReport)
    original = Capture.finish

    def revoke_before_release(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        with env.database.transaction() as connection:
            connection.execute("DELETE FROM processing_worker_binding")
        return result

    monkeypatch.setattr(Capture, "finish", revoke_before_release)
    result = service.claim_explained(worker(request))
    assert result.outcome.status == "no_work"
    assert not isinstance(result.report, ExecutionReport)
    assert not isinstance(
        service.diagnostics.report(request.scope, saved.report_id), ExecutionReport
    )
    ProcessingAdministration(
        env.database,
        LocalAdminAuthority(principal_id="admin"),
    ).register_worker(
        WorkerRegistration(
            corpus_id="work",
            principal_id="principal",
            selection=SELECTION,
        )
    )
    assert not isinstance(
        service.diagnostics.report(request.scope, saved.report_id), ExecutionReport
    )


@pytest.mark.service
def test_other_owner_rejects_processing_selection(tmp_path: Path) -> None:
    from kg.diagnostics._targets import AuthorizationBinding
    from kg.evidence._diagnostic_authorization import EvidenceReportAuthorizer

    env, _, request = setup(tmp_path / "store.db")
    target = ProcessingSelectionTarget(**SELECTION.model_dump())
    binding = AuthorizationBinding(env.service.identity, request.scope, "read", [target])
    with (
        pytest.raises(EvidenceServiceError, match="unsupported"),
        EvidenceReportAuthorizer(env.database).fence((binding,), ()),
    ):
        pytest.fail("E1 cannot authorize E4 selection")


def _process_heartbeat(path: str, payload: str, at: str) -> str:
    service = ProcessingService(
        EvidenceDatabase(Path(path)), LocalIdentity(principal_id="principal")
    )
    service._clock = lambda: datetime.fromisoformat(at)
    try:
        service.heartbeat(HeartbeatRequest.model_validate_json(payload))
        return "renewed"
    except EvidenceServiceError as error:
        return error.failure.code


def _process_reclaim(path: str, payload: str, at: str) -> str:
    service = ProcessingService(
        EvidenceDatabase(Path(path)), LocalIdentity(principal_id="principal")
    )
    service._clock = lambda: datetime.fromisoformat(at)
    return service.claim(WorkerRequest.model_validate_json(payload)).status


@pytest.mark.process
def test_heartbeat_reclaim_race_has_one_serialized_outcome(tmp_path: Path) -> None:
    env, service, request = setup(tmp_path / "store.db")
    scheduled = service.schedule(request)
    claimed = service.claim(worker(request))
    assert claimed.claim
    deadline = claimed.claim.lease_deadline
    with ProcessPoolExecutor(max_workers=2, mp_context=get_context("spawn")) as pool:
        renewal = pool.submit(
            _process_heartbeat,
            str(env.database.path),
            heartbeat(request, claimed).model_dump_json(),
            (deadline - timedelta(microseconds=1)).isoformat(),
        )
        reclaim = pool.submit(
            _process_reclaim,
            str(env.database.path),
            worker(request).model_dump_json(),
            deadline.isoformat(),
        )
        outcome = renewal.result(timeout=30)
        assert reclaim.result(timeout=30) == "no_work"
    current = service.job(job_request(request, scheduled.job_id))
    assert current.status == ("running" if outcome == "renewed" else "retry_wait")
    assert outcome in {"renewed", "state_conflict"}


@pytest.mark.service
def test_claim_scan_bound_cannot_hide_remaining_due_work(tmp_path: Path) -> None:
    env, service, request = setup(tmp_path / "store.db")
    for i in range(201):
        # Real plans/workers/schedules, same document; distinct plan versions
        # are not interchangeable, so use distinct current document identities.
        item = receipt(env.service.write(put(env.scope, external=f"doc-{i}")))
        target = request.target.model_copy(
            update={
                "document_id": item.document_id,
                "revision_id": item.revision_id,
                "state_version": item.processing.state_version,
            }
        )
        service.schedule(request.model_copy(update={"target": target, "retry_key": f"key-{i}"}))
    with env.database.transaction() as connection:
        connection.execute("UPDATE processing_guard SET blocked=1")
    result = service.claim(worker(request))
    assert result.inspected == 200 and result.scan_truncated and result.status == "no_work"
    tail = service.claim(worker(request))
    assert tail.inspected == 1 and not tail.scan_truncated
    with env.database.connection() as connection:
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM processing_job WHERE status='blocked'",
            ).fetchone()[0]
            == 201
        )


@pytest.mark.service
def test_cursor_sqlite_integer_boundary(tmp_path: Path) -> None:
    _, service, request = setup(tmp_path / "store.db")
    assert (
        service.jobs(
            JobsRequest(
                **worker(request).model_dump(),
                after_sequence=2**63 - 1,
            )
        ).entries
        == ()
    )
    with pytest.raises(ValidationError):
        JobsRequest(**worker(request).model_dump(), after_sequence=2**63)
    with pytest.raises(EvidenceServiceError, match="invalid_request"):
        service.jobs(
            JobsRequest.model_construct(
                **worker(request).model_dump(),
                after_sequence=2**63,
            )
        )


@pytest.mark.service
@pytest.mark.parametrize("operation", ["plan", "worker"])
@pytest.mark.parametrize("failure", ["deadline", "resource"])
@pytest.mark.parametrize("phase", ["entry", "after_mutation"])
def test_provisioning_budget_errors_and_rollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    failure: str,
    phase: str,
) -> None:
    from contextlib import contextmanager

    from kg._execution_budget import DeadlineStop, PrivateResourceStop
    from kg.processing import administration

    env, _, _ = setup(tmp_path / "store.db")
    admin = ProcessingAdministration(env.database, LocalAdminAuthority(principal_id="admin"))
    original = administration.writing
    error = DeadlineStop if failure == "deadline" else PrivateResourceStop

    @contextmanager
    def interrupted(*args, **kwargs):
        if phase == "entry":
            raise error()
        with original(*args, **kwargs) as context:
            yield context
            raise error()

    monkeypatch.setattr(administration, "writing", interrupted)
    with pytest.raises(EvidenceServiceError, match="budget_exceeded"):
        if operation == "plan":
            admin.register_plan(
                PlanRegistration(
                    corpus_id="work",
                    plan_id="new",
                    plan_version="1",
                    producer="test",
                    producer_version="1",
                )
            )
        else:
            admin.register_worker(
                WorkerRegistration(
                    corpus_id="work",
                    principal_id="principal",
                    selection=SELECTION.model_copy(update={"worker_id": "new"}),
                )
            )
    with env.database.connection() as connection:
        assert connection.execute("SELECT COUNT(*) FROM processing_plan").fetchone()[0] == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM processing_worker_binding").fetchone()[0] == 1
        )
