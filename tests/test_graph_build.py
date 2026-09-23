import sqlite3
from copy import copy, deepcopy
from threading import Event
from time import monotonic

import pytest
from support.graph import fixture, query, require_native, verify

from kg._execution_budget import Deadline, _graph_build_operation
from kg.evidence import _graph_observer as graph
from kg.evidence._sql import AccountedConnection
from kg.graph import _build as build
from kg.graph._build import (
    GraphBuildError,
    build_graph,
    dispose_graph,
    retry_graph_cleanup,
)
from kg.graph._native import NativeError, NativeGraphReadHandle, NativeGraphWriter


def operation():
    return _graph_build_operation(deadline=Deadline(monotonic() + 299), cancel=Event())


def test_real_native_complete_reopen_transfer_and_current_budget(tmp_path):
    require_native()
    env = fixture(tmp_path / "source.sqlite", decisions=210)
    op = operation()
    stage = build_graph(
        env.database, env.identity, env.scope, staging_parent=tmp_path, operation=op,
    )
    try:
        assert stage.accounting.semantic_items_reserved == 227
        assert stage.accounting.scratch_live_bytes == 0
        assert stage.observer._connection._budget is None
        assert stage.pending_operation is op
        assert verify(stage, op, env) == 220
        with pytest.raises(GraphBuildError) as bad:
            stage.transfer(operation=operation())
        assert bad.value.accounting == op.snapshot()
        assert stage.pending_operation is op
        with pytest.raises(AttributeError):
            stage.binding = None
        for copier in (copy, deepcopy):
            with pytest.raises(TypeError):
                copier(stage)
        resources = stage.transfer(operation=op)
        assert stage.pending_operation is None
        assert resources.native is stage.native
        with pytest.raises(GraphBuildError):
            stage.transfer(operation=op)
        assert stage.close() is None
        assert resources.native._usable
        assert dispose_graph(resources) is None
        assert not resources.directory.exists()
    finally:
        stage.close()


@pytest.mark.parametrize("physical_closed", [False, True])
def test_real_early_pragma_then_close_failure_keeps_single_residue(
    tmp_path, monkeypatch, physical_closed,
):
    env = fixture(tmp_path / "source.sqlite", decisions=0)
    op = operation()
    acquired, closes = [], []
    initialize, execute = graph._initialize_connection, AccountedConnection.execute
    close = AccountedConnection._close_observer

    def setup(connection, *, budget):
        acquired.append(connection)
        initialize(connection, budget=budget)

    def statement(connection, sql, parameters=()):
        if connection in acquired and sql == "PRAGMA foreign_keys=ON":
            raise sqlite3.OperationalError("private setup text")
        return execute(connection, sql, parameters)

    def fail_close(connection):
        closes.append(connection)
        if physical_closed:
            close(connection)
        raise sqlite3.OperationalError("private close text")

    monkeypatch.setattr(build, "_engine", lambda: None)
    monkeypatch.setattr(graph, "_initialize_connection", setup)
    monkeypatch.setattr(AccountedConnection, "execute", statement)
    monkeypatch.setattr(AccountedConnection, "_close_observer", fail_close)
    with pytest.raises(GraphBuildError) as caught:
        build_graph(env.database, env.identity, env.scope, staging_parent=tmp_path, operation=op)
    error = caught.value
    assert error.failure.code == "storage_error"
    assert error.failure.canonical_failure.code == "internal_error"
    assert error.accounting == op.snapshot()
    assert "private" not in str(error)
    assert not error.cleanup.complete
    residue = error.take_cleanup_residue()
    assert error.take_cleanup_residue() is None
    assert residue.observer._physical is acquired[0]
    assert all(c is acquired[0] for c in closes)
    assert not residue.observer._usable
    assert not retry_graph_cleanup(residue).complete
    monkeypatch.setattr(AccountedConnection, "_close_observer", close)
    assert retry_graph_cleanup(residue).complete
    assert retry_graph_cleanup(residue).complete
    assert op.snapshot().scratch_live_bytes == 0


@pytest.mark.parametrize("phase", ["insert", "checkpoint", "reopen", "verification", "fence"])
def test_failures_never_return_partial_stage_and_release_all(tmp_path, monkeypatch, phase):
    require_native()
    env = fixture(tmp_path / "source.sqlite")
    op = operation()
    if phase == "insert":
        def broken(*args):
            raise NativeError("native_error")
        monkeypatch.setattr(NativeGraphWriter, "insert", broken)
    elif phase == "checkpoint":
        original = NativeGraphWriter.run
        def broken(self, query, *args):
            if query == "CHECKPOINT":
                raise NativeError("native_error")
            return original(self, query, *args)
        monkeypatch.setattr(NativeGraphWriter, "run", broken)
    elif phase == "reopen":
        original = NativeGraphReadHandle.open
        def broken(self, path, *, read_only):
            if read_only:
                raise NativeError("native_error")
            return original(self, path, read_only=read_only)
        monkeypatch.setattr(NativeGraphReadHandle, "open", broken)
    elif phase == "verification":
        def broken(*args):
            raise NativeError("invalid_projection")
        monkeypatch.setattr(build, "_verify", broken)
    else:
        original = build._verify
        def broken(native, *args):
            result = original(native, *args)
            if not isinstance(native, NativeGraphWriter):
                with env.database.connection() as connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("UPDATE document SET external_id='changed-' || external_id")
                    connection.commit()
            return result
        monkeypatch.setattr(build, "_verify", broken)
    with pytest.raises(GraphBuildError) as caught:
        build_graph(env.database, env.identity, env.scope, staging_parent=tmp_path, operation=op)
    assert caught.value.cleanup.complete
    assert caught.value.take_cleanup_residue() is None
    assert caught.value.accounting.scratch_live_bytes == 0
    assert not list(tmp_path.glob("graph-*"))


def test_cancelled_admission_has_original_terminal_accounting(tmp_path, monkeypatch):
    env = fixture(tmp_path / "source.sqlite", decisions=0)
    op = operation()
    op.cancel.set()
    with pytest.raises(GraphBuildError) as caught:
        build_graph(env.database, env.identity, env.scope, staging_parent=tmp_path, operation=op)
    assert caught.value.failure.code == "cancelled"
    assert caught.value.accounting == op.snapshot()
    assert caught.value.accounting.stop_reason == "cancelled"
    assert caught.value.cleanup.complete


def test_native_relationship_join_distinct_members_and_all_path_proofs(tmp_path):
    require_native()
    env = fixture(tmp_path / "source.sqlite", decisions=210)
    op = operation()
    stage = build_graph(
        env.database, env.identity, env.scope, staging_parent=tmp_path, operation=op,
    )
    try:
        prefix = (
            "MATCH (person:Entity)-[:SUBJECT]->(own:Assertion)-[:OBJECT]->(project:Entity)"
            "-[:SUBJECT]->(decision:Assertion), "
            "(own)-[:SUBJECT_PROOF]->(person_proof:EntityProof), "
            "(own)-[:OBJECT_PROOF]->(project_proof:EntityProof), "
            "(decision)-[:SUBJECT_PROOF]->(decision_proof:EntityProof) "
            "WHERE person.entity_id='" + env.person + "' AND own.predicate='work:owns' "
            "AND decision.object_kind='decision' "
        )
        assert list(query(stage.native, op, prefix +
                          "RETURN count(DISTINCT decision.assertion_id)")) == [(210,)]
        rows = list(query(stage.native, op, prefix +
            "RETURN own.assertion_id,decision.assertion_id,person_proof.proof_id,"
            "project_proof.proof_id,decision_proof.proof_id"))
        from collections import Counter

        from kg.graph._native import proof_id
        expected = [
            (own_id, decision_id, proof_id(env.witnesses[own["subject"]]),
             proof_id(env.witnesses[own["object"]]), proof_id(env.witnesses[decision["subject"]]))
            for own_id, own in env.expected.items() if own["object"] is not None
            for decision_id, decision in env.expected.items()
            if decision["object"] is None and decision["subject"] == own["object"]
        ]
        assert len(rows) == 420 and Counter(rows) == Counter(expected)
    finally:
        assert stage.close() is None
