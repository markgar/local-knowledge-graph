from dataclasses import replace
from threading import Event
from time import monotonic

import pytest
from support.graph_session import count, setup, write_request

from kg._execution_budget import Deadline
from kg.evidence import EvidenceService
from kg.graph import session as module
from kg.graph._native import NativeError
from kg.graph._session_types import GraphSessionError
from kg.models.foundation import WriteBatch


@pytest.mark.service
def test_controlled_write_saved_when_separate_refresh_fails(tmp_path, monkeypatch):
    env, session, _ = setup(tmp_path, monkeypatch)
    try:
        session._run_read(env.scope, count)
        request = write_request(env)
        receipt = session.write(request)
        assert receipt.status == "applied" and session.status().state == "dirty"
        def broken(*args, **kwargs):
            raise NativeError("native_error")
        monkeypatch.setattr(module, "build_graph", broken)
        assert session.refresh().error.code == "native_error"
        replay = env.evidence.write(request)
        assert replay == receipt
    finally:
        session.close()


@pytest.mark.service
def test_batch_late_cancel_preserves_receipt_and_unstarted(tmp_path, monkeypatch):
    env, session, _ = setup(tmp_path, monkeypatch)
    cancel, seen = Event(), []
    original = EvidenceService._write_budgeted
    def write(service, request, budget):
        seen.append(budget)
        result = original(service, request, budget)
        cancel.set()
        return result
    monkeypatch.setattr(EvidenceService, "_write_budgeted", write)
    batch = WriteBatch(contract_version="foundation/1", batch_id="batch",
                       items=(write_request(env, "one"), write_request(env, "two")))
    try:
        result = session.write_batch(batch, cancel=cancel)
        assert result.status == "partial"
        assert result.outcomes[0].receipt is not None
        assert result.outcomes[1].error.code == "budget_exceeded"
        assert len(seen) == 1 and seen[0].resource_profile == "interactive/1"
        assert session.status().last_error.code == "cancelled"
        assert env.evidence.write(batch.items[0]).receipt == result.outcomes[0].receipt
        assert env.evidence.write(batch.items[1]).status == "applied"
    finally:
        session.close()


@pytest.mark.service
def test_exact_scope_checked_for_all_units_before_mutation(tmp_path, monkeypatch):
    env, session, _ = setup(tmp_path, monkeypatch)
    first, second = write_request(env, "one"), write_request(env, "two")
    access = env.scope.access.model_copy(update={"namespaces": ("mail", "notes")})
    second = second.model_copy(update={"scope": env.scope.model_copy(update={"access": access})})
    try:
        with pytest.raises(GraphSessionError, match="forbidden"):
            session.write_batch(WriteBatch(
                contract_version="foundation/1", batch_id="batch", items=(first, second),
            ))
        assert session.status().state == "unbuilt"
        assert env.evidence.write(first).status == "applied"
    finally:
        session.close()


@pytest.mark.service
@pytest.mark.parametrize("late", ["deadline", "close"])
def test_confirmed_write_survives_late_stop(tmp_path, monkeypatch, late):
    env, session, _ = setup(tmp_path, monkeypatch)
    original = EvidenceService._write_budgeted
    def write(service, request, budget):
        result = original(service, request, budget)
        if late == "deadline":
            budget.deadline = Deadline(monotonic() - 1)
        else:
            with session._lock:
                session._status = replace(session._status, closed=True)
            session._owner.dispatcher.closed.set()
        return result
    monkeypatch.setattr(EvidenceService, "_write_budgeted", write)
    batch = WriteBatch(contract_version="foundation/1", batch_id="batch",
                       items=(write_request(env, "one"), write_request(env, "two")))
    try:
        result = session.write_batch(batch)
        assert result.outcomes[0].receipt is not None
        assert result.outcomes[1].error.code == "budget_exceeded"
        assert env.evidence.write(batch.items[0]).receipt == result.outcomes[0].receipt
        assert session.status().last_error.code == (
            "deadline_exceeded" if late == "deadline" else "closed"
        )
    finally:
        session.close()


@pytest.mark.service
def test_started_units_keep_individual_e1_reports_without_caller_context(tmp_path, monkeypatch):
    from kg.evidence import _reporting as reporting
    from kg.models.execution import SUMMARY_OPTIONS

    env, session, _ = setup(tmp_path, monkeypatch)
    cancel, reports, contexts, pools = Event(), [], [], []
    original, deliver = EvidenceService._write_budgeted, reporting.deliver
    def write(service, request, budget):
        contexts.append(reporting._EXPLANATION.get())
        pools.append(budget)
        result = original(service, request, budget)
        if len(pools) == 2:
            cancel.set()
        return result
    def report(value):
        reports.append(value)
        deliver(value)
    monkeypatch.setattr(EvidenceService, "_write_budgeted", write)
    monkeypatch.setattr(reporting, "deliver", report)
    caller = reporting.Explanation(SUMMARY_OPTIONS)
    token = reporting._EXPLANATION.set(caller)
    try:
        result = session.write_batch(WriteBatch(
            contract_version="foundation/1", batch_id="report-batch",
            items=tuple(write_request(env, str(i)) for i in range(3)),
        ), cancel=cancel)
        assert result.status == "partial" and contexts == [None, None]
        assert len(reports) == 2 and pools[0] is pools[1]
        assert pools[0].resource_profile == "interactive/1"
        assert caller.report.state == "not_collected"
        assert result.outcomes[2].error.code == "budget_exceeded"
    finally:
        reporting._EXPLANATION.reset(token)
        session.close()


@pytest.mark.process
def test_real_concurrent_close_reports_closed_without_losing_receipt(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from time import sleep
    env, session, _ = setup(tmp_path, monkeypatch)
    confirmed, release = Event(), Event()
    original = EvidenceService._write_budgeted
    def write(service, request, budget):
        result = original(service, request, budget)
        confirmed.set()
        assert release.wait(5)
        return result
    monkeypatch.setattr(EvidenceService, "_write_budgeted", write)
    batch = WriteBatch(contract_version="foundation/1", batch_id="closing",
                       items=(write_request(env, "one"), write_request(env, "two")))
    try:
        with ThreadPoolExecutor(max_workers=2) as workers:
            write_future = workers.submit(session.write_batch, batch)
            assert confirmed.wait(5)
            closed = workers.submit(session.close)
            end = monotonic() + 5
            while not session.status().closed and monotonic() < end:
                sleep(.005)
            assert session.status().closed
            release.set()
            result = write_future.result()
            assert result.outcomes[0].receipt is not None
            assert result.outcomes[1].error.code == "budget_exceeded"
            assert not closed.result().cleanup_pending
        assert session.status().last_error.code == "closed"
        assert env.evidence.write(batch.items[0]).receipt == result.outcomes[0].receipt
    finally:
        release.set()
        session.close()
