"""Supervisor-side preparation and atomic release of dependent query results."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from kg._execution_budget import Deadline, DeadlineStop, PrivateBudget, PrivateResourceStop
from kg.diagnostics._collector import Capture, CaptureUnavailable
from kg.diagnostics._targets import AuthorizationBinding, KnowledgeTarget, ReportTargets
from kg.evidence._read_context import ObserverReference, ReadObserver
from kg.evidence._values import token
from kg.evidence.errors import EvidenceServiceError
from kg.knowledge._selection import DecisionSelectionItem, EntitySelectionItem
from kg.models.execution import Explained, ExplainOptions, ReportAvailability
from kg.models.foundation import (
    AggregateResult,
    CountStep,
    EntitiesResult,
    ErrorCode,
    Failure,
    QueryRequest,
    QueryResult,
    Record,
    RecordsResult,
    RecordsStep,
    ResolveStep,
)
from kg.models.query import QueryExecution, StopReason, SupportInspection, SupportInspectionRequest
from kg.models.query_events import QueryBudget as BudgetEvent
from kg.models.query_events import QueryDecision, QueryPlan
from kg.query._dispatch import Stopped
from kg.query._meter import Frame, Ledger, Reply
from kg.query._plan_worker import Summary
from kg.query._retention import SET_BYTES, Allocation, RetainedSet, size, utf8_size

if TYPE_CHECKING:
    from kg.query.service import QueryService

LOGGER = logging.getLogger(__name__)


class Transfer:
    def __init__(self, allocation: Allocation, capture: Capture | CaptureUnavailable) -> None:
        self.allocation, self.capture = allocation, capture
        self.members: list[str] = []
        self.entities: list[str] = []
        self.records: list[Record] = []
        self.summary: Summary | None = None
        self.member_bytes = 2
        self.output_bytes = 2
        self.pending: Frame | None = None
        self.chunks: list[str] = []
        self.received = 0
        self.last_record: str | None = None

    def receive(self, frame: Frame) -> Reply:
        if frame.action == "payload":
            if self.pending is not None or frame.n > SET_BYTES or self.summary is not None:
                raise ValueError("Invalid payload header")
            self.allocation.reserve(frame.n * 3)
            self.pending, self.received = frame, 0
            self.chunks = []
        elif frame.action == "chunk":
            if self.pending is None or not frame.text:
                raise ValueError("Unexpected payload chunk")
            self.received += len(frame.text.encode())
            if self.received > self.pending.n:
                raise ValueError("Payload overflow")
            self.chunks.append(frame.text)
            if self.received == self.pending.n:
                self.accept("".join(self.chunks), self.pending)
                self.pending = None
                self.chunks = []
        else:
            raise ValueError("Unexpected transfer frame")
        return Reply(state="ok")

    def accept(self, payload: str, header: Frame) -> None:
        if header.kind == "summary":
            self.summary = Summary.model_validate_json(payload)
            return
        if header.kind == "decision":
            member = DecisionSelectionItem.model_validate_json(payload)
            if self.last_record is not None and member.record.record_id <= self.last_record:
                raise ValueError("Decision ordering changed")
            self.last_record = member.record.record_id
            self.member_bytes += header.n + bool(self.members)
            if self.member_bytes > SET_BYTES:
                raise Stopped("budget_exceeded", "retention_limit")
            self.members.append(payload)
            if len(self.records) < 1000:
                output_size = size(member.record) + bool(self.records)
                self.allocation.reserve(output_size * 2)
                self.output_bytes += output_size
                self.records.append(member.record)
            target = KnowledgeTarget(
                contribution_id=member.record.record_id,
                witness_ids=(member.dependencies.subject_witness.contribution_id,),
            )
        else:
            entity = EntitySelectionItem.model_validate_json(payload)
            if self.entities and entity.entity_id <= self.entities[-1]:
                raise ValueError("Entity ordering changed")
            self.entities.append(entity.entity_id)
            output_size = size(entity.entity_id) + 1
            self.allocation.reserve(output_size * 2)
            self.output_bytes += output_size
            target = KnowledgeTarget(
                contribution_id=entity.witness.contribution_id,
                witness_ids=(entity.witness.contribution_id,),
            )
        if self.output_bytes > SET_BYTES:
            raise Stopped("budget_exceeded", "retention_limit")
        with self.capture.guard():
            self.capture.retain(ReportTargets(values=(target,)))


def bindings_for(
    service: QueryService,
    observer: ReadObserver,
    capture: Capture | CaptureUnavailable,
) -> tuple[AuthorizationBinding, ...]:
    # Successful worker validation plus the original generation protects business
    # membership even when diagnostics cannot retain its bounded target manifest.
    business = AuthorizationBinding(service.identity, observer.scope, "read", [])
    return (
        (*capture.group.bindings, business) if capture.group.state == "provisional" else (business,)
    )


def execute(
    service: QueryService,
    request: QueryRequest,
    options: ExplainOptions,
    start: float,
    observer: ReadObserver,
    budget: PrivateBudget,
    capture: Capture | CaptureUnavailable,
    reference: ObserverReference,
) -> Explained[QueryExecution]:
    from kg.query.service import closure

    required = closure(request)
    output = next(s for s in request.steps if s.step_id == request.output_step)
    for step in request.steps:
        if step.step_id not in required:
            continue
        if not isinstance(step, (ResolveStep, RecordsStep, CountStep)):
            raise Stopped("unsupported", "unsupported_operation")
        if isinstance(step, RecordsStep) and step.record_type != "decision":
            raise Stopped("unsupported", "unsupported_restriction")
    allocation = Allocation(service._support)
    retained: RetainedSet | None = None
    published = False
    try:
        service._support.expire()
        allocation.reserve(size(request.scope) * 3 + 16384)
        transfer = Transfer(allocation, capture)
        ledger = Ledger(budget, request.budget.max_operations, request.budget.max_records)
        ledger.transfer = transfer.receive
        service._run_worker(observer, request, ledger)
        summary = transfer.summary
        if summary is None or transfer.pending is not None:
            raise ValueError("Incomplete worker output")
        steps = tuple(
            item.model_copy(
                update={
                    "operations_executed": ledger.by_step.get(item.step_id, (0, 0))[0],
                    "records_examined": ledger.by_step.get(item.step_id, (0, 0))[1],
                }
            )
            for item in summary.steps
        )
        if (
            tuple(s.step_id for s in steps) != tuple(s.step_id for s in request.steps)
            or sum(s.operations_executed for s in steps) != ledger.operations
        ):
            raise ValueError("Worker step correlation mismatch")
        read_id = token()
        set_id = None
        state = summary.state
        truncated = not summary.exact
        reason: StopReason | None = "record_budget" if truncated else None
        if state == "ambiguous" or isinstance(output, ResolveStep):
            data: EntitiesResult | RecordsResult | AggregateResult = EntitiesResult(
                kind="entities",
                entity_ids=tuple(transfer.entities[:1000]),
            )
            if summary.count != len(transfer.entities):
                raise ValueError("Entity count mismatch")
            truncated |= len(transfer.entities) > 1000
            if state == "ambiguous":
                reason = "dependency_ambiguous"
        elif isinstance(output, CountStep):
            if summary.count != len(transfer.members):
                raise ValueError("Membership count mismatch")
            set_id = token()
            data = AggregateResult(
                kind="aggregate",
                count=summary.count,
                exact=summary.exact,
                supporting_records_step=output.records_step,
            )
            metadata = {
                "scope": request.scope,
                "source_request_id": request.request_id,
                "read_state_id": read_id,
                "records_step_id": output.records_step,
                "association": "direct_explicit_association/1",
                "exact": summary.exact,
                "members": (),
            }
            payload_bytes = size(metadata) - 2 + transfer.member_bytes
            allocation.reserve(size(metadata))
            retained = RetainedSet(
                request.scope,
                request.request_id,
                read_id,
                output.records_step,
                summary.exact,
                observer.retain(),
                transfer.members,
                payload_bytes,
            )
            if payload_bytes > SET_BYTES:
                raise Stopped("budget_exceeded", "retention_limit")
        else:
            if summary.count != len(transfer.members):
                raise ValueError("Record count mismatch")
            data = RecordsResult(kind="records", records=tuple(transfer.records))
            if summary.count > 1000:
                state, truncated = "partial", True
                reason = "display_limit" if summary.exact else "record_budget"
        result = QueryResult(
            contract_version="foundation/1",
            request_id=request.request_id,
            scope=request.scope,
            outcome=state,
            read_state_id=read_id,
            result_set_id=set_id,
            operations_executed=ledger.operations,
            records_examined=ledger.records,
            truncated=truncated,
            exhaustion="eligible_set" if summary.exact else "none",
            data=data,
        )
        result.validate_for(request)
        output_size = size(result)
        if output_size > SET_BYTES:
            raise Stopped("budget_exceeded", "retention_limit")
        allocation.reserve(output_size * 2)
        outcome = QueryExecution(
            result=result,
            steps=steps,
            elapsed_milliseconds=(time.monotonic() - start) * 1000,
            work_accounting="scoped_semantic_reservations/1",
            stop_reason=reason,
        )
        with capture.guard():
            capture.append(
                QueryPlan(
                    required_steps=required,
                    pruned_steps=tuple(
                        s.step_id for s in request.steps if s.step_id not in required
                    ),
                )
            )
            for telemetry in steps:
                if not telemetry.operations_executed:
                    continue
                capture.append(
                    QueryDecision(
                        kind="query.resolution"
                        if telemetry.operation == "resolve"
                        else "query.selection",
                        step_id=telemetry.step_id,
                        state="unique"
                        if telemetry.operation == "resolve" and telemetry.state == "complete"
                        else "exhausted"
                        if telemetry.state == "complete"
                        else "empty"
                        if telemetry.state == "empty"
                        else "ambiguous"
                        if telemetry.state == "ambiguous"
                        else "partial",
                        semantics="entity"
                        if telemetry.operation == "resolve"
                        else "record_instances"
                        if telemetry.operation == "count"
                        else "direct-subject-decision/1",
                    )
                )
                if telemetry.operation != "count":
                    capture.append(
                        BudgetEvent(
                            step_id=telemetry.step_id,
                            stage="resolve" if telemetry.operation == "resolve" else "records",
                            reservations=telemetry.records_examined,
                        )
                    )
            ids = (
                transfer.entities
                if isinstance(data, EntitiesResult)
                else [r.record_id for r in transfer.records]
            )
            capture.append(
                QueryDecision(
                    kind="query.output",
                    step_id=output.step_id,
                    state="ambiguous"
                    if state == "ambiguous"
                    else "exact"
                    if summary.exact
                    else "lower_bound",
                    semantics="entity"
                    if isinstance(data, EntitiesResult)
                    else "record_instances"
                    if isinstance(output, CountStep)
                    else "direct-subject-decision/1",
                    support_set_id=set_id,
                    selected_ids=tuple(ids[:200]) if options.detail == "detailed" else (),
                )
            )
        capture.finish("stopped" if state in ("ambiguous", "partial") else "succeeded")
        bindings = bindings_for(service, observer, capture)
        observers = tuple(capture.group.observers) or (reference,)
        with (
            service._authorizer.release(bindings, observers, budget, targets_verified=True),
            service._dispatcher.condition,
        ):
            service._check_open()
            budget.check_deadline()
            report = service._diagnostics._publish(capture)
            outcome = outcome.model_copy(
                update={
                    "elapsed_milliseconds": (time.monotonic() - start) * 1000,
                }
            )
            budget.check_deadline()
            if retained is not None and set_id is not None:
                allocation.publish(set_id, retained)
                published = True
            return Explained(outcome=outcome, report=report)
    finally:
        if retained is not None and not published:
            retained.observer.close()
        allocation.close()


def inspection_failure(
    request: SupportInspectionRequest,
    start: float,
    code: ErrorCode,
    reason: StopReason,
) -> SupportInspection:
    return SupportInspection(
        request_id=request.request_id,
        scope=request.scope,
        outcome="state_changed"
        if code == "state_changed"
        else "unsupported"
        if code == "unsupported"
        else "failed",
        elapsed_milliseconds=(time.monotonic() - start) * 1000,
        work_accounting="redacted",
        stop_reason=reason,
        error=Failure(code=code, diagnostic_id=token()),
    )


def inspect(
    service: QueryService,
    request: SupportInspectionRequest,
    options: ExplainOptions,
    start: float,
    deadline: Deadline,
) -> Explained[SupportInspection]:
    budget = PrivateBudget(deadline)
    allocation = Allocation(service._support)
    capture: Capture | CaptureUnavailable | None = None
    admitted = False
    try:
        with service._observer(request.scope, deadline, budget):
            admitted = True
            service._support.expire()
            item = service._support.sets.get(request.result_set_id)
            if (
                item is None
                or item.scope != request.scope
                or item.records_step_id != request.records_step_id
            ):
                raise Stopped("not_found", "not_found")
            reference = item.observer
            observer = reference.observer
            # Reject already-invalid generations before expensive revalidation.
            with service._authorizer.release(
                (AuthorizationBinding(service.identity, request.scope, "read", []),),
                (reference,),
                budget,
            ):
                pass
            capture = service._collector.begin_capture(
                "inspect_support",
                request.scope,
                required="read",
                request_id=request.request_id,
                options=options,
                observer=reference,
            )
            end = min(len(item.members), request.start_ordinal + request.limit)
            begin = min(request.start_ordinal, len(item.members))
            selected: str | None = None
            records: list[Record] = []
            allocation.reserve(size(request.scope) * 3 + 16384)

            def transfer(frame: Frame) -> Reply:
                nonlocal selected
                if frame.action == "fetch":
                    index = begin + frame.view
                    if index >= end:
                        return Reply(state="ok")
                    if frame.view != len(records):
                        raise ValueError("Inspection ordinal mismatch")
                    selected = item.members[index]
                    length = utf8_size(selected)
                    allocation.reserve(length * 3)
                    member = DecisionSelectionItem.model_validate_json(selected)
                    records.append(member.record)
                    assert capture is not None
                    with capture.guard():
                        capture.retain(
                            ReportTargets(
                                values=(
                                    KnowledgeTarget(
                                        contribution_id=member.record.record_id,
                                        witness_ids=(
                                            member.dependencies.subject_witness.contribution_id,
                                        ),
                                    ),
                                )
                            )
                        )
                    return Reply(state="ok", value=length)
                if frame.action != "fetch_chunk" or selected is None:
                    raise ValueError("Unexpected inspection transfer")
                offset = frame.n - 1
                from kg.query._retention import CHUNK

                text = selected[offset : offset + CHUNK]
                if not text:
                    raise ValueError("Invalid inspection offset")
                return Reply(state="ok", text=text, value=int(offset + len(text) == len(selected)))

            ledger = Ledger(budget, request.budget.max_operations, request.budget.max_records)
            ledger.transfer = transfer
            service._run_worker(observer, request, ledger)
            if (
                len(records) != end - begin
                or ledger.records != len(records)
                or ledger.operations != 1
            ):
                raise ValueError("Incomplete inspection")
            outcome = SupportInspection(
                request_id=request.request_id,
                scope=request.scope,
                outcome="complete" if records else "empty",
                read_state_id=item.read_state_id,
                result_set_id=request.result_set_id,
                records_step_id=item.records_step_id,
                exact=item.exact,
                total=len(item.members),
                records=tuple(records),
                next_ordinal=end if end < len(item.members) else None,
                exhausted=end == len(item.members),
                operations_executed=1,
                records_examined=len(records),
                elapsed_milliseconds=(time.monotonic() - start) * 1000,
                work_accounting="scoped_semantic_reservations/1",
            )
            length = size(outcome)
            if length > SET_BYTES:
                raise Stopped("budget_exceeded", "retention_limit")
            allocation.reserve(length * 2)
            with capture.guard():
                capture.append(
                    BudgetEvent(
                        step_id=item.records_step_id,
                        stage="support",
                        reservations=ledger.records,
                    )
                )
                capture.append(
                    QueryDecision(
                        kind="query.output",
                        step_id=item.records_step_id,
                        state="displayed",
                        semantics="record_instances",
                        support_set_id=request.result_set_id,
                        selected_ids=tuple(r.record_id for r in records[:200])
                        if options.detail == "detailed"
                        else (),
                    )
                )
            capture.finish("succeeded")
            bindings = bindings_for(service, observer, capture)
            observers = tuple(capture.group.observers) or (reference,)
            with (
                service._authorizer.release(bindings, observers, budget, targets_verified=True),
                service._dispatcher.condition,
            ):
                service._check_open()
                if time.monotonic() >= item.expires:
                    raise Stopped("not_found", "not_found")
                budget.check_deadline()
                report = service._diagnostics._publish(capture)
                budget.check_deadline()
                return Explained(outcome=outcome, report=report)
    except DeadlineStop:
        code: ErrorCode = "budget_exceeded"
        reason: StopReason = "time_budget"
    except PrivateResourceStop:
        code, reason = "budget_exceeded", "resource_budget"
    except Stopped as error:
        code, reason = error.code, error.reason
    except EvidenceServiceError as error:
        code = (
            "state_changed"
            if admitted and error.failure.code == "forbidden"
            else error.failure.code
        )
        reason = (
            "state_changed"
            if code == "state_changed"
            else "forbidden"
            if code == "forbidden"
            else "internal_error"
        )
    except (ValueError, TypeError, KeyError, OSError, EOFError, MemoryError) as error:
        LOGGER.error("Support inspection failure class=%s", type(error).__name__)
        code, reason = "internal_error", "internal_error"
    finally:
        allocation.close()
    if capture is not None:
        capture.group.redact()
        if isinstance(capture, Capture) and capture.active:
            capture.finish("failed", reason=code)
    return Explained(
        outcome=inspection_failure(request, start, code, reason),
        report=ReportAvailability(state="redacted", reason=code),
    )
