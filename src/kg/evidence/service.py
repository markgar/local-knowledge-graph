from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from datetime import datetime
from typing import TYPE_CHECKING

from kg.evidence import _dispatch
from kg.evidence import _reporting as reporting
from kg.evidence._explained_reads import ExplainedReads
from kg.evidence._values import now, validated
from kg.evidence.database import EvidenceDatabase
from kg.models.evidence import EvidenceCapabilities, LocalIdentity
from kg.models.execution import SUMMARY_OPTIONS, Explained, ExplainOptions
from kg.models.foundation import (
    BatchResult,
    DocumentReceipt,
    PutDocument,
    RemoveDocument,
    WriteBatch,
    WriteOutcome,
    WriteRequest,
)

if TYPE_CHECKING:
    from kg.diagnostics._collector import Capture, CaptureUnavailable

_BATCH_CAPTURE: ContextVar[
    tuple[EvidenceService, Capture | CaptureUnavailable] | None
] = ContextVar(
    "evidence_batch_capture", default=None,
)


class EvidenceService(ExplainedReads):
    def __init__(self, database: EvidenceDatabase, identity: LocalIdentity) -> None:
        from kg.diagnostics import DiagnosticService
        from kg.diagnostics._collector import Collector
        from kg.evidence._diagnostic_authorization import EvidenceReportAuthorizer

        self.database = database
        self.identity = validated(LocalIdentity, identity)
        self._clock: Callable[[], datetime] = now
        self._collector = Collector("evidence", self.identity)
        self.diagnostics = DiagnosticService(self._collector, EvidenceReportAuthorizer(database))

    def capabilities(self) -> EvidenceCapabilities:
        return EvidenceCapabilities()

    def write(self, request: WriteRequest) -> WriteOutcome:
        request = validated(WriteRequest, request)
        batch_capture = _BATCH_CAPTURE.get()
        if batch_capture is not None and batch_capture[0] is self:
            return self._run_write(request, batch_capture[1])
        capture = self._collector.begin_capture(
            "write",
            request.scope,
            required="write_documents",
            request_id=request.request_id,
            options=reporting.options(),
        )
        try:
            result = self._run_write(request, capture)
        except BaseException:
            capture.group.close()
            raise
        capture.finish(
            result.status,
            reason=result.error.code if result.error else None,
            diagnostic_id=result.error.diagnostic_id if result.error else None,
        )
        reporting.deliver(self.diagnostics._publish(capture))
        return result

    def _run_write(
        self, request: WriteRequest, capture: Capture | CaptureUnavailable
    ) -> WriteOutcome:
        from kg.diagnostics._targets import DocumentTarget, ReportTargets, WriterTarget

        if isinstance(request.payload, (PutDocument, RemoveDocument)):
            with capture.guard():
                capture.retain(
                    ReportTargets(
                        values=(
                            WriterTarget(
                                document=request.payload.document,
                                owner_id=request.attribution.owner_id,
                                writer_id=request.attribution.writer_id,
                            ),
                        )
                    )
                )
        result = _dispatch.write(
            self.database,
            self.identity,
            request,
            self._clock(),
            capture=capture,
        )
        if isinstance(result.receipt, DocumentReceipt):
            with capture.guard():
                capture.retain(
                    ReportTargets(
                        values=(
                            DocumentTarget(
                                document_id=result.receipt.document_id,
                                revision_id=result.receipt.revision_id,
                                state_version=result.receipt.processing.state_version,
                            ),
                        )
                    )
                )
        if result.error is not None and result.error.code in {
            "forbidden",
            "not_found",
            "state_changed",
        }:
            capture.group.redact(result.error.code)
        return result

    def write_batch(self, batch: WriteBatch) -> BatchResult:
        batch = validated(WriteBatch, batch)
        scope = batch.items[0].scope
        capture: Capture | CaptureUnavailable
        if any(item.scope != scope for item in batch.items):
            capture = self._collector._not_collected
        else:
            capture = self._collector.begin_capture(
                "write_batch",
                scope,
                required="write_documents",
                request_id=batch.batch_id,
                options=reporting.options(),
            )
        batch_token = _BATCH_CAPTURE.set((self, capture))
        try:
            outcomes = tuple(self.write(item) for item in batch.items)
        except BaseException:
            capture.group.close()
            raise
        finally:
            _BATCH_CAPTURE.reset(batch_token)
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
        capture.finish(result.status)
        reporting.deliver(self.diagnostics._publish(capture))
        return result

    def write_explained(
        self,
        request: WriteRequest,
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[WriteOutcome]:
        return reporting.explained(options, self.write, request)

    def write_batch_explained(
        self,
        batch: WriteBatch,
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[BatchResult]:
        return reporting.explained(options, self.write_batch, batch)
