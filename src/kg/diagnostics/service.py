from __future__ import annotations

from kg._execution_budget import DeadlineStop, PrivateResourceStop
from kg.diagnostics._collector import (
    _LOCK,
    Capture,
    CaptureUnavailable,
    Collector,
    unavailable,
)
from kg.diagnostics._targets import ReportAuthorizer
from kg.evidence._reads import check_token
from kg.evidence._values import validated
from kg.evidence.errors import EvidenceServiceError
from kg.models.execution import ExecutionReport, ReportAvailability, ReportHeaderPage
from kg.models.foundation import Scope


class DiagnosticService:
    """Constructed by the owning service with its trusted collector and authorizer."""

    def __init__(self, collector: Collector, authorizer: ReportAuthorizer) -> None:
        self._collector = collector
        self._authorizer = authorizer

    def _disclose(
        self,
        scope: Scope,
        capture: Capture,
        *,
        publish: bool = False,
    ) -> ExecutionReport | ReportAvailability:
        with _LOCK:
            if (
                capture.binding.identity != self._collector.identity
                or capture.binding.scope != scope
            ):
                return unavailable()
            group = capture.group
            if capture.active or (group.state == "provisional" and not publish):
                return unavailable()
            bindings, observers = tuple(group.bindings), tuple(group.observers)
            revision = group.revision
        try:
            # Never wait for SQLite while holding the collector lock: writers may
            # be appending observations while holding the canonical write lock.
            with self._authorizer.fence(bindings, observers) as fence, _LOCK:
                self._collector._expire()
                if (
                    self._collector._captures.get(capture.report_id) is not capture
                    or group.revision != revision
                ):
                    return unavailable()
                if publish:
                    group.release(fence)
                if group.state == "redacted":
                    return ReportAvailability(
                        state="redacted",
                        execution_id=capture.execution_id,
                        diagnostic_id=capture.diagnostic_id,
                        reason=group.reason,
                    )
                if group.state != "released" or capture.prepared is None:
                    return unavailable()
                return capture.prepared
        except (EvidenceServiceError, DeadlineStop, PrivateResourceStop):
            group.redact()
            return unavailable()
        except MemoryError:
            capture._capacity_failure()
            return unavailable()

    def _publish(
        self, capture: Capture | CaptureUnavailable
    ) -> ExecutionReport | ReportAvailability:
        if isinstance(capture, CaptureUnavailable):
            return capture.availability
        if capture.parent_execution_id is not None:
            return unavailable()
        try:
            return self._disclose(capture.binding.scope, capture, publish=True)
        except MemoryError:
            capture._capacity_failure()
            return unavailable()

    def report(self, scope: Scope, report_id: str) -> ExecutionReport | ReportAvailability:
        scope = validated(Scope, scope)
        report_id = check_token(report_id)
        for capture in self._collector.candidates():
            if capture.report_id == report_id or capture.diagnostic_id == report_id:
                return self._disclose(scope, capture)
        return unavailable()

    def recent(self, scope: Scope, *, limit: int = 20) -> ReportHeaderPage:
        return self._list(scope, limit=limit, request_id=None)

    def for_request(self, scope: Scope, request_id: str, *, limit: int = 20) -> ReportHeaderPage:
        return self._list(scope, limit=limit, request_id=check_token(request_id))

    def _list(self, scope: Scope, *, limit: int, request_id: str | None) -> ReportHeaderPage:
        scope = validated(Scope, scope)
        if type(limit) is not int or not 1 <= limit <= 32:
            raise EvidenceServiceError("invalid_request")
        headers = []
        captures: list[Capture] = []
        for capture in self._collector.candidates():
            if request_id is not None and capture.request_id != request_id:
                continue
            result = self._disclose(scope, capture)
            if isinstance(result, ExecutionReport):
                headers.append(self._collector.header(result))
                captures.append(capture)
                if len(headers) == limit:
                    break
        if not captures:
            return ReportHeaderPage(entries=())
        with _LOCK:
            revisions = tuple(capture.group.revision for capture in captures)
            bindings = tuple(binding for capture in captures for binding in capture.group.bindings)
            observers = tuple(
                observer for capture in captures for observer in capture.group.observers
            )
        try:
            with self._authorizer.fence(bindings, observers), _LOCK:
                self._collector._expire()
                if any(
                    capture.group.revision != revision
                    or capture.group.state != "released"
                    or self._collector._captures.get(capture.report_id) is not capture
                    for capture, revision in zip(captures, revisions, strict=True)
                ):
                    return ReportHeaderPage(entries=())
                return ReportHeaderPage(entries=tuple(headers))
        except (EvidenceServiceError, DeadlineStop, PrivateResourceStop):
            for capture in captures:
                capture.group.redact()
            return ReportHeaderPage(entries=())
