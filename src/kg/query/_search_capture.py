"""Bounded transport of actual worker observations, never worker disclosure."""

from collections.abc import Callable

from kg.diagnostics._bounds import REPORT_BYTES
from kg.diagnostics._collector import Capture, CaptureUnavailable
from kg.diagnostics._targets import ReportTargets
from kg.models.execution import ExecutionReport
from kg.models.foundation import RankedResult, Value
from kg.query._meter import Frame, RemoteBudget, Reply
from kg.query._retention import CHUNK, SET_BYTES, Allocation, encode, size

# Covers admitted worker capture, serialization/chunks and supervisor decoding.
# The supervisor reserves this in the original scratch pool before enabling capture.
CAPTURE_SCRATCH = 8 * REPORT_BYTES


class CapturedSearch(Value):
    report: ExecutionReport
    targets: ReportTargets


def transmit_capture(budget: RemoteBudget, capture: Capture | CaptureUnavailable) -> None:
    payload: str | None = None
    with capture.guard():
        capture.finish("succeeded")
        if isinstance(capture, Capture) and capture.prepared is not None:
            value = CapturedSearch(
                report=capture.prepared,
                targets=ReportTargets(values=tuple(capture.binding.targets)),
            )
            if size(value) <= REPORT_BYTES:
                payload = encode(value)
            else:
                capture.group.discard()
    if payload is None:
        budget.rpc(Frame(action="capture_drop"))
        return
    with capture.guard():
        budget.rpc(Frame(action="payload", kind="capture", n=len(payload.encode())))
        for start in range(0, len(payload), CHUNK):
            budget.rpc(Frame(action="chunk", text=payload[start : start + CHUNK]))
    if capture.group.state != "provisional":
        budget.rpc(Frame(action="capture_drop"))


def accept_capture(payload: str, capture: Capture | CaptureUnavailable) -> None:
    with capture.guard():
        value = CapturedSearch.model_validate_json(payload)
        report = value.report
        if (
            report.owning_service != "indexing"
            or report.operation != "search"
            or report.outcome != "succeeded"
        ):
            raise ValueError("Invalid captured search")
        capture.retain(value.targets)
        for configuration in report.configuration_ids:
            capture.configure(configuration, ReportTargets())
        for recorded in report.events:
            capture.append(recorded.event)
        if isinstance(capture, Capture):
            capture.truncated |= report.truncated
        capture.finish("succeeded")


class SearchTransfer:
    """One bounded ranked output and one optional, independently admitted trace."""

    def __init__(
        self, allocation: Allocation, capture: Capture | CaptureUnavailable,
        drop_scratch: Callable[[], None] | None = None,
    ) -> None:
        self.allocation = allocation
        self.capture = capture
        self.drop_scratch = drop_scratch
        self.ranked: RankedResult | None = None
        self.pending: Frame | None = None
        self.received = 0
        self.chunks: list[str] = []
        self.capture_received = False

    def receive(self, frame: Frame) -> Reply:
        if frame.action in ("capture_drop", "capture_reclaimed"):
            if (
                frame.action == "capture_drop"
                and self.pending is not None and self.pending.kind != "capture"
            ):
                raise ValueError("Capture interrupted business output")
            self.capture.group.discard()
            if self.pending is None or self.pending.kind == "capture":
                self.pending = None
                self.chunks = []
            if self.drop_scratch is not None:
                self.drop_scratch()
        elif frame.action == "payload":
            if self.pending is not None:
                raise ValueError("Overlapping search payload")
            if frame.kind == "capture":
                if frame.n > REPORT_BYTES or self.capture_received:
                    raise ValueError("Invalid capture payload")
                self.capture_received = True
            elif frame.kind == "ranked":
                if frame.n > SET_BYTES or self.ranked is not None:
                    raise ValueError("Invalid ranked payload")
                self.allocation.reserve(frame.n * 3)
            else:
                raise ValueError("Unexpected search payload")
            self.pending, self.received, self.chunks = frame, 0, []
        elif frame.action == "chunk":
            if self.pending is None or not frame.text:
                raise ValueError("Unexpected search chunk")
            self.received += len(frame.text.encode())
            if self.received > self.pending.n:
                raise ValueError("Search payload overflow")
            if self.pending.kind == "capture":
                with self.capture.guard():
                    if self.capture.group.state == "provisional":
                        self.chunks.append(frame.text)
                        if self.received == self.pending.n:
                            accept_capture("".join(self.chunks), self.capture)
            else:
                self.chunks.append(frame.text)
                if self.received == self.pending.n:
                    self.ranked = RankedResult.model_validate_json("".join(self.chunks))
            if self.received == self.pending.n:
                self.pending, self.chunks = None, []
        else:
            raise ValueError("Unexpected search transfer frame")
        return Reply(state="ok")
