from copy import copy, deepcopy
from threading import Event, get_ident
from time import monotonic

import pytest
from support.graph_session import count, setup, write_request

from kg._execution_budget import CancelledStop, Deadline, PrivateBudget, PrivateResourceStop
from kg.evidence.errors import EvidenceServiceError
from kg.graph._session_types import GraphSessionError, _WarmReadMeter


@pytest.mark.service
def test_lazy_reuse_same_original_observer_fresh_accounting_and_restart(tmp_path, monkeypatch):
    env, session, builds = setup(tmp_path, monkeypatch)
    try:
        assert session.status().state == "unbuilt" and not builds
        assert not (tmp_path / "derived").exists()
        assert session._run_read(env.scope, count) == 42
        generation, resources = session._generation, session._resources
        assert builds[0][2] != get_ident()
        assert resources.observer._connection._budget is None
        assert not resources.observer._connection.in_transaction
        assert session._accounting.scratch_live_bytes == 0
        assert session._run_read(env.scope, count) == 42
        assert len(builds) == 1 and session._generation == generation
        cold, warm = resources.native.calls
        assert cold[0] is builds[0][1].budget
        assert warm[0].resource_profile == "interactive/1"
        assert cold[0] is not warm[0] and cold[2] == warm[2] == 5000
        assert warm[0].deadline.remaining() <= 30
        assert resources.observer.binding is resources.binding
        directory = resources.directory
        assert session.close().closed and not directory.exists()
        assert session.close().closed
        from kg.graph import LocalGraphSession
        with LocalGraphSession(env.database, env.identity, env.scope,
                               graph_directory=tmp_path / "derived") as restarted:
            assert restarted.status().state == "unbuilt"
            assert restarted._run_read(env.scope, count) == 42
            assert restarted._generation != generation
            assert len(builds) == 2
    finally:
        session.close()


@pytest.mark.service
def test_scope_generation_and_copy_rejection_do_not_destroy_ready(tmp_path, monkeypatch):
    env, session, builds = setup(tmp_path, monkeypatch)
    try:
        session._run_read(env.scope, count)
        generation = session._generation
        access = env.scope.access.model_copy(update={"namespaces": ("mail", "notes")})
        with pytest.raises(GraphSessionError, match="forbidden"):
            session._run_read(env.scope.model_copy(update={"access": access}), count)
        with pytest.raises(GraphSessionError, match="generation_invalid"):
            session._run_read(env.scope, count, expected_generation=object())
        assert session._generation is generation and len(builds) == 1
        for copier in (copy, deepcopy):
            with pytest.raises(TypeError):
                copier(session)
        assert session._run_read(env.scope, count, expected_generation=generation) == 42
    finally:
        session.close()


@pytest.mark.service
def test_external_commit_invalidates_warm_without_rebuild(tmp_path, monkeypatch):
    env, session, builds = setup(tmp_path, monkeypatch)
    try:
        session._run_read(env.scope, count)
        assert env.evidence.write(write_request(env)).receipt is not None
        with pytest.raises(GraphSessionError, match="state_changed") as caught:
            session._run_read(env.scope, count)
        assert caught.value.failure.retry == "finish_import_then_refresh"
        assert session.status().state == "dirty" and len(builds) == 1
        assert session._run_read(env.scope, count) == 42
        assert len(builds) == 2
    finally:
        session.close()


@pytest.mark.service
def test_escaped_native_rows_and_context_never_reusable(tmp_path, monkeypatch):
    env, session, _ = setup(tmp_path, monkeypatch)
    escaped = []
    def consume(context):
        rows = context.native.execute("RETURN 42", {})
        escaped.extend((context, rows))
        return rows.read()[0][0]
    try:
        assert session._run_read(env.scope, consume) == 42
        context, rows = escaped
        with pytest.raises(GraphSessionError):
            rows.read()
        with pytest.raises(GraphSessionError):
            rows.close()
        def later(current):
            with pytest.raises(GraphSessionError, match="generation_invalid"):
                context.native.execute("RETURN 42", {})
            with pytest.raises(GraphSessionError, match="generation_invalid"):
                rows.read()
            with pytest.raises(EvidenceServiceError):
                context.canonical.check_active()
            return count(current)
        assert session._run_read(env.scope, later) == 42
    finally:
        session.close()


@pytest.mark.service
@pytest.mark.parametrize("bad", [[], {"raw": 42}, iter((42,)), "x" * (8 << 20)])
def test_no_mutable_lazy_or_oversized_success(tmp_path, monkeypatch, bad):
    env, session, _ = setup(tmp_path, monkeypatch)
    try:
        with pytest.raises(GraphSessionError):
            session._run_read(env.scope, lambda context: bad)
        assert session.status().state == "error"
        assert session._accounting.scratch_live_bytes == 0
        assert session._resources is None
    finally:
        session.close()


@pytest.mark.service
def test_output_cumulative_bound_before_accumulation(tmp_path, monkeypatch):
    env, session, _ = setup(tmp_path, monkeypatch)
    def consume(context):
        values = []
        for _ in range(200):
            values.append(context.retain("x" * 100000))
        return tuple(values)
    try:
        with pytest.raises(GraphSessionError, match="resource_exhausted"):
            session._run_read(env.scope, consume)
        assert session._accounting.scratch_live_bytes == 0
    finally:
        session.close()


@pytest.mark.service
def test_warm_meter_abi_cancel_latch_and_ordinary_limits():
    cancel = Event()
    meter = _WarmReadMeter(PrivateBudget(Deadline(monotonic() + 30)), cancel)
    for _ in range(1000):
        meter.reserve_public("support_member", 64)
    assert meter.counts["support_member"] == 64000
    for stage, quantity in (("invented", 1), ("resolve_entity", 65), ("resolve_entity", True)):
        with pytest.raises(ValueError):
            meter.reserve_public(stage, quantity)
    with pytest.raises(PrivateResourceStop):
        meter.reserve_visits(100001)
    cancel.set()
    with pytest.raises(CancelledStop):
        meter.check_deadline()
    cancel.clear()
    with pytest.raises(CancelledStop):
        meter.check_deadline()


@pytest.mark.service
def test_value_output_size_includes_field_structure_and_requires_frozen():
    from kg.graph._session_types import GraphGeneration
    from kg.graph.session import _size
    from kg.models.foundation import Value
    class Output(Value):
        left: str
        right: str
    value = Output(left="", right="")
    assert _size(value) >= len(value.model_dump_json().encode())
    with pytest.raises(PrivateResourceStop):
        _size(value, len(value.model_dump_json().encode()) - 1)
    class Mutable(Value):
        model_config = {"frozen": False}
        value: str
    with pytest.raises(GraphSessionError, match="invalid_request"):
        _size(Mutable(value="unfrozen"))
    class Envelope(Value):
        generation: GraphGeneration
    envelope = Envelope(generation=GraphGeneration("session", 1))
    assert _size(envelope) >= len(envelope.model_dump_json().encode())


@pytest.mark.service
@pytest.mark.parametrize("warm", [False, True])
def test_precancelled_read_uses_same_graph_error_interface(tmp_path, monkeypatch, warm):
    env, session, builds = setup(tmp_path, monkeypatch)
    try:
        if warm:
            session._run_read(env.scope, count)
        cancel = Event()
        cancel.set()
        with pytest.raises(GraphSessionError) as caught:
            session._run_read(env.scope, count, cancel=cancel)
        assert caught.value.failure.code == "cancelled"
        assert len(builds) == int(warm)
    finally:
        session.close()


@pytest.mark.service
def test_retained_and_returned_output_have_separate_bounds_and_fenced_scratch(
    tmp_path, monkeypatch,
):
    from contextlib import contextmanager

    from kg.evidence._graph_observer import GraphSourceOperation
    env, session, _ = setup(tmp_path, monkeypatch)
    original, charged = GraphSourceOperation.release_fence, []
    @contextmanager
    def fence(operation):
        charged.append(operation.budget._scratch)
        with original(operation) as current:
            yield current
    monkeypatch.setattr(GraphSourceOperation, "release_fence", fence)
    value = "x" * (5 << 20)
    def consume(context):
        assert context.retain(value) is value
        return value
    try:
        assert session._run_read(env.scope, consume) == value
        assert charged[-1] >= 10 << 20
        assert session._accounting.scratch_live_bytes == 0
        def repeated(context):
            context.retain(value)
            context.retain((value,))
            return 1
        with pytest.raises(GraphSessionError, match="resource_exhausted"):
            session._run_read(env.scope, repeated)
        def hidden(context):
            context.retain(("x" * (8 << 20),))
            return 1
        with pytest.raises(GraphSessionError, match="resource_exhausted"):
            session._run_read(env.scope, hidden)
    finally:
        session.close()
