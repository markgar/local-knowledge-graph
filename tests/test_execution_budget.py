from __future__ import annotations

import copy
import sqlite3
import time

import pytest

from kg._execution_budget import (
    Deadline,
    DeadlineStop,
    LocalExecutionMeter,
    PrivateBudget,
    PrivateResourceStop,
    PublicAccounting,
    PublicBudgetStop,
)
from kg.evidence._sql import AccountedConnection


def pool() -> PrivateBudget:
    return PrivateBudget(Deadline(time.monotonic() + 30))


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


@pytest.mark.parametrize("amount", [0, -1, True, 1.5])
def test_invalid_reservations(amount) -> None:
    budget = pool()
    with pytest.raises(ValueError):
        budget.reserve_visits(amount)
    with pytest.raises(ValueError):
        budget.reserve_vm(amount)


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
