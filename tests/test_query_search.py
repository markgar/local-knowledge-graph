"""Actual E1 -> IndexService -> E3 -> spawned Q1, using controlled models only."""

import multiprocessing
from functools import partial

import pytest
from support.evidence import environment
from support.index_search import prepared
from support.query_search import controlled_worker, no_data, request

from kg.models.execution import ExecutionReport, ExplainOptions
from kg.models.foundation import EvidenceStep, QueryBudget, QueryRequest, SearchStep
from kg.query import QueryService, _worker


@pytest.fixture
def controlled(monkeypatch):
    monkeypatch.setattr(_worker, "run", controlled_worker)


def test_real_ranked_output_five_charges_exact_quotes_and_linked_stage_facts(
    tmp_path, monkeypatch,
):
    env = environment(tmp_path / "ranked.db")
    text = "alpha\r\nCafe\u0301 \U0001f680"
    prepared(env, text=text)
    trace = multiprocessing.get_context("spawn").Queue()
    monkeypatch.setattr(_worker, "run", partial(controlled_worker, trace=trace))
    with QueryService(env.database, env.service.identity) as service:
        value = request(env, records=5)
        explained = service.execute_explained(
            value, ExplainOptions(detail="detailed", include_quotes=True),
        )
        result = explained.outcome.result
        assert result.error is None, explained
        result.validate_for(value)
        assert result.outcome == "complete"
        assert result.exhaustion == "candidate_pool"
        assert result.records_examined == 5 and result.operations_executed == 1
        hit, = result.data.hits
        assert env.service.evidence(env.scope, hit.evidence).quote == text
        assert isinstance(explained.report, ExecutionReport)
        stages = [
            e.event for e in explained.report.events if e.event.kind == "query.budget"
        ]
        assert [(e.stage, e.reservations) for e in stages] == [
            ("passage", 1), ("lexical", 1), ("dense", 1), ("rerank", 1), ("final_evidence", 1),
        ]
        nested = next(
            e.event for e in explained.report.events if e.event.kind == "query.nested_execution"
        )
        child = service.diagnostics.report(env.scope, nested.report_id)
        assert isinstance(child, ExecutionReport)
        assert child.parent_execution_id == explained.report.execution_id
        assert child.step_id == "search" and child.owning_service == "indexing"
        candidate = next(e.event for e in child.events if e.event.kind == "indexing.candidate")
        assert candidate.reference == hit.evidence
        assert candidate.lexical_rank == candidate.dense_rank == candidate.rerank_rank == 1
        assert candidate.rerank_score == hit.score
        quote = next(e.event for e in child.events if e.event.kind == "execution.quote")
        assert quote.quote == text
        assert [trace.get(timeout=2) for _ in range(5)] == [
            "reranker_load", "embedding_load", "query_encode", "temp", "rerank",
        ]
        # A subsequent literal request is a new execution, not an atomic dependency.
        evidence = QueryRequest(
            contract_version="foundation/1", request_id="inspect", scope=env.scope,
            steps=(EvidenceStep(operation="evidence", step_id="e", evidence=hit.evidence),),
            output_step="e", budget=QueryBudget(max_milliseconds=30000),
        )
        inspected = service.execute(evidence)
        assert inspected.result.records_examined == 1
        assert inspected.result.read_state_id != result.read_state_id
        assert inspected.result.data.records[0].support.evidence == (hit.evidence,)
    trace.close()


@pytest.mark.parametrize("limit", [1, 2, 3, 4])
def test_no_partial_pipeline_at_each_public_boundary(tmp_path, controlled, limit):
    env = environment(tmp_path / "budget.db")
    prepared(env, text="alpha")
    with QueryService(env.database, env.service.identity) as service:
        explained = service.execute_explained(request(env, records=limit))
        no_data(explained.outcome, "record_budget")
        assert not isinstance(explained.report, ExecutionReport)
        assert not service.diagnostics.for_request(env.scope, "ranked").entries
        for collector in (service._collector, service._search_collector):
            assert all(not c.events and c.prepared is None for c in collector.candidates())


@pytest.mark.parametrize("expired", [False, True])
def test_search_reclaims_only_expired_retained_support(tmp_path, controlled, monkeypatch, expired):
    from support.query_knowledge import plan, produce, setup

    from kg.query import _retention

    env = setup(tmp_path / "expiry.db")
    prepared(env, namespace="email", text="alpha")
    subject, _ = produce(env, 25)
    narrow = env.scope.model_copy(update={
        "access": env.scope.access.model_copy(update={"namespaces": ("email",)}),
    })
    value = request(env, records=5).model_copy(update={"scope": narrow})
    with QueryService(env.database, env.service.identity) as service:
        count = service.execute(plan(env, subject))
        assert count.result.error is None, count
        retained = service._support.sets[count.result.result_set_id]
        if expired:
            retained.expires = 0
        monkeypatch.setattr(_retention, "TOTAL_BYTES", service._support.bytes + 1)
        result = service.execute(value)
        if expired:
            assert result.result.outcome == "complete", result
            assert service._support.bytes == 0 and not service._support.sets
        else:
            no_data(result, "retention_limit")
            assert service._support.sets[count.result.result_set_id] is retained
            assert service._support.bytes == retained.payload_bytes


@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("mode,reason", [
    ("provider_unavailable", "provider_unavailable"),
    ("mismatch", "index_not_ready"),
    ("private_visits", "resource_budget"),
    ("private_vm", "resource_budget"),
    ("private_scratch", "resource_budget"),
])
def test_provider_readiness_and_private_failures_are_not_empty(
    tmp_path, monkeypatch, empty, mode, reason,
):
    env = environment(tmp_path / "fault.db")
    prepared(env, text="" if empty else "alpha")
    monkeypatch.setattr(_worker, "run", partial(controlled_worker, mode=mode))
    with QueryService(env.database, env.service.identity) as service:
        outcome = service.execute(request(env))
        no_data(outcome, reason)
        if mode == "mismatch":
            assert outcome.result.outcome == "stale_index"
        assert service._support.bytes == 0


def test_empty_still_initializes_both_providers_and_encodes_query(tmp_path, monkeypatch):
    env = environment(tmp_path / "empty.db")
    prepared(env, text="")
    trace = multiprocessing.get_context("spawn").Queue()
    monkeypatch.setattr(_worker, "run", partial(controlled_worker, trace=trace))
    with QueryService(env.database, env.service.identity) as service:
        result = service.execute(request(env, records=1))
        assert result.result.outcome == "empty"
        assert result.result.data.hits == ()
        assert result.result.operations_executed == 1 and result.result.records_examined == 0
        assert result.result.exhaustion == "candidate_pool"
        assert [trace.get(timeout=2) for _ in range(3)] == [
            "reranker_load", "embedding_load", "query_encode",
        ]
    trace.close()


def test_unrelated_search_and_dependent_record_branches_are_pruned(tmp_path, controlled):
    from kg.models.foundation import CountStep, RecordsStep, ResolveStep

    env = environment(tmp_path / "closure.db")
    prepared(env, text="alpha")
    steps = (
        SearchStep(operation="search", step_id="unused_search", text="do not execute"),
        ResolveStep(operation="resolve", step_id="subject", entity_id="absent"),
        RecordsStep(operation="records", step_id="actions", entity_step="subject",
                    record_type="action"),
        CountStep(operation="count", step_id="count", records_step="actions"),
        SearchStep(operation="search", step_id="search", text="alpha"),
    )
    with QueryService(env.database, env.service.identity) as service:
        result = service.execute(request(env, records=5, steps=steps))
        assert result.result.error is None, result
        assert [s.state for s in result.steps] == ["not_needed"] * 4 + ["complete"]
        assert result.result.records_examined == 5 and result.result.operations_executed == 1


def test_scope_filters_before_readiness_and_ranking(tmp_path, controlled):
    from support.evidence import receipt
    from support.indexing import request as document

    env = environment(tmp_path / "scope.db")
    prepared(env, text="alpha")
    value = request(env, records=5)
    narrow = env.scope.model_copy(update={
        "access": env.scope.access.model_copy(update={"namespaces": ("markdown",)}),
    })
    value = value.model_copy(update={"scope": narrow})
    with QueryService(env.database, env.service.identity) as service:
        before = service.execute(value)
        receipt(env.service.write(document(env, namespace="email", text="alpha " * 100)))
        other = environment(env.database.path, corpus="other")
        prepared(other, text="alpha " * 200)
        after = service.execute(value)
        assert before.result.data == after.result.data
        assert before.result.records_examined == after.result.records_examined == 5
        broad = service.execute(request(env))
        no_data(broad, "index_not_ready")


def test_wrong_logical_configuration_is_not_reused(tmp_path, controlled):
    from kg.models.indexing import IndexConfiguration

    env = environment(tmp_path / "configuration.db")
    prepared(env, text="alpha")
    configuration = IndexConfiguration(contextual=True, representation="generic-title-quote/1")
    with QueryService(
        env.database, env.service.identity, search_configuration=configuration,
    ) as service:
        no_data(service.execute(request(env)), "index_not_ready")


@pytest.mark.parametrize("failure", ["worker", "supervisor", "admission"])
def test_optional_capture_failure_preserves_actual_ranking(
    tmp_path, controlled, monkeypatch, failure,
):
    from kg.query import _search_capture

    env = environment(tmp_path / "capture-fault.db")
    prepared(env, text="alpha")
    with QueryService(env.database, env.service.identity) as service:
        baseline = service.execute(request(env, records=5))
        if failure == "worker":
            monkeypatch.setattr(_worker, "run", partial(controlled_worker, mode="capture_failure"))
        elif failure == "supervisor":
            def unavailable(*args, **kwargs):
                raise MemoryError()
            monkeypatch.setattr(_search_capture.CapturedSearch, "model_validate_json", unavailable)
        else:
            from kg.diagnostics._collector import CaptureUnavailable

            def decline(*args, **kwargs):
                group = kwargs["group"]
                group.discard()
                return CaptureUnavailable(group)
            monkeypatch.setattr(service._search_collector, "begin_capture", decline)
        explained = service.execute_explained(request(env, records=5))
        assert explained.outcome.result.data == baseline.result.data
        assert explained.outcome.result.records_examined == 5
        assert not isinstance(explained.report, ExecutionReport)
        assert service._support.bytes == 0


@pytest.mark.parametrize("reserved", [62 << 20, (62 << 20) + (512 << 10)])
def test_optional_transport_scratch_refusal_does_not_spend_business_allowance(
    tmp_path, controlled, monkeypatch, reserved,
):
    from contextlib import contextmanager

    env = environment(tmp_path / "optional-scratch.db")
    prepared(env, text="alpha")
    with QueryService(env.database, env.service.identity) as service:
        original = service._observer

        @contextmanager
        def constrained(scope, deadline, budget):
            with (
                original(scope, deadline, budget) as observer,
                budget.reserve_scratch(reserved, "general"),
            ):
                yield observer

        monkeypatch.setattr(service, "_observer", constrained)
        explained = service.execute_explained(request(env, records=5))
        assert explained.outcome.result.outcome == "complete", explained
        assert explained.outcome.result.records_examined == 5
        assert not isinstance(explained.report, ExecutionReport)
