import time

import pytest
from support.evidence import put, receipt
from support.query_knowledge import plan, produce, setup, write

from kg.models.execution import ExecutionReport
from kg.models.foundation import (
    AddAlias,
    AddAssertion,
    AddEntitySupport,
    DocumentDependency,
    PathsStep,
    QueryBudget,
    RecordsStep,
    ResolveStep,
    SearchStep,
    SourceSupport,
    StoredEntity,
    StringObject,
)
from kg.models.query import SupportInspectionRequest
from kg.query import QueryService


def inspection(request, result, **kwargs):
    return SupportInspectionRequest(
        request_id="inspect",
        scope=request.scope,
        result_set_id=result.result_set_id,
        records_step_id="decisions",
        budget=request.budget,
        **kwargs,
    )


@pytest.mark.parametrize("count", [25, 1001])
def test_real_produced_count_display_and_full_inspection(tmp_path, count, monkeypatch):
    from kg.query._meter import Ledger

    stops = []
    cleanup = []
    reserve = Ledger.reserve
    close = Ledger.close

    def recorded(ledger, frame):
        reply = reserve(ledger, frame)
        if reply.state != "ok":
            stops.append((frame, ledger.budget._vm, ledger.budget._visits, len(ledger.views)))
        return reply

    monkeypatch.setattr(Ledger, "reserve", recorded)

    def released(ledger):
        close(ledger)
        cleanup.append((ledger.budget._vm, ledger.budget._visits, ledger.budget._scratch))

    monkeypatch.setattr(Ledger, "close", released)
    env = setup(tmp_path / "decisions.db")
    entity_id, expected = produce(env, count)
    request = plan(env, entity_id)
    with QueryService(env.database, env.service.identity) as service:
        explained = service.execute_explained(request)
        execution = explained.outcome
        assert execution.result.error is None, execution
        result = execution.result
        result.validate_for(request)
        assert result.data.count == count and result.data.exact
        assert result.operations_executed == 3
        assert result.records_examined == count + 1
        assert [s.records_examined for s in execution.steps] == [1, count, 0]
        pages = []
        start = 0
        while True:
            page = service.inspect_support(inspection(request, result, start_ordinal=start))
            assert page.error is None, (page, stops)
            assert page.total == count and page.exact
            assert page.read_state_id == result.read_state_id
            assert page.records_examined == len(page.records)
            pages.extend(page.records)
            if page.exhausted:
                break
            start = page.next_ordinal
        assert [r.record_id for r in pages] == sorted(expected)
        assert all(r.support == env.support for r in pages)
        assert service.inspect_support(inspection(request, result)).records == tuple(pages[:1000])
        records = service.execute(plan(env, entity_id, output="decisions"))
        assert records.result.error is None, records
        assert [r.record_id for r in records.result.data.records] == sorted(expected)[:1000]
        assert records.result.truncated == (count > 1000)
        assert records.result.exhaustion == "eligible_set"
        if count > 1000:
            assert records.stop_reason == "display_limit"
            assert not isinstance(explained.report, ExecutionReport)
        else:
            assert isinstance(explained.report, ExecutionReport)
        assert service.capabilities(env.scope).operations == (
            "evidence",
            "resolve",
            "records",
            "count",
            "search",
        )
        assert len(service._support.sets) == 1
        assert not stops
        assert all(
            vm <= 10_000_000 and visits <= 100_000 and scratch == 0
            for vm, visits, scratch in cleanup
        )


@pytest.mark.parametrize("limit", [1, 2, 25, 26])
def test_public_partial_count_and_exact_boundary(tmp_path, limit):
    env = setup(tmp_path / "partial.db")
    subject, expected = produce(env, 25)
    request = plan(env, subject, max_records=limit)
    with QueryService(env.database, env.service.identity) as service:
        result = service.execute(request)
        assert result.result.error is None, result
        assert result.result.data.count == min(limit - 1, 25)
        assert result.result.data.exact == (limit == 26)
        assert result.result.outcome == ("complete" if limit == 26 else "partial")
        page = service.inspect_support(inspection(request, result.result))
        assert page.error is None, page
        assert {r.record_id for r in page.records} <= expected


def test_empty_ambiguity_and_retention_lifecycle(tmp_path):
    env = setup(tmp_path / "empty.db")
    with QueryService(env.database, env.service.identity) as service:
        request = plan(env, "absent")
        result = service.execute(request)
        assert result.result.outcome == "empty", result
        assert result.result.data.count == 0 and result.result.data.exact
        page_request = inspection(request, result.result)
        page = service.inspect_support(page_request)
        assert page.outcome == "empty" and page.total == 0 and page.exhausted
        wrong = page_request.model_copy(update={"records_step_id": "wrong"})
        denied = service.inspect_support(wrong)
        assert denied.error.code == "not_found" and denied.total is None
        service._support.sets[result.result.result_set_id].expires = time.monotonic() - 1
        assert service.inspect_support(page_request).error.code == "not_found"
        assert not service._support.sets
        produce(env, 0)
        produce(env, 0)
        ambiguous = service.execute(plan(env, name="Project"))
        assert ambiguous.result.outcome == "ambiguous", ambiguous
        assert len(ambiguous.result.data.entity_ids) == 2
        assert [s.state for s in ambiguous.steps] == ["ambiguous", "not_started", "not_started"]
        assert ambiguous.result.result_set_id is None


def no_data(outcome, reason):
    assert outcome.stop_reason == reason
    assert outcome.work_accounting == "redacted"
    if hasattr(outcome, "result"):
        assert outcome.steps == ()
        outcome = outcome.result
        assert outcome.data is None
    else:
        assert not outcome.records and outcome.total is None
        assert outcome.next_ordinal is None and outcome.exact is None
    assert outcome.operations_executed == outcome.records_examined == 0
    assert outcome.read_state_id is outcome.result_set_id is None


def test_exact_alias_ambiguity_incomplete_dependency_and_output_correlation(tmp_path):
    env = setup(tmp_path / "selectors.db")
    first, expected = produce(env, 3)
    write(
        env,
        tuple(
            AddAlias(
                kind="alias",
                local_id=f"a{i}",
                entity=StoredEntity(kind="stored", entity_id=first),
                alias="Alias",
                support=env.support,
            )
            for i in range(2)
        ),
    )
    second, _ = produce(env, 0, name="Second")
    with QueryService(env.database, env.service.identity) as service:
        exact = service.execute(plan(env, first))
        alias = service.execute(plan(env, name="Alias"))
        assert exact.result.data == alias.result.data
        assert alias.result.records_examined == 4
        assert service.execute(plan(env, "Project")).result.outcome == "empty"
        original = plan(env, first)
        reordered = original.model_copy(
            update={
                "steps": (
                    ResolveStep(operation="resolve", step_id="unused", entity_id=second),
                    RecordsStep(
                        operation="records",
                        step_id="other",
                        entity_step="unused",
                        record_type="action",
                    ),
                    SearchStep(operation="search", step_id="search", text="unsupported but pruned"),
                    *original.steps,
                )
            }
        )
        result = service.execute(reordered)
        assert result.result.data.count == len(expected)
        assert [s.state for s in result.steps[:3]] == ["not_needed"] * 3
        produce(env, 0)
        produce(env, 0)
        limited = plan(env, name="Project", max_records=1)
        no_data(service.execute(limited), "record_budget")
        partial = service.execute(limited.model_copy(update={"output_step": "subject"}))
        assert partial.result.outcome == "partial" and partial.result.truncated
        assert len(partial.result.data.entity_ids) == 1
        ambiguous = service.execute(plan(env, name="Project", max_records=2))
        assert ambiguous.result.outcome == "ambiguous" and ambiguous.result.truncated
        assert ambiguous.result.operations_executed == 1


@pytest.mark.parametrize("kind", ["action", "blocker", "conflict", "paths"])
def test_unsupported_semantics_preflight_even_empty(tmp_path, kind):
    env = setup(tmp_path / "unsupported.db")
    request = plan(env, "absent")
    if kind == "paths":
        step = PathsStep(
            operation="paths", step_id="unsupported", entity_step="subject", predicate="work:owns"
        )
    else:
        step = RecordsStep(
            operation="records",
            step_id="unsupported",
            entity_step="subject",
            record_type=kind,
            status="open" if kind == "action" else None,
        )
    request = request.model_copy(
        update={"steps": (request.steps[0], step), "output_step": "unsupported"}
    )
    with QueryService(env.database, env.service.identity) as service:
        result = service.execute(request)
        no_data(
            result,
            "unsupported_operation" if kind == "paths" else "unsupported_restriction",
        )
        assert not service._support.sets


def test_unregistered_encoding_empty_subject_is_not_exact_zero(tmp_path):
    env = setup(tmp_path / "unregistered.db", registered=False)
    with QueryService(env.database, env.service.identity) as service:
        no_data(service.execute(plan(env, "absent")), "unsupported_operation")
        assert service.capabilities(env.scope).record_types == ()


@pytest.mark.parametrize("phase", ["between_steps", "release", "inspection"])
def test_actual_write_restore_invalidates_original_observer(tmp_path, monkeypatch, phase):
    from kg.query._meter import Ledger

    env = setup(tmp_path / "race.db")
    subject, _ = produce(env, 25)
    request = plan(env, subject)
    changed = False

    def restore():
        nonlocal changed
        if changed:
            return
        changed = True
        current = env.service.current(env.scope, put(env.scope).payload.document)
        result = env.service.write(put(env.scope, text="different", state=current.state_version))
        assert result.error is None
        state = result.receipt.processing.state_version
        assert env.service.write(put(env.scope, state=state)).error is None

    with QueryService(env.database, env.service.identity) as service:
        if phase == "between_steps":
            original = Ledger.reserve

            def reserve(ledger, frame):
                if frame.action == "begin" and frame.step_id == "decisions":
                    restore()
                return original(ledger, frame)

            monkeypatch.setattr(Ledger, "reserve", reserve)
        else:
            original = service._run_worker

            def run(*args):
                result = original(*args)
                restore()
                return result

            if phase == "inspection":
                result = service.execute(request)
                assert result.result.data.count == 25
                monkeypatch.setattr(service, "_run_worker", run)
                no_data(
                    service.inspect_support(inspection(request, result.result)), "state_changed"
                )
                return
            monkeypatch.setattr(service, "_run_worker", run)
        no_data(service.execute(request), "state_changed")
        assert not service._support.sets and service._support.bytes == 0
        assert not service.diagnostics.for_request(env.scope, request.request_id).entries


def test_scope_policy_and_restart_do_not_unlock_support(tmp_path):
    env = setup(tmp_path / "scope.db")
    subject, _ = produce(env, 2)
    request = plan(env, subject)
    with QueryService(env.database, env.service.identity) as service:
        result = service.execute(request)
        original = inspection(request, result.result)
        narrowed = env.scope.model_copy(
            update={
                "access": env.scope.access.model_copy(
                    update={
                        "namespaces": ("markdown",),
                        "grants": ("read",),
                    }
                )
            }
        )
        no_data(
            service.inspect_support(original.model_copy(update={"scope": narrowed})), "not_found"
        )
        guessed = original.model_copy(update={"result_set_id": "unknown"})
        no_data(service.inspect_support(guessed), "not_found")
        revoked = env.policy.model_copy(
            update={
                "grants": tuple(g for g in env.policy.grants if g.grant != "write_knowledge"),
            }
        )
        refreshed = env.admin.replace_policy(revoked, env.scope.access.policy_version)
        no_data(service.inspect_support(original), "forbidden")
    assert service._support.bytes == 0
    no_data(service.inspect_support(original), "service_closed")
    # A fresh service never reselects under a previously issued support identity.
    fresh_scope = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(
                update={
                    "policy_version": refreshed.policy_version,
                    "grants": ("read", "write_documents"),
                }
            )
        }
    )
    with QueryService(env.database, env.service.identity) as restarted:
        no_data(
            restarted.inspect_support(original.model_copy(update={"scope": fresh_scope})),
            "not_found",
        )


def test_private_stop_after_retained_prefix_discards_everything(tmp_path, monkeypatch):
    from kg.query._meter import Ledger

    env = setup(tmp_path / "private.db")
    subject, _ = produce(env, 250)
    reserve = Ledger.reserve
    exhausted = False

    def stop(ledger, frame):
        nonlocal exhausted
        reply = reserve(ledger, frame)
        if not exhausted and ledger.records >= 205:
            exhausted = True
            ledger.budget.reserve_vm(10_000_000 - ledger.budget._vm)
        return reply

    monkeypatch.setattr(Ledger, "reserve", stop)
    with QueryService(env.database, env.service.identity) as service:
        no_data(service.execute(plan(env, subject)), "resource_budget")
        assert exhausted and not service._support.sets and service._support.bytes == 0


def test_inspection_public_limit_never_releases_partial_slice(tmp_path):
    env = setup(tmp_path / "inspect-limit.db")
    subject, _ = produce(env, 25)
    request = plan(env, subject)
    with QueryService(env.database, env.service.identity) as service:
        result = service.execute(request)
        value = inspection(request, result.result).model_copy(
            update={
                "budget": QueryBudget(max_records=1, max_milliseconds=30_000),
            }
        )
        no_data(service.inspect_support(value), "record_budget")
        assert service.inspect_support(inspection(request, result.result)).total == 25


def test_retention_quotas_do_not_evict_or_partially_publish(tmp_path, monkeypatch):
    from kg.query import _retention

    env = setup(tmp_path / "quota.db")
    request = plan(env, "absent")
    monkeypatch.setattr(_retention, "MAX_SETS", 2)
    with QueryService(env.database, env.service.identity) as service:
        first = service.execute(request)
        second = service.execute(request)
        retained = service._support.bytes
        no_data(service.execute(request), "retention_limit")
        assert len(service._support.sets) == 2 and service._support.bytes == retained
        assert service.inspect_support(inspection(request, first.result)).total == 0
        assert service.inspect_support(inspection(request, second.result)).total == 0
        monkeypatch.setattr(_retention, "TOTAL_BYTES", retained)
        no_data(service.execute(request), "retention_limit")
        assert service._support.bytes == retained


def test_complete_conjunctive_support_and_exact_sequence_witness(tmp_path, monkeypatch):
    import kg.knowledge._write as writes
    from kg.knowledge import KnowledgeService
    from kg.knowledge._selection import DecisionSelectionItem

    env = setup(tmp_path / "witness.db")
    subject, _ = produce(env, 0)
    token = writes.token
    calls = 0

    def ordered():
        nonlocal calls
        calls += 1
        return "000-later-contribution" if calls == 2 else token()

    with monkeypatch.context() as patch:
        patch.setattr(writes, "token", ordered)
        later = write(
            env,
            (
                AddEntitySupport(
                    kind="entity_support",
                    local_id="later",
                    entity=StoredEntity(kind="stored", entity_id=subject),
                    name="Project",
                    entity_type="project",
                    support=env.support,
                ),
            ),
        )["later"]
    witness = (
        KnowledgeService(env.database, env.service.identity).entity(env.scope, subject).witness
    )
    assert later < witness.contribution_id
    saved = receipt(env.service.write(put(env.scope, external="second", namespace="email")))
    ref = (
        env.service.anchors(
            env.scope,
            saved.document_id,
            saved.processing.state_version,
        )
        .entries[0]
        .reference
    )
    support = SourceSupport(kind="source", evidence=(*env.support.evidence, ref))
    dependency = DocumentDependency(
        source_namespace="email",
        document_id=saved.document_id,
        revision_id=saved.revision_id,
        state_version=saved.processing.state_version,
    )
    record = write(
        env,
        (
            AddAssertion(
                kind="assertion",
                local_id="both",
                subject=StoredEntity(kind="stored", entity_id=subject),
                predicate="work:decision",
                object=StringObject(kind="string", value="Ship"),
                interpretation="explicit",
                support=support,
            ),
        ),
        dependencies=(env.dependency, dependency),
    )["both"]
    request = plan(env, subject)
    with QueryService(env.database, env.service.identity) as service:
        result = service.execute(request)
        assert result.result.data.count == 1, result
        retained = service._support.sets[result.result.result_set_id]
        member = DecisionSelectionItem.model_validate_json(retained.members[0])
        assert member.dependencies.subject_witness == witness
        assert member.record.record_id == record
        assert member.record.support == support
        page = service.inspect_support(inspection(request, result.result))
        assert page.records == (member.record,)
        # The whole assertion disappears under a valid narrower scope; no shortened support.
        scoped = request.scope.model_copy(
            update={
                "access": request.scope.access.model_copy(
                    update={
                        "namespaces": ("markdown",),
                    }
                )
            }
        )
        hidden = service.execute(request.model_copy(update={"scope": scoped}))
        assert hidden.result.data.count == 0 and hidden.result.data.exact
        assert hidden.result.records_examined == 1
        env.service.write(
            put(
                env.scope,
                external="second",
                namespace="email",
                state=saved.processing.state_version,
                text="Changed",
            )
        )
        no_data(service.inspect_support(inspection(request, result.result)), "state_changed")
        assert service.execute(request).result.data.count == 0


def test_deadline_close_and_expiry_do_not_publish_or_release_late(tmp_path, monkeypatch):
    env = setup(tmp_path / "late.db")
    subject, _ = produce(env, 1)
    request = plan(env, subject)
    with QueryService(env.database, env.service.identity) as service:
        no_data(
            service.execute(
                request.model_copy(
                    update={
                        "budget": QueryBudget(max_milliseconds=1),
                    }
                )
            ),
            "time_budget",
        )
        assert not service._support.sets
        result = service.execute(request)
        original = service._run_worker

        def expire(*args):
            elapsed = original(*args)
            service._support.sets[result.result.result_set_id].expires = time.monotonic() - 1
            return elapsed

        monkeypatch.setattr(service, "_run_worker", expire)
        no_data(service.inspect_support(inspection(request, result.result)), "not_found")
    assert service._support.bytes == 0


def test_executable_count_inspection_example(tmp_path):
    import json
    import subprocess
    import sys
    from pathlib import Path

    env = setup(tmp_path / "example.db")
    subject, expected = produce(env, 25)
    request = plan(env, subject)
    path = tmp_path / "query.json"
    path.write_text(request.model_dump_json())
    result = subprocess.run(
        [
            sys.executable,
            str(Path("examples/query_decisions.py")),
            str(env.database.path),
            str(path),
            env.service.identity.principal_id,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=35,
    )
    execution, page = [json.loads(line) for line in result.stdout.splitlines()]
    assert execution["result"]["data"]["count"] == 25
    assert {r["record_id"] for r in page["records"]} == expected


def test_diagnostics_capacity_does_not_spend_business_revalidation_budget(tmp_path, monkeypatch):
    env = setup(tmp_path / "report-parity.db")
    subject, expected = produce(env, 133)
    request = plan(env, subject)
    with QueryService(env.database, env.service.identity) as service:
        execution = service.execute(request)
        value = inspection(request, execution.result)
        normal = service.inspect_support_explained(value)
        assert normal.outcome.error is None, normal
        assert isinstance(normal.report, ExecutionReport)
        original = service._collector.begin_capture

        def unavailable(*args, **kwargs):
            capture = original(*args, **kwargs)
            capture.group.discard()
            return capture

        monkeypatch.setattr(service._collector, "begin_capture", unavailable)
        unavailable_result = service.inspect_support_explained(value)
        assert unavailable_result.outcome.error is None, unavailable_result
        assert not isinstance(unavailable_result.report, ExecutionReport)
        assert normal.outcome.records == unavailable_result.outcome.records
        assert {r.record_id for r in normal.outcome.records} == expected
        assert normal.outcome.records_examined == unavailable_result.outcome.records_examined == 133


def test_inspection_quota_is_reserved_before_retained_payload_copy(tmp_path, monkeypatch):
    from kg.query import _retention
    from kg.query._retention import size

    class NoEncoding(str):
        def encode(self, *args, **kwargs):
            raise AssertionError("Retained payload encoded before reservation")

    env = setup(tmp_path / "copy.db")
    subject, _ = produce(env, 1)
    request = plan(env, subject)
    with QueryService(env.database, env.service.identity) as service:
        execution = service.execute(request)
        retained = service._support.sets[execution.result.result_set_id]
        retained.members[0] = NoEncoding(retained.members[0])
        value = inspection(request, execution.result)
        assert service.inspect_support(value).error is None
        monkeypatch.setattr(
            _retention,
            "TOTAL_BYTES",
            service._support.bytes + size(request.scope) * 3 + 16384,
        )
        no_data(service.inspect_support(value), "retention_limit")


def test_ambiguity_report_labels_actual_entity_output(tmp_path):
    from kg.models.execution import ExplainOptions

    env = setup(tmp_path / "ambiguity-report.db")
    first, _ = produce(env, 0)
    second, _ = produce(env, 0)
    request = plan(env, name="Project")
    with QueryService(env.database, env.service.identity) as service:
        result = service.execute_explained(request, ExplainOptions(detail="detailed"))
        assert result.outcome.result.outcome == "ambiguous"
        assert isinstance(result.report, ExecutionReport)
        event = next(e.event for e in result.report.events if e.event.kind == "query.output")
        assert event.semantics == "entity"
        assert event.selected_ids == tuple(sorted((first, second)))
        assert event.support_set_id is None


def test_report_eviction_does_not_remove_valid_retained_support(tmp_path):
    env = setup(tmp_path / "independent-retention.db")
    subject, expected = produce(env, 25)
    request = plan(env, subject)
    with QueryService(env.database, env.service.identity) as service:
        explained = service.execute_explained(request)
        assert isinstance(explained.report, ExecutionReport)
        for _ in range(33):
            service.capabilities(env.scope)
        assert not isinstance(
            service.diagnostics.report(env.scope, explained.report.report_id), ExecutionReport,
        )
        inspected = service.inspect_support(inspection(request, explained.outcome.result))
        assert inspected.error is None
        assert {record.record_id for record in inspected.records} == expected
