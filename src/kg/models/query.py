"""Executable query/1 envelopes; foundation results remain unchanged."""

from typing import Literal, Self

from pydantic import Field, model_validator

from kg.models.foundation import QueryBudget, QueryResult, Scope, Token, Value

StopReason = Literal[
    "unsupported_operation",
    "unsupported_restriction",
    "operation_budget",
    "record_budget",
    "time_budget",
    "display_limit",
    "retention_limit",
    "resource_budget",
    "admission_limit",
    "service_closed",
    "state_changed",
    "index_not_ready",
    "provider_unavailable",
    "dependency_ambiguous",
    "invalid_request",
    "forbidden",
    "internal_error",
    "not_found",
]
Operation = Literal["evidence", "resolve", "records", "count", "search", "paths"]


class StepTelemetry(Value):
    step_id: Token
    operation: Operation
    state: Literal[
        "not_needed", "not_started", "complete", "empty", "ambiguous", "partial", "failed"
    ]
    operations_executed: int = Field(ge=0, le=1)
    records_examined: int = Field(ge=0, le=10_000)
    elapsed_milliseconds: float = Field(ge=0, allow_inf_nan=False)


class QueryExecution(Value):
    interface_version: Literal["query/1"] = "query/1"
    result: QueryResult
    elapsed_milliseconds: float = Field(ge=0, allow_inf_nan=False)
    steps: tuple[StepTelemetry, ...] = Field(default=(), max_length=16)
    stop_reason: StopReason | None = None
    work_accounting: Literal["scoped_semantic_reservations/1", "redacted"]

    @model_validator(mode="after")
    def accounting(self) -> Self:
        if self.result.data is None:
            if (
                self.work_accounting != "redacted"
                or self.steps
                or self.result.operations_executed
                or self.result.records_examined
                or self.result.read_state_id is not None
                or self.result.result_set_id is not None
                or self.result.continuation is not None
                or self.result.truncated
                or self.result.exhaustion != "none"
            ):
                raise ValueError("No-data executions must redact all captured work")
        elif (
            self.work_accounting != "scoped_semantic_reservations/1"
            or sum(step.operations_executed for step in self.steps)
            != self.result.operations_executed
            or sum(step.records_examined for step in self.steps) != self.result.records_examined
            or len({step.step_id for step in self.steps}) != len(self.steps)
        ):
            raise ValueError("Disclosed step accounting must equal totals")
        return self


class QueryCapabilities(Value):
    interface_version: Literal["query/1"] = "query/1"
    scope: Scope
    operations: tuple[Literal["evidence"], ...] = ("evidence",)
    evidence_kinds: tuple[Literal["anchor"], ...] = ("anchor",)
    adapter_version: Literal["canonical-anchor/1"] = "canonical-anchor/1"
    maximum_budget: QueryBudget = QueryBudget(
        max_operations=16,
        max_records=10_000,
        max_milliseconds=30_000,
    )
