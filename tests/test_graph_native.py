import sys
from threading import Event
from time import monotonic

import pytest
from support.graph import require_native

from kg._execution_budget import CancelledStop, Deadline, DeadlineStop, PrivateBudget
from kg.graph import _native
from kg.graph._native import NativeError, NativeGraphReadHandle, NativeGraphWriter, scalar


@pytest.mark.unit
def test_optional_missing_runtime_is_import_safe(monkeypatch):
    monkeypatch.setattr(sys, "platform", "unsupported")
    with pytest.raises(NativeError, match="graph_unavailable"):
        _native._engine()


@pytest.mark.native
@pytest.mark.requires_native
def test_native_ordinary_budget_readonly_and_timeout(tmp_path):
    require_native()
    budget, cancel = PrivateBudget(Deadline(monotonic() + 30)), Event()
    writer = NativeGraphWriter()
    writer.open(tmp_path / "graph.lbug", read_only=False)
    writer.schema(budget, cancel)
    writer.close()
    native = NativeGraphReadHandle()
    native.open(tmp_path / "graph.lbug", read_only=True)
    try:
        assert scalar(native, "MATCH (e:Entity) RETURN count(e)", budget, cancel) == 0
        rows = native.execute("RETURN $value", {"value": "exact\r\nCafe\u0301"},
                              budget=budget, cancel=cancel)
        assert rows._budget is budget and rows._cancel is cancel
        assert rows.read() == (("exact\r\nCafe\u0301",),)
        assert rows.read() == ()
        rows.close()
        assert rows._budget is rows._cancel is None
        with pytest.raises(NativeError):
            native.execute("CREATE (:Entity {entity_id:'forbidden'})", {},
                           budget=budget, cancel=cancel)
        with pytest.raises(ValueError):
            native.execute("RETURN 1; RETURN 2", {}, budget=budget, cancel=cancel)
        expired = PrivateBudget(Deadline(monotonic() - 1))
        with pytest.raises(DeadlineStop):
            native.execute("RETURN 1", {}, budget=expired, cancel=cancel)
        cancel.set()
        with pytest.raises(CancelledStop):
            native.execute("RETURN 1", {}, budget=budget, cancel=cancel)
    finally:
        native.close()


@pytest.mark.unit
def test_failed_native_close_is_indeterminate_and_never_double_closes():
    calls = []
    class Connection:
        def close(self):
            calls.append(1)
            raise RuntimeError("physical status unknown")
    native = NativeGraphReadHandle()
    native._connection = Connection()
    for _ in range(2):
        with pytest.raises(NativeError):
            native.close()
    assert calls == [1]


@pytest.mark.native
@pytest.mark.requires_native
def test_actual_native_query_timeout_preserves_deadline_stop(tmp_path):
    require_native()
    native = NativeGraphReadHandle()
    native.open(tmp_path / "timeout.lbug", read_only=False)
    try:
        budget = PrivateBudget(Deadline(monotonic() + 0.05))
        with pytest.raises(DeadlineStop):
            native.execute(
                "UNWIND range(1,100000) AS x UNWIND range(1,100000) AS y RETURN sum(x*y)",
                {}, budget=budget, cancel=Event(),
            )
    finally:
        native.close()


@pytest.mark.unit
@pytest.mark.parametrize("seconds,cap,maximum", [(30, None, 30000), (30, 5000, 5000),
                                               (.5, 5000, 500)])
def test_optional_native_timeout_never_extends_original_deadline(seconds, cap, maximum):
    timeouts = []
    class Result:
        def close(self):
            pass
    class Connection:
        def set_query_timeout(self, value):
            timeouts.append(value)
        def execute(self, *args):
            return Result()
        def close(self):
            pass
    native = NativeGraphReadHandle()
    native._connection, native._usable = Connection(), True
    budget = PrivateBudget(Deadline(monotonic() + seconds))
    try:
        rows = native.execute("RETURN 1", {}, budget=budget, cancel=Event(),
                              timeout_milliseconds=cap)
        assert 1 <= timeouts[0] <= maximum
        assert rows._budget is budget
        rows.close()
    finally:
        native.close()


@pytest.mark.unit
@pytest.mark.parametrize("cap", [0, -1, True, 1.5, "5000"])
def test_invalid_native_timeout_rejected_without_native_call(cap):
    native = NativeGraphReadHandle()
    native._connection, native._usable = object(), True
    with pytest.raises(ValueError, match="timeout"):
        native.execute("RETURN 1", {}, budget=PrivateBudget(Deadline(monotonic() + 30)),
                       cancel=Event(), timeout_milliseconds=cap)


@pytest.mark.native
@pytest.mark.requires_native
def test_actual_native_cap_interrupts_before_request_deadline(tmp_path):
    require_native()
    native = NativeGraphReadHandle()
    native.open(tmp_path / "timeout-cap.lbug", read_only=False)
    budget = PrivateBudget(Deadline(monotonic() + 30))
    try:
        with pytest.raises(NativeError, match="native_error"):
            native.execute(
                "UNWIND range(1,100000) AS x UNWIND range(1,100000) AS y RETURN sum(x*y)",
                {}, budget=budget, cancel=Event(), timeout_milliseconds=50,
            )
        assert budget.deadline.remaining() > 20
    finally:
        native.close()


@pytest.mark.unit
def test_committed_checkpoint_failure_never_rolls_back_or_replays():
    calls = []

    class Connection:
        def set_query_timeout(self, timeout):
            pass

        def execute(self, statement, parameters):
            calls.append(statement)
            raise RuntimeError(_native.COMMITTED_CHECKPOINT_FAILURE + " synthetic failure")

        def close(self):
            calls.append("close")

    writer = NativeGraphWriter()
    writer._connection = Connection()
    writer._usable = writer._transaction = True
    budget = PrivateBudget(Deadline(monotonic() + 30))
    with pytest.raises(NativeError, match="native_error"):
        writer.run("COMMIT", {}, budget, Event())
    assert not writer._transaction
    writer.close()
    assert calls == ["COMMIT", "close"]


@pytest.mark.unit
@pytest.mark.parametrize("actual", [True, 1.0, None, 2])
def test_node_conflict_checks_types_and_values(monkeypatch, actual):
    writer = NativeGraphWriter()
    monkeypatch.setattr(writer, "run", lambda *args: (("id", actual),))
    with pytest.raises(NativeError, match="invalid_projection"):
        writer._node("Entity", {"entity_id": "id", "creation_sequence": 1},
                     PrivateBudget(Deadline(monotonic() + 30)), Event())


@pytest.mark.unit
@pytest.mark.parametrize("actual", ["", '{"a":2,"b":1}', None])
def test_node_conflict_checks_ordered_serialized_proof(monkeypatch, actual):
    writer = NativeGraphWriter()
    monkeypatch.setattr(writer, "run", lambda *args: (("id", actual),))
    with pytest.raises(NativeError, match="invalid_projection"):
        writer._node("EntityProof", {"proof_id": "id", "witness_json": '{"a":1,"b":2}'},
                     PrivateBudget(Deadline(monotonic() + 30)), Event())


@pytest.mark.unit
@pytest.mark.parametrize("actual", [True, 1.0])
def test_edge_ordinal_conflict_is_type_exact(monkeypatch, actual):
    writer = NativeGraphWriter()
    monkeypatch.setattr(writer, "run", lambda *args: (("source", "target", actual),))
    with pytest.raises(NativeError, match="invalid_projection"):
        writer._edge("ASSERTION_SOURCE", "source", "target",
                     PrivateBudget(Deadline(monotonic() + 30)), Event(), 1)


@pytest.mark.unit
def test_rollback_result_failure_keeps_cleanup_custody_without_replay():
    calls = []

    class Result:
        def close(self):
            calls.append("result.close")
            raise RuntimeError("unconfirmed result close")

    result = Result()

    class Connection:
        def execute(self, statement, parameters):
            calls.append(statement)
            return result

        def close(self):
            calls.append("connection.close")

    writer = NativeGraphWriter()
    writer._connection = Connection()
    writer._usable = writer._transaction = True
    for _ in range(2):
        with pytest.raises(NativeError):
            writer.close()
    assert calls == ["ROLLBACK", "result.close"]
    assert not writer._usable and not writer._transaction
    assert writer._cleanup_result is result
    assert writer._indeterminate


@pytest.mark.unit
@pytest.mark.parametrize("consumer", ["writer", "scalar"])
def test_row_cleanup_preserves_primary_stop_and_detaches_request(monkeypatch, consumer, caplog):
    from kg._execution_budget import _graph_build_operation
    from kg.graph._native import NativeRows

    calls = []

    class Result:
        def has_next(self):
            return True

        def get_next(self):
            raise DeadlineStop()

        def close(self):
            calls.append("close")
            raise RuntimeError("unconfirmed native result close")

    operation = _graph_build_operation(deadline=Deadline(monotonic() + 299), cancel=Event())
    native = NativeGraphWriter()
    native._usable = True
    rows = NativeRows(native, Result(), operation.budget, operation.cancel)
    rows._scratch.append(operation.budget.reserve_scratch(1024, "general"))
    native._rows.append(rows)
    monkeypatch.setattr(native, "execute", lambda *args, **kwargs: rows)
    with pytest.raises(DeadlineStop):
        if consumer == "writer":
            native.run("RETURN 1", {}, operation.budget, operation.cancel)
        else:
            scalar(native, "RETURN 1", operation.budget, operation.cancel)
    assert "Native row cleanup failed" in caplog.text
    assert rows._budget is rows._cancel is None
    assert operation.snapshot().scratch_live_bytes == 0
    for _ in range(2):
        with pytest.raises(NativeError):
            native.close()
    assert calls == ["close"] and native._rows == [rows]
