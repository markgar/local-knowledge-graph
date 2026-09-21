from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from kg.evidence import _dispatch
from kg.evidence._reads import EvidenceReads
from kg.evidence._values import now, validated
from kg.evidence.database import EvidenceDatabase
from kg.models.evidence import EvidenceCapabilities, LocalIdentity
from kg.models.foundation import (
    BatchResult,
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
        return _dispatch.write(self.database, self.identity, request, self._clock())

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
