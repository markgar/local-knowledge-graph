"""One process-local collector and terminal parent/child disclosure decision."""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass, field
from functools import wraps
from threading import RLock
from typing import Concatenate, Literal
from uuid import uuid4

from pydantic import TypeAdapter

from kg.diagnostics._bounds import (
    DEPENDENCY_BYTES,
    MAX_REPORTS,
    PAYLOAD_BYTES,
    REPORT_BYTES,
    RETENTION_SECONDS,
    TOTAL_BYTES,
    bounded_size,
)
from kg.diagnostics._targets import AuthorizationBinding, EvidenceTarget, ReportTargets
from kg.evidence._read_context import ObserverReference, ReleaseFence
from kg.evidence._values import validated
from kg.evidence.errors import EvidenceServiceError
from kg.models.evidence import Grant, LocalIdentity
from kg.models.execution import (
    SUMMARY_OPTIONS,
    ExecutionEvent,
    ExecutionReport,
    ExplainOptions,
    OperationName,
    QuoteEvent,
    RecordedEvent,
    ReportAvailability,
    ReportHeader,
    ServiceName,
)
from kg.models.execution_events import (
    CommitEvent,
    CommitObservation,
    ObservationKind,
    SafeOutcome,
    SafeReason,
)
from kg.models.foundation import EvidenceRef, Scope, Token

LOGGER = logging.getLogger(__name__)
_LOCK = RLock()
_EVENT: TypeAdapter[ExecutionEvent] = TypeAdapter(ExecutionEvent)
_TOKEN = TypeAdapter(Token)
_OPERATION: TypeAdapter[OperationName] = TypeAdapter(OperationName)
_OBSERVATION: TypeAdapter[ObservationKind] = TypeAdapter(ObservationKind)
_NO_TARGETS = ReportTargets()
_UNAVAILABLE = ReportAvailability(state="unavailable")


def unavailable() -> ReportAvailability:
    return _UNAVAILABLE


def _admitted[**P](
    method: Callable[Concatenate[Capture, P], bool],
) -> Callable[Concatenate[Capture, P], bool]:
    @wraps(method)
    def invoke(capture: Capture, /, *args: P.args, **kwargs: P.kwargs) -> bool:
        try:
            return method(capture, *args, **kwargs)
        except MemoryError:
            capture._capacity_failure()
            return False

    return invoke


@contextmanager
def _guard(group: DisclosureGroup) -> Iterator[None]:
    """Protect only owner-side diagnostic construction, never business execution."""
    try:
        yield
    except MemoryError:
        LOGGER.warning("Execution report unavailable: capacity")
        group.discard()


class DisclosureGroup:
    """Single bounded supervisor-owned decision shared even after parent eviction."""

    def __init__(self) -> None:
        self.state: Literal["provisional", "released", "redacted", "unavailable"] = "provisional"
        self.reason: SafeReason | None = None
        self.bindings: list[AuthorizationBinding] = []
        self.observers: list[ObserverReference] = []
        self.members: list[Capture] = []
        self.dependency_bytes = 0
        self.revision = 0
        self._live = 0
        self._root_execution: str | None = None

    def release(self, fence: ReleaseFence) -> None:
        with _LOCK:
            fence.check_active()
            if self.observers and any(
                item.observer.session_id != fence.session_id for item in self.observers
            ):
                raise EvidenceServiceError("invalid_request")
            if self.state != "provisional":
                return
            if any(item.active for item in self.members):
                raise EvidenceServiceError("invalid_request")
            self.state = "released"

    def redact(self, reason: SafeReason = "unavailable") -> None:
        with _LOCK:
            self.state = "redacted"
            self.reason = reason
            for capture in self.members:
                capture.events.clear()
                capture.configuration_ids.clear()
                capture.prepared = None
            self.revision += 1

    def discard(self) -> None:
        with _LOCK:
            self.redact()
            self.state = "unavailable"

    def close(self) -> None:
        """Supervisor timeout/close/IPC loss never auto-publishes children."""
        with _LOCK:
            if self.state == "provisional":
                self.redact("closed")
            for capture in tuple(self.members):
                if capture.active:
                    capture.finish("failed", reason="closed")

    def _drop(self, capture: Capture) -> None:
        self._live -= 1
        # Keep bounded authorization bindings after a parent's report is evicted,
        # but never keep the parent's event payload or the capture itself.
        self.members.remove(capture)
        if self._live == 0:
            self.close()
            for reference in self.observers:
                reference.close()
            self.observers.clear()
            self.bindings.clear()


@dataclass
class CaptureUnavailable:
    group: DisclosureGroup
    availability: ReportAvailability = field(
        default_factory=lambda: ReportAvailability(state="not_collected"),
    )

    def guard(self) -> AbstractContextManager[None]:
        return _guard(self.group)

    def append(self, event: ExecutionEvent, targets: ReportTargets = _NO_TARGETS) -> bool:
        return False

    def retain(self, targets: ReportTargets) -> bool:
        return False

    def quote(self, reference: EvidenceRef, text: str, targets: ReportTargets) -> bool:
        return False

    def configure(self, configuration_id: str, targets: ReportTargets) -> bool:
        return False

    def finish(
        self,
        outcome: SafeOutcome,
        commit: CommitObservation = "not_attempted",
        *,
        reason: SafeReason | None = None,
        diagnostic_id: str | None = None,
    ) -> ReportAvailability:
        return self.availability


@dataclass(frozen=True)
class PreparedReport:
    """Non-disclosing handle: preparation is not release authorization."""

    report_id: str
    execution_id: str


@dataclass(eq=False)
class Capture:
    collector: Collector
    group: DisclosureGroup
    binding: AuthorizationBinding
    report_id: str
    execution_id: str
    operation: OperationName
    request_id: str | None
    options: ExplainOptions
    observation_kind: ObservationKind
    parent_execution_id: str | None = None
    step_id: str | None = None
    job_id: str | None = None
    unit_id: str | None = None
    events: list[RecordedEvent] = field(default_factory=list)
    configuration_ids: list[str] = field(default_factory=list)
    payload_bytes: int = 8192
    truncated: bool = False
    active: bool = True
    completed_at: float | None = None
    completion_order: int = 0
    prepared: ExecutionReport | None = None
    diagnostic_id: str | None = None

    def guard(self) -> AbstractContextManager[None]:
        return _guard(self.group)

    @_admitted
    def retain(self, targets: ReportTargets) -> bool:
        with _LOCK:
            if not self.active or self.group.state != "provisional":
                return False
            size = bounded_size(targets, DEPENDENCY_BYTES - self.group.dependency_bytes)
            if size + self.group.dependency_bytes > DEPENDENCY_BYTES:
                self.group.discard()
                return False
            try:
                targets = validated(ReportTargets, targets)
                for target in targets.values:
                    if target not in self.binding.targets:
                        if sum(len(item.targets) for item in self.group.bindings) >= 200:
                            self.group.discard()
                            return False
                        self.binding.targets.append(target)
                        self.group.dependency_bytes += bounded_size(target, DEPENDENCY_BYTES)
                        self.group.revision += 1
                return True
            except MemoryError:
                self._capacity_failure()
                return False

    @_admitted
    def append(self, event: ExecutionEvent, targets: ReportTargets = _NO_TARGETS) -> bool:
        with _LOCK:
            if not self.retain(targets):
                return False
            limit = 32 if self.options.detail == "summary" else 200
            if len(self.events) >= limit:
                self.truncated = True
                return False
            if self.options.detail == "summary" and (
                isinstance(event, QuoteEvent)
                or event.kind == "indexing.candidate"
                or bool(getattr(event, "selected_ids", ()))
            ):
                return False
            if isinstance(event, QuoteEvent) and not self.options.include_quotes:
                return False
            size = bounded_size(event, PAYLOAD_BYTES - self.payload_bytes) + 64
            if size + self.payload_bytes > PAYLOAD_BYTES:
                self.truncated = True
                return False
            if isinstance(event, QuoteEvent) and not any(
                isinstance(target, EvidenceTarget) and target.reference == event.reference
                for target in self.binding.targets
            ):
                self.group.discard()
                return False
            try:
                event = _EVENT.validate_python(event.model_dump(), strict=True)
                self.events.append(RecordedEvent(sequence=len(self.events), event=event))
                self.payload_bytes += size
                return True
            except MemoryError:
                self._capacity_failure()
                return False

    @_admitted
    def quote(self, reference: EvidenceRef, text: str, targets: ReportTargets) -> bool:
        # Do not slice, encode or validate a potentially huge source before admission.
        with _LOCK:
            if not self.options.include_quotes or self.options.detail != "detailed":
                return False
            if not self.retain(targets):
                return False
            if (
                len(text) > 8192
                or len(self.events) >= 200
                or bounded_size(text, PAYLOAD_BYTES)
                + bounded_size(reference, PAYLOAD_BYTES)
                + self.payload_bytes
                + 256
                > PAYLOAD_BYTES
            ):
                self.truncated = True
                return False
            return self.append(QuoteEvent(reference=reference, quote=text), targets)

    @_admitted
    def configure(self, configuration_id: str, targets: ReportTargets) -> bool:
        with _LOCK:
            if not self.retain(targets):
                return False
            if configuration_id in self.configuration_ids:
                return True
            size = bounded_size(configuration_id, PAYLOAD_BYTES - self.payload_bytes) + 1
            if len(self.configuration_ids) >= 32 or size + self.payload_bytes > PAYLOAD_BYTES:
                self.truncated = True
                return False
            try:
                configuration_id = _TOKEN.validate_python(configuration_id, strict=True)
                self.configuration_ids.append(configuration_id)
                self.payload_bytes += size
                return True
            except MemoryError:
                self._capacity_failure()
                return False

    def _capacity_failure(self) -> None:
        LOGGER.warning("Execution report unavailable: capacity")
        self.group.discard()

    def finish(
        self,
        outcome: SafeOutcome,
        commit: CommitObservation = "not_attempted",
        *,
        reason: SafeReason | None = None,
        diagnostic_id: str | None = None,
    ) -> PreparedReport | ReportAvailability:
        with _LOCK:
            if not self.active:
                raise EvidenceServiceError("invalid_request")
            try:
                if commit != "not_attempted":
                    self.append(CommitEvent(observation=commit))
                if diagnostic_id is not None:
                    diagnostic_id = _TOKEN.validate_python(diagnostic_id, strict=True)
                self.diagnostic_id = diagnostic_id
                if self.group.state == "provisional":
                    self.prepared = ExecutionReport(
                        report_id=self.report_id,
                        execution_id=self.execution_id,
                        owning_service=self.collector.service,
                        operation=self.operation,
                        request_id=self.request_id,
                        diagnostic_id=diagnostic_id,
                        parent_execution_id=self.parent_execution_id,
                        step_id=self.step_id,
                        job_id=self.job_id,
                        unit_id=self.unit_id,
                        observation_kind=self.observation_kind,
                        capture_level=self.options.detail,
                        outcome=outcome,
                        reason=reason,
                        configuration_ids=tuple(self.configuration_ids),
                        events=tuple(self.events),
                        captured_event_count=len(self.events),
                        displayed_event_count=len(self.events),
                        truncated=self.truncated,
                    )
                self.active = False
                self.collector._complete(self)
                # Preparation is private; public callers still need the disclosure gate.
                return (
                    PreparedReport(self.report_id, self.execution_id)
                    if self.prepared is not None else unavailable()
                )
            except MemoryError:
                self.active = False
                self._capacity_failure()
                self.collector._remove(self)
            return unavailable()


class Collector:
    def __init__(self, service: ServiceName, identity: LocalIdentity) -> None:
        self.service = service
        self.identity = validated(LocalIdentity, identity)
        self._captures: dict[str, Capture] = {}
        self._order = 0
        self._not_collected = CaptureUnavailable(DisclosureGroup())
        self._not_collected.group.discard()

    def _expire(self) -> None:
        at = time.monotonic()
        for capture in tuple(self._captures.values()):
            if capture.completed_at is not None and at - capture.completed_at >= RETENTION_SECONDS:
                self._remove(capture)

    def _remove(self, capture: Capture) -> None:
        if self._captures.pop(capture.report_id, None) is not None:
            capture.events.clear()
            capture.configuration_ids.clear()
            capture.prepared = None
            capture.group._drop(capture)

    def _complete(self, capture: Capture) -> None:
        self._order += 1
        capture.completion_order = self._order
        capture.completed_at = time.monotonic()

    def begin_capture(
        self,
        operation: OperationName,
        scope: Scope,
        *,
        required: Grant,
        request_id: str | None = None,
        options: ExplainOptions = SUMMARY_OPTIONS,
        group: DisclosureGroup | None = None,
        observer: ObserverReference | None = None,
        observation_kind: ObservationKind = "captured_execution",
        step_id: str | None = None,
        job_id: str | None = None,
        unit_id: str | None = None,
    ) -> Capture | CaptureUnavailable:
        try:
            group = group if group is not None else DisclosureGroup()
            return self._begin_capture(
                operation,
                scope,
                required=required,
                request_id=request_id,
                options=options,
                group=group,
                observer=observer,
                observation_kind=observation_kind,
                step_id=step_id,
                job_id=job_id,
                unit_id=unit_id,
            )
        except MemoryError:
            LOGGER.warning("Execution report not collected: capacity")
            if group is None:
                return self._not_collected
            group.discard()
            with _LOCK:
                for member in tuple(group.members):
                    if member.collector._captures.get(member.report_id) is not member:
                        group._drop(member)
            return self._not_collected

    def _begin_capture(
        self,
        operation: OperationName,
        scope: Scope,
        *,
        required: Grant,
        request_id: str | None = None,
        options: ExplainOptions = SUMMARY_OPTIONS,
        group: DisclosureGroup | None = None,
        observer: ObserverReference | None = None,
        observation_kind: ObservationKind = "captured_execution",
        step_id: str | None = None,
        job_id: str | None = None,
        unit_id: str | None = None,
    ) -> Capture | CaptureUnavailable:
        with _LOCK:
            group = group if group is not None else DisclosureGroup()
            size = bounded_size(scope, DEPENDENCY_BYTES) + 1024
            header_size = 8192 + bounded_size(
                (request_id, step_id, job_id, unit_id),
                PAYLOAD_BYTES,
            )
            if (
                group.state != "provisional"
                or len(group.bindings) >= MAX_REPORTS
                or size + group.dependency_bytes > DEPENDENCY_BYTES
                or header_size > PAYLOAD_BYTES
                or (
                    group.bindings
                    and any(
                        item.identity != self.identity or item.scope != scope
                        for item in group.bindings
                    )
                )
            ):
                group.discard()
                return CaptureUnavailable(group)
            self._expire()
            while len(self._captures) >= min(MAX_REPORTS, TOTAL_BYTES // REPORT_BYTES):
                completed = [item for item in self._captures.values() if not item.active]
                if not completed:
                    group.discard()
                    return CaptureUnavailable(group)
                self._remove(
                    min(completed, key=lambda item: (item.completion_order, item.report_id))
                )
            scope = validated(Scope, scope)
            options = validated(ExplainOptions, options)
            operation = _OPERATION.validate_python(operation, strict=True)
            observation_kind = _OBSERVATION.validate_python(observation_kind, strict=True)
            for value in (request_id, step_id, job_id, unit_id):
                if value is not None:
                    _TOKEN.validate_python(value, strict=True)
            binding = AuthorizationBinding(self.identity, scope, required, [])
            capture = Capture(
                self,
                group,
                binding,
                str(uuid4()),
                str(uuid4()),
                operation,
                request_id,
                options,
                observation_kind,
                parent_execution_id=group._root_execution,
                step_id=step_id,
                job_id=job_id,
                unit_id=unit_id,
                payload_bytes=header_size,
            )
            group._root_execution = group._root_execution or capture.execution_id
            group.bindings.append(binding)
            group.dependency_bytes += size
            group.revision += 1
            group.members.append(capture)
            group._live += 1
            if observer is not None:
                reference = observer.observer.retain()
                try:
                    group.observers.append(reference)
                except MemoryError:
                    reference.close()
                    raise
            self._captures[capture.report_id] = capture
            return capture

    def candidates(self) -> tuple[Capture, ...]:
        with _LOCK:
            self._expire()
            return tuple(
                sorted(
                    (item for item in self._captures.values() if not item.active),
                    key=lambda item: (item.completion_order, item.report_id),
                    reverse=True,
                )
            )

    def header(self, report: ExecutionReport) -> ReportHeader:
        return ReportHeader(**{key: getattr(report, key) for key in ReportHeader.model_fields})
