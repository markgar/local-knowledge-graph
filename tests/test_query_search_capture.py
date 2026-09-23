"""Transport checks, not acceptance of a search implementation."""

from support.evidence import environment

from kg.diagnostics._collector import Capture, Collector
from kg.diagnostics._targets import DocumentTarget, ReportTargets
from kg.models.execution import ExplainOptions
from kg.models.indexing_events import IndexPhase
from kg.query import _search_capture
from kg.query._meter import Frame
from kg.query._retention import Allocation, Registry


class Wire:
    def __init__(self):
        self.frames = []

    def rpc(self, frame):
        self.frames.append(frame)


def captures(tmp_path):
    env = environment(tmp_path / "capture.db")
    options = ExplainOptions(detail="detailed")
    worker = Collector("indexing", env.service.identity).begin_capture(
        "search", env.scope, required="read", options=options,
    )
    parent = Collector("query", env.service.identity).begin_capture(
        "execute", env.scope, required="read", options=options,
    )
    child = Collector("indexing", env.service.identity).begin_capture(
        "search", env.scope, required="read", options=options,
        group=parent.group, step_id="ranked",
    )
    return worker, parent, child


def test_actual_captured_facts_stay_provisional_and_parent_redaction_wins(tmp_path):
    worker, parent, child = captures(tmp_path)
    targets = ReportTargets(values=(DocumentTarget(document_id="document"),))
    worker.configure("configuration", targets)
    worker.append(IndexPhase(phase="lexical", status="complete"), targets)
    wire = Wire()
    _search_capture.transmit_capture(wire, worker)
    payload = "".join(frame.text for frame in wire.frames if frame.action == "chunk")
    assert len(payload.encode()) == wire.frames[0].n
    assert all(len(frame.model_dump_json().encode()) <= 65536 for frame in wire.frames)
    _search_capture.accept_capture(payload, child)
    assert isinstance(child, Capture)
    assert child.prepared.configuration_ids == ("configuration",)
    assert child.prepared.events[0].event == worker.events[0].event
    assert child.binding.targets == list(targets.values)
    assert child.parent_execution_id == parent.execution_id
    assert child.group.state == worker.group.state == "provisional"
    parent.group.redact()
    assert child.prepared is None
    assert child.events == []
    assert worker.group.state == "provisional"


def test_optional_capture_serialization_failure_is_explicit(tmp_path, monkeypatch):
    worker, _, _ = captures(tmp_path)
    wire = Wire()

    def unavailable(*args):
        raise MemoryError()

    monkeypatch.setattr(_search_capture, "encode", unavailable)
    _search_capture.transmit_capture(wire, worker)
    assert [frame.action for frame in wire.frames] == ["capture_drop"]
    assert worker.group.state == "unavailable"


def test_optional_supervisor_capture_failure_discards_group(tmp_path, monkeypatch):
    worker, parent, child = captures(tmp_path)
    wire = Wire()
    _search_capture.transmit_capture(wire, worker)
    payload = "".join(frame.text for frame in wire.frames if frame.action == "chunk")

    def unavailable(*args, **kwargs):
        raise MemoryError()

    monkeypatch.setattr(_search_capture.CapturedSearch, "model_validate_json", unavailable)
    _search_capture.accept_capture(payload, child)
    assert parent.group.state == "unavailable"
    assert child.prepared is None


def test_capture_failure_does_not_replace_ranked_business_output(tmp_path, monkeypatch):
    from kg.models.foundation import RankedResult

    worker, parent, child = captures(tmp_path)
    wire = Wire()
    _search_capture.transmit_capture(wire, worker)
    allocation = Allocation(Registry())
    transfer = _search_capture.SearchTransfer(allocation, child)

    def unavailable(*args, **kwargs):
        raise MemoryError()

    monkeypatch.setattr(_search_capture.CapturedSearch, "model_validate_json", unavailable)
    for frame in wire.frames:
        assert transfer.receive(frame).state == "ok"
    assert parent.group.state == "unavailable"
    result = RankedResult(kind="ranked", hits=())
    payload = result.model_dump_json()
    transfer.receive(Frame(action="payload", kind="ranked", n=len(payload.encode())))
    transfer.receive(Frame(action="chunk", text=payload))
    assert transfer.ranked == result
    assert transfer.pending is None
    allocation.close()


def test_diagnostic_drop_cannot_discard_partial_business_output(tmp_path):
    import pytest

    _, _, child = captures(tmp_path)
    allocation = Allocation(Registry())
    transfer = _search_capture.SearchTransfer(allocation, child)
    transfer.receive(Frame(action="payload", kind="ranked", n=50))
    with pytest.raises(ValueError, match="business output"):
        transfer.receive(Frame(action="capture_drop"))
    allocation.close()


def test_reclamation_preserves_pending_ranked_transfer(tmp_path):
    from kg.models.foundation import RankedResult

    _, parent, child = captures(tmp_path)
    released = []
    allocation = Allocation(Registry())
    transfer = _search_capture.SearchTransfer(allocation, child, lambda: released.append(True))
    result = RankedResult(kind="ranked", hits=())
    payload = result.model_dump_json()
    transfer.receive(Frame(action="payload", kind="ranked", n=len(payload.encode())))
    transfer.receive(Frame(action="capture_reclaimed"))
    transfer.receive(Frame(action="chunk", text=payload))
    assert released == [True]
    assert parent.group.state == "unavailable"
    assert transfer.ranked == result
    allocation.close()
