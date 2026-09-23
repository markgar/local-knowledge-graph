from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event
from time import monotonic, sleep

import pytest
from support.graph_session import count, setup, write_request

from kg._execution_budget import CancelledStop, Deadline, DeadlineStop
from kg.evidence._graph_observer import GraphSourceObserver, GraphSourceOperation
from kg.graph._session_types import GraphSessionError


@pytest.mark.parametrize("phase", ["adoption", "answer", "fence"])
def test_cold_cancellation_after_builder_keeps_original_latched_reason(
    tmp_path, monkeypatch, phase,
):
    env, session, builds = setup(tmp_path, monkeypatch)
    cancel = Event()
    original, observed = GraphSourceObserver.operation, []
    @contextmanager
    def operation(observer, binding, identity, scope, deadline, budget):
        observed.append((binding, deadline, budget))
        if len(observed) == (2 if phase == "adoption" else 3) and phase != "fence":
            cancel.set()
            try:
                budget.check_deadline()
            except CancelledStop:
                cancel.clear()
                raise
        with original(observer, binding, identity, scope, deadline, budget) as current:
            yield current
    monkeypatch.setattr(GraphSourceObserver, "operation", operation)
    def consume(context):
        result = count(context)
        if phase == "fence":
            cancel.set()
        return result
    try:
        with pytest.raises(GraphSessionError, match="cancelled"):
            session._run_read(env.scope, consume, cancel=cancel)
        assert len(builds) == 1
        assert session._accounting.stop_reason == "cancelled"
        assert (
            session._accounting.expires_at_monotonic
            == builds[0][1].snapshot().expires_at_monotonic
        )
        assert all(item[2] is builds[0][1].budget for item in observed)
        assert len({id(item[0]) for item in observed}) == 1
        assert session._generation is None and session._resources is None
        assert session._cleanup_outcome.complete
    finally:
        session.close()


@pytest.mark.parametrize("phase", ["adoption", "answer"])
def test_source_commit_at_final_fence_releases_nothing(tmp_path, monkeypatch, phase):
    env, session, builds = setup(tmp_path, monkeypatch)
    original, fences = GraphSourceOperation.release_fence, []
    @contextmanager
    def fence(operation):
        fences.append(operation.session_id)
        if len(fences) == (2 if phase == "adoption" else 3):
            assert env.evidence.write(write_request(env)).receipt
        with original(operation) as current:
            yield current
    monkeypatch.setattr(GraphSourceOperation, "release_fence", fence)
    try:
        with pytest.raises(GraphSessionError, match="state_changed"):
            session._run_read(env.scope, count)
        assert len(builds) == 1 and session._generation is None
        assert len(set(fences)) == len(fences)
    finally:
        session.close()


def test_fence_exit_failure_rolls_back_ready_pointer(tmp_path, monkeypatch):
    env, session, _ = setup(tmp_path, monkeypatch)
    original, calls = GraphSourceOperation.release_fence, []
    @contextmanager
    def fence(operation):
        calls.append(operation.session_id)
        with original(operation) as current:
            yield current
        if len(calls) == 2:
            raise DeadlineStop()
    monkeypatch.setattr(GraphSourceOperation, "release_fence", fence)
    try:
        result = session.refresh()
        assert result.error.code == "deadline_exceeded"
        assert session._generation is None and session._resources is None
    finally:
        session.close()


def test_native_callback_holds_snapshot_but_no_writer_lock(tmp_path, monkeypatch):
    env, session, _ = setup(tmp_path, monkeypatch)
    def consume(context):
        assert context.canonical.connection.in_transaction
        assert env.evidence.write(write_request(env)).receipt
        return count(context)
    try:
        with pytest.raises(GraphSessionError, match="state_changed"):
            session._run_read(env.scope, consume)
    finally:
        session.close()


def test_queue_fifo_cap_reentry_and_ordinary_expiry(tmp_path, monkeypatch):
    env, session, _ = setup(tmp_path, monkeypatch)
    entered, release = Event(), Event()
    seen = []
    def blocked(context):
        with pytest.raises(GraphSessionError, match="invalid_request"):
            session._run_read(env.scope, count)
        entered.set()
        assert release.wait(5)
        return count(context)
    try:
        with ThreadPoolExecutor(max_workers=10) as workers:
            first = workers.submit(session._run_read, env.scope, blocked)
            assert entered.wait(5)
            futures = []
            for index in range(8):
                def read(context, index=index):
                    seen.append(index)
                    return count(context)
                futures.append(workers.submit(session._run_read, env.scope, read))
                end = monotonic() + 5
                while len(session._owner.dispatcher.queue) < index + 1 and monotonic() < end:
                    sleep(.005)
                assert len(session._owner.dispatcher.queue) == index + 1
            with pytest.raises(GraphSessionError, match="busy"):
                session._run_read(env.scope, count)
            release.set()
            assert first.result() == 42
            assert all(future.result() == 42 for future in futures)
        assert seen == list(range(8))
        with pytest.raises(DeadlineStop):
            session._owner.call(
                lambda: pytest.fail("expired call executed"), Deadline(monotonic() - 1),
            )
    finally:
        release.set()
        session.close()


def test_warm_queued_behind_write_does_not_upgrade(tmp_path, monkeypatch):
    env, session, builds = setup(tmp_path, monkeypatch)
    session._run_read(env.scope, count)
    entered, release = Event(), Event()
    def barrier():
        entered.set()
        assert release.wait(5)
    try:
        with ThreadPoolExecutor(max_workers=3) as workers:
            active = workers.submit(session._owner.call, barrier, Deadline(monotonic() + 10))
            assert entered.wait(5)
            write = workers.submit(session.write, write_request(env))
            while len(session._owner.dispatcher.queue) < 1:
                sleep(.005)
            read = workers.submit(session._run_read, env.scope, count)
            while len(session._owner.dispatcher.queue) < 2:
                sleep(.005)
            release.set()
            active.result()
            assert write.result().receipt
            with pytest.raises(GraphSessionError, match="state_changed"):
                read.result()
        assert len(builds) == 1 and session.status().state == "dirty"
    finally:
        release.set()
        session.close()


@pytest.mark.parametrize("phase", ["idle", "answer"])
def test_revocation_is_forbidden_not_empty_graph(tmp_path, monkeypatch, phase):
    env, session, _ = setup(tmp_path, monkeypatch)
    def revoke():
        with env.database.transaction() as connection:
            connection.execute("DELETE FROM policy_grant WHERE grant_name='read'")
    def consume(context):
        result = count(context)
        if phase == "answer":
            revoke()
        return result
    try:
        session._run_read(env.scope, count)
        if phase == "idle":
            revoke()
        with pytest.raises(GraphSessionError, match="forbidden"):
            session._run_read(env.scope, consume)
        assert session._generation is None
    finally:
        session.close()
