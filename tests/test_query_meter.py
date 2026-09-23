import time

import pytest

from kg._execution_budget import Deadline, PrivateBudget, PrivateResourceStop
from kg.query._meter import Frame, Ledger


def test_authoritative_global_and_retained_local_views():
    pool = PrivateBudget(Deadline(time.monotonic() + 10))
    pool.reserve_visits(90_000)  # supervisor admission shares the worker's allowance
    ledger = Ledger(pool, 1, 1)
    local = ledger.reserve(Frame(action="limited", n=10_000)).value
    nested = ledger.reserve(Frame(action="limited", view=local, n=6000)).value
    assert ledger.reserve(Frame(action="visits", view=nested, n=5000)).state == "ok"
    assert ledger.reserve(Frame(action="visits", view=local, n=5000)).state == "ok"
    assert ledger.reserve(Frame(action="visits", view=nested)).state == "private"
    with pytest.raises(PrivateResourceStop):
        pool.reserve_visits()
    assert (ledger.operations, ledger.records) == (0, 0)


def test_public_acknowledgements_are_not_refunded_and_runtime_operations_guard():
    ledger = Ledger(PrivateBudget(Deadline(time.monotonic() + 10)), 1, 1)
    assert ledger.reserve(Frame(action="begin")).state == "ok"
    assert ledger.reserve(Frame(action="begin")).state == "public"
    assert ledger.reserve(Frame(action="public")).state == "ok"
    assert ledger.reserve(Frame(action="public")).state == "public"
    ledger.close()
    assert (ledger.operations, ledger.records) == (1, 1)


def test_remote_scratch_and_vm_come_from_same_root_and_cleanup_is_idempotent():
    pool = PrivateBudget(Deadline(time.monotonic() + 10))
    ledger = Ledger(pool, 1, 1)
    local = ledger.reserve(Frame(action="limited", n=100)).value
    scratch = ledger.reserve(Frame(action="scratch", view=local, n=64 << 20)).value
    assert ledger.reserve(Frame(action="scratch")).state == "private"
    assert ledger.reserve(Frame(action="release", view=scratch)).state == "ok"
    pool.reserve_vm(9_999_999)
    assert ledger.reserve(Frame(action="quantum", view=local)).value == 1
    assert ledger.reserve(Frame(action="quantum")).state == "private"
    ledger.close()
    ledger.close()


def test_expired_deadline_cannot_be_reset_by_view_or_frame():
    pool = PrivateBudget(Deadline(time.monotonic() - 1))
    ledger = Ledger(pool, 1, 1)
    assert ledger.reserve(Frame(action="limited", n=100)).state == "deadline"
    assert ledger.reserve(Frame(action="begin")).state == "deadline"


def test_stage_facts_record_only_acknowledged_reservations():
    ledger = Ledger(PrivateBudget(Deadline(time.monotonic() + 10)), 2, 5)
    assert ledger.reserve(Frame(action="begin", step_id="first")).state == "ok"
    for stage in (
        "search_temp", "search_lexical", "search_vector", "search_rerank",
        "search_final_evidence",
    ):
        assert ledger.reserve(Frame(action="public", stage=stage)).state == "ok"
    assert ledger.reserve(Frame(action="begin", step_id="second")).state == "ok"
    assert ledger.reserve(Frame(action="public", stage="search_temp")).state == "public"
    assert sum(ledger.by_stage.values()) == ledger.records == 5
    assert all(step_id == "first" and count == 1 for (step_id, _), count in ledger.by_stage.items())
    assert ledger.by_step == {"first": (1, 5), "second": (1, 0)}
