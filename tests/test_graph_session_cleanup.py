import sqlite3
from threading import get_ident
from time import monotonic

import pytest
from support.graph_session import count, setup

from kg._execution_budget import Deadline
from kg.evidence import _graph_observer as observer
from kg.evidence._sql import AccountedConnection
from kg.graph import _build as build
from kg.graph import session as module
from kg.graph._session_types import GraphSessionError


def test_unexpected_disposal_keeps_custody_after_partial_release(tmp_path, monkeypatch):
    env, session, builds = setup(tmp_path, monkeypatch)
    session._run_read(env.scope, count)
    resource = session._resources
    cleanup, calls = build.retry_graph_cleanup, []
    def partial(residue):
        if residue.native_reader is not None:
            residue.native_reader.close()
            residue.native_reader = None
            calls.append(get_ident())
        raise RuntimeError("bookkeeping failed after confirmed native close")
    monkeypatch.setattr(build, "retry_graph_cleanup", partial)
    try:
        assert session.refresh().error.code == "cleanup_failed"
        assert resource._custody and session._resources is resource
        assert session.status().cleanup_pending and len(builds) == 1
        assert session.refresh().error.code == "cleanup_failed"
        assert len(calls) == 1
        monkeypatch.setattr(build, "retry_graph_cleanup", cleanup)
        assert session.refresh().state == "ready"
        assert len(builds) == 2 and len(calls) == 1
        assert not resource._custody
    finally:
        monkeypatch.setattr(build, "retry_graph_cleanup", cleanup)
        session.close()


def test_stage_close_exception_never_consumes_reachable_owner(tmp_path, monkeypatch):
    env, session, builds = setup(tmp_path, monkeypatch)
    original, cleanup = build.StagedGraph.transfer, build.retry_graph_cleanup
    def reject(stage, *, operation):
        raise ValueError("before transfer")
    def broken(residue):
        raise RuntimeError("cleanup")
    monkeypatch.setattr(build.StagedGraph, "transfer", reject)
    monkeypatch.setattr(build, "retry_graph_cleanup", broken)
    try:
        assert session.refresh().error.code == "invalid_request"
        stage = builds[0][0]
        assert session._stage is stage and stage._resources is not None
        monkeypatch.setattr(build, "retry_graph_cleanup", cleanup)
        monkeypatch.setattr(build.StagedGraph, "transfer", original)
        assert session.refresh().state == "ready"
        assert stage._resources is stage.pending_operation is None
    finally:
        monkeypatch.setattr(build, "retry_graph_cleanup", cleanup)
        session.close()


def test_early_sql_setup_and_failed_close_residue_blocks_next_build(tmp_path, monkeypatch):
    env, session, builds = setup(tmp_path, monkeypatch)
    initialize, close = observer._initialize_connection, AccountedConnection._close_observer
    calls, sources = [], []
    def setup_failed(connection, *, budget):
        sources.append(connection)
        raise sqlite3.OperationalError("setup failure")
    def close_failed(connection):
        calls.append(get_ident())
        raise sqlite3.OperationalError("close failure")
    monkeypatch.setattr(module, "build_graph", build.build_graph)
    monkeypatch.setattr(build, "_engine", lambda: None)
    monkeypatch.setattr(observer, "_initialize_connection", setup_failed)
    monkeypatch.setattr(AccountedConnection, "_close_observer", close_failed)
    try:
        assert session.refresh().error.code == "storage_error"
        residue = session._residue
        assert residue.observer._physical is sources[0]
        assert session.status().cleanup_pending and not builds
        primary = session._build_failure
        assert session.refresh().error.code == "cleanup_failed"
        assert session._residue is residue and len(sources) == 1
        assert session._build_failure is primary
        assert session._inventory()[0].observer_open
        assert len(set(calls)) == 1 and calls[0] != get_ident()
    finally:
        monkeypatch.setattr(observer, "_initialize_connection", initialize)
        monkeypatch.setattr(AccountedConnection, "_close_observer", close)
        assert not session.close().cleanup_pending


def test_owner_parks_for_explicit_retry_even_when_log_handler_raises(tmp_path, monkeypatch):
    env, session, _ = setup(tmp_path, monkeypatch)
    session._run_read(env.scope, count)
    cleanup = build.retry_graph_cleanup
    calls = []
    def broken(residue):
        calls.append(get_ident())
        raise RuntimeError("cleanup callback error")
    def bad_log(*args, **kwargs):
        raise RuntimeError("logging handler error")
    monkeypatch.setattr(build, "retry_graph_cleanup", broken)
    monkeypatch.setattr(module.LOGGER, "error", bad_log)
    from kg.graph import _session_owner
    monkeypatch.setattr(_session_owner.LOGGER, "error", bad_log)
    # Do not wait the production 32s join grace for an intentional parked owner.
    thread = session._owner.dispatcher.thread
    join = thread.join
    monkeypatch.setattr(thread, "join", lambda timeout: join(.1))
    first = session.close()
    assert first.closed and first.cleanup_pending and thread.is_alive()
    assert len(calls) == 1
    monkeypatch.setattr(build, "retry_graph_cleanup", cleanup)
    assert not session.close().cleanup_pending
    assert not thread.is_alive() and len(calls) == 1


def test_lost_original_observer_cannot_be_reopened_into_trust(tmp_path, monkeypatch):
    env, session, builds = setup(tmp_path, monkeypatch)
    try:
        session._run_read(env.scope, count)
        session._owner.call(
            session._resources.observer.close, Deadline(monotonic() + 30),
        )
        with pytest.raises(GraphSessionError):
            session._run_read(env.scope, count)
        assert len(builds) == 1 and session.status().state != "ready"
    finally:
        session.close()


def test_failed_stage_close_permanently_forbids_transfer(tmp_path, monkeypatch):
    env, session, _ = setup(tmp_path, monkeypatch)
    request = session._admit(build=True, cancel=None)
    original, native_closes = build.retry_graph_cleanup, []
    def partial(residue):
        if residue.native_reader is not None:
            residue.native_reader.close()
            residue.native_reader = None
            native_closes.append(1)
        raise RuntimeError("partial cleanup")
    def exercise():
        stage = module.build_graph(
            env.database, env.identity, env.scope,
            staging_parent=tmp_path, operation=request.bulk,
        )
        monkeypatch.setattr(build, "retry_graph_cleanup", partial)
        try:
            with pytest.raises(RuntimeError, match="partial cleanup"):
                stage.close()
            assert stage.pending_operation is None and stage._resources is not None
            with pytest.raises(build.GraphBuildError) as caught:
                stage.transfer(operation=request.bulk)
            assert caught.value.failure.code == "invalid_request"
            assert native_closes == [1]
        finally:
            monkeypatch.setattr(build, "retry_graph_cleanup", original)
            assert stage.close() is None
    try:
        session._owner.call(exercise, request.budget.deadline)
    finally:
        session.close()


@pytest.mark.parametrize("failure", ["mapping_log", "partial_cleanup"])
def test_failed_build_always_hands_off_custody_after_unexpected_exception(
    tmp_path, monkeypatch, failure,
):
    from kg.graph._native import NativeError, NativeGraphWriter
    env, session, _ = setup(tmp_path, monkeypatch)
    cleanup, writer_close = build.retry_graph_cleanup, NativeGraphWriter.close
    observers, releases = [], []
    source = build.open_graph_source
    def open_source(*args):
        result = source(*args)
        observers.append(result)
        return result
    def fail_open(*args, **kwargs):
        raise NativeError("native_error")
    def fail_close(*args):
        raise RuntimeError("retry-safe empty writer close failed")
    def bad_log(*args, **kwargs):
        raise RuntimeError("log handler")
    def partial(residue):
        if residue.native_writer is not None:
            residue.native_writer.close()
            residue.native_writer = None
            releases.append(get_ident())
        raise RuntimeError("cleanup bookkeeping")
    monkeypatch.setattr(module, "build_graph", build.build_graph)
    monkeypatch.setattr(build, "_engine", lambda: None)
    monkeypatch.setattr(build, "open_graph_source", open_source)
    monkeypatch.setattr(NativeGraphWriter, "open", fail_open)
    if failure == "mapping_log":
        monkeypatch.setattr(build.LOGGER, "error", bad_log)
        monkeypatch.setattr(NativeGraphWriter, "close", fail_close)
    else:
        monkeypatch.setattr(build, "retry_graph_cleanup", partial)
        monkeypatch.setattr(module, "retry_graph_cleanup", partial)
    try:
        assert session.refresh().error.code == "native_error"
        residue = session._residue
        assert residue is not None and residue.observer is observers[0]
        assert observers[0]._physical is not None
        assert session.status().cleanup_pending
        assert session._build_failure.code == "native_error"
        assert session._accounting.scratch_live_bytes == 0
        # Explicit retry is blocked; no new original observer can bless old work.
        assert session.refresh().error.code == "cleanup_failed"
        assert len(observers) == 1 and session._residue is residue
        assert session.status().last_error.code == "native_error"
        if failure == "partial_cleanup":
            assert len(releases) == 1
    finally:
        monkeypatch.setattr(NativeGraphWriter, "close", writer_close)
        monkeypatch.setattr(build, "retry_graph_cleanup", cleanup)
        monkeypatch.setattr(module, "retry_graph_cleanup", cleanup)
        assert not session.close().cleanup_pending
        assert observers[0]._physical is None
