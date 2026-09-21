"""E1-owned dispatch; optional trusted participants never wrap this transaction."""

from __future__ import annotations

from datetime import datetime

from kg.evidence import _receipts, _store, _writer
from kg.evidence._authorization import authorize, authorize_writer
from kg.evidence._coordination import (
    CoordinatedWriteParticipant,
    NewWork,
    SettledFailure,
    SettledSuccess,
    UnitIdentity,
    WriteCommit,
)
from kg.evidence._transactions import CanonicalWriteContext, writing
from kg.evidence._values import validated
from kg.evidence.database import EvidenceDatabase
from kg.evidence.errors import EvidenceServiceError
from kg.models.evidence import LocalIdentity
from kg.models.foundation import (
    BatchResult,
    DocumentReceipt,
    Failure,
    PutDocument,
    RemoveDocument,
    WriteBatch,
    WriteOutcome,
    WriteRequest,
)


def failed(request_id: str, failure: Failure) -> WriteOutcome:
    return WriteOutcome(
        request_id=request_id,
        status="conflict" if failure.code in {"state_conflict", "retry_conflict"}
        else "failed" if failure.code == "internal_error" else "rejected",
        error=failure,
    )


def require_current_receipt(
    context: CanonicalWriteContext, request: WriteRequest, receipt: DocumentReceipt,
) -> None:
    """First snapshot observation is not historical replay or K1 receipt adoption."""
    connection = context.connection
    if not isinstance(request.payload, (PutDocument, RemoveDocument)):
        raise EvidenceServiceError("unsupported")
    document = _store.document(connection, request.scope.corpus_id, receipt.document_id)
    current = _store.head(connection, receipt.document_id)
    namespace = connection.execute(
        "SELECT policy_token FROM source_namespace WHERE corpus_id=? AND namespace=?",
        (request.scope.corpus_id, document["namespace"]),
    ).fetchone()
    external = request.payload.document
    if (
        document["namespace"] != external.source_namespace
        or document["external_id"] != external.external_id
        or document["synchronization_scope"] != external.synchronization_scope
        or document["owner_id"] != request.attribution.owner_id
        or current["state_version"] != receipt.processing.state_version
        or current["revision_id"] != receipt.revision_id
        or current["metadata_snapshot_id"] != receipt.metadata_snapshot_id
        or namespace is None or current["namespace_token"] != namespace[0]
    ):
        raise EvidenceServiceError("state_conflict")


def write(
    database: EvidenceDatabase, identity: LocalIdentity, request: WriteRequest,
    observed_at: datetime, *, participant: CoordinatedWriteParticipant | None = None,
    unit: UnitIdentity | None = None,
) -> WriteOutcome:
    request = validated(WriteRequest, request)
    if (participant is None) != (unit is None):
        raise EvidenceServiceError("invalid_request")
    if unit is not None:
        unit = validated(UnitIdentity, unit)
        if unit.corpus_id != request.scope.corpus_id:
            raise EvidenceServiceError("invalid_request")
    try:
        with writing(database, identity) as context:
            if not isinstance(request.payload, (PutDocument, RemoveDocument)):
                raise EvidenceServiceError("unsupported")
            authorize(context.connection, identity, request.scope, "write_documents")
            key = _receipts.canonical_key(request)
            admission = (
                participant.classify_unit(context, unit, key)
                if participant is not None and unit is not None else NewWork()
            )
            if not isinstance(admission, (NewWork, SettledSuccess, SettledFailure)):
                raise EvidenceServiceError("internal_error")
            if isinstance(admission, SettledFailure):
                return failed(request.request_id, admission.failure)
            stored = _receipts.lookup_key(context, key)
            key_id = admission.canonical_key_id if isinstance(admission, SettledSuccess) else (
                stored["key_id"] if stored is not None else None
            )
            if key_id is not None:
                replay = _receipts.replay_only(context, identity, request, key_id, observed_at)
                if isinstance(replay, _receipts.ReplayMissing):
                    raise EvidenceServiceError("state_conflict")
                if isinstance(replay, (_receipts.ReplayExpired, _receipts.ReplayConflict)):
                    return failed(request.request_id, replay.failure)
                outcome = replay.outcome(request.request_id)
                if isinstance(admission, SettledSuccess) or participant is None:
                    return outcome
                assert unit is not None
                participant.guard_new(context, unit, request)
                require_current_receipt(context, request, replay.receipt)
                committed = WriteCommit(key_id=key_id, outcome=outcome)
                participant.acknowledge_write(context, unit, committed)
                return outcome
            authorize_writer(
                context.connection, identity, request.scope, request.attribution,
                request.payload.document,
            )
            if participant is not None:
                assert unit is not None
                participant.guard_new(context, unit, request)
            at = _receipts.clock(context.connection, observed_at)
            doc, status = _writer.apply(context, request, at)
            receipt = _store.receipt(context.connection, doc)
            key_id = _receipts.save_document(context, request, receipt, status, at)
            outcome = WriteOutcome(request_id=request.request_id, status=status, receipt=receipt)
            if participant is not None:
                assert unit is not None
                committed = WriteCommit(key_id=key_id, outcome=outcome)
                participant.acknowledge_write(context, unit, committed)
            return outcome
    except EvidenceServiceError as error:
        return failed(request.request_id, error.failure)


def write_batch(
    database: EvidenceDatabase, identity: LocalIdentity, batch: WriteBatch,
    observed_at: datetime, *, participant: CoordinatedWriteParticipant,
    units: tuple[UnitIdentity, ...],
) -> BatchResult:
    batch = validated(WriteBatch, batch)
    units = tuple(validated(UnitIdentity, unit) for unit in units)
    if len(units) != len(batch.items) or len({unit.unit_id for unit in units}) != len(units):
        raise EvidenceServiceError("invalid_request")
    for ordinal, (unit, request) in enumerate(zip(units, batch.items, strict=True)):
        if (
            unit.ordinal != ordinal or unit.batch_id != batch.batch_id
            or unit.corpus_id != request.scope.corpus_id
        ):
            raise EvidenceServiceError("invalid_request")
    outcomes = tuple(
        write(database, identity, request, observed_at, participant=participant, unit=unit)
        for unit, request in zip(units, batch.items, strict=True)
    )
    successes = sum(outcome.receipt is not None for outcome in outcomes)
    result = BatchResult(
        contract_version="foundation/1", batch_id=batch.batch_id, outcomes=outcomes,
        status="complete" if successes == len(outcomes) else "partial" if successes else "failed",
    )
    result.validate_for(batch)
    return result
