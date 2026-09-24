from __future__ import annotations

import time

import pytest
from support.evidence import environment, put, receipt
from support.query_workers import (
    die_after_hydration,
    partial_frame_after_hydration,
    repeated_local_hydration,
)

from kg._execution_budget import Deadline, PrivateBudget
from kg.diagnostics._collector import Collector
from kg.diagnostics._targets import EvidenceTarget, ReportTargets
from kg.diagnostics.service import DiagnosticService
from kg.evidence.errors import EvidenceServiceError
from kg.indexing._passages import produce
from kg.models.execution import ExecutionReport, ExplainOptions
from kg.models.execution_events import EvidenceEvent
from kg.models.foundation import (
    EvidenceStep,
    ExpectedState,
    QueryBudget,
    QueryRequest,
    RemoveDocument,
    SuppliedAnchor,
    SuppliedContent,
)
from kg.models.processing import (
    DocumentTarget,
    HeartbeatRequest,
    PlanRegistration,
    ScheduleRequest,
    WorkerRegistration,
    WorkerRequest,
    WorkerSelection,
)
from kg.processing import ProcessingAdministration, ProcessingService
from kg.query import QueryService, _worker
from kg.query._meter import Ledger

TEXT = "a" * 1023 + "\r\nCafe\u0301 \U0001f680\0tail"


def publish(env, *, policy, text=TEXT, **kwargs):
    value = put(env.scope, text=text, **kwargs)
    value = value.model_copy(
        update={
            "payload": value.payload.model_copy(
                update={
                    "content": SuppliedContent(
                        text=text,
                        passage_policy=policy,
                        anchors=(
                            SuppliedAnchor(
                                local_id="tail", start=1023, end=len(text), quote=text[1023:]
                            ),
                            SuppliedAnchor(local_id="prefix", start=0, end=1023, quote=text[:1023]),
                        ),
                    ),
                }
            ),
        }
    )
    saved = receipt(env.service.write(value))
    produce(
        env.database,
        env.service.identity,
        env.scope,
        value.attribution,
        saved.document_id,
        saved.processing.state_version,
    )
    page = env.service.passages(env.scope, saved.document_id, saved.processing.state_version)
    assert len(page.entries) == 2
    return value, saved, page


def plan(scope, reference):
    return QueryRequest(
        contract_version="foundation/1",
        request_id="passages",
        scope=scope,
        steps=(EvidenceStep(operation="evidence", step_id="e", evidence=reference),),
        output_step="e",
        budget=QueryBudget(max_operations=1, max_records=1),
    )


def no_data(explained, reason):
    execution = explained.outcome
    assert execution.stop_reason == reason
    assert execution.work_accounting == "redacted"
    assert execution.steps == ()
    result = execution.result
    assert (
        result.data is result.read_state_id is result.result_set_id is result.continuation is None
    )
    assert result.operations_executed == result.records_examined == 0
    assert result.exhaustion == "none" and not result.truncated
    assert explained.report.state in {"unavailable", "redacted"}


@pytest.fixture(params=["codepoint-window/1", "supplied-anchors/1"])
def passages(tmp_path, request):
    env = environment(tmp_path / "passages.db")
    value, saved, page = publish(env, policy=request.param)
    with QueryService(env.database, env.service.identity) as service:
        yield env, service, value, saved, page


@pytest.mark.process
def test_exact_multi_passage_refs_quotes_and_one_semantic_charge(passages, monkeypatch):
    env, service, _, _, page = passages
    frames = []
    calls = []
    reserve = Ledger.reserve
    run = service._run_worker

    def record(self, frame):
        frames.append(frame)
        return reserve(self, frame)

    def counted(*args):
        calls.append(True)
        return run(*args)

    monkeypatch.setattr(Ledger, "reserve", record)
    monkeypatch.setattr(service, "_run_worker", counted)
    assert "".join(entry.quote for entry in page.entries) == TEXT
    for entry in page.entries:
        for reference in (entry.reference, entry.reference.model_copy(update={"passage_id": None})):
            request = plan(env.scope, reference)
            result = service.execute_explained(
                request,
                ExplainOptions(detail="detailed", include_quotes=True),
            )
            result.outcome.result.validate_for(request)
            assert result.outcome.result.outcome == "complete"
            record_value = result.outcome.result.data.records[0]
            assert record_value.record_id == reference.anchor_id
            assert record_value.record_type == "evidence"
            assert record_value.support.evidence == (reference,)
            assert result.outcome.result.records_examined == 1
            assert isinstance(result.report, ExecutionReport)
            charges = [f for f in frames if f.action == "public"]
            assert [(f.stage, f.n) for f in charges] == [("evidence_reference", 1)]
            assert [
                (e.event.stage, e.event.reservations)
                for e in result.report.events
                if e.event.kind == "query.budget"
            ] == [("evidence_reference", 1)]
            assert env.service.evidence(env.scope, reference).quote == TEXT[entry.start : entry.end]
            assert env.service.citation(env.scope, entry.citation).quote_hash == entry.quote_hash
            before = len(frames)
            retained = service.diagnostics.report(env.scope, result.report.report_id)
            assert retained == result.report and len(frames) == before
            frames.clear()
    assert (
        len(calls) == 4
    )  # Lookup never re-executes; generated anchors also use the real resolver.


@pytest.mark.process
@pytest.mark.parametrize(
    "field",
    [
        "source_namespace",
        "document_id",
        "revision_id",
        "anchor_id",
        "passage_id",
    ],
)
def test_mismatched_real_chain_is_not_found_before_public_charge(passages, monkeypatch, field):
    env, service, _, _, page = passages
    _, _, other = publish(env, policy=page.passage_policy, external="other", namespace="email")
    ref = page.entries[0].reference
    replacement = getattr(other.entries[1].reference, field)
    request = plan(env.scope, ref.model_copy(update={field: replacement}))
    charges = []
    original = Ledger.reserve

    def reserve(self, frame):
        if frame.action == "public":
            charges.append(frame)
        return original(self, frame)

    monkeypatch.setattr(Ledger, "reserve", reserve)
    no_data(service.execute_explained(request), "not_found")
    assert charges == []
    assert service.diagnostics.for_request(env.scope, request.request_id).entries == ()


@pytest.mark.process
def test_scoped_passage_and_whole_value_validation(passages):
    env, service, _, _, page = passages
    ref = page.entries[0].reference
    narrow = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(update={"namespaces": ("email",)}),
        }
    )
    valid = plan(env.scope, ref)
    malformed = [
        valid.model_copy(update={"scope": narrow}),
        *(
            valid.model_copy(
                update={
                    "steps": (
                        valid.steps[0].model_copy(
                            update={"evidence": ref.model_copy(update=fields)}
                        ),
                    )
                }
            )
            for fields in ({"passage_id": ""}, {"corpus_id": "foreign"})
        ),
    ]
    for request in malformed:
        with pytest.raises(EvidenceServiceError, match="invalid_request"):
            service.execute(request)


@pytest.mark.process
def test_history_citations_metadata_policy_sets_and_remove_restore(passages):
    env, service, value, saved, original = passages
    _, newer, same = publish(
        env,
        policy=original.passage_policy,
        state=saved.processing.state_version,
        title="New",
    )
    assert same.passage_set_id == original.passage_set_id
    assert same.entries[0].reference == original.entries[0].reference
    ref = original.entries[0].reference
    assert env.service.evidence(env.scope, ref).metadata.metadata.title == "Title"
    assert (
        env.service.citation(env.scope, same.entries[0].citation).metadata.metadata.title == "New"
    )
    alternate = (
        "supplied-anchors/1"
        if original.passage_policy == "codepoint-window/1"
        else "codepoint-window/1"
    )
    _, changed, different = publish(
        env,
        policy=alternate,
        state=newer.processing.state_version,
    )
    assert different.passage_set_id != original.passage_set_id
    assert not env.service.evidence(env.scope, ref).is_current_support
    with pytest.raises(EvidenceServiceError, match="not_found"):
        env.service.evidence(env.scope, ref, state_version=changed.processing.state_version)
    for citation in (
        original.entries[0].citation.model_copy(
            update={"state_version": changed.processing.state_version}
        ),
        original.entries[0].citation.model_copy(
            update={
                "metadata_snapshot_id": same.entries[0].citation.metadata_snapshot_id,
            }
        ),
    ):
        with pytest.raises(EvidenceServiceError, match="not_found"):
            env.service.citation(env.scope, citation)
    assert service.execute(plan(env.scope, ref)).result.outcome == "complete"
    removed = receipt(
        env.service.write(
            value.model_copy(
                update={
                    "retry_key": "remove",
                    "payload": RemoveDocument(
                        operation="remove_document",
                        document=value.payload.document,
                        precondition=ExpectedState(
                            kind="match", state_version=changed.processing.state_version
                        ),
                    ),
                }
            )
        )
    )
    assert not env.service.citation(env.scope, original.entries[0].citation).is_active
    assert service.execute(plan(env.scope, ref)).result.outcome == "complete"
    _, restored, returned = publish(
        env,
        policy=original.passage_policy,
        state=removed.processing.state_version,
    )
    assert restored.processing.state_version != saved.processing.state_version
    assert returned.passage_set_id == original.passage_set_id
    assert (
        env.service.citation(env.scope, original.entries[0].citation).metadata.metadata.title
        == "Title"
    )
    assert service.execute(plan(env.scope, ref)).result.data.records[0].support.evidence == (ref,)


@pytest.mark.process
def test_exact_shared_scratch_boundary_and_vm_exhaustion(passages, monkeypatch):
    env, service, _, _, page = passages
    request = plan(env.scope, page.entries[0].reference)
    peaks = []
    reserve = Ledger.reserve
    run = service._run_worker

    def record(self, frame):
        reply = reserve(self, frame)
        peaks.append(self.budget._scratch)
        return reply

    with monkeypatch.context() as patch:
        patch.setattr(Ledger, "reserve", record)
        assert service.execute(request).result.outcome == "complete"
    peak = max(peaks)
    assert 0 < peak < 64 << 20
    for extra in (0, 1):

        def occupied(observer, step, ledger, extra=extra):
            with ledger.budget.reserve_scratch((128 << 20) - peak + extra, "general"):
                return run(observer, step, ledger)

        with monkeypatch.context() as patch:
            patch.setattr(service, "_run_worker", occupied)
            result = service.execute_explained(request)
        if extra:
            no_data(result, "resource_budget")
        else:
            assert result.outcome.result.outcome == "complete"

    def exhaust_vm(observer, step, ledger):
        ledger.budget.reserve_vm(10_000_000 - ledger.budget._vm)
        return run(observer, step, ledger)

    monkeypatch.setattr(service, "_run_worker", exhaust_vm)
    no_data(service.execute_explained(request), "resource_budget")


@pytest.mark.process
@pytest.mark.parametrize(
    "worker,reason",
    [
        (die_after_hydration, "internal_error"),
        (partial_frame_after_hydration, "time_budget"),
        (repeated_local_hydration, "resource_budget"),
    ],
)
def test_actual_hydration_worker_loss_and_retained_local_budget(
    passages, monkeypatch, worker, reason
):
    env, service, _, _, page = passages
    request = plan(env.scope, page.entries[0].reference).model_copy(
        update={
            "budget": QueryBudget(max_records=1, max_milliseconds=2000),
        }
    )
    ledgers = []
    outstanding = []
    run = service._run_worker
    close = Ledger.close

    def observed(observer, step, ledger):
        ledgers.append(ledger)
        return run(observer, step, ledger)

    def cleanup(ledger):
        outstanding.append((len(ledger.scratch), ledger.budget._scratch))
        close(ledger)

    with monkeypatch.context() as patch:
        patch.setattr(_worker, "run", worker)
        patch.setattr(service, "_run_worker", observed)
        patch.setattr(Ledger, "close", cleanup)
        no_data(service.execute_explained(request), reason)
    assert len(ledgers) == 1
    assert ledgers[0].operations == ledgers[0].records == 1
    if worker in (die_after_hydration, partial_frame_after_hydration):
        assert len(outstanding) == 1
        assert outstanding[0][0] > 0 and outstanding[0][1] > 0
    assert not ledgers[0].scratch and ledgers[0].budget._scratch == 0
    assert service.diagnostics.for_request(env.scope, request.request_id).entries == ()
    assert service.execute(request).result.outcome == "complete"


@pytest.mark.process
def test_actual_processing_heartbeat_invalidates_passage_release(passages, monkeypatch):
    env, service, _, saved, page = passages
    admin = ProcessingAdministration(env.database, env.admin.authority)
    admin.register_plan(
        PlanRegistration(
            corpus_id=env.scope.corpus_id,
            plan_id="index",
            plan_version="1",
            producer="test",
            producer_version="1",
        )
    )
    selection = WorkerSelection(
        namespace="markdown",
        owner_id="owner",
        writer_id="writer",
        plan_id="index",
        plan_version="1",
        worker_id="worker",
    )
    admin.register_worker(
        WorkerRegistration(
            corpus_id=env.scope.corpus_id,
            principal_id=env.service.identity.principal_id,
            selection=selection,
        )
    )
    with env.database.connection() as connection:
        namespace_token = connection.execute(
            "SELECT namespace_token FROM document_state WHERE state_version=?",
            (saved.processing.state_version,),
        ).fetchone()[0]
    processing = ProcessingService(env.database, env.service.identity)
    scheduled = processing.schedule(
        ScheduleRequest(
            scope=env.scope,
            selection=selection,
            request_id="schedule",
            retry_key="schedule",
            target=DocumentTarget(
                document_id=saved.document_id,
                revision_id=saved.revision_id,
                state_version=saved.processing.state_version,
                namespace_token=namespace_token,
            ),
        )
    )
    assert scheduled.status == "applied"
    claim = processing.claim(
        WorkerRequest(
            scope=env.scope,
            selection=selection,
            request_id="claim",
        )
    ).claim
    assert claim is not None
    original = service._run_worker

    def heartbeat(*args):
        elapsed = original(*args)
        processing.heartbeat(
            HeartbeatRequest(
                scope=env.scope,
                selection=selection,
                request_id="heartbeat",
                job_id=claim.job_id,
                claim_fence=claim.claim_fence,
            )
        )
        return elapsed

    monkeypatch.setattr(service, "_run_worker", heartbeat)
    request = plan(env.scope, page.entries[0].reference)
    no_data(service.execute_explained(request), "state_changed")
    assert service.diagnostics.for_request(env.scope, request.request_id).entries == ()


@pytest.mark.process
def test_complete_targets_and_group_redaction_after_eviction_and_restored_rights(
    passages,
    monkeypatch,
):
    env, service, _, _, page = passages
    request = plan(env.scope, page.entries[0].reference)
    retained = []
    original = service._run_worker
    target = EvidenceTarget(reference=page.entries[1].reference, citation=page.entries[1].citation)

    def nested(*args):
        elapsed = original(*args)
        parent = next(c for c in service._collector._captures.values() if c.active)
        collector = Collector("evidence", service.identity)
        child = collector.begin_capture(
            "evidence",
            request.scope,
            required="read",
            group=parent.group,
            observer=parent.group.observers[0],
            options=ExplainOptions(detail="detailed"),
        )
        child.append(
            EvidenceEvent(phase="read", decision="observed"), ReportTargets(values=(target,))
        )
        child.finish("succeeded")
        retained.append((parent, collector, child))
        return elapsed

    with monkeypatch.context() as patch:
        patch.setattr(service, "_run_worker", nested)
        explained = service.execute_explained(request)
    assert isinstance(explained.report, ExecutionReport)
    parent, collector, child = retained[0]
    assert any(
        EvidenceTarget(reference=request.steps[0].evidence) in binding.targets
        for binding in parent.group.bindings
    )
    assert any(target in binding.targets for binding in parent.group.bindings)
    revoked = env.admin.replace_policy(
        env.policy.model_copy(update={"grants": ()}),
        env.scope.access.policy_version,
    )

    def inspect():
        service._collector._remove(parent)
        facade = DiagnosticService(collector, service._authorizer)
        service._authorizer.lookup_budget = PrivateBudget(Deadline(time.monotonic() + 5))
        try:
            return facade.report(env.scope, child.report_id)
        finally:
            service._authorizer.lookup_budget = None

    assert service._dispatcher.call(inspect, Deadline(time.monotonic() + 5)).state == "unavailable"
    assert child.group.state == "redacted" and not child.events and child.prepared is None
    env.admin.replace_policy(env.policy, revoked.policy_version)
    assert service._dispatcher.call(inspect, Deadline(time.monotonic() + 5)).state in {
        "redacted",
        "unavailable",
    }
    assert child.group.state == "redacted" and not child.events and child.prepared is None
    assert service.diagnostics.for_request(env.scope, request.request_id).entries == ()
    service._dispatcher.call(lambda: collector._remove(child), Deadline(time.monotonic() + 5))
