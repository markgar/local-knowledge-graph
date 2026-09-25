"""Test-only access to the private canonical knowledge write kernel."""

from __future__ import annotations

import time

from kg._execution_budget import Deadline, DeadlineStop, PrivateBudget, PrivateResourceStop
from kg.evidence import _receipts
from kg.evidence import _reporting as reporting
from kg.evidence._authorization import authorize
from kg.evidence._transactions import writing
from kg.evidence._values import timestamp, validated
from kg.evidence.errors import EvidenceServiceError
from kg.knowledge import _write
from kg.knowledge._reports import authorize_target, retain_manifest
from kg.knowledge._store import Store
from kg.knowledge._write import Manifest
from kg.knowledge._write_models import ChangeSet, ChangeSetReceipt
from kg.models.execution import SUMMARY_OPTIONS
from kg.models.execution_events import CommitEvent, EvidenceEvent
from kg.models.foundation import BatchResult, WriteBatch, WriteOutcome, WriteRequest


def _outcome(request_id, status, *, receipt=None, error=None):
    return WriteOutcome.model_construct(
        request_id=request_id,
        status=status,
        receipt=receipt,
        error=error,
    )


def write(service, request: WriteRequest, *, budget: PrivateBudget | None = None):
    """Exercise a private canonical plan without reopening the removed public API."""
    if not isinstance(request.payload, ChangeSet):
        return service.write(request)
    request = request.model_copy(update={"payload": validated(ChangeSet, request.payload)})
    capture = service._collector.begin_capture(
        "write",
        request.scope,
        required="write_knowledge",
        request_id=request.request_id,
    )
    context = None
    outcome = None
    try:
        root = budget or PrivateBudget(Deadline(time.monotonic() + 30))
        limited = root.limited(max_visits=10_000)
        with writing(service.database, service.identity, budget=limited) as context:
            authorize(context.connection, service.identity, request.scope, "write_knowledge")
            authorize(context.connection, service.identity, request.scope, "read")
            with capture.guard():
                capture.append(EvidenceEvent(phase="authorization", decision="passed"))
            key = _receipts.canonical_key(request)
            stored = _receipts.lookup_key(context, key)
            if stored is not None:
                response = context.connection.execute(
                    "SELECT * FROM knowledge_write_response WHERE key_id=?",
                    (stored["key_id"],),
                ).fetchone()
                if response is None:
                    raise EvidenceServiceError("internal_error")
                manifest = Manifest.model_validate_json(response["authorization_json"])
                if (manifest.owner_id, manifest.writer_id) != (
                    request.attribution.owner_id,
                    request.attribution.writer_id,
                ):
                    raise EvidenceServiceError("forbidden")
                store = Store(context.connection, request.scope, limited)
                try:
                    for target in manifest.targets:
                        authorize_target(store, context.identity, target)
                finally:
                    store.close()
                observed = _receipts.clock(context.connection, service._clock())
                if stored["expired"] or timestamp(observed) >= stored["expires_at"]:
                    context.connection.execute(
                        "DELETE FROM knowledge_write_response WHERE key_id=?",
                        (stored["key_id"],),
                    )
                    context.connection.execute(
                        "UPDATE write_key SET expired=1 WHERE key_id=?",
                        (stored["key_id"],),
                    )
                    raise EvidenceServiceError("retry_expired")
                if _write.digest(request) != stored["digest"]:
                    raise EvidenceServiceError("retry_conflict")
                receipt = ChangeSetReceipt.model_validate_json(response["receipt_json"])
                retain_manifest(capture, manifest)
                outcome = _outcome(request.request_id, stored["status"], receipt=receipt)
                with capture.guard():
                    capture.append(EvidenceEvent(phase="replay", decision="replayed"))
                return outcome
            at = _receipts.clock(context.connection, service._clock())
            with capture.guard():
                capture.append(EvidenceEvent(phase="replay", decision="fresh"))
            receipt, status, _, manifest = _write.apply_plan(
                context,
                request,
                request.payload,
                at,
                limited,
                capture=capture,
            )
            retain_manifest(capture, manifest)
            with capture.guard():
                capture.append(EvidenceEvent(phase="mutation", decision=status))
            outcome = _outcome(request.request_id, status, receipt=receipt)
            return outcome
    except EvidenceServiceError as error:
        status = (
            "conflict"
            if error.failure.code in {"state_conflict", "retry_conflict"}
            else "failed"
            if error.failure.code in {"internal_error", "budget_exceeded"}
            else "rejected"
        )
        outcome = _outcome(request.request_id, status, error=error.failure)
        if error.failure.code in {
            "forbidden",
            "not_found",
            "state_changed",
            "budget_exceeded",
        }:
            capture.group.redact(error.failure.code)
        return outcome
    except (PrivateResourceStop, DeadlineStop):
        failure = EvidenceServiceError("budget_exceeded").failure
        outcome = _outcome(request.request_id, "failed", error=failure)
        return outcome
    finally:
        with capture.guard():
            capture.append(
                CommitEvent(
                    observation=(context.commit_outcome if context is not None else "not_attempted")
                )
            )
        capture.finish(
            outcome.status if outcome is not None else "failed",
            reason=outcome.error.code if outcome is not None and outcome.error else None,
            diagnostic_id=(
                outcome.error.diagnostic_id if outcome is not None and outcome.error else None
            ),
        )
        reporting.deliver(service.diagnostics._publish(capture))


def write_explained(service, request, options=None):
    def operation(value):
        return write(service, value)

    return (
        reporting.explained(options, operation, request)
        if options is not None
        else reporting.explained(SUMMARY_OPTIONS, operation, request)
    )


def write_batch(service, batch: WriteBatch):
    outcomes = tuple(write(service, request) for request in batch.items)
    successes = sum(outcome.receipt is not None for outcome in outcomes)
    return BatchResult.model_construct(
        contract_version="foundation/1",
        batch_id=batch.batch_id,
        outcomes=outcomes,
        status="complete" if successes == len(outcomes) else "partial" if successes else "failed",
    )
