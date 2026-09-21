"""Actual E4 control commits against delivered Q1 observers and E3 evidence."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from support.evidence import environment, put, receipt

from kg.diagnostics._targets import (
    AuthorizationBinding,
    EvidenceTarget,
    ProcessingSelectionTarget,
)
from kg.evidence._diagnostic_authorization import EvidenceReportAuthorizer
from kg.evidence.errors import EvidenceServiceError
from kg.indexing._passages import produce
from kg.models.execution import ExecutionReport, ExplainOptions
from kg.models.foundation import EvidenceStep, QueryBudget, QueryRequest
from kg.models.processing import (
    DocumentTarget,
    HeartbeatRequest,
    JobRequest,
    JobsRequest,
    PlanRegistration,
    ScheduleRequest,
    WorkerRegistration,
    WorkerRequest,
    WorkerSelection,
)
from kg.processing import ProcessingAdministration, ProcessingService
from kg.query import QueryService


@pytest.fixture
def combined(tmp_path: Path):
    env = environment(tmp_path / "combined.db")
    supplied = put(env.scope)
    supplied = supplied.model_copy(
        update={
            "payload": supplied.payload.model_copy(
                update={
                    "content": supplied.payload.content.model_copy(
                        update={
                            "passage_policy": "supplied-anchors/1",
                        }
                    ),
                }
            ),
        }
    )
    saved = receipt(env.service.write(supplied))
    produce(
        env.database,
        env.service.identity,
        env.scope,
        supplied.attribution,
        saved.document_id,
        saved.processing.state_version,
    )
    passage = env.service.passages(
        env.scope,
        saved.document_id,
        saved.processing.state_version,
    ).entries[0]
    selection = WorkerSelection(
        namespace="markdown",
        owner_id="owner",
        writer_id="writer",
        plan_id="index",
        plan_version="1",
        worker_id="worker",
    )
    admin = ProcessingAdministration(env.database, env.admin.authority)
    admin.register_plan(
        PlanRegistration(
            corpus_id=env.scope.corpus_id,
            plan_id="index",
            plan_version="1",
            producer="integration",
            producer_version="1",
        )
    )
    admin.register_worker(
        WorkerRegistration(
            corpus_id=env.scope.corpus_id,
            principal_id=env.service.identity.principal_id,
            selection=selection,
        )
    )
    with env.database.connection() as connection:
        token = connection.execute(
            "SELECT namespace_token FROM document_state WHERE state_version=?",
            (saved.processing.state_version,),
        ).fetchone()[0]
    scheduled = ScheduleRequest(
        scope=env.scope,
        selection=selection,
        request_id="schedule",
        retry_key="scheduled",
        target=DocumentTarget(
            document_id=saved.document_id,
            revision_id=saved.revision_id,
            state_version=saved.processing.state_version,
            namespace_token=token,
        ),
    )
    processing = ProcessingService(env.database, env.service.identity)
    query = QueryRequest(
        contract_version="foundation/1",
        request_id="query",
        scope=env.scope,
        steps=(
            EvidenceStep(
                operation="evidence",
                step_id="e",
                evidence=passage.reference.model_copy(update={"passage_id": None}),
            ),
        ),
        output_step="e",
        budget=QueryBudget(max_milliseconds=30_000),
    )
    with QueryService(env.database, env.service.identity) as service:
        yield env, processing, scheduled, service, query, passage


@pytest.mark.parametrize("operation", ["schedule", "heartbeat", "inspection"])
def test_processing_commit_between_real_query_read_and_original_release(
    combined,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    env, processing, scheduled, query, request, _ = combined
    worker = WorkerRequest(
        scope=env.scope,
        selection=scheduled.selection,
        request_id="claim",
    )
    claim = None
    if operation != "schedule":
        processing.schedule(scheduled)
        claim = processing.claim(worker).claim
        assert claim is not None
    read_finished, continue_release = Event(), Event()
    original = query._run_worker
    original_observer_changed = []

    def gate(observer, step, ledger):
        # Execute the real spawned child/read/accounting before the competing commit.
        elapsed = original(observer, step, ledger)
        read_finished.set()
        assert continue_release.wait(15), "processing call did not release the query gate"
        original_observer_changed.append(observer.changed())
        return elapsed

    monkeypatch.setattr(query, "_run_worker", gate)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(query.execute_explained, request, ExplainOptions(detail="detailed"))
        try:
            assert read_finished.wait(15), "real spawned query did not finish reading"
            if operation == "schedule":
                result = processing.schedule(scheduled)
                assert result.status == "applied"
                inspected = processing.job(
                    JobRequest(
                        **worker.model_dump(),
                        job_id=result.job_id,
                    )
                )
                assert inspected.status == "queued"
            else:
                assert claim is not None
                before = processing.job(
                    JobRequest(
                        **worker.model_dump(),
                        job_id=claim.job_id,
                    )
                )
                if operation == "heartbeat":
                    processing.heartbeat(
                        HeartbeatRequest(
                            **worker.model_dump(),
                            job_id=claim.job_id,
                            claim_fence=claim.claim_fence,
                        )
                    )
                after = processing.job(
                    JobRequest(
                        **worker.model_dump(),
                        job_id=claim.job_id,
                    )
                )
                assert after.status_version == before.status_version + (operation == "heartbeat")
        finally:
            continue_release.set()
        explained = pending.result(timeout=15)
    changed = operation != "inspection"
    assert original_observer_changed == [changed]
    if changed:
        assert explained.outcome.stop_reason == "state_changed"
        assert explained.outcome.work_accounting == "redacted"
        assert explained.outcome.steps == ()
        result = explained.outcome.result
        assert result.data is result.read_state_id is result.result_set_id is None
        assert result.operations_executed == result.records_examined == 0
        assert explained.report.state == "redacted"
        assert query.diagnostics.for_request(env.scope, request.request_id).entries == ()
        # A fresh observation may succeed; it cannot restore the withheld invocation.
        monkeypatch.setattr(query, "_run_worker", original)
        later = query.execute_explained(request)
        assert later.outcome.result.outcome == "complete"
        assert isinstance(later.report, ExecutionReport)
        assert later.report.execution_id != explained.report.execution_id
    else:
        assert explained.outcome.result.outcome == "complete"
        assert isinstance(explained.report, ExecutionReport)
    with env.database.connection() as connection:
        assert tuple(
            connection.execute(
                "SELECT indexing,enrichment FROM processing_state WHERE state_version=?",
                (scheduled.target.state_version,),
            ).fetchone()
        ) == ("pending", "pending")


@pytest.mark.parametrize("operation", ["claim", "jobs"])
def test_empty_processing_reports_and_passage_reports_keep_separate_authority(
    combined,
    operation: str,
) -> None:
    env, processing, scheduled, query, query_request, passage = combined
    worker = WorkerRequest(
        scope=env.scope,
        selection=scheduled.selection,
        request_id="empty",
    )
    empty = (
        processing.claim_explained(worker)
        if operation == "claim"
        else processing.jobs_explained(JobsRequest(**worker.model_dump()))
    )
    if operation == "claim":
        assert empty.outcome.status == "no_work"
    else:
        assert empty.outcome.entries == ()
    assert isinstance(empty.report, ExecutionReport)
    source = env.service.evidence_explained(
        env.scope,
        passage.reference,
        ExplainOptions(detail="detailed", include_quotes=True),
    )
    assert source.outcome.reference.passage_id is not None
    assert isinstance(source.report, ExecutionReport)
    assert any(event.event.kind == "execution.quote" for event in source.report.events)
    anchor = query.execute_explained(query_request)
    assert anchor.outcome.result.outcome == "complete"
    assert isinstance(anchor.report, ExecutionReport)
    assert processing.diagnostics.for_request(env.scope, "empty").entries
    assert isinstance(
        env.service.diagnostics.report(env.scope, source.report.report_id),
        ExecutionReport,
    )
    assert not processing.capabilities().acknowledges_work
    assert query.capabilities(env.scope).operations == ("evidence",)

    # A valid new union member still grants no authority to another owning service.
    selection = ProcessingSelectionTarget(**scheduled.selection.model_dump())
    binding = AuthorizationBinding(env.service.identity, env.scope, "read", [selection])
    with (
        pytest.raises(EvidenceServiceError, match="unsupported"),
        EvidenceReportAuthorizer(env.database).fence((binding,), ()),
    ):
        pytest.fail("passage-aware E1 authorizer accepted an E4 selection")
    source_binding = AuthorizationBinding(
        env.service.identity,
        env.scope,
        "read",
        [EvidenceTarget(reference=passage.reference)],
    )
    with (
        pytest.raises(EvidenceServiceError, match="unsupported"),
        processing._authorizer.fence((source_binding,), ()),
    ):
        pytest.fail("scheduler-only E4 authorizer accepted an evidence target")

    with env.database.transaction() as connection:
        connection.execute(
            "DELETE FROM processing_worker_binding WHERE corpus_id=? AND worker_id=?",
            (env.scope.corpus_id, scheduled.selection.worker_id),
        )
    assert not isinstance(
        processing.diagnostics.report(env.scope, empty.report.report_id),
        ExecutionReport,
    )
    assert processing.diagnostics.for_request(env.scope, "empty").entries == ()
    # E4 registration loss does not revoke E1 passage authority; Q1's original
    # observation is invalidated by any canonical commit, regardless of target.
    assert isinstance(
        env.service.diagnostics.report(env.scope, source.report.report_id),
        ExecutionReport,
    )
    assert not isinstance(
        query.diagnostics.report(env.scope, anchor.report.report_id),
        ExecutionReport,
    )
    passage_request = query_request.model_copy(
        update={
            "steps": (
                query_request.steps[0].model_copy(
                    update={"evidence": passage.reference},
                ),
            )
        }
    )
    executed = query.execute_explained(passage_request)
    assert executed.outcome.result.outcome == "complete"
    assert executed.outcome.result.data.records[0].support.evidence == (passage.reference,)
    assert executed.outcome.result.records_examined == 1
    assert isinstance(executed.report, ExecutionReport)
    assert [
        (event.event.stage, event.event.reservations)
        for event in executed.report.events
        if event.event.kind == "query.budget"
    ] == [("evidence_reference", 1)]
