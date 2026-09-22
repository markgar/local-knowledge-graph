from __future__ import annotations

import json
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest
from support.evidence import environment, put, receipt
from support.knowledge import schema
from support.query_workers import (
    die_after_ack,
    false_completion,
    over_local,
    over_public,
    partial_frame,
    stall_after_ack,
    too_large,
)

import kg.query.service as query_module
from kg._execution_budget import Deadline, PrivateBudget
from kg.diagnostics._collector import Collector
from kg.diagnostics.service import DiagnosticService
from kg.indexing._passages import produce
from kg.knowledge import KnowledgeAdministration
from kg.models.execution import ExecutionReport, ExplainOptions
from kg.models.foundation import (
    CountStep,
    EvidenceStep,
    QueryBudget,
    QueryRequest,
    RecordsStep,
    ResolveStep,
    SearchStep,
)
from kg.query import QueryService, QueryServiceError, _worker
from kg.query.service import closure


@pytest.fixture(params=["anchor", "codepoint-window/1", "supplied-anchors/1"])
def query(tmp_path, request):
    env = environment(tmp_path / "query.db")
    value = put(env.scope)
    if request.param != "anchor":
        value = value.model_copy(
            update={
                "payload": value.payload.model_copy(
                    update={
                        "content": value.payload.content.model_copy(
                            update={"passage_policy": request.param}
                        )
                    }
                )
            }
        )
    saved = receipt(env.service.write(value))
    ref = (
        env.service.anchors(
            env.scope,
            saved.document_id,
            saved.processing.state_version,
        )
        .entries[0]
        .reference
    )
    if request.param != "anchor":
        produce(
            env.database, env.service.identity, env.scope, value.attribution,
            saved.document_id, saved.processing.state_version,
        )
        ref = env.service.passages(
            env.scope, saved.document_id, saved.processing.state_version,
        ).entries[0].reference
    plan = QueryRequest(
        contract_version="foundation/1",
        request_id="query",
        scope=env.scope,
        steps=(EvidenceStep(operation="evidence", step_id="e", evidence=ref),),
        output_step="e",
    )
    with QueryService(env.database, env.service.identity) as service:
        yield env, service, plan


def assert_redacted(execution, reason):
    assert execution.stop_reason == reason
    assert execution.work_accounting == "redacted"
    assert execution.steps == ()
    result = execution.result
    assert result.data is None and result.read_state_id is None and result.result_set_id is None
    assert result.operations_executed == result.records_examined == 0
    assert not result.truncated and result.exhaustion == "none"


def test_real_anchor_history_and_pruned_unsupported(query):
    env, service, request = query
    original = env.service.current(env.scope, put(env.scope).payload.document)
    # Historical evidence stays readable after the current document changes.
    env.service.write(put(env.scope, state=original.state_version, text="replacement"))
    request = request.model_copy(
        update={
            "steps": (
                SearchStep(operation="search", step_id="unused", text="never load a model"),
                *request.steps,
            )
        }
    )
    result = service.execute(request)
    assert result.result.outcome == "complete"
    result.result.validate_for(request)
    assert result.result.data.records[0].support.evidence == (request.steps[1].evidence,)
    assert [s.state for s in result.steps] == ["not_needed", "complete"]
    assert result.result.records_examined == result.result.operations_executed == 1
    assert service.capabilities(request.scope).operations == ("evidence", "resolve")


def test_summary_discovery_detailed_single_execution_and_owner_thread(query, monkeypatch):
    _, service, request = query
    calls = []
    original = service._run_worker

    def counted(*args):
        calls.append(True)
        return original(*args)

    monkeypatch.setattr(service, "_run_worker", counted)
    ordinary = service.execute(request)
    assert ordinary.result.outcome == "complete"
    with ThreadPoolExecutor() as pool:
        headers = pool.submit(service.diagnostics.for_request, request.scope, "query").result()
    assert len(headers.entries) == 1
    summary = service.diagnostics.report(request.scope, headers.entries[0].report_id)
    assert isinstance(summary, ExecutionReport)
    assert summary.capture_level == "summary"
    detailed = service.execute_explained(request, ExplainOptions(detail="detailed"))
    assert detailed.outcome.result.outcome == "complete"
    assert isinstance(detailed.report, ExecutionReport)
    assert detailed.report.execution_id != summary.execution_id
    assert len(calls) == 2
    assert any(getattr(e.event, "selected_ids", ()) for e in detailed.report.events)


def test_required_dependent_closure_explicitly_unsupported(query):
    _, service, request = query
    request = request.model_copy(
        update={
            "steps": (
                ResolveStep(operation="resolve", step_id="r", entity_id="missing"),
                RecordsStep(
                    operation="records", step_id="s", entity_step="r", record_type="decision"
                ),
                CountStep(operation="count", step_id="c", records_step="s"),
            ),
            "output_step": "c",
        }
    )
    assert closure(request) == ("r", "s", "c")
    assert_redacted(service.execute(request), "unsupported_operation")


def test_validation_reconstructs_and_checks_unrelated_steps(query):
    _, service, request = query
    invalid = request.model_copy(
        update={
            "budget": QueryBudget(max_operations=1),
            "steps": (
                *request.steps,
                SearchStep(operation="search", step_id="unused", text="x"),
            ),
        }
    )
    with pytest.raises(QueryServiceError) as error:
        service.execute(invalid)
    assert error.value.failure.code == "invalid_request"
    with pytest.raises(QueryServiceError):
        service.execute(request.model_copy(update={"output_step": "missing"}))


def test_missing_anchor_and_passage_are_not_empty(query):
    _, service, request = query
    step = request.steps[0]
    missing = step.model_copy(
        update={"evidence": step.evidence.model_copy(update={"anchor_id": "no"})}
    )
    assert_redacted(service.execute(request.model_copy(update={"steps": (missing,)})), "not_found")
    passage = step.model_copy(
        update={"evidence": step.evidence.model_copy(update={"passage_id": "p"})}
    )
    assert_redacted(
        service.execute(request.model_copy(update={"steps": (passage,)})),
        "not_found",
    )


def test_global_commit_before_release_irreversibly_redacts_report(query, monkeypatch):
    env, service, request = query
    original = service._run_worker

    def intervening(*args):
        elapsed = original(*args)
        env.service.write(put(env.scope, external="unrelated"))
        return elapsed

    monkeypatch.setattr(service, "_run_worker", intervening)
    result = service.execute_explained(request, ExplainOptions(detail="detailed"))
    assert_redacted(result.outcome, "state_changed")
    assert result.report.state == "redacted"
    assert service.diagnostics.for_request(request.scope, "query").entries == ()


def test_deadline_does_not_publish_late_worker_result(query, monkeypatch):
    _, service, request = query
    original = service._run_worker

    def late(*args):
        value = original(*args)
        time.sleep(0.4)
        return value

    monkeypatch.setattr(service, "_run_worker", late)
    request = request.model_copy(update={"budget": QueryBudget(max_milliseconds=500)})
    assert_redacted(service.execute(request), "time_budget")
    assert service.diagnostics.for_request(request.scope, "query").entries == ()


def test_close_is_idempotent_and_no_post_close_execution(query):
    _, service, request = query
    service.close()
    service.close()
    assert_redacted(service.execute(request), "service_closed")


@pytest.mark.parametrize(
    "worker,reason",
    [
        (die_after_ack, "internal_error"),
        (false_completion, "internal_error"),
        (over_local, "resource_budget"),
        (over_public, "record_budget"),
        (too_large, "internal_error"),
    ],
)
def test_real_process_protocol_and_failures_never_release(query, monkeypatch, worker, reason):
    _, service, request = query
    monkeypatch.setattr(_worker, "run", worker)
    request = request.model_copy(update={"budget": QueryBudget(max_records=1)})
    assert_redacted(service.execute(request), reason)
    assert service.diagnostics.for_request(request.scope, request.request_id).entries == ()


@pytest.mark.parametrize("worker", [stall_after_ack, partial_frame])
def test_native_stall_is_killed_and_following_call_can_run(query, monkeypatch, worker):
    _, service, request = query
    with monkeypatch.context() as patch:
        patch.setattr(_worker, "run", worker)
        start = time.monotonic()
        short = request.model_copy(update={"budget": QueryBudget(max_milliseconds=1000)})
        assert_redacted(service.execute(short), "time_budget")
        assert time.monotonic() - start < 3
    assert service.execute(request).result.outcome == "complete"


def test_initial_policy_denial_vs_post_admission_rotation(query, monkeypatch):
    env, service, request = query
    wrong = request.scope.model_copy(
        update={
            "access": request.scope.access.model_copy(update={"policy_version": "old"}),
        }
    )
    assert_redacted(service.execute(request.model_copy(update={"scope": wrong})), "forbidden")
    original = service._run_worker

    def rotate(*args):
        elapsed = original(*args)
        env.admin.replace_policy(
            env.policy.model_copy(update={"grants": env.policy.grants[:-1]}),
            env.scope.access.policy_version,
        )
        return elapsed

    monkeypatch.setattr(service, "_run_worker", rotate)
    assert_redacted(service.execute(request), "state_changed")


def test_retained_report_original_observer_invalidates_on_later_commit(query):
    env, service, request = query
    initial = service.execute_explained(request)
    assert isinstance(initial.report, ExecutionReport)
    env.service.write(put(env.scope, external="later"))
    assert (
        service.diagnostics.report(request.scope, initial.report.report_id).state == "unavailable"
    )
    assert service.diagnostics.for_request(request.scope, "query").entries == ()


def test_same_content_edit_restore_is_still_state_changed(query, monkeypatch):
    env, service, request = query
    original = service._run_worker

    def restore(*args):
        elapsed = original(*args)
        current = env.service.current(env.scope, put(env.scope).payload.document)
        edited = receipt(
            env.service.write(
                put(
                    env.scope,
                    state=current.state_version,
                    text="changed",
                )
            )
        )
        env.service.write(put(env.scope, state=edited.processing.state_version))
        return elapsed

    monkeypatch.setattr(service, "_run_worker", restore)
    assert_redacted(service.execute(request), "state_changed")


def test_close_cancels_active_worker_and_staged_report(query, monkeypatch):
    _, service, request = query
    started = Event()
    original = service._run_worker

    def running(*args):
        started.set()
        return original(*args)

    monkeypatch.setattr(service, "_run_worker", running)
    monkeypatch.setattr(_worker, "run", stall_after_ack)
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(service.execute, request)
        assert started.wait(2)
        service.close()
        assert_redacted(pending.result(), "service_closed")
    assert service._collector._captures == {}


def test_nested_staging_remains_redacted_after_parent_eviction(query, monkeypatch):
    env, service, request = query
    original = service._run_worker
    retained = []

    def nested(*args):
        parent = next(c for c in service._collector._captures.values() if c.active)
        child_collector = Collector("indexing", service.identity)
        child = child_collector.begin_capture(
            "search",
            request.scope,
            required="read",
            group=parent.group,
            observer=parent.group.observers[0],
        )
        child.finish("succeeded")
        retained.append((parent, child_collector, child))
        elapsed = original(*args)
        env.service.write(put(env.scope, external="invalidates-family"))
        return elapsed

    monkeypatch.setattr(service, "_run_worker", nested)
    assert_redacted(service.execute(request), "state_changed")

    def check():
        parent, collector, child = retained[0]
        service._collector._remove(parent)
        assert child.prepared is None and not child.events and child.group.state == "redacted"
        facade = DiagnosticService(collector, service._authorizer)
        service._authorizer.lookup_budget = PrivateBudget(Deadline(time.monotonic() + 5))
        try:
            assert facade.report(request.scope, child.report_id).state in {
                "unavailable",
                "redacted",
            }
            assert child.group.state == "redacted"
        finally:
            collector._remove(child)
            service._authorizer.lookup_budget = None

    service._dispatcher.call(check, Deadline(time.monotonic() + 5))


def test_report_allocation_failure_finishes_capture_and_does_not_change_business(
    query, monkeypatch
):
    _, service, request = query

    def fail_event(**kwargs):
        raise MemoryError()

    with monkeypatch.context() as patch:
        patch.setattr(query_module, "QueryPlan", fail_event)
        result = service.execute_explained(request)
    assert result.outcome.result.outcome == "complete"
    assert result.report.state == "unavailable"
    assert all(
        not c.active and c.completed_at is not None for c in service._collector._captures.values()
    )
    again = service.execute_explained(request)
    assert isinstance(again.report, ExecutionReport)
    assert len(service.diagnostics.for_request(request.scope, "query").entries) == 1


def test_business_memory_error_is_not_success_shaped_or_swallowed_by_capture(query, monkeypatch):
    _, service, request = query

    def fail_business(*args):
        raise MemoryError()

    monkeypatch.setattr(service, "_run_worker", fail_business)
    assert_redacted(service.execute(request), "internal_error")
    assert all(not c.active and c.prepared is None for c in service._collector._captures.values())


def test_capabilities_translates_internal_control_exceptions(query, monkeypatch):
    _, service, request = query
    with monkeypatch.context() as patch:

        def expired(*args):
            from kg._execution_budget import DeadlineStop

            raise DeadlineStop()

        patch.setattr(service._dispatcher, "call", expired)
        with pytest.raises(QueryServiceError) as error:
            service.capabilities(request.scope)
        assert error.value.failure.code == "budget_exceeded"
    service.close()
    with pytest.raises(QueryServiceError) as error:
        service.capabilities(request.scope)
    assert error.value.failure.code == "unsupported"


def test_python_example_executes_real_canonical_anchor(query, tmp_path):
    env, _, request = query
    path = tmp_path / "query.json"
    path.write_text(request.model_dump_json())
    completed = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).parents[1] / "examples/query_anchor.py"),
            str(env.database.path),
            str(path),
            env.service.identity.principal_id,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )
    value = json.loads(completed.stdout)
    assert value["outcome"]["result"]["outcome"] == "complete"
    assert value["report"]["owning_service"] == "query"


def test_delivered_passage_and_anchor_capabilities_match_execution(query):
    env, service, request = query
    value = put(env.scope, external="published-passages")
    value = value.model_copy(
        update={
            "payload": value.payload.model_copy(
                update={
                    "content": value.payload.content.model_copy(
                        update={"passage_policy": "supplied-anchors/1"}
                    ),
                }
            )
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
    reference = (
        env.service.passages(
            env.scope,
            saved.document_id,
            saved.processing.state_version,
        )
        .entries[0]
        .reference
    )
    assert reference.passage_id is not None
    assert env.service.evidence(env.scope, reference).quote == value.payload.content.text
    anchor = reference.model_copy(update={"passage_id": None})
    direct = request.model_copy(
        update={
            "budget": QueryBudget(max_records=1),
            "steps": (EvidenceStep(operation="evidence", step_id="e", evidence=anchor),),
        }
    )
    explained = service.execute_explained(direct)
    assert explained.outcome.result.outcome == "complete"
    assert explained.outcome.result.records_examined == 1
    assert isinstance(explained.report, ExecutionReport)
    assert isinstance(
        service.diagnostics.report(direct.scope, explained.report.report_id),
        ExecutionReport,
    )
    passage = direct.model_copy(
        update={
            "steps": (EvidenceStep(operation="evidence", step_id="e", evidence=reference),),
        }
    )
    result = service.execute(passage)
    assert result.result.outcome == "complete"
    assert result.result.records_examined == 1
    assert result.result.data.records[0].support.evidence == (reference,)
    capabilities = service.capabilities(env.scope)
    assert capabilities.evidence_kinds == ("anchor", "passage")
    assert capabilities.adapter_version == "canonical-evidence/1"


def test_shared_resolver_scratch_uses_original_supervisor_pool(query, monkeypatch):
    _, service, request = query
    original = service._run_worker

    def occupied(observer, step, ledger):
        with ledger.budget.reserve_scratch(64 << 20, "general"):
            return original(observer, step, ledger)

    monkeypatch.setattr(service, "_run_worker", occupied)
    assert_redacted(service.execute(request), "resource_budget")
    assert service.diagnostics.for_request(request.scope, request.request_id).entries == ()


def test_actual_knowledge_registry_commit_invalidates_query_release(query, monkeypatch):
    env, service, request = query
    admin = KnowledgeAdministration(env.database, env.admin.authority)
    original = service._run_worker

    def register(*args):
        elapsed = original(*args)
        assert admin.register_knowledge_schema(schema()).status == "applied"
        return elapsed

    monkeypatch.setattr(service, "_run_worker", register)
    assert_redacted(service.execute(request), "state_changed")
    assert service.diagnostics.for_request(request.scope, request.request_id).entries == ()
