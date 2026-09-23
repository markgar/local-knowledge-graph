"""Scoped durable control-plane calls, sharing canonical budgets and diagnostics."""

import sqlite3
import time
from collections.abc import Callable
from datetime import datetime

from kg._execution_budget import (
    Deadline,
    DeadlineStop,
    LocalExecutionMeter,
    PrivateBudget,
    PrivateResourceStop,
)
from kg.diagnostics import DiagnosticService
from kg.diagnostics._collector import Collector
from kg.diagnostics._targets import (
    ProcessingSelectionTarget,
    ReportTargets,
)
from kg.evidence import _reporting as reporting
from kg.evidence._read_context import observe, read_context, release_fence
from kg.evidence._transactions import CanonicalWriteContext, writing
from kg.evidence._values import now, validated
from kg.evidence.database import EvidenceDatabase
from kg.evidence.errors import EvidenceServiceError
from kg.models.evidence import LocalIdentity
from kg.models.execution import SUMMARY_OPTIONS, Explained, ExplainOptions, OperationName
from kg.models.processing import (
    Claim,
    ClaimResult,
    FailRequest,
    HeartbeatRequest,
    JobPage,
    JobRequest,
    JobsRequest,
    JobView,
    ProcessingCapabilities,
    RecoveryResult,
    RetryReceipt,
    RetryRequest,
    ScheduleReceipt,
    ScheduleRequest,
    WorkerRequest,
)
from kg.models.processing_events import ProcessingDecision
from kg.processing import _authorization as auth
from kg.processing import _controls, _core
from kg.processing._diagnostics import ProcessingReportAuthorizer


def _validate[R: WorkerRequest](request: R, kind: type[R]) -> R:
    request = validated(kind, request)
    if len(request.model_dump_json().encode()) > 16_384:
        raise EvidenceServiceError("invalid_request")
    return request


def _selection(request: WorkerRequest) -> ProcessingSelectionTarget:
    return ProcessingSelectionTarget(**request.selection.model_dump())


class ProcessingService:
    def __init__(self, database: EvidenceDatabase, identity: LocalIdentity) -> None:
        self.database = database
        self.identity = validated(LocalIdentity, identity)
        self._clock: Callable[[], datetime] = now
        self._collector = Collector("processing", self.identity)
        self._authorizer = ProcessingReportAuthorizer(database)
        self.diagnostics = DiagnosticService(self._collector, self._authorizer)

    def capabilities(self) -> ProcessingCapabilities:
        return ProcessingCapabilities()

    def _write[R: WorkerRequest, T](
        self,
        operation: OperationName,
        request: R,
        action: Callable[
            [sqlite3.Connection, LocalIdentity, R, datetime, _core.CaptureType],
            T | EvidenceServiceError,
        ],
        *,
        budget: PrivateBudget | None = None,
    ) -> T:
        capture = self._collector.begin_capture(
            operation,
            request.scope,
            required="read",
            request_id=request.request_id,
            options=reporting.options(),
        )
        with capture.guard():
            capture.retain(ReportTargets(values=(_selection(request),)))
        context: CanonicalWriteContext | None = None
        try:
            try:
                with writing(
                    self.database,
                    self.identity,
                    budget=budget or PrivateBudget(Deadline(time.monotonic() + 5)),
                ) as context:
                    result = action(
                        context.connection, self.identity, request, self._clock(), capture
                    )
            except (DeadlineStop, PrivateResourceStop):
                raise EvidenceServiceError("budget_exceeded") from None
            if isinstance(result, EvidenceServiceError):
                raise result
        except EvidenceServiceError as error:
            if error.failure.code in {"forbidden", "not_found", "state_changed"}:
                capture.group.redact(error.failure.code)
            capture.finish(
                "failed",
                context.commit_outcome if context is not None else "not_attempted",
                reason=error.failure.code,
                diagnostic_id=error.failure.diagnostic_id,
            )
            reporting.deliver(self.diagnostics._publish(capture))
            raise
        except BaseException:
            capture.group.close()
            raise
        capture.finish(
            "succeeded",
            context.commit_outcome,
            diagnostic_id=result.diagnostic_id
            if operation == "fail" and isinstance(result, JobView)
            else None,
        )
        reporting.deliver(self.diagnostics._publish(capture))
        return result

    def schedule(self, request: ScheduleRequest) -> ScheduleReceipt:
        return self._write("schedule", _validate(request, ScheduleRequest), _core.schedule)

    def claim(self, request: WorkerRequest) -> ClaimResult:
        return self._write("claim", _validate(request, WorkerRequest), _core.claim)

    def heartbeat(self, request: HeartbeatRequest) -> Claim:
        return self._write("heartbeat", _validate(request, HeartbeatRequest), _core.heartbeat)

    def fail(self, request: FailRequest) -> JobView:
        return self._write("fail", _validate(request, FailRequest), _controls.fail)

    def retry(self, request: RetryRequest) -> RetryReceipt:
        return self._write("retry", _validate(request, RetryRequest), _controls.retry)

    def recover(self, request: JobsRequest) -> RecoveryResult:
        return self._write("recover", _validate(request, JobsRequest), _controls.recover)

    def _read[R: WorkerRequest, T](
        self,
        operation: OperationName,
        request: R,
        action: Callable[[sqlite3.Connection, R, _core.CaptureType], T],
    ) -> T:
        budget = PrivateBudget(Deadline(time.monotonic() + 5))
        try:
            with observe(
                self.database,
                self.identity,
                request.scope,
                budget.deadline,
                budget,
            ) as observer:
                reference = observer.retain()
                try:
                    capture = self._collector.begin_capture(
                        operation,
                        request.scope,
                        required="read",
                        request_id=request.request_id,
                        options=reporting.options(),
                        observation_kind="current_inspection",
                        observer=reference,
                    )
                    with capture.guard():
                        capture.retain(ReportTargets(values=(_selection(request),)))
                    try:
                        meter = LocalExecutionMeter(
                            budget,
                            max_operations=1,
                            max_items=1,
                        ).begin_step(operation)
                        with read_context(
                            self.database,
                            self.identity,
                            request.scope,
                            observer.session_id,
                            budget.deadline,
                            meter,
                        ) as context:
                            auth.selection(
                                context.connection,
                                self.identity,
                                request.scope,
                                request.selection,
                            )
                            result = action(context.connection, request, capture)
                        with release_fence(
                            observer,
                            self.identity,
                            request.scope,
                            budget.deadline,
                        ):
                            capture.finish("succeeded")
                        reporting.deliver(self.diagnostics._publish(capture))
                        return result
                    except EvidenceServiceError as error:
                        capture.group.redact(error.failure.code)
                        capture.finish(
                            "failed",
                            reason=error.failure.code,
                            diagnostic_id=error.failure.diagnostic_id,
                        )
                        reporting.deliver(self.diagnostics._publish(capture))
                        raise
                    except BaseException:
                        capture.group.close()
                        raise
                finally:
                    reference.close()
        except (DeadlineStop, PrivateResourceStop):
            raise EvidenceServiceError("budget_exceeded") from None

    @staticmethod
    def _job(
        connection: sqlite3.Connection,
        request: JobRequest,
        capture: _core.CaptureType,
    ) -> JobView:
        row = auth.job(connection, request.scope, request.selection, request.job_id)
        result = _core.view(connection, row)
        auth.document(connection, request.scope, request.selection, result.target, current=False)
        _core.retain(capture, result.job_id)
        with capture.guard():
            capture.append(
                ProcessingDecision(
                    kind="processing.status_observation",
                    decision=result.status,
                    observation_kind="current_inspection",
                    job_id=result.job_id,
                )
            )
        return result

    @staticmethod
    def _jobs(
        connection: sqlite3.Connection,
        request: JobsRequest,
        capture: _core.CaptureType,
    ) -> JobPage:
        s = request.selection
        rows = connection.execute(
            "SELECT * FROM processing_job WHERE corpus_id=? AND namespace=? AND owner_id=? "
            "AND writer_id=? AND principal_id=? AND plan_id=? AND plan_version=? "
            "AND target_kind='document' AND creation_sequence>? "
            "ORDER BY creation_sequence LIMIT ?",
            (
                request.scope.corpus_id,
                s.namespace,
                s.owner_id,
                s.writer_id,
                request.scope.access.principal_id,
                s.plan_id,
                s.plan_version,
                request.after_sequence,
                request.limit + 1,
            ),
        ).fetchall()
        entries = []
        for row in rows[: request.limit]:
            entry = _core.view(connection, row)
            auth.document(connection, request.scope, s, entry.target, current=False)
            entries.append(entry)
            _core.retain(capture, entry.job_id)
            with capture.guard():
                capture.append(
                    ProcessingDecision(
                        kind="processing.status_observation",
                        decision=entry.status,
                        observation_kind="current_inspection",
                        job_id=entry.job_id,
                    )
                )
        return JobPage(
            entries=tuple(entries),
            next_after=entries[-1].creation_sequence if len(rows) > request.limit else None,
        )

    def job(self, request: JobRequest) -> JobView:
        return self._read("job", _validate(request, JobRequest), self._job)

    def jobs(self, request: JobsRequest) -> JobPage:
        return self._read("jobs", _validate(request, JobsRequest), self._jobs)

    def schedule_explained(
        self,
        request: ScheduleRequest,
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[ScheduleReceipt]:
        return reporting.explained(options, self.schedule, request)

    def claim_explained(
        self,
        request: WorkerRequest,
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[ClaimResult]:
        return reporting.explained(options, self.claim, request)

    def heartbeat_explained(
        self,
        request: HeartbeatRequest,
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[Claim]:
        return reporting.explained(options, self.heartbeat, request)

    def job_explained(
        self,
        request: JobRequest,
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[JobView]:
        return reporting.explained(options, self.job, request)

    def fail_explained(
        self,
        request: FailRequest,
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[JobView]:
        return reporting.explained(options, self.fail, request)

    def retry_explained(
        self,
        request: RetryRequest,
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[RetryReceipt]:
        return reporting.explained(options, self.retry, request)

    def recover_explained(
        self,
        request: JobsRequest,
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[RecoveryResult]:
        return reporting.explained(options, self.recover, request)

    def jobs_explained(
        self,
        request: JobsRequest,
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[JobPage]:
        return reporting.explained(options, self.jobs, request)
