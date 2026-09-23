from uuid import uuid4

import pytest
from support.graph import fixture, require_native

from kg.evidence import EvidenceService
from kg.graph import LocalGraphSession
from kg.graph import session as session_module
from kg.graph._native import NativeError
from kg.graph._session_types import GraphSessionError
from kg.knowledge import KnowledgeService
from kg.knowledge._graph_export import GraphAssertion
from kg.models.foundation import WithdrawAssertion, WriteBatch, WriteRequest


def request(env, target):
    return WriteRequest(
        contract_version="foundation/1", request_id=str(uuid4()), retry_key=str(uuid4()),
        scope=env.scope, attribution=env.attribution,
        payload=WithdrawAssertion(operation="withdraw_assertion", contribution_id=target),
    )


def assertions(context):
    rows = context.native.execute(
        "MATCH (a:Assertion) RETURN a.dependency_json ORDER BY a.assertion_id", {},
    )
    try:
        proofs = []
        while page := rows.read():
            proofs.extend(GraphAssertion.model_validate_json(row[0]) for row in page)
        return context.retain(tuple(proofs))
    finally:
        rows.close()


def empty_count(context):
    rows = context.native.execute(
        "MATCH (a:Assertion) WHERE a.assertion_id = $id RETURN count(a)", {"id": "absent"},
    )
    try:
        return context.retain(rows.read())
    finally:
        rows.close()


def test_native_controlled_withdrawal_exact_proofs_receipts_and_refresh(tmp_path, monkeypatch):
    require_native()
    env = fixture(tmp_path / "native.sqlite", decisions=25)
    with LocalGraphSession(
        env.database, env.identity, env.scope, graph_directory=tmp_path / "graph",
    ) as graph:
        before = graph._run_read(env.scope, assertions)
        relation = next(p for p in before if p.object_witness is not None)
        decision = next(p for p in before if p.decision_member is not None)
        requests = (request(env, relation.assertion_id), request(env, decision.assertion_id))
        generation = graph._generation
        result = graph.write_batch(WriteBatch(
            contract_version="foundation/1", batch_id="withdraw", items=requests,
        ))
        assert result.status == "complete"
        assert graph.status().state == "dirty"
        with pytest.raises(GraphSessionError, match="generation_invalid"):
            graph._run_read(env.scope, assertions, expected_generation=generation)
        with monkeypatch.context() as patch:
            def fail(*args, **kwargs):
                raise NativeError("native_error")
            patch.setattr(session_module, "build_graph", fail)
            assert graph.refresh().error.code == "native_error"
        for req, outcome in zip(requests, result.outcomes, strict=True):
            assert env.evidence.write(req).receipt == outcome.receipt
            assert outcome.status == "applied"
        assert graph.refresh().state == "ready"
        after = graph._run_read(env.scope, assertions)
        removed = {p.payload.contribution_id for p in requests}
        assert {p.assertion_id for p in after} == {p.assertion_id for p in before} - removed
        assert {p.assertion_id: p for p in after} == {
            p.assertion_id: p for p in before if p.assertion_id not in removed
        }
        knowledge = KnowledgeService(env.database, env.identity)
        for req in requests:
            historical = knowledge.contribution(
                env.scope, req.payload.contribution_id, mode="history",
            )
            assert not historical.is_current and historical.withdrawal is not None
        assert graph.write(requests[0]).status == "applied"
        assert graph.status().state == "dirty"
        assert graph.refresh().state == "ready"
        assert graph.write(request(env, relation.assertion_id)).status == "unchanged"
        assert graph.status().state == "dirty"


def test_external_withdrawal_invalidates_even_empty_count_and_release(tmp_path):
    require_native()
    env = fixture(tmp_path / "external.sqlite", decisions=2)
    with LocalGraphSession(
        env.database, env.identity, env.scope, graph_directory=tmp_path / "graph",
    ) as graph:
        before = graph._run_read(env.scope, assertions)
        assert env.evidence.write(request(env, before[0].assertion_id)).status == "applied"
        assert graph.status().state == "ready"  # Idle status is not a freshness check.
        with pytest.raises(GraphSessionError, match="state_changed"):
            graph._run_read(env.scope, empty_count)
        assert graph.refresh().state == "ready"
        def interleaved(context):
            result = empty_count(context)
            assert env.evidence.write(request(env, before[1].assertion_id)).status == "applied"
            return result
        with pytest.raises(GraphSessionError, match="state_changed"):
            graph._run_read(env.scope, interleaved)
        assert graph.status().state == "dirty"


def test_native_build_excludes_withdrawal_committed_during_export(tmp_path, monkeypatch):
    from kg.knowledge import _graph_export

    require_native()
    env = fixture(tmp_path / "race.sqlite", decisions=2)
    target = next(iter(env.expected))
    original = _graph_export.GraphExportCursor._assertion
    committed = []
    def race(stream, *args, **kwargs):
        result = original(stream, *args, **kwargs)
        if not committed:
            committed.append(env.evidence.write(request(env, target)))
        return result
    with LocalGraphSession(
        env.database, env.identity, env.scope, graph_directory=tmp_path / "graph",
    ) as graph:
        with monkeypatch.context() as patch:
            patch.setattr(_graph_export.GraphExportCursor, "_assertion", race)
            result = graph.refresh()
            assert result.state == "dirty" and result.error.code == "state_changed"
        assert committed[0].status == "applied"
        assert graph.refresh().state == "ready"
        assert target not in {p.assertion_id for p in graph._run_read(env.scope, assertions)}


def test_confirmed_withdrawal_survives_late_graph_cancel(tmp_path, monkeypatch):
    from threading import Event

    require_native()
    env = fixture(tmp_path / "cancel.sqlite", decisions=2)
    cancel = Event()
    original = EvidenceService._write_budgeted
    def write(service, req, budget):
        result = original(service, req, budget)
        cancel.set()
        return result
    monkeypatch.setattr(EvidenceService, "_write_budgeted", write)
    with LocalGraphSession(
        env.database, env.identity, env.scope, graph_directory=tmp_path / "graph",
    ) as graph:
        graph._run_read(env.scope, assertions)
        req = request(env, next(iter(env.expected)))
        outcome = graph.write(req, cancel=cancel)
        assert outcome.status == "applied" and graph.status().state == "dirty"
        assert env.evidence.write(req).receipt == outcome.receipt


@pytest.mark.parametrize("phase", ["before_snapshot", "before_adoption"])
def test_native_withdrawal_invalidates_build_boundaries(tmp_path, monkeypatch, phase):
    from kg.graph import _build

    require_native()
    env = fixture(tmp_path / "boundaries.sqlite", decisions=2)
    target = next(iter(env.expected))
    module, name = (
        (_build, "open_graph_source") if phase == "before_snapshot"
        else (session_module, "build_graph")
    )
    original = getattr(module, name)
    def race(*args, **kwargs):
        result = original(*args, **kwargs)
        assert env.evidence.write(request(env, target)).status == "applied"
        return result
    with LocalGraphSession(
        env.database, env.identity, env.scope, graph_directory=tmp_path / "graph",
    ) as graph:
        with monkeypatch.context() as patch:
            patch.setattr(module, name, race)
            result = graph.refresh()
            assert result.state == "dirty" and result.error.code == "state_changed"
        assert graph.refresh().state == "ready"
        assert target not in {p.assertion_id for p in graph._run_read(env.scope, assertions)}
