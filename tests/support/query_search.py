"""Controlled provider seams inside the real spawned Q1/E3 execution."""

import os

from kg.models.foundation import QueryBudget, QueryRequest, SearchStep
from kg.query._worker import run as run_query
from support.index_search import Reranker, SearchProvider


def request(env, *, records=10000, milliseconds=30000, text="alpha", steps=None):
    return QueryRequest(
        contract_version="foundation/1", request_id="ranked", scope=env.scope,
        steps=steps or (SearchStep(operation="search", step_id="search", text=text),),
        output_step="search",
        budget=QueryBudget(max_records=records, max_milliseconds=milliseconds),
    )


def no_data(execution, reason):
    assert execution.result.error is not None, execution
    assert execution.stop_reason == reason
    assert execution.result.data is None
    assert execution.result.records_examined == execution.result.operations_executed == 0
    assert execution.result.read_state_id is None
    assert execution.result.result_set_id is None
    assert execution.steps == ()
    assert execution.work_accounting == "redacted"


def controlled_worker(*args, mode="normal", ready=None, resume=None, trace=None):
    from kg.evidence._sql import AccountedConnection
    from kg.indexing import EvidenceSearchService
    from kg.query import _search_capture
    from kg.retrieval.dense import DenseIndexError

    current_context = None
    original_search = EvidenceSearchService._search_in_context

    def search(service, context, *search_args, **kwargs):
        nonlocal current_context
        current_context = context
        return original_search(service, context, *search_args, **kwargs)

    EvidenceSearchService._search_in_context = search

    def record(name):
        if trace is not None:
            trace.put(name)

    def block():
        if ready is not None:
            ready.set()
        if resume is None or not resume.wait(60):
            raise RuntimeError("Controlled provider not released")

    def initialize(service, database, identity):
        original_init(service, database, identity)

        def provider(profile):
            record("embedding_load")
            if mode == "provider_unavailable":
                raise DenseIndexError("controlled unavailable")
            result = SearchProvider(
                profile, runtime="mismatch" if mode == "mismatch" else "controlled-test",
            )

            def encode():
                record("query_encode")
                if mode == "block_model":
                    block()
                elif mode.startswith("private_"):
                    budget = current_context.meter.private_budget
                    if mode == "private_visits":
                        budget.reserve_visits(100_000)
                    elif mode == "private_vm":
                        budget.reserve_vm(10_000_000)
                    else:
                        budget.reserve_scratch(64 << 20, "general")

            result.before_encode = encode
            return result

        def reranker():
            record("reranker_load")
            result = Reranker()

            def score():
                record("rerank")
                if mode == "block_rerank":
                    block()
                elif mode == "die_rerank":
                    os._exit(19)

            result.before_score = score
            return result

        service._provider_factory = provider
        service._reranker_factory = reranker

    original_init = EvidenceSearchService.__init__
    EvidenceSearchService.__init__ = initialize
    original_execute = AccountedConnection.execute

    def execute(connection, sql, parameters=()):
        result = original_execute(connection, sql, parameters)
        if sql.startswith("INSERT INTO temp.e3_search"):
            record("temp")
            if mode == "block_temp":
                block()
            elif mode == "die_temp":
                os._exit(19)
        return result

    AccountedConnection.execute = execute
    if mode == "capture_failure":
        def unavailable(value):
            raise MemoryError()
        _search_capture.encode = unavailable
    run_query(*args)


def heartbeat_action(env, value, saved):
    from kg.models.processing import (
        DocumentTarget,
        HeartbeatRequest,
        PlanRegistration,
        ScheduleRequest,
        WorkerRegistration,
        WorkerRequest,
        WorkerSelection,
    )
    from kg.processing import ProcessingAdministration, ProcessingService

    selection = WorkerSelection(
        namespace="markdown", owner_id="owner", writer_id="writer",
        plan_id="index", plan_version="1", worker_id="worker",
    )
    admin = ProcessingAdministration(env.database, env.admin.authority)
    admin.register_plan(PlanRegistration(
        corpus_id=env.scope.corpus_id, plan_id="index", plan_version="1",
        producer="query-search-acceptance", producer_version="1",
    ))
    admin.register_worker(WorkerRegistration(
        corpus_id=env.scope.corpus_id, principal_id=env.service.identity.principal_id,
        selection=selection,
    ))
    with env.database.connection() as connection:
        namespace_token = connection.execute(
            "SELECT namespace_token FROM document_state WHERE state_version=?",
            (saved.processing.state_version,),
        ).fetchone()[0]
    processing = ProcessingService(env.database, env.service.identity)
    processing.schedule(ScheduleRequest(
        scope=env.scope, selection=selection, request_id="schedule", retry_key="schedule",
        target=DocumentTarget(
            document_id=saved.document_id, revision_id=saved.revision_id,
            state_version=saved.processing.state_version, namespace_token=namespace_token,
        ),
    ))
    worker = WorkerRequest(scope=env.scope, selection=selection, request_id="claim")
    claim = processing.claim(worker).claim
    assert claim is not None

    def heartbeat():
        processing.heartbeat(HeartbeatRequest(
            **worker.model_dump(), job_id=claim.job_id, claim_fence=claim.claim_fence,
        ))

    return heartbeat
