"""Dependent K1 execution in one canonical snapshot; no producer semantics here."""

from __future__ import annotations

import time
from typing import Literal

from pydantic import Field

from kg._execution_budget import PublicBudgetStop
from kg.evidence._read_context import CanonicalReadContext
from kg.evidence.errors import EvidenceServiceError
from kg.knowledge._reader import KnowledgeReader
from kg.knowledge._selection import (
    DecisionSelectionItem,
    EntitySelector,
    SelectionFailure,
    SelectionTerminal,
)
from kg.knowledge._store import Store
from kg.models.foundation import CountStep, QueryRequest, RecordsStep, ResolveStep, Value
from kg.models.query import StepTelemetry, SupportInspectionRequest
from kg.query._dispatch import Stopped
from kg.query._meter import Frame, RemoteBudget
from kg.query._retention import CHUNK, SET_BYTES, encode, size


class Summary(Value):
    exact: bool
    count: int = Field(ge=0, le=10_000)
    state: Literal["complete", "empty", "partial", "ambiguous"]
    steps: tuple[StepTelemetry, ...] = Field(max_length=16)


def decisions_available(context: CanonicalReadContext) -> bool:
    store = Store(context.connection, context.scope, context.meter.private_budget, context=context)
    try:
        try:
            return any(p.record_projection is not None for p in store.schema().predicates)
        except EvidenceServiceError as error:
            if error.failure.code == "unsupported":
                raise Stopped("unsupported", "unsupported_operation") from None
            raise
    finally:
        store.close()


def transmit(
    budget: RemoteBudget,
    value: Value,
    kind: Literal["entity", "decision", "summary"],
) -> None:
    length = size(value)
    if length > SET_BYTES:
        raise Stopped("budget_exceeded", "retention_limit")
    budget.rpc(Frame(action="payload", n=length, kind=kind))
    with budget.reserve_scratch(max(1, length * 3), "general"):
        payload = encode(value)
        if len(payload.encode()) != length:
            raise ValueError("Payload sizing mismatch")
        for start in range(0, len(payload), CHUNK):
            budget.rpc(Frame(action="chunk", text=payload[start : start + CHUNK]))


def terminal_exact(terminal: SelectionTerminal | None) -> bool:
    if terminal is None or terminal.kind == "eligible_eof":
        return True
    if terminal.kind == "public_budget_stop":
        return False
    if terminal.kind in ("private_resource_stop", "deadline_stop"):
        raise Stopped(
            "budget_exceeded",
            "resource_budget" if terminal.kind == "private_resource_stop" else "time_budget",
        )
    if isinstance(terminal, SelectionFailure):
        raise EvidenceServiceError(terminal.failure.code)
    raise ValueError("Unexpected selection terminal")


def execute(context: CanonicalReadContext, budget: RemoteBudget, request: QueryRequest) -> None:
    from kg.query.service import closure

    required = closure(request)
    if any(
        isinstance(s, RecordsStep) and s.step_id in required for s in request.steps
    ) and not decisions_available(context):
        raise Stopped("unsupported", "unsupported_operation")
    reader = KnowledgeReader(context, context.identity)
    steps: list[StepTelemetry] = []
    subject: str | None = None
    exact = True
    count = 0
    state: Literal["complete", "empty", "partial", "ambiguous"] = "empty"
    halted = False
    for step in request.steps:
        started = time.monotonic()
        if step.step_id not in required or halted:
            steps.append(
                StepTelemetry(
                    step_id=step.step_id,
                    operation=step.operation,
                    state="not_started" if step.step_id in required else "not_needed",
                    operations_executed=0,
                    records_examined=0,
                    elapsed_milliseconds=0,
                )
            )
            continue
        budget.rpc(Frame(action="begin", step_id=step.step_id))
        if isinstance(step, ResolveStep):
            cursor = reader.resolve_entity(EntitySelector(name=step.name, entity_id=step.entity_id))
            count = 0
            try:
                while True:
                    page = cursor.read()
                    exact = terminal_exact(page.terminal)
                    for item in page.items:
                        transmit(budget, item, "entity")
                        subject = item.entity_id
                        count += 1
                    if page.terminal is not None:
                        break
            finally:
                cursor.close()
            if count > 1:
                state, halted = "ambiguous", True
            elif not exact:
                if step.step_id != request.output_step:
                    raise Stopped("budget_exceeded", "record_budget")
                state = "partial"
            else:
                state = "complete" if count else "empty"
        elif isinstance(step, RecordsStep):
            count = 0
            if subject is not None:
                decisions = reader.select_decisions(subject)
                try:
                    while True:
                        decision_page = decisions.read()
                        exact = terminal_exact(decision_page.terminal)
                        for member in decision_page.items:
                            transmit(budget, member, "decision")
                            count += 1
                        if decision_page.terminal is not None:
                            break
                finally:
                    decisions.close()
            state = "partial" if not exact else "complete" if count else "empty"
        elif isinstance(step, CountStep):
            pass
        else:
            raise Stopped("unsupported", "unsupported_operation")
        steps.append(
            StepTelemetry(
                step_id=step.step_id,
                operation=step.operation,
                state=state,
                operations_executed=1,
                records_examined=0,
                elapsed_milliseconds=(time.monotonic() - started) * 1000,
            )
        )
    transmit(budget, Summary(exact=exact, count=count, state=state, steps=tuple(steps)), "summary")


def inspect(
    context: CanonicalReadContext,
    budget: RemoteBudget,
    request: SupportInspectionRequest,
) -> None:
    budget.rpc(Frame(action="begin", step_id=request.records_step_id))
    reader = KnowledgeReader(context, context.identity)
    for index in range(request.limit):
        length = budget.rpc(Frame(action="fetch", view=index))
        if not length:
            break
        with budget.reserve_scratch(length * 3, "general"):
            chunks: list[str] = []
            offset = 0
            while True:
                reply = budget.exchange(Frame(action="fetch_chunk", n=offset + 1))
                chunks.append(reply.text)
                offset += len(reply.text)
                if reply.value:
                    break
            payload = "".join(chunks)
            if len(payload.encode()) != length:
                raise ValueError("Inspection payload mismatch")
            member = DecisionSelectionItem.model_validate_json(payload)
            try:
                context.meter.reserve_public("support_member")
            except PublicBudgetStop:
                raise Stopped("budget_exceeded", "record_budget") from None
            reader.revalidate_member(member)
    context.check_active()
