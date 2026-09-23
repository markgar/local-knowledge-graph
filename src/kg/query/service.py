from __future__ import annotations

import logging
import multiprocessing
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Self

from kg._execution_budget import Deadline, DeadlineStop, PrivateBudget, PrivateResourceStop
from kg.diagnostics._collector import Capture, CaptureUnavailable, Collector
from kg.diagnostics._targets import AuthorizationBinding, EvidenceTarget, ReportTargets
from kg.diagnostics.service import DiagnosticService
from kg.evidence._read_context import ReadObserver, observe
from kg.evidence._values import token, validated
from kg.evidence.database import EvidenceDatabase
from kg.evidence.errors import EvidenceServiceError
from kg.models.evidence import LocalIdentity
from kg.models.execution import (
    SUMMARY_OPTIONS,
    ExecutionReport,
    Explained,
    ExplainOptions,
    ReportAvailability,
    ReportHeaderPage,
)
from kg.models.foundation import (
    CountStep,
    ErrorCode,
    EvidenceStep,
    Failure,
    PathsStep,
    QueryRequest,
    QueryResult,
    Record,
    RecordsResult,
    RecordsStep,
    Scope,
    SearchStep,
    SourceSupport,
)
from kg.models.indexing import DEFAULT_CONFIGURATION, IndexConfiguration
from kg.models.query import (
    QueryCapabilities,
    QueryExecution,
    StepTelemetry,
    StopReason,
    SupportInspection,
    SupportInspectionRequest,
)
from kg.models.query_events import QueryBudget as BudgetEvent
from kg.models.query_events import QueryDecision, QueryPlan
from kg.query import _worker
from kg.query._channel import Channel
from kg.query._dispatch import Dispatcher, Stopped
from kg.query._meter import Ledger
from kg.query._reports import QueryAuthorizer
from kg.query._retention import Registry
from kg.query._search import SearchWork

LOGGER = logging.getLogger(__name__)


class QueryServiceError(EvidenceServiceError):
    pass


def closure(request: QueryRequest) -> tuple[str, ...]:
    by_id = {step.step_id: step for step in request.steps}
    required = {request.output_step}
    step = by_id[request.output_step]
    while isinstance(step, (CountStep, RecordsStep, PathsStep)):
        dependency = step.records_step if isinstance(step, CountStep) else step.entity_step
        required.add(dependency)
        step = by_id[dependency]
    return tuple(step.step_id for step in request.steps if step.step_id in required)


def failure(
    request: QueryRequest,
    start: float,
    code: ErrorCode,
    reason: StopReason,
) -> QueryExecution:
    result = QueryResult(
        contract_version="foundation/1",
        request_id=request.request_id,
        scope=request.scope,
        outcome="state_changed"
        if code == "state_changed"
        else "stale_index"
        if code == "stale_index"
        else ("unsupported" if code == "unsupported" else "failed"),
        read_state_id=None,
        result_set_id=None,
        operations_executed=0,
        records_examined=0,
        exhaustion="none",
        error=Failure(code=code, diagnostic_id=token()),
    )
    result.validate_for(request)
    return QueryExecution(
        result=result,
        elapsed_milliseconds=(time.monotonic() - start) * 1000,
        stop_reason=reason,
        work_accounting="redacted",
    )


class QueryDiagnostics:
    def __init__(self, service: QueryService) -> None:
        self._service: QueryService = service

    def report(self, scope: Scope, report_id: str) -> ExecutionReport | ReportAvailability:
        def inspect() -> ExecutionReport | ReportAvailability:
            report = self._service._diagnostics.report(scope, report_id)
            if isinstance(report, ExecutionReport) or report.state == "redacted":
                return report
            return self._service._search_diagnostics.report(scope, report_id)

        return self._call(inspect)

    def recent(self, scope: Scope, *, limit: int = 20) -> ReportHeaderPage:
        return self._call(
            lambda: self._service._diagnostics.recent(scope, limit=limit),
        )

    def for_request(self, scope: Scope, request_id: str, *, limit: int = 20) -> ReportHeaderPage:
        return self._call(
            lambda: self._service._diagnostics.for_request(scope, request_id, limit=limit),
        )

    def _call[T](self, function: Callable[[], T]) -> T:
        deadline = Deadline(time.monotonic() + 5)

        def inspect() -> T:
            self._service._authorizer.lookup_budget = PrivateBudget(deadline)
            try:
                result = function()
                self._service._check_open()
                deadline.remaining()
                return result
            finally:
                self._service._authorizer.lookup_budget = None

        try:
            return self._service._dispatcher.call(inspect, deadline)
        except (DeadlineStop, PrivateResourceStop):
            raise QueryServiceError("budget_exceeded") from None
        except Stopped as error:
            raise QueryServiceError(error.code) from None


class QueryService:
    def __init__(
        self, database: EvidenceDatabase, identity: LocalIdentity, *,
        search_configuration: IndexConfiguration = DEFAULT_CONFIGURATION,
    ) -> None:
        self.database = database
        self.identity = validated(LocalIdentity, identity)
        self.search_configuration = validated(IndexConfiguration, search_configuration)
        self._collector = Collector("query", self.identity)
        self._search_collector = Collector("indexing", self.identity)
        self._authorizer = QueryAuthorizer(database)
        self._diagnostics: DiagnosticService = DiagnosticService(self._collector, self._authorizer)
        self._search_diagnostics = DiagnosticService(self._search_collector, self._authorizer)
        self._support = Registry()
        self._dispatcher = Dispatcher(self._cleanup)
        self.diagnostics = QueryDiagnostics(self)

    def _cleanup(self) -> None:
        self._support.close()
        for collector in (self._collector, self._search_collector):
            for capture in tuple(collector._captures.values()):
                capture.group.close()
                collector._remove(capture)

    def close(self) -> None:
        self._dispatcher.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def execute(self, request: QueryRequest) -> QueryExecution:
        return self.execute_explained(request, ExplainOptions()).outcome

    def execute_explained(
        self,
        request: QueryRequest,
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[QueryExecution]:
        start = time.monotonic()
        try:
            request = validated(QueryRequest, request)
            options = validated(ExplainOptions, options)
        except EvidenceServiceError:
            raise QueryServiceError("invalid_request") from None
        deadline = Deadline(start + request.budget.max_milliseconds / 1000)
        try:
            return self._dispatcher.call(
                lambda: self._execute(request, options, start, deadline),
                deadline,
            )
        except DeadlineStop:
            outcome = failure(request, start, "budget_exceeded", "time_budget")
        except Stopped as error:
            outcome = failure(request, start, error.code, error.reason)
        return Explained(outcome=outcome, report=ReportAvailability(state="unavailable"))

    @contextmanager
    def _observer(
        self,
        scope: Scope,
        deadline: Deadline,
        budget: PrivateBudget,
    ) -> Iterator[ReadObserver]:
        with observe(self.database, self.identity, scope, deadline, budget) as observer:
            yield observer

    def _check_open(self) -> None:
        if self._dispatcher.closed.is_set():
            raise Stopped("unsupported", "service_closed")

    def _execute(
        self,
        request: QueryRequest,
        options: ExplainOptions,
        start: float,
        deadline: Deadline,
    ) -> Explained[QueryExecution]:
        budget = PrivateBudget(deadline)
        capture: Capture | CaptureUnavailable | None = None
        admitted = False
        try:
            with self._observer(request.scope, deadline, budget) as observer:
                admitted = True
                reference = observer.retain()
                try:
                    capture = self._collector.begin_capture(
                        "execute",
                        request.scope,
                        required="read",
                        request_id=request.request_id,
                        options=options,
                        observer=reference,
                    )
                    required = closure(request)
                    output = next(s for s in request.steps if s.step_id == request.output_step)
                    if isinstance(output, SearchStep):
                        from kg.query._search import execute as search

                        return search(
                            self, request, output, options, start, observer, budget,
                            capture, reference,
                        )
                    if not isinstance(output, EvidenceStep):
                        from kg.query._plans import execute

                        return execute(
                            self,
                            request,
                            options,
                            start,
                            observer,
                            budget,
                            capture,
                            reference,
                        )
                    ledger = Ledger(
                        budget,
                        request.budget.max_operations,
                        request.budget.max_records,
                    )
                    elapsed = self._run_worker(observer, output, ledger)
                    result = QueryResult(
                        contract_version="foundation/1",
                        request_id=request.request_id,
                        scope=request.scope,
                        outcome="complete",
                        read_state_id=token(),
                        result_set_id=None,
                        operations_executed=ledger.operations,
                        records_examined=ledger.records,
                        exhaustion="eligible_set",
                        data=RecordsResult(
                            kind="records",
                            records=(
                                Record(
                                    record_id=output.evidence.anchor_id,
                                    record_type="evidence",
                                    support=SourceSupport(
                                        kind="source", evidence=(output.evidence,)
                                    ),
                                ),
                            ),
                        ),
                    )
                    result.validate_for(request)
                    outcome = QueryExecution(
                        result=result,
                        elapsed_milliseconds=(time.monotonic() - start) * 1000,
                        work_accounting="scoped_semantic_reservations/1",
                        steps=tuple(
                            StepTelemetry(
                                step_id=step.step_id,
                                operation=step.operation,
                                state="complete" if step.step_id in required else "not_needed",
                                operations_executed=ledger.operations
                                if step.step_id in required
                                else 0,
                                records_examined=ledger.records if step.step_id in required else 0,
                                elapsed_milliseconds=elapsed if step.step_id in required else 0,
                            )
                            for step in request.steps
                        ),
                    )
                    target = EvidenceTarget(reference=output.evidence)
                    binding = AuthorizationBinding(self.identity, request.scope, "read", [target])
                    with capture.guard():
                        capture.append(
                            QueryPlan(
                                required_steps=required,
                                pruned_steps=tuple(
                                    s.step_id for s in request.steps if s.step_id not in required
                                ),
                            ),
                            ReportTargets(values=(target,)),
                        )
                        capture.append(
                            QueryDecision(
                                kind="query.output",
                                step_id=output.step_id,
                                state="exact",
                                semantics="evidence",
                                selected_ids=(output.evidence.anchor_id,)
                                if options.detail == "detailed"
                                else (),
                            )
                        )
                        capture.append(
                            BudgetEvent(
                                step_id=output.step_id,
                                stage="evidence_reference",
                                reservations=ledger.records,
                            )
                        )
                    capture.finish("succeeded")
                    bindings = (*capture.group.bindings, binding)
                    observers = tuple(capture.group.observers) or (reference,)
                    with (
                        self._authorizer.release(bindings, observers, budget),
                        self._dispatcher.condition,
                    ):
                        self._check_open()
                        deadline.remaining()
                        report = self._diagnostics._publish(capture)
                        outcome = outcome.model_copy(
                            update={
                                "elapsed_milliseconds": (time.monotonic() - start) * 1000,
                            }
                        )
                        deadline.remaining()
                        return Explained(outcome=outcome, report=report)
                finally:
                    reference.close()
        except DeadlineStop:
            code: ErrorCode = "budget_exceeded"
            reason: StopReason = "time_budget"
        except PrivateResourceStop:
            code, reason = "budget_exceeded", "resource_budget"
        except Stopped as error:
            code, reason = error.code, error.reason
        except EvidenceServiceError as error:
            code = error.failure.code
            if admitted and code == "forbidden":
                code = "state_changed"
            reason = (
                "state_changed"
                if code == "state_changed"
                else "forbidden"
                if code == "forbidden"
                else "not_found"
                if code == "not_found"
                else "unsupported_restriction"
                if code == "unsupported"
                else "internal_error"
            )
        except (ValueError, TypeError, KeyError, OSError, EOFError, MemoryError) as error:
            code, reason = "internal_error", "internal_error"
            LOGGER.error("Query failure class=%s", type(error).__name__)
        if capture is not None:
            capture.group.redact()
            capture.group.close()
            if isinstance(capture, Capture) and capture.active:
                capture.finish("failed", reason=code)
        outcome = failure(request, start, code, reason)
        return Explained(
            outcome=outcome,
            report=ReportAvailability(
                state="redacted",
                reason=code,
            ),
        )

    def _run_worker(
        self,
        observer: ReadObserver,
        step: EvidenceStep | QueryRequest | SupportInspectionRequest | SearchWork,
        ledger: Ledger,
    ) -> float:
        start = time.monotonic()
        context = multiprocessing.get_context("spawn")
        parent, child = context.Pipe()
        process = context.Process(
            target=_worker.run,
            args=(
                child,
                self.database.path,
                self.identity,
                observer.scope,
                observer.session_id,
                ledger.budget.deadline,
                step,
            ),
        )
        channel: Channel | None = None
        stop: StopReason = "resource_budget"
        try:
            process.start()
            child.close()
            channel = Channel(parent)
            while True:
                self._check_open()
                remaining = ledger.budget.deadline.remaining()
                frame = channel.receive(min(0.02, remaining))
                if frame is None:
                    # Exit can precede the pump delivering a buffered terminal frame.
                    # Only ordered frame/EOF delivery, not liveness, ends the drain.
                    continue
                ledger.budget.check_deadline()
                if frame.action == "done":
                    if not isinstance(
                        step, (QueryRequest, SupportInspectionRequest, SearchWork)
                    ) and (ledger.operations != 1 or ledger.records != 1):
                        raise Stopped("internal_error", "internal_error")
                    break
                if frame.action == "error":
                    if frame.reason is not None:
                        raise Stopped(frame.code or "internal_error", frame.reason)
                    if frame.code == "budget_exceeded":
                        raise Stopped("budget_exceeded", stop)
                    raise EvidenceServiceError(frame.code or "internal_error")
                reply = (
                    ledger.transfer(frame)
                    if frame.action in (
                        "payload", "chunk", "fetch", "fetch_chunk",
                        "capture_drop", "capture_reclaimed",
                    )
                    and ledger.transfer is not None
                    else ledger.reserve(frame)
                )
                if reply.state not in ("ok", "capture_reclaim"):
                    stop = (
                        "time_budget"
                        if reply.state == "deadline"
                        else "resource_budget"
                        if reply.state == "private"
                        else "operation_budget"
                        if frame.action == "begin"
                        else "record_budget"
                    )
                channel.reply(reply)
                if reply.state not in ("ok", "capture_reclaim") and not (
                    reply.state == "public"
                    and isinstance(step, QueryRequest)
                    and frame.action == "public"
                ):
                    raise Stopped("budget_exceeded", stop)
            while process.is_alive():
                self._check_open()
                remaining = ledger.budget.deadline.remaining()
                process.join(timeout=min(0.02, remaining))
            self._check_open()
            ledger.budget.check_deadline()
            if process.exitcode != 0:
                raise Stopped("internal_error", "internal_error")
        finally:
            child.close()
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                    process.join(timeout=0.5)
                if process.is_alive():
                    process.kill()
                    process.join(timeout=0.5)
                if process.is_alive():
                    self._dispatcher.closed.set()
                    raise Stopped("internal_error", "internal_error")
                process.close()
            if channel is not None:
                try:
                    channel.close()
                except OSError:
                    self._dispatcher.closed.set()
                    raise
            else:
                parent.close()
            ledger.close()
        ledger.budget.check_deadline()
        return (time.monotonic() - start) * 1000

    def inspect_support(self, request: SupportInspectionRequest) -> SupportInspection:
        return self.inspect_support_explained(request).outcome

    def inspect_support_explained(
        self,
        request: SupportInspectionRequest,
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[SupportInspection]:
        from kg.query._plans import inspect, inspection_failure

        start = time.monotonic()
        try:
            request = validated(SupportInspectionRequest, request)
            options = validated(ExplainOptions, options)
        except EvidenceServiceError:
            raise QueryServiceError("invalid_request") from None
        deadline = Deadline(start + request.budget.max_milliseconds / 1000)
        try:
            return self._dispatcher.call(
                lambda: inspect(self, request, options, start, deadline),
                deadline,
            )
        except DeadlineStop:
            code: ErrorCode = "budget_exceeded"
            reason: StopReason = "time_budget"
        except Stopped as error:
            code, reason = error.code, error.reason
        return Explained(
            outcome=inspection_failure(request, start, code, reason),
            report=ReportAvailability(state="unavailable"),
        )

    def capabilities(self, scope: Scope) -> QueryCapabilities:
        scope = validated(Scope, scope)
        deadline = Deadline(time.monotonic() + 5)

        def inspect() -> QueryCapabilities:
            budget = PrivateBudget(deadline)
            with self._observer(scope, deadline, budget) as observer:
                reference = observer.retain()
                capture: Capture | CaptureUnavailable | None = None
                try:
                    from kg.knowledge._store import Store

                    with self.database.connection(budget=budget) as connection:
                        connection.execute("BEGIN")
                        store = Store(connection, scope, budget)
                        try:
                            try:
                                enabled = any(
                                    p.record_projection is not None
                                    for p in store.schema().predicates
                                )
                            except EvidenceServiceError as error:
                                if error.failure.code != "unsupported":
                                    raise
                                enabled = False
                        finally:
                            store.close()
                            connection.rollback()
                    capture = self._collector.begin_capture(
                        "capabilities",
                        scope,
                        required="read",
                        observer=reference,
                    )
                    capture.finish("succeeded")
                    binding = AuthorizationBinding(self.identity, scope, "read", [])
                    with (
                        self._authorizer.release(
                            tuple(capture.group.bindings) or (binding,),
                            tuple(capture.group.observers) or (reference,),
                            budget,
                        ),
                        self._dispatcher.condition,
                    ):
                        self._check_open()
                        self._diagnostics._publish(capture)
                        deadline.remaining()
                        return QueryCapabilities(
                            scope=scope,
                            operations=("evidence", "resolve", "records", "count", "search")
                            if enabled
                            else ("evidence", "resolve", "search"),
                            record_types=("decision",) if enabled else (),
                            association="direct_explicit_association/1" if enabled else None,
                            count_identity="submitted_assertion_id" if enabled else None,
                        )
                except (EvidenceServiceError, DeadlineStop, PrivateResourceStop, Stopped):
                    if capture is not None:
                        capture.group.close()
                        capture.group.redact()
                    raise
                finally:
                    reference.close()

        try:
            return self._dispatcher.call(inspect, deadline)
        except (DeadlineStop, PrivateResourceStop):
            raise QueryServiceError("budget_exceeded") from None
        except Stopped as error:
            raise QueryServiceError(error.code) from None
