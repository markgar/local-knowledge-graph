"""Executable query/1 envelopes; foundation results remain unchanged."""

from typing import Literal, Self

from pydantic import Field, model_validator

from kg.models.foundation import Failure, QueryBudget, QueryResult, Record, Scope, Token, Value

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
    operations: tuple[Literal["evidence", "resolve", "records", "count"], ...] = (
        "evidence",
        "resolve",
    )
    record_types: tuple[Literal["decision"], ...] = ()
    association: Literal["direct_explicit_association/1"] | None = None
    count_identity: Literal["submitted_assertion_id"] | None = None
    support_inspection: bool = True
    evidence_kinds: tuple[Literal["anchor", "passage"], ...] = ("anchor", "passage")
    adapter_version: Literal["canonical-evidence/1"] = "canonical-evidence/1"
    maximum_budget: QueryBudget = QueryBudget(
        max_operations=16,
        max_records=10_000,
        max_milliseconds=30_000,
    )


class SupportInspectionRequest(Value):
    interface_version: Literal["query/1"] = "query/1"
    request_id: Token
    scope: Scope
    result_set_id: Token
    records_step_id: Token
    start_ordinal: int = Field(default=0, ge=0)
    limit: int = Field(default=1000, ge=1, le=1000)
    budget: QueryBudget = Field(default_factory=QueryBudget)


class SupportInspection(Value):
    interface_version: Literal["query/1"] = "query/1"
    request_id: Token
    scope: Scope
    outcome: Literal["complete", "empty", "failed", "state_changed", "unsupported"]
    read_state_id: Token | None = None
    result_set_id: Token | None = None
    records_step_id: Token | None = None
    exact: bool | None = None
    total: int | None = Field(default=None, ge=0)
    records: tuple[Record, ...] = Field(default=(), max_length=1000)
    next_ordinal: int | None = Field(default=None, ge=0)
    exhausted: bool | None = None
    operations_executed: int = Field(default=0, ge=0, le=1)
    records_examined: int = Field(default=0, ge=0, le=1000)
    elapsed_milliseconds: float = Field(ge=0, allow_inf_nan=False)
    work_accounting: Literal["scoped_semantic_reservations/1", "redacted"]
    stop_reason: StopReason | None = None
    error: Failure | None = None

    @model_validator(mode="after")
    def disclosure(self) -> Self:
        if self.error is not None:
            if (
                self.outcome not in ("failed", "state_changed", "unsupported")
                or self.work_accounting != "redacted"
                or self.records
                or self.total is not None
                or self.exact is not None
                or self.next_ordinal is not None
                or self.exhausted is not None
                or self.read_state_id
                or self.result_set_id
                or self.records_step_id
                or self.operations_executed
                or self.records_examined
            ):
                raise ValueError("Inspection failure must withhold retained membership")
        elif (
            self.outcome not in ("complete", "empty")
            or self.work_accounting != "scoped_semantic_reservations/1"
            or self.total is None
            or self.exact is None
            or self.exhausted is None
            or not self.read_state_id
            or not self.result_set_id
            or not self.records_step_id
            or self.operations_executed != 1
            or self.records_examined != len(self.records)
            or (self.outcome == "empty") != (not self.records)
            or self.exhausted != (self.next_ordinal is None)
        ):
            raise ValueError("Inspection success requires correlated membership and accounting")
        return self
