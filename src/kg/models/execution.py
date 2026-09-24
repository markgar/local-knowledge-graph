"""Strict process-local execution-report/1 values, independent of service imports."""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from kg.models.execution_events import (
    CommitEvent,
    EvidenceEvent,
    ObservationKind,
    SafeOutcome,
    SafeReason,
)
from kg.models.foundation import EvidenceRef, Text, Token, Value
from kg.models.indexing_events import IndexEvent
from kg.models.knowledge_events import KnowledgeEvent
from kg.models.processing_events import ProcessingEvent
from kg.models.query_events import QueryEvent

ServiceName = Literal["evidence", "knowledge", "indexing", "query", "processing"]
OperationName = Literal[
    "classification_review",
    "classification_history",
    "write",
    "write_batch",
    "current",
    "document",
    "state",
    "content",
    "history",
    "revisions",
    "evidence",
    "citation",
    "anchors",
    "revision_anchors",
    "capabilities",
    "replace_seed_set",
    "resolve",
    "records",
    "contribution",
    "entity",
    "entities",
    "contributions",
    "seed_set",
    "produce",
    "process",
    "pending",
    "cleanup",
    "passages",
    "index",
    "search",
    "status",
    "execute",
    "inspect_support",
    "claim",
    "heartbeat",
    "checkpoint",
    "acknowledge",
    "job",
    "jobs",
    "retry",
    "recover",
    "register_batch",
    "resume_batch",
    "batch_status",
    "unit_receipt",
    "schedule",
    "fail",
    "begin_snapshot",
    "observe_page",
    "finish_snapshot",
]


class ExplainOptions(Value):
    detail: Literal["summary", "detailed"] = "summary"
    include_quotes: bool = False

    @model_validator(mode="after")
    def quote_opt_in(self) -> Self:
        if self.include_quotes and self.detail != "detailed":
            raise ValueError("quotes require detailed capture")
        return self


SUMMARY_OPTIONS = ExplainOptions()


class QuoteEvent(Value):
    kind: Literal["execution.quote"] = "execution.quote"
    reference: EvidenceRef
    quote: Text = Field(max_length=8192)


ExecutionEvent = Annotated[
    CommitEvent
    | EvidenceEvent
    | QuoteEvent
    | KnowledgeEvent
    | IndexEvent
    | QueryEvent
    | ProcessingEvent,
    Field(discriminator="kind"),
]


class RecordedEvent(Value):
    sequence: int = Field(ge=0, lt=200)
    event: ExecutionEvent


class ReportAvailability(Value):
    state: Literal["not_collected", "unavailable", "redacted"]
    execution_id: Token | None = None
    diagnostic_id: Token | None = None
    reason: SafeReason | None = None


class ReportHeader(Value):
    report_version: Literal["execution-report/1"] = "execution-report/1"
    report_id: Token
    execution_id: Token
    owning_service: ServiceName
    operation: OperationName
    request_id: Token | None = None
    diagnostic_id: Token | None = None
    parent_execution_id: Token | None = None
    step_id: Token | None = None
    job_id: Token | None = None
    unit_id: Token | None = None
    observation_kind: ObservationKind
    capture_level: Literal["summary", "detailed"]
    state: Literal["collected"] = "collected"
    outcome: SafeOutcome
    reason: SafeReason | None = None


class ExecutionReport(ReportHeader):
    configuration_ids: tuple[Token, ...] = Field(default=(), max_length=32)
    events: tuple[RecordedEvent, ...] = Field(max_length=200)
    captured_event_count: int = Field(ge=0, le=200)
    displayed_event_count: int = Field(ge=0, le=200)
    truncated: bool

    @model_validator(mode="after")
    def event_counts(self) -> Self:
        limit = 32 if self.capture_level == "summary" else 200
        if not (
            self.displayed_event_count == len(self.events) <= self.captured_event_count <= limit
        ):
            raise ValueError("inconsistent event counts")
        if tuple(item.sequence for item in self.events) != tuple(range(len(self.events))):
            raise ValueError("events must preserve capture order")
        if self.capture_level == "summary" and any(
            isinstance(item.event, QuoteEvent)
            or item.event.kind == "indexing.candidate"
            or bool(getattr(item.event, "selected_ids", ()))
            for item in self.events
        ):
            raise ValueError("summary cannot contain candidate or quote details")
        return self


class ReportHeaderPage(Value):
    entries: tuple[ReportHeader, ...] = Field(max_length=32)


class Explained[T](Value):
    outcome: T
    report: ExecutionReport | ReportAvailability
