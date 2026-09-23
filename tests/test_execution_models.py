"""Value validation is not acceptance for the four planned services."""

import pytest
from pydantic import TypeAdapter, ValidationError

from kg.models.execution import (
    ExecutionEvent,
    ExecutionReport,
    ExplainOptions,
    RecordedEvent,
    ReportAvailability,
)
from kg.models.execution_events import EvidenceEvent
from kg.models.foundation import EvidenceRef
from kg.models.indexing_events import IndexCandidate, IndexPhase
from kg.models.knowledge_events import KnowledgeValidation
from kg.models.processing_events import ProcessingAck, ProcessingDecision
from kg.models.query_events import QueryBudget, QueryDecision


def report(**updates):
    values = dict(
        report_id="report",
        execution_id="execution",
        owning_service="evidence",
        operation="write",
        observation_kind="captured_execution",
        capture_level="summary",
        outcome="applied",
        events=(),
        captured_event_count=0,
        displayed_event_count=0,
        truncated=False,
    )
    return ExecutionReport(**(values | updates))


@pytest.mark.parametrize(
    "options",
    [
        {"include_quotes": True},
        {"detail": "full"},
        {"include_quotes": 1},
        {"detail": "detailed", "request": "secret"},
        {"detail": None},
    ],
)
def test_strict_options(options):
    with pytest.raises(ValidationError):
        ExplainOptions(**options)


@pytest.mark.parametrize(
    "updates",
    [
        {"report_version": "execution-report/2"},
        {"operation": "user supplied operation"},
        {"captured_event_count": 1, "displayed_event_count": 2},
        {"captured_event_count": 33},
        {"displayed_event_count": True},
        {"state": "redacted"},
        {"events": [{"kind": "arbitrary", "payload": "secret"}]},
        {"request_body": "secret"},
        {"private_vm": 100},
    ],
)
def test_strict_envelope(updates):
    with pytest.raises(ValidationError):
        report(**updates)


def test_no_redacted_counts_and_explicit_null_fields():
    value = report()
    dumped = value.model_dump()
    assert dumped["request_id"] is None and dumped["parent_execution_id"] is None
    assert ExecutionReport.model_validate_json(value.model_dump_json()) == value
    with pytest.raises(ValidationError):
        ReportAvailability(state="redacted", captured_event_count=0)
    assert "events" not in ReportAvailability(state="redacted").model_dump()


def test_closed_package_union_and_nullable_candidate_stages():
    adapter = TypeAdapter(ExecutionEvent)
    for event in (
        KnowledgeValidation(phase="type", status="rejected", reason="invalid_type"),
        QueryBudget(step_id="step", stage="dense", reservations=5),
        ProcessingAck(observation="acknowledgement_staged"),
    ):
        assert adapter.validate_json(event.model_dump_json()) == event
    reference = EvidenceRef(
        corpus_id="work",
        source_namespace="source",
        document_id="doc",
        revision_id="rev",
        anchor_id="anchor",
    )
    candidate = IndexCandidate(reference=reference, lexical_member=True, lexical_rank=1)
    assert candidate.model_dump()["rerank_score"] is None
    with pytest.raises(ValidationError):
        IndexCandidate(reference=reference, dense_score=float("nan"))
    with pytest.raises(ValidationError):
        report(
            events=(RecordedEvent(sequence=0, event=candidate),),
            captured_event_count=1,
            displayed_event_count=1,
        )


@pytest.mark.parametrize(
    "reason",
    [
        "provider_unavailable",
        "invalid_provider_output",
        "unsupported_policy",
        "superseded_attempt",
        "claim_lost",
        "processor_not_available",
        "not_processed",
        "storage_failure",
        "inactive",
        "index_not_ready",
    ],
)
def test_approved_index_failure_distinctions_round_trip(reason):
    event = IndexPhase(phase="publication", status="failed", reason=reason)
    assert TypeAdapter(ExecutionEvent).validate_json(event.model_dump_json()) == event
    envelope = report(
        reason=reason,
        outcome="failed",
        events=(RecordedEvent(sequence=0, event=event),),
        captured_event_count=1,
        displayed_event_count=1,
    )
    assert ExecutionReport.model_validate_json(envelope.model_dump_json()).reason == reason
    with pytest.raises(ValidationError):
        IndexPhase(phase="publication", status="failed", reason="RAW-UNTRUSTED-PROVIDER-EXCEPTION")
    with pytest.raises(ValidationError):
        report(
            events=(
                RecordedEvent(
                    sequence=1,
                    event=EvidenceEvent(phase="read", decision="observed"),
                ),
            ),
            captured_event_count=1,
            displayed_event_count=1,
        )


@pytest.mark.parametrize("operation", [
    "entity", "entities", "contributions", "register_batch", "resume_batch",
    "batch_status", "unit_receipt", "schedule", "fail", "begin_snapshot",
    "observe_page", "finish_snapshot",
])
def test_approved_operation_names_round_trip(operation):
    value = report(operation=operation)
    assert ExecutionReport.model_validate_json(value.model_dump_json()) == value


@pytest.mark.parametrize("decision", ["queued", "retry_wait", "superseded", "cancelled"])
@pytest.mark.parametrize("reason", [
    "awaiting_input", "dependency_changed", "retry_scheduled", "retry_exhausted",
    "purge_blocked", "plan_disabled", "authority_unavailable",
])
def test_processing_status_and_reasons_round_trip(decision, reason):
    event = ProcessingDecision(
        kind="processing.status_observation", decision=decision,
        observation_kind="current_inspection", reason=reason,
    )
    assert TypeAdapter(ExecutionEvent).validate_json(event.model_dump_json()) == event
    envelope = report(
        owning_service="processing", operation="job", reason=reason,
        events=(RecordedEvent(sequence=0, event=event),),
        captured_event_count=1, displayed_event_count=1,
    )
    assert ExecutionReport.model_validate_json(envelope.model_dump_json()) == envelope
    availability = ReportAvailability(state="redacted", reason=reason)
    assert ReportAvailability.model_validate_json(availability.model_dump_json()) == availability


@pytest.mark.parametrize("updates", [
    {"decision": "RAW-SECRET-STATE"},
    {"reason": "RAW-SECRET-REASON"},
    {"reason": "boundaries_required"},
    {"kind": "processing.user_supplied"},
    {"private_visits": 100},
])
def test_processing_vocabulary_stays_closed(updates):
    with pytest.raises(ValidationError):
        ProcessingDecision(**{
            "kind": "processing.status_observation", "decision": "queued", **updates,
        })


def test_query_ambiguity_uses_existing_outcome_and_typed_decision():
    event = QueryDecision(kind="query.resolution", step_id="resolve", state="ambiguous")
    envelope = report(
        owning_service="query", operation="execute", outcome="stopped",
        events=(RecordedEvent(sequence=0, event=event),),
        captured_event_count=1, displayed_event_count=1,
    )
    assert ExecutionReport.model_validate_json(envelope.model_dump_json()) == envelope
    with pytest.raises(ValidationError):
        report(outcome="ambiguous")


@pytest.mark.parametrize("reason", ["budget_exceeded", "deadline"])
def test_query_limit_mapping_does_not_disclose_private_work(reason):
    envelope = report(owning_service="query", operation="execute", outcome="stopped", reason=reason)
    assert ExecutionReport.model_validate_json(envelope.model_dump_json()) == envelope
    availability = ReportAvailability(state="redacted", reason=reason)
    assert ReportAvailability.model_validate_json(availability.model_dump_json()) == availability
    for value in (envelope, availability):
        assert "private" not in value.model_dump_json()
    for private_reason in ("operation_budget", "record_budget", "time_budget"):
        with pytest.raises(ValidationError):
            report(reason=private_reason)
