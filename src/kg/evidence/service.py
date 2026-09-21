from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from kg.evidence import _receipts, _store, _writer
from kg.evidence._authorization import authorize_writer
from kg.evidence._reads import EvidenceReads
from kg.evidence._transactions import writing
from kg.evidence._values import now, request_digest, validated
from kg.evidence.database import EvidenceDatabase
from kg.evidence.errors import EvidenceServiceError
from kg.models.evidence import EvidenceCapabilities, LocalIdentity
from kg.models.foundation import (
    BatchResult,
    PutDocument,
    RemoveDocument,
    WriteBatch,
    WriteOutcome,
    WriteRequest,
)


class EvidenceService(EvidenceReads):
    def __init__(self, database: EvidenceDatabase, identity: LocalIdentity) -> None:
        self.database = database
        self.identity = validated(LocalIdentity, identity)
        self._clock: Callable[[], datetime] = now

    def capabilities(self) -> EvidenceCapabilities:
        return EvidenceCapabilities()

    def write(self, request: WriteRequest) -> WriteOutcome:
        request = validated(WriteRequest, request)
        digest = request_digest(request)
        try:
            with writing(self.database, self.identity) as context:
                if not isinstance(request.payload, (PutDocument, RemoveDocument)):
                    raise EvidenceServiceError("unsupported")
                authorize_writer(
                    context.connection,
                    self.identity,
                    request.scope,
                    request.attribution,
                    request.payload.document,
                )
                at = _receipts.clock(context.connection, self._clock())
                replay = _receipts.replay(context.connection, self.identity, request, at, digest)
                if replay is not None:
                    return replay
                doc, status = _writer.apply(context.connection, request, at)
                receipt = _store.receipt(context.connection, doc)
                _receipts.save(context.connection, request, receipt, status, at, digest)
                return WriteOutcome(request_id=request.request_id, status=status, receipt=receipt)
        except EvidenceServiceError as error:
            code = error.failure.code
            return WriteOutcome(
                request_id=request.request_id,
                status=(
                    "conflict"
                    if code in {"state_conflict", "retry_conflict"}
                    else "failed"
                    if code == "internal_error"
                    else "rejected"
                ),
                error=error.failure,
            )

    def write_batch(self, batch: WriteBatch) -> BatchResult:
        batch = validated(WriteBatch, batch)
        outcomes = tuple(self.write(item) for item in batch.items)
        successes = sum(outcome.receipt is not None for outcome in outcomes)
        result = BatchResult(
            contract_version="foundation/1",
            batch_id=batch.batch_id,
            outcomes=outcomes,
            status="complete"
            if successes == len(outcomes)
            else "partial"
            if successes
            else "failed",
        )
        result.validate_for(batch)
        return result
