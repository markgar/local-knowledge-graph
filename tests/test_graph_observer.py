from __future__ import annotations

import copy
import pickle
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event

import pytest
from support.evidence import environment, put, receipt

from kg._execution_budget import (
    CancelledStop,
    Deadline,
    DeadlineStop,
    LocalExecutionMeter,
    PrivateBudget,
    PrivateResourceStop,
    _graph_build_operation,
)
from kg.evidence import EvidenceDatabase, EvidenceServiceError
from kg.evidence import _graph_observer as graph
from kg.evidence._graph_observer import GraphSourceCleanupError, open_graph_source
from kg.evidence._read_context import read_evidence
from kg.evidence._sql import AccountedConnection, AccountedCursor
from kg.indexing._selection import ProjectionHandle
from kg.models.evidence import LocalIdentity, PolicyGrant


def pool():
    return PrivateBudget(Deadline(time.monotonic() + 30))


def meter(budget):
    return LocalExecutionMeter(budget, max_operations=16, max_items=100)


def operation(env, source, budget=None, *, binding=None, identity=None, scope=None, deadline=None):
    budget = pool() if budget is None else budget
    return source.operation(
        source.binding if binding is None else binding,
        env.service.identity if identity is None else identity,
        env.scope if scope is None else scope,
        budget.deadline if deadline is None else deadline,
        budget,
    )


@pytest.fixture
def opened(tmp_path):
    env = environment(tmp_path / "graph.db")
    budget = pool()
    source = open_graph_source(
        env.database, env.service.identity, env.scope, budget.deadline, budget,
    )
    try:
        yield env, source, budget
    finally:
        source.close()


@contextmanager
def failure(code):
    with pytest.raises(EvidenceServiceError) as caught:
        yield
    assert caught.value.failure.code == code


@pytest.mark.service
def test_idle_and_one_hundred_operations_keep_physical_baseline(opened, monkeypatch):
    env, source, old = opened
    connection, baseline, binding = source._connection, source._version, source.binding
    old_usage = old._visits, old._vm
    statements = []
    connection.set_trace_callback(statements.append)
    monkeypatch.setattr(time, "monotonic", lambda: old.deadline.expires_at_monotonic + 60)
    assert statements == []
    assert not connection.in_transaction and connection._budget is None
    sessions = set()
    for _ in range(100):
        budget = pool()
        execution = meter(budget)
        with operation(env, source, budget) as current:
            assert current.budget is budget
            sessions.add(current.session_id)
            with current.read_context(execution.begin_step("read")) as context:
                assert context.session_id == current.session_id
                assert context.connection.execute("SELECT 1").fetchone()[0] == 1
            with current.release_fence() as fence:
                assert fence.session_id == current.session_id
                fence.check_active()
        assert connection._budget is None and not connection.in_transaction
        assert source._connection is connection and source._version == baseline
        assert source.binding is binding
        assert execution.public_accounting().items_consumed == 0
        assert budget._vm > 0 and budget._visits > 0
    assert len(sessions) == 100
    assert (old._visits, old._vm) == old_usage


@pytest.mark.service
def test_three_scopes_share_root_but_not_snapshot_authority(opened):
    env, source, budget = opened
    execution = meter(budget)
    counters, sessions = [], []
    with operation(env, source, budget) as export:
        sessions.append(export.session_id)
        with export.read_context(execution.begin_step("export")) as snapshot:
            handle = ProjectionHandle(snapshot.session_id, env.scope, "config", ())
            handle.check(snapshot)
        with export.release_fence() as export_fence:
            export_fence.check_active()
    counters.append((budget._visits, budget._vm))
    with operation(env, source, budget) as adoption:
        sessions.append(adoption.session_id)
        with failure("invalid_request"):
            export_fence.check_active()
        with adoption.release_fence() as adoption_fence:
            adoption_fence.check_active()
    counters.append((budget._visits, budget._vm))
    with operation(env, source, budget) as answer:
        sessions.append(answer.session_id)
        with answer.read_context(execution.begin_step("answer")) as context:
            with failure("invalid_request"):
                handle.check(context)
            with failure("invalid_request"):
                snapshot.check_active()
            with failure("invalid_request"):
                adoption_fence.check_active()
        with answer.release_fence():
            pass
    counters.append((budget._visits, budget._vm))
    assert len(set(sessions)) == 3
    assert counters[0][0] < counters[1][0] < counters[2][0]
    assert counters[0][1] < counters[1][1] < counters[2][1]


@pytest.mark.service
def test_bulk_build_root_spans_three_scopes_then_parks_for_interactive_request(tmp_path):
    env = environment(tmp_path / "bulk-graph.db")
    build = _graph_build_operation(deadline=Deadline(time.monotonic() + 300), cancel=Event())
    budget = build.budget
    source = open_graph_source(
        env.database, env.service.identity, env.scope, budget.deadline, budget,
    )
    connection, baseline = source._connection, source._version
    previous = build.snapshot()
    sessions = set()
    try:
        for phase in ("export", "adoption", "answer"):
            with operation(env, source, budget) as current:
                sessions.add(current.session_id)
                assert current.budget is budget and current.deadline is budget.deadline
                if phase != "adoption":
                    with current.read_context(build.meter) as context:
                        assert context.meter is build.meter
                        assert context.connection._budget is budget
                        held = context._reserve_scratch(8192, "general")
                        assert build.snapshot().scratch_live_bytes == 8192
                        context._release_reservation(held)
                        assert build.snapshot().scratch_live_bytes == 0
                        timeout = context.connection.execute("PRAGMA busy_timeout").fetchone()[0]
                        assert timeout == 100
                with current.release_fence() as fence:
                    fence.check_active()
            observed = build.snapshot()
            assert observed.visits_reserved > previous.visits_reserved
            assert observed.vm_instructions_reserved > previous.vm_instructions_reserved
            assert observed.expires_at_monotonic == previous.expires_at_monotonic
            assert observed.scratch_live_bytes == 0
            previous = observed
            assert connection._budget is None and not connection.in_transaction
        assert len(sessions) == 3
        build.cancel.set()
        with operation(env, source) as current:
            assert current.session_id not in sessions
            assert current.budget.resource_profile == "interactive/1"
            with current.read_context(meter(current.budget).begin_step("read")) as context:
                assert context.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
            with current.release_fence():
                pass
        assert build.snapshot() == previous
        assert source._connection is connection and source._version == baseline
    finally:
        source.close()


@pytest.mark.service
@pytest.mark.parametrize("cancel_phase", range(3))
def test_bulk_cancellation_unwinds_current_scope_without_losing_original_source(
    tmp_path, cancel_phase,
):
    env = environment(tmp_path / "bulk-cancel.db")
    build = _graph_build_operation(deadline=Deadline(time.monotonic() + 300), cancel=Event())
    budget = build.budget
    source = open_graph_source(
        env.database, env.service.identity, env.scope, budget.deadline, budget,
    )
    connection, baseline = source._connection, source._version
    try:
        for _ in range(cancel_phase):
            with operation(env, source, budget) as current, current.release_fence():
                pass
        with (
            pytest.raises(CancelledStop),
            operation(env, source, budget) as current,
            current.read_context(build.meter) as context,
        ):
            context._reserve_scratch(8192, "general")
            build.cancel.set()
            context.connection.execute("SELECT 1")
        assert build.snapshot().stop_reason == "cancelled"
        assert build.snapshot().scratch_live_bytes == 0
        assert connection._budget is None and not connection.in_transaction
        with operation(env, source) as current, current.release_fence():
            pass
        assert source._connection is connection and source._version == baseline
    finally:
        source.close()


@pytest.mark.service
@pytest.mark.parametrize("phase", range(3))
def test_foreign_binding_is_rejected_in_every_cold_scope(opened, phase):
    env, source, budget = opened
    for _ in range(phase):
        with operation(env, source, budget) as current, current.release_fence():
            pass
    with failure("invalid_request"), operation(
        env, source, budget, binding=copy.copy(source.binding),
    ):
        pytest.fail("Copied source binding admitted")
    with operation(env, source, budget) as current, current.release_fence():
        pass


@pytest.mark.service
@pytest.mark.parametrize("difference", [
    "principal", "corpus", "policy", "narrow", "broad", "grants", "namespace_order", "grant_order",
])
def test_exact_validated_identity_and_scope(opened, difference):
    env, source, _ = opened
    identity, scope = env.service.identity, env.scope
    access = scope.access
    if difference == "principal":
        identity = LocalIdentity(principal_id="another")
    elif difference == "corpus":
        scope = scope.model_copy(update={"corpus_id": "another"})
    else:
        changes = {
            "policy": {"policy_version": "another"},
            "narrow": {"namespaces": ("markdown",)},
            "broad": {"namespaces": (*access.namespaces, "other")},
            "grants": {"grants": ("read",)},
            "namespace_order": {"namespaces": tuple(reversed(access.namespaces))},
            "grant_order": {"grants": tuple(reversed(access.grants))},
        }[difference]
        scope = scope.model_copy(update={"access": access.model_copy(update=changes)})
    with failure("forbidden"), operation(env, source, identity=identity, scope=scope):
        pytest.fail("Different effective scope admitted")
    assert source._usable
    with operation(env, source) as current, current.release_fence():
        pass


@pytest.mark.service
def test_malformed_scope_and_mismatched_deadline_never_rebind(opened):
    env, source, _ = opened
    bad = env.scope.model_copy(update={"corpus_id": 1})
    with failure("invalid_request"), operation(env, source, scope=bad):
        pass
    with failure("invalid_request"), operation(
        env, source, deadline=Deadline(time.monotonic() + 100),
    ):
        pass
    assert source._connection._budget is None


@pytest.mark.process
def test_no_nested_rebinding_and_wrong_thread_cannot_close_owner(opened):
    env, source, _ = opened
    with operation(env, source) as current:
        with failure("invalid_request"), operation(env, source):
            pytest.fail("Nested root replaced")
        with failure("invalid_request"):
            source.close()
        with ThreadPoolExecutor(max_workers=1) as executor:
            with failure("invalid_request"):
                executor.submit(source.close).result()
            with failure("invalid_request"):
                executor.submit(lambda: operation(env, source).__enter__()).result()
        assert source._connection._budget is current.budget
        with current.release_fence():
            pass


@pytest.mark.service
@pytest.mark.parametrize("misuse", ["foreign_meter", "snapshot_twice", "fence_in_snapshot"])
def test_snapshot_misuse_aborts_the_operation(opened, misuse):
    env, source, _ = opened
    with operation(env, source) as current:
        step = meter(current.budget).begin_step("read")
        if misuse == "foreign_meter":
            with failure("invalid_request"), current.read_context(meter(pool()).begin_step("bad")):
                pass
        else:
            with current.read_context(step):
                if misuse == "fence_in_snapshot":
                    with failure("invalid_request"), current.release_fence():
                        pass
            if misuse == "snapshot_twice":
                with failure("invalid_request"), current.read_context(step):
                    pass
        with failure("invalid_request"), current.release_fence():
            pytest.fail("Aborted operation released")


@pytest.mark.service
def test_terminal_fence_and_expired_operation_are_not_reusable(opened):
    env, source, _ = opened
    with operation(env, source) as current:
        with current.release_fence() as fence:
            fence.check_active()
        with failure("invalid_request"):
            current.check_current()
        with failure("invalid_request"), current.release_fence():
            pass
    with failure("invalid_request"):
        fence.check_active()
    with failure("invalid_request"):
        current.check_current()


@pytest.mark.service
@pytest.mark.parametrize("stop", ["visits", "vm", "deadline"])
def test_stops_are_not_reset_but_a_later_request_can_reuse_source(opened, monkeypatch, stop):
    env, source, _ = opened
    budget = pool()
    with operation(env, source, budget) as current:
        if stop == "visits":
            budget.reserve_visits(100_000 - budget._visits)
        elif stop == "vm":
            budget.reserve_vm(10_000_000 - budget._vm)
        else:
            monkeypatch.setattr(time, "monotonic", lambda: budget.deadline.expires_at_monotonic)
        with pytest.raises(DeadlineStop if stop == "deadline" else PrivateResourceStop):
            current.check_current()
        with failure("invalid_request"), current.release_fence():
            pass
    consumed = budget._visits, budget._vm
    with operation(env, source) as current, current.release_fence():
        pass
    assert (budget._visits, budget._vm) == consumed


@pytest.mark.service
@pytest.mark.parametrize(
    "gap", ["before_snapshot", "in_snapshot", "after_snapshot", "after_export"],
)
def test_actual_commit_at_every_gap_withholds_all_payload(opened, gap):
    env, source, _ = opened
    released = None
    provisional = {"ids": ("entity",), "count": 1, "paths": ("proof",), "citations": ("quote",)}
    with failure("state_changed"):
        if gap == "after_export":
            with operation(env, source) as export, export.release_fence():
                pass
            receipt(env.service.write(put(env.scope)))
        with operation(env, source) as current:
            if gap == "before_snapshot":
                receipt(env.service.write(put(env.scope)))
            with current.read_context(meter(current.budget).begin_step("read")) as context:
                count = context.connection.execute("SELECT count(*) FROM document").fetchone()[0]
                if gap == "in_snapshot":
                    receipt(env.service.write(put(env.scope)))
                    assert context.connection.execute(
                        "SELECT count(*) FROM document",
                    ).fetchone()[0] == count
            if gap == "after_snapshot":
                receipt(env.service.write(put(env.scope)))
            with current.release_fence():
                pending = provisional
            released = pending
    assert released is None and not source._usable


@pytest.mark.service
@pytest.mark.parametrize("change", ["new", "metadata", "restore", "unrelated", "policy", "revoke"])
def test_idle_commits_and_policy_changes_invalidate(tmp_path, change):
    env = environment(tmp_path / "change.db")
    saved = receipt(env.service.write(put(env.scope, text="A")))
    budget = pool()
    source = open_graph_source(
        env.database, env.service.identity, env.scope, budget.deadline, budget,
    )
    try:
        if change == "new":
            receipt(env.service.write(put(env.scope, external="new")))
        elif change in ("metadata", "restore"):
            updated = receipt(env.service.write(put(
                env.scope, text="B" if change == "restore" else "A",
                title="Changed", state=saved.processing.state_version,
            )))
            if change == "restore":
                receipt(env.service.write(put(
                    env.scope, text="A", state=updated.processing.state_version,
                )))
        elif change == "unrelated":
            with env.database.transaction() as writer:
                writer.execute("UPDATE processing_guard SET epoch=epoch+1")
        else:
            policy = env.policy
            if change == "revoke":
                policy = policy.model_copy(update={
                    "grants": tuple(g for g in policy.grants if g.grant != "read"),
                })
            else:
                policy = policy.model_copy(update={"grants": (
                    *policy.grants,
                    PolicyGrant(principal_id="other", namespace="email", grant="read"),
                )})
            env.admin.replace_policy(policy, env.scope.access.policy_version)
        with (
            failure("forbidden" if change in ("policy", "revoke") else "state_changed"),
            operation(env, source),
        ):
            pytest.fail("Changed source reused")
        assert not source._usable
    finally:
        source.close()


@pytest.mark.service
def test_final_fence_reauthorizes_and_excludes_writers_only_while_active(opened):
    env, source, _ = opened
    with env.database.connection() as witness:
        version = witness.execute("PRAGMA data_version").fetchone()[0]
        with operation(env, source) as current:
            with env.database.connection() as writer:
                writer.execute("BEGIN IMMEDIATE")
                writer.rollback()
            with current.release_fence(), env.database.connection() as writer:
                writer.execute("PRAGMA busy_timeout=1")
                with pytest.raises(sqlite3.OperationalError):
                    writer.execute("BEGIN IMMEDIATE")
            with env.database.connection() as writer:
                writer.execute("BEGIN IMMEDIATE")
                writer.rollback()
        assert witness.execute("PRAGMA data_version").fetchone()[0] == version


@pytest.mark.service
def test_revocation_after_snapshot_is_forbidden_at_fence(opened):
    env, source, _ = opened
    with operation(env, source) as current:
        with current.read_context(meter(current.budget).begin_step("read")):
            pass
        policy = env.policy.model_copy(update={
            "grants": tuple(g for g in env.policy.grants if g.grant != "read"),
        })
        env.admin.replace_policy(policy, env.scope.access.policy_version)
        with failure("forbidden"), current.release_fence():
            pytest.fail("Revocation disclosed a result")


@pytest.mark.service
@pytest.mark.parametrize("loss", ["close", "raw_close", "replace"])
def test_lost_physical_observer_is_never_replaced(opened, loss):
    env, source, _ = opened
    original = source._connection
    replacement = None
    if loss == "close":
        original.close()
    elif loss == "raw_close":
        sqlite3.Connection.close(original)
    else:
        replacement = env.database._open(create=False)
        source._connection = replacement
    try:
        with failure("state_changed"), operation(env, source):
            pytest.fail("Lost connection admitted")
        assert not source._usable
    finally:
        if replacement is not None:
            replacement.close()


@pytest.mark.service
def test_fresh_observer_and_serialized_binding_cannot_certify_old_source(opened):
    env, old, _ = opened
    budget = pool()
    fresh = open_graph_source(
        env.database, env.service.identity, env.scope, budget.deadline, budget,
    )
    try:
        assert fresh._version == old._version
        for binding in (old.binding, pickle.loads(pickle.dumps(fresh.binding))):
            with failure("invalid_request"), operation(env, fresh, binding=binding):
                pass
        for value in (old, fresh):
            for clone in (copy.copy, copy.deepcopy, pickle.dumps):
                with pytest.raises(TypeError):
                    clone(value)
        with operation(env, fresh) as current:
            for clone in (copy.copy, copy.deepcopy, pickle.dumps):
                with pytest.raises(TypeError):
                    clone(current)
    finally:
        fresh.close()


@pytest.mark.service
def test_database_path_is_pinned_and_no_live_snapshot_can_escape(opened, tmp_path):
    env, source, _ = opened
    env.database.path = tmp_path / "not-this-source.db"
    with operation(env, source) as current:
        scope = current.read_context(meter(current.budget).begin_step("read"))
        context = scope.__enter__()
        assert context.connection.execute("SELECT 1").fetchone()[0] == 1
    with failure("invalid_request"):
        context.check_active()
    scope.__exit__(None, None, None)
    with operation(env, source) as current, current.release_fence():
        pass


@pytest.mark.service
@pytest.mark.parametrize("exit_failure", ["deadline", "rollback", "close"])
def test_post_body_failure_withholds_tentative_publication(opened, monkeypatch, exit_failure):
    env, source, _ = opened
    ready = None
    try:
        with operation(env, source) as current, current.release_fence():
            ready = "tentative"
            if exit_failure == "deadline":
                monkeypatch.setattr(
                    time, "monotonic", lambda: current.deadline.expires_at_monotonic,
                )
            else:
                original = getattr(AccountedConnection, exit_failure)

                def fail(connection):
                    original(connection)
                    raise sqlite3.OperationalError("injected exit failure")

                monkeypatch.setattr(AccountedConnection, exit_failure, fail)
    except (EvidenceServiceError, DeadlineStop):
        ready = None
    else:
        pytest.fail("Post-body failure released readiness")
    assert ready is None


@pytest.mark.service
def test_exact_evidence_survives_unchanged_source_across_requests(tmp_path):
    env = environment(tmp_path / "evidence.db")
    saved = receipt(env.service.write(put(env.scope)))
    reference = env.service.anchors(
        env.scope, saved.document_id, saved.processing.state_version,
    ).entries[0].reference
    budget = pool()
    source = open_graph_source(
        env.database, env.service.identity, env.scope, budget.deadline, budget,
    )
    try:
        for _ in range(3):
            with operation(env, source) as current:
                with current.read_context(meter(current.budget).begin_step("evidence")) as context:
                    evidence = read_evidence(context, reference)
                with current.release_fence():
                    assert evidence.quote == "A\r\nCafe\u0301 \U0001f680"
    finally:
        source.close()


@pytest.mark.service
@pytest.mark.parametrize("setup_failure", ["before_setup", "pragma", "admission"])
@pytest.mark.parametrize("closed_before_error", [False, True])
def test_factory_custody_before_setup_and_confirmation_aware_retry(
    tmp_path, monkeypatch, setup_failure, closed_before_error,
):
    env = environment(tmp_path / "custody.db")
    budget = pool()
    acquired = []
    initialize = graph._initialize_connection
    execute = AccountedConnection.execute
    close = AccountedConnection._close_observer
    setup_error = sqlite3.OperationalError("setup failed")
    cleanup_calls = []

    def setup(connection, *, budget):
        acquired.append(connection)
        assert connection._budget is None and connection.row_factory is None
        if setup_failure == "before_setup":
            raise setup_error
        initialize(connection, budget=budget)

    def statement(connection, sql, parameters=()):
        if connection in acquired and sql == "PRAGMA foreign_keys=ON" and setup_failure == "pragma":
            raise setup_error
        return execute(connection, sql, parameters)

    def failed_close(connection):
        cleanup_calls.append(connection)
        connection._closed = True
        if closed_before_error:
            close(connection)
        raise sqlite3.OperationalError("physical close failed")

    monkeypatch.setattr(graph, "_initialize_connection", setup)
    monkeypatch.setattr(AccountedConnection, "execute", statement)
    monkeypatch.setattr(AccountedConnection, "_close_observer", failed_close)
    if setup_failure == "admission":
        def rejected(*args):
            raise EvidenceServiceError("forbidden")
        monkeypatch.setattr(graph, "authorize", rejected)
    with pytest.raises(GraphSourceCleanupError) as caught:
        open_graph_source(env.database, env.service.identity, env.scope, budget.deadline, budget)
    error = caught.value
    expected = "forbidden" if setup_failure == "admission" else "internal_error"
    assert error.original_failure.failure.code == expected
    assert error.failure.code == "internal_error"
    source = error.source
    assert source._physical is acquired[0] is cleanup_calls[0]
    assert source._physical._closed and not source._close_confirmed and not source._usable
    assert source._operation is None
    monkeypatch.setattr(AccountedConnection, "_close_observer", close)
    source.close()
    assert source._close_confirmed and source._physical is None
    source.close()
    with pytest.raises(sqlite3.ProgrammingError):
        sqlite3.Connection.execute(acquired[0], "SELECT 1")


@pytest.mark.service
def test_factory_preserves_stop_subtype_and_original_failure_identity(tmp_path, monkeypatch):
    env = environment(tmp_path / "cancelled.db")
    budget = pool()

    class CancelledStop(PrivateResourceStop):
        pass

    original = CancelledStop()
    close = AccountedConnection._close_observer

    def stopped(*args, **kwargs):
        raise original

    def failed_close(connection):
        raise sqlite3.OperationalError("cleanup failed")

    monkeypatch.setattr(graph, "_initialize_connection", stopped)
    monkeypatch.setattr(AccountedConnection, "_close_observer", failed_close)
    with pytest.raises(GraphSourceCleanupError) as caught:
        open_graph_source(env.database, env.service.identity, env.scope, budget.deadline, budget)
    assert caught.value.original_failure is original
    monkeypatch.setattr(AccountedConnection, "_close_observer", close)
    caught.value.source.close()


@pytest.mark.service
@pytest.mark.parametrize("phase", ["entry", "poll", "fence"])
def test_current_operation_preserves_latched_stop_subtype(opened, monkeypatch, phase):
    env, source, _ = opened
    budget = pool()

    class CancelledStop(PrivateResourceStop):
        pass

    original = CancelledStop()

    def stopped():
        raise original

    with pytest.raises(CancelledStop) as caught:
        if phase == "entry":
            monkeypatch.setattr(budget, "reserve_sql_quantum", stopped)
        with operation(env, source, budget) as current:
            monkeypatch.setattr(budget, "reserve_sql_quantum", stopped)
            if phase == "poll":
                current.check_current()
            else:
                with current.release_fence():
                    pytest.fail("Cancelled operation released")
    assert caught.value is original
    assert source._usable and source._connection._budget is None
    with operation(env, source) as current, current.release_fence():
        pass


@pytest.mark.service
@pytest.mark.parametrize("visit", [1, 2, 3])
def test_retained_authorization_failure_cannot_pin_an_idle_snapshot(opened, monkeypatch, visit):
    env, source, _ = opened
    budget = pool()
    reserve = budget.reserve_visits
    visits = 0

    def stop_before_fetch(n=1):
        nonlocal visits
        visits += 1
        if visits == visit:
            raise PrivateResourceStop()
        reserve(n)

    with operation(env, source, budget) as current:
        monkeypatch.setattr(budget, "reserve_visits", stop_before_fetch)
        with pytest.raises(PrivateResourceStop) as retained:
            current.check_current()
    assert retained.value.__traceback__ is not None
    assert not source._connection.in_transaction
    receipt(env.service.write(put(env.scope)))
    with failure("state_changed"), operation(env, source) as current, current.release_fence():
        pytest.fail("Retained authorization cursor hid the external commit")


@pytest.mark.service
def test_authorization_execute_failure_also_closes_the_owned_cursor(opened, monkeypatch):
    env, source, _ = opened
    execute = AccountedCursor.execute

    def fail_after_execute(cursor, sql, parameters=()):
        result = execute(cursor, sql, parameters)
        if cursor.connection is source._connection and sql.startswith("SELECT policy_version"):
            raise PrivateResourceStop()
        return result

    with operation(env, source) as current:
        monkeypatch.setattr(AccountedCursor, "execute", fail_after_execute)
        with pytest.raises(PrivateResourceStop) as retained:
            current.check_current()
    monkeypatch.setattr(AccountedCursor, "execute", execute)
    assert retained.value.__traceback__ is not None
    receipt(env.service.write(put(env.scope)))
    with failure("state_changed"), operation(env, source):
        pytest.fail("Failed execute pinned an implicit snapshot")


@pytest.mark.service
def test_direct_close_failure_keeps_cleanup_owner_until_confirmation(opened, monkeypatch):
    env, source, _ = opened
    physical = source._physical
    close = AccountedConnection._close_observer

    def failed_close(connection):
        connection._closed = True
        raise sqlite3.OperationalError("failed close")

    monkeypatch.setattr(AccountedConnection, "_close_observer", failed_close)
    with failure("internal_error"):
        source.close()
    assert source._physical is physical and not source._close_confirmed
    with failure("state_changed"), operation(env, source):
        pass
    monkeypatch.setattr(AccountedConnection, "_close_observer", close)
    source.close()
    source.close()
    assert source._close_confirmed and source._physical is None


@pytest.mark.service
def test_factory_failure_with_successful_cleanup_reraises_original(tmp_path, monkeypatch):
    env = environment(tmp_path / "failure.db")
    budget = pool()
    original = EvidenceServiceError("unsupported")
    acquired = []

    def setup(connection, *, budget):
        acquired.append(connection)
        raise original

    monkeypatch.setattr(graph, "_initialize_connection", setup)
    with pytest.raises(EvidenceServiceError) as caught:
        open_graph_source(env.database, env.service.identity, env.scope, budget.deadline, budget)
    assert caught.value is original
    with pytest.raises(sqlite3.ProgrammingError):
        sqlite3.Connection.execute(acquired[0], "SELECT 1")


@pytest.mark.service
def test_ordinary_open_reuses_setup_and_ordinary_close_path(tmp_path, monkeypatch):
    from kg.evidence import database

    env = environment(tmp_path / "ordinary.db")
    setup = database._initialize_connection
    close = AccountedConnection.close
    events = []

    def initialize(connection, *, budget):
        events.append(("setup", budget))
        setup(connection, budget=budget)

    def ordinary_close(connection):
        events.append(("close", None))
        close(connection)

    def unexpected(connection):
        pytest.fail("Ordinary caller used graph close")

    monkeypatch.setattr(database, "_initialize_connection", initialize)
    monkeypatch.setattr(AccountedConnection, "close", ordinary_close)
    monkeypatch.setattr(AccountedConnection, "_close_observer", unexpected)
    connection = env.database._open(create=False)
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert connection.row_factory is sqlite3.Row and connection._budget is None
    connection.close()
    assert events == [("setup", None), ("close", None)]
    with pytest.raises(EvidenceServiceError):
        graph.GraphSourceObserver(env.database, env.service.identity, env.scope, _key=object())


@pytest.mark.service
def test_missing_database_does_not_create_one(tmp_path):
    path = tmp_path / "missing.db"
    env = environment(tmp_path / "available.db")
    budget = pool()
    with failure("internal_error"):
        open_graph_source(
            EvidenceDatabase(path), env.service.identity, env.scope, budget.deadline, budget,
        )
    assert not path.exists()
