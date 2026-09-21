"""Private E1 instrumentation; public explained methods execute the same call once."""

from __future__ import annotations

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from functools import wraps
from typing import TYPE_CHECKING

from kg.evidence._values import validated
from kg.evidence.errors import EvidenceServiceError
from kg.models.evidence import (
    AnchorPage,
    DocumentView,
    EvidenceView,
    RevisionAnchorPage,
    RevisionPage,
    StatePage,
)
from kg.models.execution import (
    SUMMARY_OPTIONS,
    ExecutionReport,
    Explained,
    ExplainOptions,
    OperationName,
    ReportAvailability,
)
from kg.models.execution_events import EvidenceEvent
from kg.models.foundation import ContentResult, Scope
from kg.models.indexing import PassagePage
from kg.models.indexing_events import IndexPhase

if TYPE_CHECKING:
    from kg.diagnostics._collector import Capture, CaptureUnavailable


@dataclass
class Explanation:
    options: ExplainOptions
    report: ExecutionReport | ReportAvailability = ReportAvailability(state="not_collected")


_EXPLANATION: ContextVar[Explanation | None] = ContextVar("evidence_explanation", default=None)


def explained[**P, T](
    options: ExplainOptions,
    operation: Callable[P, T],
    *args: P.args,
    **kwargs: P.kwargs,
) -> Explained[T]:
    state = Explanation(validated(ExplainOptions, options))
    token = _EXPLANATION.set(state)
    try:
        outcome = operation(*args, **kwargs)
        return Explained(outcome=outcome, report=state.report)
    finally:
        _EXPLANATION.reset(token)


def options() -> ExplainOptions:
    state = _EXPLANATION.get()
    return state.options if state is not None else SUMMARY_OPTIONS


def deliver(report: ExecutionReport | ReportAvailability) -> None:
    state = _EXPLANATION.get()
    if state is not None:
        state.report = report


def retain_result(capture: Capture | CaptureUnavailable, result: object) -> None:
    from kg.diagnostics._targets import DocumentTarget, EvidenceTarget, ReportTargets

    if isinstance(result, DocumentView):
        capture.retain(
            ReportTargets(
                values=(
                    DocumentTarget(
                        document_id=result.document_id,
                        revision_id=result.revision_id,
                        state_version=result.state_version,
                    ),
                )
            )
        )
    elif isinstance(result, EvidenceView):
        targets = ReportTargets(
            values=(
                EvidenceTarget(
                    reference=result.reference,
                    citation=result.citation,
                ),
            )
        )
        capture.retain(targets)
        capture.quote(result.reference, result.quote, targets)
    elif isinstance(result, ContentResult):
        capture.retain(
            ReportTargets(
                values=(
                    DocumentTarget(
                        document_id=result.document_id,
                        revision_id=result.revision_id,
                    ),
                )
            )
        )
    elif isinstance(result, PassagePage):
        capture.retain(ReportTargets(values=(DocumentTarget(
            document_id=result.document_id, revision_id=result.revision_id,
            state_version=result.state_version,
        ),)))
        capture.append(IndexPhase(
            phase="passage", status="complete" if result.status == "complete" else "not_ready",
            passage_set_id=result.passage_set_id,
            reason=None if result.status == "complete" else "not_processed",
        ))
        for passage in result.entries:
            retain_result(capture, passage)
    elif isinstance(result, (AnchorPage, RevisionAnchorPage, StatePage, RevisionPage)):
        capture.retain(ReportTargets(values=(DocumentTarget(document_id=result.document_id),)))
        if isinstance(result, RevisionPage):
            for revision in result.entries:
                capture.retain(
                    ReportTargets(
                        values=(
                            DocumentTarget(
                                document_id=result.document_id,
                                revision_id=revision.revision_id,
                            ),
                        )
                    )
                )
        else:
            for entry in result.entries:
                retain_result(capture, entry)


def reported_read[**P, T](
    operation: OperationName,
) -> Callable[[Callable[P, T]], Callable[P, T]]:
    def decorate(
        function: Callable[P, T],
    ) -> Callable[P, T]:
        @wraps(function)
        def execute(*args: P.args, **kwargs: P.kwargs) -> T:
            from kg.evidence._reads import EvidenceReads

            owner = args[0] if args else kwargs.get("self")
            scope_value = args[1] if len(args) > 1 else kwargs.get("scope")
            if not isinstance(owner, EvidenceReads) or not isinstance(scope_value, Scope):
                raise EvidenceServiceError("invalid_request")
            scope = validated(Scope, scope_value)
            capture = owner._collector.begin_capture(
                operation,
                scope,
                required="read",
                options=options(),
                observation_kind="current_inspection",
            )
            try:
                result = function(*args, **kwargs)
            except EvidenceServiceError as error:
                capture.group.redact(error.failure.code)
                capture.finish(
                    "failed",
                    reason=error.failure.code,
                    diagnostic_id=error.failure.diagnostic_id,
                )
                deliver(owner.diagnostics._publish(capture))
                raise
            except BaseException:
                capture.group.close()
                raise
            with capture.guard():
                retain_result(capture, result)
                capture.append(
                    EvidenceEvent(
                        phase="read",
                        decision="observed",
                        observation_kind="current_inspection",
                    )
                )
            capture.finish("succeeded")
            deliver(owner.diagnostics._publish(capture))
            return result

        return execute

    return decorate
