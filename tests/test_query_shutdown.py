"""Deterministic supervisor scheduling: no wall-clock sleeps or spawn timing assumptions."""

import multiprocessing
from functools import partial
from threading import Event
from types import SimpleNamespace

import pytest
from support.evidence import environment, put, receipt
from support.query_workers import gated_clean_exit

import kg.query.service as query_module
from kg._execution_budget import Deadline, DeadlineStop, PrivateBudget
from kg.evidence.errors import EvidenceServiceError
from kg.models.foundation import EvidenceStep, QueryBudget, QueryRequest
from kg.query import QueryService, _worker
from kg.query._channel import Channel
from kg.query._dispatch import Stopped
from kg.query._meter import Frame, Ledger


@pytest.fixture
def supervisor(tmp_path, monkeypatch):
    env = environment(tmp_path / "shutdown.db")
    with QueryService(env.database, env.service.identity) as service:
        state = SimpleNamespace(
            now=14.0,
            exit_at=14.1,
            exit_code=0,
            cancel_at=float("inf"),
            ignore_terminate=False,
            expected_records=1,
            events=[],
            replies=[],
            joins=[],
            cleanup=[],
        )
        # Admission has already consumed four seconds of the original five-second budget.
        budget = PrivateBudget(Deadline(15.0))
        ledger = Ledger(budget, 1, 1)

        def advance(seconds):
            state.now += seconds
            if state.now >= state.cancel_at:
                service._dispatcher.closed.set()

        class Endpoint:
            def __init__(self, name):
                self.name = name
                self.closed = False

            def close(self):
                self.closed = True

        parent, child = Endpoint("parent"), Endpoint("child")

        class Process:
            pid = None

            def start(self):
                self.pid = 123

            def is_alive(self):
                return state.now < state.exit_at

            @property
            def exitcode(self):
                return None if self.is_alive() else state.exit_code

            def join(self, timeout):
                state.joins.append(timeout)
                advance(min(timeout, max(0, state.exit_at - state.now)))

            def terminate(self):
                state.cleanup.append("terminate")
                if not state.ignore_terminate:
                    state.exit_at = state.now
                    state.exit_code = -15

            def kill(self):
                state.cleanup.append("kill")
                state.exit_at = state.now
                state.exit_code = -9

            def close(self):
                assert not self.is_alive()
                state.cleanup.append("process")

        process = Process()

        class Context:
            def Pipe(self):
                return parent, child

            def Process(self, *, target, args):
                assert args[5] is budget.deadline
                return process

        class Channel:
            def __init__(self, connection):
                assert connection is parent

            def receive(self, timeout):
                assert 0 < timeout <= 0.02
                if state.events and state.events[0][0] <= state.now + timeout:
                    at, frame = state.events.pop(0)
                    advance(max(0, at - state.now))
                    if isinstance(frame, Exception):
                        raise frame
                    return frame
                advance(timeout)
                return None

            def reply(self, reply):
                state.replies.append(reply)

            def close(self):
                assert state.cleanup[-1] == "process"
                parent.close()
                state.cleanup.append("channel")

        monkeypatch.setattr(query_module.multiprocessing, "get_context", lambda method: Context())
        monkeypatch.setattr(query_module, "Channel", Channel)
        monkeypatch.setattr(query_module.time, "monotonic", lambda: state.now)
        state.events = [
            (14.0, Frame(action="begin")),
            (14.0, Frame(action="public")),
            (14.0, Frame(action="visits", n=3)),
            (14.0, Frame(action="scratch", n=128)),
        ]

        def run():
            observer = SimpleNamespace(scope=env.scope, session_id="session")
            return service._run_worker(observer, object(), ledger)

        state.run = run
        state.ledger = ledger
        state.process = process
        yield state
        assert state.cleanup[-2:] == ["process", "channel"]
        assert parent.closed and child.closed
        assert not process.is_alive()
        assert ledger.operations == 1 and ledger.records == state.expected_records
        assert ledger.budget is budget and budget.deadline.expires_at_monotonic == 15.0
        assert budget._visits == 3
        assert budget._scratch == 0 and ledger.scratch == {}


@pytest.mark.parametrize(
    "done_at,exit_at",
    [(14.25, 14.1), (14.1, 14.4), (14.25, 14.55)],
    ids=["exit-before-delivery", "slow-clean-exit", "both"],
)
def test_terminal_delivery_and_clean_exit_use_original_remaining_budget(
    supervisor, done_at, exit_at,
):
    supervisor.exit_at = exit_at
    supervisor.events.append((done_at, Frame(action="done")))
    elapsed = supervisor.run()
    assert elapsed == pytest.approx((max(done_at, exit_at) - 14) * 1000)
    assert supervisor.cleanup == ["process", "channel"]
    assert all(reply.state == "ok" for reply in supervisor.replies)
    assert len(supervisor.replies) == 4  # Terminal frames are not reservation RPCs.


@pytest.mark.parametrize(
    "terminal,error",
    [
        (EOFError(), EOFError),
        (ValueError("malformed frame"), ValueError),
        (Frame(action="error", code="not_found"), EvidenceServiceError),
    ],
    ids=["clean-exit-without-done", "malformed-terminal", "worker-error"],
)
def test_exit_cannot_replace_valid_terminal_delivery(supervisor, terminal, error):
    supervisor.events.append((14.25, terminal))
    with pytest.raises(error):
        supervisor.run()
    assert supervisor.now < 15


def test_done_cannot_replace_zero_exit_status(supervisor):
    supervisor.exit_at = 14.4
    supervisor.exit_code = 8
    supervisor.events.append((14.1, Frame(action="done")))
    with pytest.raises(Stopped) as error:
        supervisor.run()
    assert error.value.reason == "internal_error"
    assert supervisor.cleanup == ["process", "channel"]


def test_done_requires_acknowledged_accounting(supervisor):
    supervisor.events.pop(1)
    supervisor.expected_records = 0
    supervisor.events.append((14.1, Frame(action="done")))
    with pytest.raises(Stopped) as error:
        supervisor.run()
    assert error.value.reason == "internal_error"
    assert supervisor.ledger.records == 0


@pytest.mark.parametrize("phase", ["drain", "exit"])
def test_deadline_bounds_both_terminal_phases_and_kills_hung_worker(supervisor, phase):
    supervisor.exit_at = float("inf")
    supervisor.ignore_terminate = True
    if phase == "exit":
        supervisor.events.append((14.9, Frame(action="done")))
    else:
        supervisor.events.append((15.1, Frame(action="done")))
    with pytest.raises(DeadlineStop):
        supervisor.run()
    assert supervisor.now == pytest.approx(15.5)  # Original deadline + terminate grace only.
    assert supervisor.cleanup == ["terminate", "kill", "process", "channel"]


def test_dead_worker_with_delayed_terminal_cannot_extend_deadline(supervisor):
    supervisor.events.append((15.1, Frame(action="done")))
    with pytest.raises(DeadlineStop):
        supervisor.run()
    assert supervisor.now == pytest.approx(15)
    assert supervisor.cleanup == ["process", "channel"]


@pytest.mark.parametrize("phase", ["drain", "exit"])
def test_close_interrupts_both_terminal_phases(supervisor, phase):
    supervisor.exit_at = float("inf")
    supervisor.cancel_at = 14.3
    if phase == "exit":
        supervisor.events.append((14.1, Frame(action="done")))
    with pytest.raises(Stopped) as error:
        supervisor.run()
    assert error.value.reason == "service_closed"
    assert supervisor.now <= 14.32
    assert supervisor.cleanup == ["terminate", "process", "channel"]


@pytest.mark.parametrize("phase", ["drain", "exit"])
def test_real_worker_terminal_races_cleanup_and_following_call(tmp_path, monkeypatch, phase):
    env = environment(tmp_path / "real-shutdown.db")
    saved = receipt(env.service.write(put(env.scope)))
    reference = env.service.anchors(
        env.scope, saved.document_id, saved.processing.state_version,
    ).entries[0].reference
    request = QueryRequest(
        contract_version="foundation/1",
        request_id="shutdown",
        scope=env.scope,
        steps=(EvidenceStep(operation="evidence", step_id="e", evidence=reference),),
        output_step="e",
        budget=QueryBudget(max_milliseconds=5000),
    )
    context = multiprocessing.get_context("spawn")
    finished, release = context.Event(), context.Event()
    delivery = Event()
    processes, channels, ledgers = [], [], []
    joins = []
    exited_timeouts = []

    class Process:
        def __init__(self, **kwargs):
            self.process = context.Process(**kwargs)
            processes.append(self.process)

        def __getattr__(self, name):
            return getattr(self.process, name)

        def join(self, timeout):
            self.process.join(timeout)
            if phase == "exit" and finished.is_set():
                joins.append(timeout)
                if sum(joins) > 0.1:
                    release.set()

    class Context:
        def Pipe(self):
            return context.Pipe()

        def Process(self, **kwargs):
            return Process(**kwargs)

    class GatedChannel(Channel):
        def __init__(self, connection):
            channels.append(self)
            super().__init__(connection)

        def _pump(self):
            original_put = self.incoming.put

            def put_frame(frame):
                if phase == "drain" and getattr(frame, "action", None) == "done":
                    # A received terminal frame is withheld until two owner polls
                    # have observed the already-exited child, independent of load.
                    processes[0].join(timeout=5)
                    assert not processes[0].is_alive()
                    assert delivery.wait(5)
                original_put(frame)

            self.incoming.put = put_frame
            super()._pump()

        def receive(self, timeout):
            frame = super().receive(timeout)
            if phase == "drain" and frame is None and not processes[0].is_alive():
                exited_timeouts.append(timeout)
                if len(exited_timeouts) == 2:
                    delivery.set()
            return frame

    with QueryService(env.database, env.service.identity) as service:
        original_run = service._run_worker

        def recorded(observer, step, ledger):
            ledgers.append(ledger)
            return original_run(observer, step, ledger)

        with monkeypatch.context() as patch:
            patch.setattr(query_module.multiprocessing, "get_context", lambda method: Context())
            patch.setattr(query_module, "Channel", GatedChannel)
            patch.setattr(service, "_run_worker", recorded)
            if phase == "exit":
                patch.setattr(
                    _worker, "run",
                    partial(gated_clean_exit, finished=finished, release=release),
                )
            try:
                outcome = service.execute(request)
            finally:
                release.set()
                delivery.set()
        assert outcome.result.outcome == "complete"
        assert outcome.result.operations_executed == outcome.result.records_examined == 1
        assert len(ledgers) == len(processes) == len(channels) == 1
        assert ledgers[0].operations == ledgers[0].records == 1
        assert ledgers[0].budget._scratch == 0 and ledgers[0].scratch == {}
        assert processes[0]._closed
        assert channels[0].connection.closed and not channels[0].thread.is_alive()
        if phase == "exit":
            assert finished.is_set() and sum(joins) > 0.1
        else:
            assert len(exited_timeouts) >= 2
        assert service.execute(request).result.outcome == "complete"
