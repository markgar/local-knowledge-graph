from __future__ import annotations

import copy
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from threading import Event

import pytest

from kg._execution_budget import (
    BudgetedStep,
    CancelledStop,
    Deadline,
    DeadlineStop,
    LocalExecutionMeter,
    PrivateBudget,
    PrivateResourceStop,
    PublicAccounting,
    PublicBudgetStop,
    _graph_build_operation,
    _selection_budget,
)
from kg.evidence._sql import AccountedConnection
from kg.evidence.errors import EvidenceServiceError


def pool() -> PrivateBudget:
    return PrivateBudget(Deadline(time.monotonic() + 30))


@pytest.mark.unit
def test_exact_five_stage_schedule_and_stop_before_fifth() -> None:
    stages = (
        "search_temp", "search_lexical", "search_vector", "search_rerank", "search_final_evidence",
    )
    for limit in (4, 5):
        meter = LocalExecutionMeter(pool(), max_operations=2, max_items=limit)
        step = meter.begin_step("search")
        emitted = []
        try:
            for stage in stages:
                step.reserve_public(stage)
                emitted.append(stage)
        except PublicBudgetStop:
            assert limit == 4
        assert len(emitted) == limit
        assert meter.public_accounting() == PublicAccounting(1, limit)
        # Counts of existing selections, readiness and fusion have no public reservation.
        meter.begin_step("count")
        assert meter.public_accounting() == PublicAccounting(2, limit)
        with pytest.raises(PublicBudgetStop):
            meter.begin_step("empty")


@pytest.mark.unit
def test_steps_inherit_one_private_pool_no_hidden_public_charges() -> None:
    budget = pool()
    meter = LocalExecutionMeter(budget, max_operations=3, max_items=10)
    first, second = meter.begin_step("one"), meter.begin_step("two")
    assert first.private_budget is second.private_budget is budget
    first.reserve_visits(99_999)
    second.reserve_visits()
    with pytest.raises(PrivateResourceStop):
        first.reserve_visits()
    assert meter.public_accounting() == PublicAccounting(2, 0)
    assert vars(meter.public_accounting()) == {"operations_executed": 2, "items_consumed": 0}


@pytest.mark.unit
def test_scratch_aggregate_unit_copy_and_idempotent_release() -> None:
    budget = pool()
    held = budget.reserve_scratch(64 << 20, "general")
    with pytest.raises(PrivateResourceStop):
        budget.reserve_scratch(1, "general")
    with pytest.raises(TypeError):
        copy.copy(held)
    with pytest.raises(TypeError):
        copy.deepcopy(held)
    held.release()
    held.release()
    with budget.reserve_scratch(64 << 20, "general"), pytest.raises(PrivateResourceStop):
        budget.reserve_scratch(1, "general")
    for unit, limit in (("text", 8), ("context", 8), ("vector", 16), ("reranker", 8)):
        with pytest.raises(PrivateResourceStop):
            budget.reserve_scratch((limit << 20) + 1, unit)
    for sequences, tokens in ((9, 8192), (8, 8193)):
        with pytest.raises(PrivateResourceStop):
            budget.reserve_provider(
                1, unit="reranker", sequences=sequences, padded_token_positions=tokens,
            )
    with budget.reserve_provider(
        8 << 20, unit="reranker", sequences=8, padded_token_positions=8192,
    ):
        pass


@pytest.mark.unit
def test_deadline_and_lock_wait_are_bounded(monkeypatch) -> None:
    budget = pool()
    reservation = budget.reserve_scratch(1, "text")
    monkeypatch.setattr(time, "monotonic", lambda: budget.deadline.expires_at_monotonic)
    with pytest.raises(DeadlineStop):
        budget.reserve_visits()
    reservation.release()
    # Simulated monotonic replacement is removed before the real timed lock wait.
    monkeypatch.undo()
    expired_lock = PrivateBudget(Deadline(time.monotonic() + .01))
    expired_lock._lock.acquire()
    try:
        with pytest.raises(DeadlineStop):
            expired_lock.reserve_visits()
    finally:
        expired_lock._lock.release()


@pytest.mark.unit
@pytest.mark.parametrize("amount", [0, -1, True, 1.5])
def test_invalid_reservations(amount) -> None:
    budget = pool()
    with pytest.raises(ValueError):
        budget.reserve_visits(amount)
    with pytest.raises(ValueError):
        budget.reserve_vm(amount)


@pytest.mark.service
def test_sql_prepays_quantum_shrinks_remainder_and_never_refunds() -> None:
    budget = pool()
    connection = sqlite3.connect(":memory:", factory=AccountedConnection)
    connection.row_factory = sqlite3.Row
    connection._budget = budget
    try:
        budget.reserve_vm(9_996_500)
        assert connection.execute("SELECT 1").fetchone()[0] == 1
        assert budget._vm == 9_998_500
        assert connection.execute("SELECT 2").fetchone()[0] == 2
        assert budget._vm == 10_000_000
        with pytest.raises(PrivateResourceStop):
            connection.execute("SELECT 3")
    finally:
        connection.close()


@pytest.mark.service
def test_busy_timeout_helper_is_metered_before_its_first_instruction() -> None:
    budget = pool()
    connection = sqlite3.connect(":memory:", factory=AccountedConnection)
    connection._budget = budget
    try:
        budget.reserve_vm(9_999_999)
        with pytest.raises(PrivateResourceStop):
            connection.execute("SELECT 1")
        assert budget._vm == 10_000_000
    finally:
        connection.close()


@pytest.mark.service
def test_precise_progress_prepays_helpers_shrinks_remainder_and_never_refunds():
    budget = pool()
    connection = sqlite3.connect(":memory:", factory=AccountedConnection)
    connection._budget = budget
    try:
        budget.reserve_vm(9_996_500)
        with connection._precise_progress():
            assert connection.execute("SELECT 1").fetchone()[0] == 1
            assert budget._vm == 9_998_500
            with connection._precise_progress():
                assert connection.execute("SELECT 2").fetchone()[0] == 2
            assert connection._precise
            assert budget._vm == 10_000_000
        assert not connection._precise
        with pytest.raises(PrivateResourceStop), connection._precise_progress():
            connection.execute("SELECT 3")
        assert not connection._precise
    finally:
        connection.close()


@pytest.mark.service
def test_precise_progress_interrupt_preserves_local_pool_and_restores_mode():
    budget = pool()
    local = budget.limited(max_visits=10_000)
    connection = sqlite3.connect(":memory:", factory=AccountedConnection)
    connection._budget = local
    try:
        budget.reserve_vm(9_998_000)
        with pytest.raises(PrivateResourceStop), connection._precise_progress():
            connection.execute(
                "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<10000) "
                "SELECT sum(x) FROM n"
            ).fetchone()
        assert budget._vm == 10_000_000
        assert connection._budget is local and local.deadline is budget.deadline
        assert not connection._precise
    finally:
        connection.close()


@pytest.mark.service
def test_sql_progress_interrupt_is_typed_and_rows_charge_privately() -> None:
    budget = pool()
    connection = sqlite3.connect(":memory:", factory=AccountedConnection)
    connection.row_factory = sqlite3.Row
    connection._budget = budget
    try:
        budget.reserve_vm(9_998_000)
        with pytest.raises(PrivateResourceStop):
            connection.execute(
                "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<10000) "
                "SELECT sum(x) FROM n"
            ).fetchone()
        assert budget._vm == 10_000_000
    finally:
        connection.close()


ROWS = (
    "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<100001) "
    "SELECT x FROM n"
)


@pytest.mark.service
def test_nested_local_caps_enforce_actual_fetches_and_do_not_reset_global_pool():
    budget = pool()
    connection = sqlite3.connect(":memory:", factory=AccountedConnection)
    connection._budget = budget
    try:
        # Independent selections share the same 100k pool, not ten new pools.
        for _ in range(10):
            local = budget.limited(max_visits=10_000)
            with connection._using_budget(local):
                cursor = connection.execute(ROWS)
                assert len(cursor.fetchmany(9999)) == 9999
                with connection._using_budget(local.limited(max_visits=1)):
                    assert cursor.fetchone()[0] == 10000
                    with pytest.raises(PrivateResourceStop):
                        cursor.fetchone()
                with pytest.raises(PrivateResourceStop):
                    cursor.fetchone()
            # Re-entering a retained cap cannot reset it.
            with connection._using_budget(local), pytest.raises(PrivateResourceStop):
                cursor.fetchone()
        assert budget._visits == 100_000
        with (
            connection._using_budget(budget.limited(max_visits=10_000)),
            pytest.raises(PrivateResourceStop),
        ):
            connection.execute("SELECT 1").fetchone()
    finally:
        connection.close()


@pytest.mark.unit
def test_local_siblings_share_vm_scratch_and_deadline(monkeypatch):
    budget = pool()
    first = budget.limited(max_visits=2)
    second = budget.limited(max_visits=2)
    first.reserve_visits(2)
    second.reserve_visits(2)
    assert budget._visits == 4
    with first.reserve_scratch(64 << 20, "general"), pytest.raises(PrivateResourceStop):
        second.reserve_scratch(1, "text")
    with second.reserve_scratch(64 << 20, "general"):
        pass
    first.reserve_vm(9_999_999)
    assert second.reserve_sql_quantum() == 1
    with pytest.raises(PrivateResourceStop):
        first.reserve_vm(1)
    for child in (first, second):
        assert child.deadline is budget.deadline
        with pytest.raises(TypeError):
            copy.copy(child)
        with pytest.raises(TypeError):
            copy.deepcopy(child)
    monkeypatch.setattr(time, "monotonic", lambda: budget.deadline.expires_at_monotonic)
    with pytest.raises(DeadlineStop):
        second.reserve_visits()
    with pytest.raises(DeadlineStop):
        first.limited(max_visits=1)


@pytest.mark.unit
@pytest.mark.parametrize("amount", [0, -1, True, 1.5, 100_001])
def test_invalid_local_caps(amount):
    with pytest.raises(ValueError):
        pool().limited(max_visits=amount)


@pytest.mark.service
def test_connection_cannot_widen_or_replace_local_pool():
    budget = pool()
    local = budget.limited(max_visits=1)
    connection = sqlite3.connect(":memory:", factory=AccountedConnection)
    connection._budget = local
    try:
        for invalid in (budget, pool(), budget.limited(max_visits=1)):
            with pytest.raises(EvidenceServiceError) as error, connection._using_budget(invalid):
                pytest.fail("Unrelated/widened budget admitted")
            assert error.value.failure.code == "invalid_request"
        assert connection._budget is local
    finally:
        connection.close()


def bulk():
    return _graph_build_operation(deadline=Deadline(time.monotonic() + 300), cancel=Event())


@pytest.mark.unit
def test_bulk_factory_preserves_exact_operation_identity_and_frozen_fields():
    deadline, cancel = Deadline(time.monotonic() + 300), Event()
    operation = _graph_build_operation(deadline=deadline, cancel=cancel)
    assert operation.cancel is cancel
    assert operation.budget.deadline is deadline
    assert operation.meter.private_budget is operation.budget
    assert operation.budget.resource_profile == "graph-build/1"
    for name, replacement in (("budget", pool()), ("meter", None), ("cancel", Event())):
        with pytest.raises(FrozenInstanceError):
            setattr(operation, name, replacement)
    for invalid in (None, object(), 3):
        with pytest.raises(ValueError):
            _graph_build_operation(deadline=invalid, cancel=cancel)
        with pytest.raises(ValueError):
            _graph_build_operation(deadline=deadline, cancel=invalid)
    with pytest.raises(ValueError):
        _graph_build_operation(deadline=Deadline(time.monotonic() + 301), cancel=cancel)
    with pytest.raises(DeadlineStop):
        _graph_build_operation(deadline=Deadline(time.monotonic() - 1), cancel=cancel)
    cancel.set()
    with pytest.raises(CancelledStop):
        _graph_build_operation(deadline=deadline, cancel=cancel)


@pytest.mark.acceptance
def test_bulk_retained_nested_sql_views_exceed_interactive_totals_without_reset():
    operation = bulk()
    budget = operation.budget
    selection = _selection_budget(budget)
    nested = _selection_budget(selection)
    connection = sqlite3.connect(":memory:", factory=AccountedConnection)
    connection._budget = budget
    try:
        cursor = connection.execute(ROWS)
        count, previous = 0, 0
        while True:
            with connection._using_budget(selection), connection._using_budget(nested):
                page = cursor.fetchmany(137)
            count += len(page)
            current = operation.snapshot()
            assert current.visits_reserved > previous
            assert selection._visits == nested._visits == current.visits_reserved
            assert selection.deadline is nested.deadline is budget.deadline
            previous = current.visits_reserved
            if not page:
                break
        assert count == 100_001
        assert current.visits_reserved == count + 2  # Partial-page EOF and final empty fetch.
        sibling = _selection_budget(budget)
        sibling.reserve_visits(10_001)
        assert operation.snapshot().visits_reserved == previous + 10_001
        assert selection._visits == previous
        with (
            connection._using_budget(selection),
            pytest.raises(EvidenceServiceError),
            connection._using_budget(sibling),
        ):
            pass
    finally:
        connection.close()


@pytest.mark.unit
def test_bulk_explicit_limits_are_not_widened_and_stops_are_terminal():
    operation = bulk()
    finite = operation.budget.limited(max_visits=2)
    nested = _selection_budget(finite)
    nested.reserve_visits(2)
    with pytest.raises(PrivateResourceStop):
        nested.reserve_visits()
    assert finite._visits == nested._visits == operation.snapshot().visits_reserved == 2
    assert operation.snapshot().stop_reason == "resource"
    with pytest.raises(PrivateResourceStop):
        operation.budget.reserve_visits()
    for invalid in (0, -1, True, 1.5, 100_001):
        with pytest.raises(ValueError):
            bulk().budget.limited(max_visits=invalid)


@pytest.mark.acceptance
@pytest.mark.parametrize("precise", [False, True])
def test_bulk_sql_prepayment_and_progress_exceed_ten_million(precise):
    operation = bulk()
    budget = operation.budget
    connection = sqlite3.connect(":memory:", factory=AccountedConnection)
    connection._budget = _selection_budget(budget)
    try:
        if precise:
            with connection._precise_progress():
                for _ in range(5001):
                    connection.execute("SELECT 1").fetchone()
        else:
            connection.execute(
                "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL "
                "SELECT x+1 FROM n WHERE x<700000) SELECT sum(x) FROM n"
            ).fetchone()
        before = operation.snapshot()
        assert before.vm_instructions_reserved > 10_000_000
        assert before.vm_instructions_reserved % 1000 == 0
        connection.execute("SELECT 2").fetchone()
        assert operation.snapshot().vm_instructions_reserved == (
            before.vm_instructions_reserved + 2000
        )
        assert not connection._precise
    finally:
        connection.close()


@pytest.mark.unit
def test_bulk_semantic_schedule_stays_on_existing_step_abi():
    operation = bulk()
    child = _selection_budget(operation.budget)
    step = BudgetedStep(operation.meter, child)
    for _ in range(4000):
        for stage in ("resolve_entity", "decision_record", "support_member"):
            step.reserve_public(stage)
    step.reserve_visits(3)
    assert operation.snapshot().semantic_items_reserved == 12_000
    assert operation.snapshot().visits_reserved == 3
    assert not hasattr(step, "reserve_semantic")
    for stage, n in (("invalid", 1), ("support_member", 65), ("resolve_entity", True)):
        with pytest.raises(ValueError):
            step.reserve_public(stage, n)
    assert operation.snapshot().semantic_items_reserved == 12_000
    assert operation.snapshot().stop_reason is None


@pytest.mark.unit
@pytest.mark.parametrize("stop", ["cancelled", "deadline", "resource"])
def test_bulk_terminal_snapshots_and_cleanup_preserve_work_and_peak(stop, monkeypatch):
    operation = bulk()
    budget = operation.budget
    operation.meter.reserve_public("resolve_entity")
    budget.reserve_visits(17)
    budget.reserve_vm(10_000_001)
    held = budget.reserve_scratch(64 << 20, "general")
    if stop == "cancelled":
        operation.cancel.set()
        with pytest.raises(CancelledStop):
            budget.check_deadline()
        operation.cancel.clear()
    elif stop == "deadline":
        monkeypatch.setattr(time, "monotonic", lambda: budget.deadline.expires_at_monotonic)
        with pytest.raises(DeadlineStop):
            budget.check_deadline()
        monkeypatch.undo()
    else:
        with pytest.raises(PrivateResourceStop):
            budget.reserve_scratch(1, "general")
    expected = {"cancelled": CancelledStop, "deadline": DeadlineStop,
                "resource": PrivateResourceStop}[stop]
    before = operation.snapshot()
    assert before.stop_reason == stop
    for action in (budget.check_deadline, budget.reserve_visits, budget.reserve_sql_quantum):
        with pytest.raises(expected):
            action()
    held.release()
    held.release()
    after = operation.snapshot()
    assert after.scratch_live_bytes == 0
    assert before.scratch_live_bytes == after.scratch_peak_bytes == 64 << 20
    assert after.visits_reserved == before.visits_reserved == 17
    assert after.vm_instructions_reserved == before.vm_instructions_reserved == 10_000_001
    assert after.semantic_items_reserved == 1


@pytest.mark.service
@pytest.mark.parametrize("precise", [False, True])
def test_bulk_cancellation_during_sql_progress_and_between_batches(precise, monkeypatch):
    operation = bulk()
    budget = operation.budget
    connection = sqlite3.connect(":memory:", factory=AccountedConnection)
    connection._budget = budget
    original = budget.reserve_sql_quantum
    calls = 0

    def cancel_during_progress():
        nonlocal calls
        calls += 1
        if calls == 10:
            operation.cancel.set()
        return original()

    monkeypatch.setattr(budget, "reserve_sql_quantum", cancel_during_progress)
    try:
        with (
            connection._precise_progress() if precise else connection._using_budget(budget),
            pytest.raises(CancelledStop),
        ):
            connection.execute(
                "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL "
                "SELECT x+1 FROM n WHERE x<10000) SELECT sum(x) FROM n"
            ).fetchone()
        assert operation.snapshot().stop_reason == "cancelled"
        assert isinstance(connection._stop, CancelledStop)
        operation.cancel.clear()
        with pytest.raises(CancelledStop):
            connection.execute("SELECT 1")
    finally:
        connection.close()
    fresh = bulk()
    connection = sqlite3.connect(":memory:", factory=AccountedConnection)
    connection._budget = fresh.budget
    try:
        cursor = connection.execute(ROWS)
        assert len(cursor.fetchmany(200)) == 200
        fresh.cancel.set()
        with pytest.raises(CancelledStop):
            cursor.fetchmany(200)
        assert fresh.snapshot().visits_reserved == 200
    finally:
        connection.close()


@pytest.mark.process
def test_bulk_cancel_interrupts_root_lock_wait_without_replacing_deadline(monkeypatch):
    operation = bulk()
    checked = Event()
    original = operation.budget.check_deadline

    def check():
        checked.set()
        original()

    monkeypatch.setattr(operation.budget, "check_deadline", check)
    with ThreadPoolExecutor(max_workers=1) as executor:
        operation.budget._lock.acquire()
        try:
            future = executor.submit(operation.budget.reserve_visits)
            assert checked.wait(timeout=2)
            operation.cancel.set()
            with pytest.raises(CancelledStop):
                future.result(timeout=2)
        finally:
            operation.budget._lock.release()
    assert operation.snapshot().visits_reserved == 0
    assert operation.snapshot().stop_reason == "cancelled"


@pytest.mark.unit
@pytest.mark.parametrize("unit,limit", [("text", 8), ("context", 8),
                                      ("vector", 16), ("reranker", 8)])
def test_bulk_per_unit_scratch_limits_unchanged(unit, limit):
    operation = bulk()
    with pytest.raises(PrivateResourceStop):
        operation.budget.reserve_scratch((limit << 20) + 1, unit)
    assert operation.snapshot().stop_reason == "resource"
    assert operation.snapshot().scratch_live_bytes == 0


@pytest.mark.service
@pytest.mark.parametrize("profile,maximum", [("interactive", 5000), ("bulk", 100)])
def test_busy_timeout_profile_preserves_prepaid_helper(profile, maximum):
    budget = bulk().budget if profile == "bulk" else pool()
    connection = sqlite3.connect(":memory:", factory=AccountedConnection)
    connection._budget = budget
    try:
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == maximum
        assert budget._vm == 2000
    finally:
        connection.close()


@pytest.mark.unit
def test_remote_budget_profile_and_selection_keep_existing_rpc_contract():
    from kg.query._meter import RemoteBudget

    class StubRemote(RemoteBudget):
        def rpc(self, frame):
            assert frame.action == "limited" and frame.n == 10_000 and frame.view == 0
            return 7

    remote = StubRemote(None, Deadline(time.monotonic() + 30))
    assert remote.resource_profile == "interactive/1"
    child = _selection_budget(remote)
    assert isinstance(child, RemoteBudget)
    assert child.view == 7 and child.inherits(remote)
    assert child.resource_profile == "interactive/1"


@pytest.mark.service
def test_graph_observer_rebinding_charges_current_poll_and_clears_statement_stop():
    connection = sqlite3.connect(":memory:", factory=AccountedConnection)
    first, second = pool(), pool()
    try:
        with connection._observer_operation_budget(first):
            assert connection.execute("PRAGMA data_version").fetchone()[0] > 0
            assert first._vm == 2000 and first._visits == 1
            with pytest.raises(EvidenceServiceError), connection._observer_operation_budget(second):
                pytest.fail("Replaced active allowance")
            with pytest.raises(EvidenceServiceError), connection._using_budget(second):
                pytest.fail("Widened inherited allowance")
            first.reserve_vm(10_000_000 - first._vm - 2000)
            with pytest.raises(PrivateResourceStop):
                connection.execute(
                    "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<10000) "
                    "SELECT sum(x) FROM n",
                ).fetchone()
            assert connection._stop is not None
        assert connection._budget is connection._stop is None
        assert not connection._precise and connection._remaining == 0
        with connection._observer_operation_budget(second):
            assert connection.execute("PRAGMA data_version").fetchone()[0] > 0
        assert first._vm == 10_000_000
        assert second._vm == 2000 and second._visits == 1
        connection.execute("BEGIN")
        with pytest.raises(EvidenceServiceError), connection._observer_operation_budget(pool()):
            pass
        connection.rollback()
    finally:
        connection.close()
