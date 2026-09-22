"""Q1-owned preparation and single fenced release of ranked worker output."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Literal

from kg._execution_budget import PrivateBudget, PrivateResourceStop, ScratchReservation
from kg.diagnostics._collector import Capture, CaptureUnavailable
from kg.evidence._read_context import ObserverReference, ReadObserver
from kg.evidence._values import token
from kg.models.execution import Explained, ExplainOptions
from kg.models.foundation import QueryRequest, QueryResult, SearchStep, Value
from kg.models.indexing import IndexConfiguration
from kg.models.query import QueryExecution, StepTelemetry
from kg.models.query_events import QueryBudget as BudgetEvent
from kg.models.query_events import QueryDecision, QueryNested, QueryPlan
from kg.query._meter import Ledger
from kg.query._plans import bindings_for
from kg.query._retention import Allocation, size
from kg.query._search_capture import CAPTURE_SCRATCH, SearchTransfer

if TYPE_CHECKING:
    from kg.query.service import QueryService

LOGGER = logging.getLogger(__name__)


class SearchWork(Value):
    request: QueryRequest
    configuration: IndexConfiguration
    options: ExplainOptions
    capture: bool


def execute(
    service: QueryService,
    request: QueryRequest,
    output: SearchStep,
    options: ExplainOptions,
    start: float,
    observer: ReadObserver,
    budget: PrivateBudget,
    parent: Capture | CaptureUnavailable,
    reference: ObserverReference,
) -> Explained[QueryExecution]:
    child = service._search_collector.begin_capture(
        "search", request.scope, required="read", request_id=request.request_id,
        options=options, group=parent.group, observer=reference, step_id=output.step_id,
    )
    allocation = Allocation(service._support)
    scratch: ScratchReservation | None = None
    try:
        if child.group.state == "provisional":
            try:
                scratch = budget.reserve_scratch(CAPTURE_SCRATCH, "general")
            except PrivateResourceStop:
                LOGGER.warning("Query search report unavailable: scratch capacity")
                child.group.discard()
        transfer = SearchTransfer(allocation, child)
        ledger = Ledger(budget, request.budget.max_operations, request.budget.max_records)
        ledger.transfer = transfer.receive
        work = SearchWork(
            request=request, configuration=service.search_configuration,
            options=options, capture=scratch is not None,
        )
        elapsed = service._run_worker(observer, work, ledger)
        data = transfer.ranked
        if data is None or transfer.pending is not None:
            raise ValueError("Incomplete ranked output")
        if ledger.operations != 1 or ledger.by_step.get(output.step_id) != (1, ledger.records):
            raise ValueError("Search accounting correlation mismatch")
        if isinstance(child, Capture) and child.active:
            child.group.discard()
            child.finish("failed", reason="unavailable")
        state: Literal["complete", "empty"] = "complete" if data.hits else "empty"
        result = QueryResult(
            contract_version="foundation/1", request_id=request.request_id, scope=request.scope,
            outcome=state, read_state_id=token(), result_set_id=None,
            operations_executed=ledger.operations, records_examined=ledger.records,
            exhaustion="candidate_pool", data=data,
        )
        result.validate_for(request)
        allocation.reserve(size(result) * 2)
        outcome = QueryExecution(
            result=result, elapsed_milliseconds=(time.monotonic() - start) * 1000,
            work_accounting="scoped_semantic_reservations/1",
            steps=tuple(
                StepTelemetry(
                    step_id=step.step_id, operation=step.operation,
                    state=state if step.step_id == output.step_id else "not_needed",
                    operations_executed=1 if step.step_id == output.step_id else 0,
                    records_examined=ledger.records if step.step_id == output.step_id else 0,
                    elapsed_milliseconds=elapsed if step.step_id == output.step_id else 0,
                )
                for step in request.steps
            ),
        )
        with parent.guard():
            parent.append(QueryPlan(
                required_steps=(output.step_id,),
                pruned_steps=tuple(s.step_id for s in request.steps if s.step_id != output.step_id),
            ))
            parent.append(QueryDecision(
                kind="query.output", step_id=output.step_id,
                state="exhausted" if data.hits else "empty", semantics="search",
                selected_ids=tuple(hit.evidence.anchor_id for hit in data.hits)
                if options.detail == "detailed" else (),
            ))
            stages = {
                "search_temp": "passage", "search_lexical": "lexical", "search_vector": "dense",
                "search_rerank": "rerank", "search_final_evidence": "final_evidence",
            }
            for (step_id, stage), reservations in ledger.by_stage.items():
                parent.append(BudgetEvent.model_validate({
                    "step_id": step_id, "stage": stages[stage], "reservations": reservations,
                }))
            if isinstance(child, Capture) and child.prepared is not None:
                parent.append(QueryNested(
                    step_id=output.step_id, execution_id=child.execution_id,
                    report_id=child.report_id,
                ))
        parent.finish("succeeded")
        bindings = bindings_for(service, observer, parent)
        observers = tuple(parent.group.observers) or (reference,)
        with (
            service._authorizer.release(bindings, observers, budget, targets_verified=True),
            service._dispatcher.condition,
        ):
            service._check_open()
            budget.check_deadline()
            report = service._diagnostics._publish(parent)
            outcome = outcome.model_copy(update={
                "elapsed_milliseconds": (time.monotonic() - start) * 1000,
            })
            budget.check_deadline()
            return Explained(outcome=outcome, report=report)
    finally:
        if isinstance(child, Capture) and child.active:
            child.group.redact()
            child.finish("failed", reason="unavailable")
        if scratch is not None:
            scratch.release()
        allocation.close()
