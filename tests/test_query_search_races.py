"""Real source/control commits and process loss across ranked execution."""

import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from functools import partial

import pytest
from support.evidence import environment, receipt
from support.index_search import prepared
from support.indexing import process
from support.indexing import request as document
from support.query_search import controlled_worker, heartbeat_action, no_data, request

from kg.models.execution import ExecutionReport, ExplainOptions
from kg.models.foundation import ExpectedState, RemoveDocument
from kg.query import QueryService, _worker


@pytest.mark.parametrize(
    "change", ["text", "metadata", "remove_restore", "policy", "unrelated", "rebuild", "heartbeat"],
)
@pytest.mark.parametrize("stage", ["temp", "release"])
def test_actual_mutations_invalidate_original_observer_and_all_reports(
    tmp_path, monkeypatch, change, stage,
):
    env = environment(tmp_path / "race.db")
    _, index, _, _, value, saved = prepared(env, text="alpha")
    heartbeat = heartbeat_action(env, value, saved) if change == "heartbeat" else None
    context = multiprocessing.get_context("spawn")
    ready, resume = context.Event(), context.Event()
    monkeypatch.setattr(_worker, "run", partial(
        controlled_worker, mode="block_temp" if stage == "temp" else "normal",
        ready=ready, resume=resume,
    ))
    with QueryService(env.database, env.service.identity) as service:
        original = service._run_worker
        captured = []

        def gate(*args):
            elapsed = original(*args)
            if stage == "release":
                child = next(iter(service._search_collector._captures.values()))
                assert child.prepared is not None and child.group.state == "provisional"
                assert not isinstance(service._search_diagnostics._publish(child), ExecutionReport)
                captured.append(child.report_id)
                ready.set()
                assert resume.wait(15)
            return elapsed

        monkeypatch.setattr(service, "_run_worker", gate)
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                service.execute_explained, request(env), ExplainOptions(detail="detailed"),
            )
            try:
                assert ready.wait(15)
                if change == "heartbeat":
                    heartbeat()
                elif change == "rebuild":
                    assert process(index, env, value, saved, mode="rebuild").outcome == "ready"
                    index.cleanup(env.scope, value.attribution, saved.document_id)
                elif change == "policy":
                    policy = env.policy.model_copy(update={
                        "grants": tuple(g for g in env.policy.grants if g.grant != "read"),
                    })
                    rotated = env.admin.replace_policy(policy, env.scope.access.policy_version)
                elif change == "remove_restore":
                    removed = receipt(env.service.write(value.model_copy(update={
                        "retry_key": "remove",
                        "payload": RemoveDocument(
                            operation="remove_document", document=value.payload.document,
                            precondition=ExpectedState(
                                kind="match", state_version=saved.processing.state_version,
                            ),
                        ),
                    })))
                    receipt(env.service.write(document(
                        env, text="alpha", state=removed.processing.state_version,
                    )))
                else:
                    receipt(env.service.write(document(
                        env, text="beta" if change == "text" else "alpha",
                        title="new" if change == "metadata" else "Title",
                        external="outside" if change == "unrelated" else "doc",
                        state=None if change == "unrelated" else saved.processing.state_version,
                    )))
            finally:
                resume.set()
            explained = pending.result(timeout=15)
        no_data(explained.outcome, "state_changed")
        assert not isinstance(explained.report, ExecutionReport)
        if change == "policy":
            env.admin.replace_policy(env.policy, rotated.policy_version)
        for collector in (service._collector, service._search_collector):
            for capture in collector.candidates():
                assert capture.group.state == "redacted"
                assert capture.prepared is None and capture.events == []
                assert not capture.active
        for report_id in captured:
            assert not isinstance(service.diagnostics.report(env.scope, report_id), ExecutionReport)
        assert service._support.bytes == 0


def test_later_invalidation_remains_terminal_after_parent_eviction(tmp_path, monkeypatch):
    env = environment(tmp_path / "retained.db")
    _, _, _, _, _, saved = prepared(env, text="alpha")
    monkeypatch.setattr(_worker, "run", controlled_worker)
    with QueryService(env.database, env.service.identity) as service:
        explained = service.execute_explained(request(env), ExplainOptions(detail="detailed"))
        child_id = next(e.event.report_id for e in explained.report.events
                        if e.event.kind == "query.nested_execution")
        parent = service._collector._captures[explained.report.report_id]
        service._collector._remove(parent)
        assert isinstance(service.diagnostics.report(env.scope, child_id), ExecutionReport)
        updated = receipt(env.service.write(document(
            env, text="beta", state=saved.processing.state_version,
        )))
        assert not isinstance(service.diagnostics.report(env.scope, child_id), ExecutionReport)
        receipt(env.service.write(document(
            env, text="alpha", state=updated.processing.state_version,
        )))
        assert not isinstance(service.diagnostics.report(env.scope, child_id), ExecutionReport)
        child = service._search_collector._captures[child_id]
        assert child.group.state == "redacted" and not child.events and child.prepared is None


@pytest.mark.parametrize("stage", ["temp", "rerank"])
@pytest.mark.parametrize("ending", ["die", "close", "deadline"])
def test_process_loss_cleans_snapshot_temp_handles_and_scratch(
    tmp_path, monkeypatch, stage, ending,
):
    from kg.query._meter import Ledger

    env = environment(tmp_path / "cleanup.db")
    prepared(env, text="alpha")
    with env.database.connection() as connection:
        before = tuple(connection.iterdump())
    context = multiprocessing.get_context("spawn")
    ready, resume = context.Event(), context.Event()
    mode = f"die_{stage}" if ending == "die" else f"block_{stage}"
    monkeypatch.setattr(_worker, "run", partial(
        controlled_worker, mode=mode, ready=ready, resume=resume,
    ))
    ledgers = []
    original_close = Ledger.close

    def closed(ledger):
        original_close(ledger)
        ledgers.append(ledger)

    monkeypatch.setattr(Ledger, "close", closed)
    with QueryService(env.database, env.service.identity) as service:
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(
                service.execute, request(env, milliseconds=3000 if ending == "deadline" else 30000),
            )
            if ending == "close":
                assert ready.wait(15)
                service.close()
            result = pending.result(timeout=15)
        no_data(result, {
            "die": "internal_error", "close": "service_closed", "deadline": "time_budget",
        }[ending])
        assert service._support.bytes == 0
    assert ledgers and not ledgers[0].scratch and ledgers[0].budget._scratch == 0
    assert not [p for p in multiprocessing.active_children() if p.name.startswith("SpawnProcess")]
    with env.database.connection() as connection:
        assert tuple(connection.iterdump()) == before
        assert not connection.execute(
            "SELECT name FROM sqlite_temp_master WHERE name LIKE 'e3_%'",
        ).fetchall()
    monkeypatch.setattr(_worker, "run", controlled_worker)
    with QueryService(env.database, env.service.identity) as restarted:
        assert restarted.execute(request(env, records=5)).result.outcome == "complete"
