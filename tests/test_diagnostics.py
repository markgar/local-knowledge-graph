"""Shared lifetime/disclosure seam tests, not future K1/E3/Q1/E4 acceptance."""

from __future__ import annotations

import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from support.evidence import environment, put, receipt

from kg._execution_budget import Deadline, PrivateBudget
from kg.diagnostics import DiagnosticService, _collector
from kg.diagnostics._bounds import REPORT_BYTES, TOTAL_BYTES, bounded_size
from kg.diagnostics._collector import Capture, CaptureUnavailable, Collector
from kg.diagnostics._targets import DocumentTarget, EvidenceTarget, ReportTargets
from kg.evidence._diagnostic_authorization import EvidenceReportAuthorizer
from kg.evidence._read_context import observe
from kg.evidence.errors import EvidenceServiceError
from kg.models.execution import ExecutionReport, ExplainOptions, QuoteEvent
from kg.models.execution_events import EvidenceEvent
from kg.models.foundation import EvidenceRef


def begin(env, *, collector=None, group=None, options=None, observer=None):
    return (collector or env.service._collector).begin_capture(
        "document",
        env.scope,
        required="read",
        group=group,
        options=options or ExplainOptions(),
        observer=observer,
    )


def complete(env, capture):
    capture.finish("succeeded")
    return env.service.diagnostics._publish(capture)


def test_summary_and_detailed_event_counts(tmp_path):
    env = environment(tmp_path / "reports.db")
    for detail, limit in (("summary", 32), ("detailed", 200)):
        capture = begin(env, options=ExplainOptions(detail=detail))
        event = EvidenceEvent(phase="read", decision="observed")
        for _ in range(limit):
            assert capture.append(event)
        with patch.object(type(event), "model_dump", side_effect=AssertionError("copied overflow")):
            assert not capture.append(event)
        report = complete(env, capture)
        assert len(report.events) == limit
        assert report.captured_event_count == limit
        assert report.truncated
        assert len(report.model_dump_json().encode()) <= REPORT_BYTES


def test_active_reservations_and_deterministic_completed_eviction(tmp_path):
    env = environment(tmp_path / "reports.db")
    captures = [begin(env) for _ in range(TOTAL_BYTES // REPORT_BYTES)]
    assert all(isinstance(item, Capture) for item in captures)
    denied = begin(env)
    assert isinstance(denied, CaptureUnavailable)
    assert denied.availability.state == "not_collected"
    assert all(item.active for item in captures)
    first = captures[0]
    complete(env, first)
    new = begin(env)
    assert isinstance(new, Capture)
    assert first.report_id not in env.service._collector._captures
    assert all(item.report_id in env.service._collector._captures for item in captures[1:])
    assert env.service.diagnostics.report(env.scope, first.report_id).state == "unavailable"


def test_retention_clock_starts_at_completion_and_tie_breaks_by_completion(tmp_path, monkeypatch):
    env = environment(tmp_path / "reports.db")
    clock = SimpleNamespace(value=0)
    monkeypatch.setattr(_collector, "time", SimpleNamespace(monotonic=lambda: clock.value))
    first = begin(env)
    second = begin(env)
    clock.value = 1000
    second_report = complete(env, second)
    first_report = complete(env, first)
    assert [h.report_id for h in env.service.diagnostics.recent(env.scope).entries] == [
        first_report.report_id,
        second_report.report_id,
    ]
    clock.value = 1299.999
    assert env.service.diagnostics.report(env.scope, first_report.report_id) == first_report
    clock.value = 1300
    assert env.service.diagnostics.report(env.scope, first_report.report_id).state == "unavailable"
    assert env.service._collector._captures == {}
    assert first.group.bindings == []


def test_size_before_copy_and_quote_construction(tmp_path):
    env = environment(tmp_path / "reports.db")
    capture = begin(env, options=ExplainOptions(detail="detailed", include_quotes=True))
    ref = EvidenceRef(
        corpus_id="work",
        source_namespace="markdown",
        document_id="doc",
        revision_id="rev",
        anchor_id="anchor",
    )
    huge = QuoteEvent.model_construct(reference=ref, quote="X" * REPORT_BYTES)
    with patch.object(QuoteEvent, "model_dump", side_effect=AssertionError("oversized copied")):
        assert not capture.append(huge)
    with patch("kg.diagnostics._collector.QuoteEvent", side_effect=AssertionError("quote copied")):
        assert not capture.quote(ref, "X" * REPORT_BYTES, ReportTargets())
    assert capture.events == []
    assert capture.truncated
    assert bounded_size(huge, REPORT_BYTES) > REPORT_BYTES


def test_dependency_overflow_withholds_entire_report_not_just_last_event(tmp_path):
    env = environment(tmp_path / "reports.db")
    capture = begin(env)
    capture.append(EvidenceEvent(phase="read", decision="observed"))
    oversized = ReportTargets.model_construct(
        values=tuple(DocumentTarget(document_id=str(index)) for index in range(201))
    )
    with patch.object(ReportTargets, "model_dump", side_effect=AssertionError("targets copied")):
        assert not capture.retain(oversized)
    assert complete(env, capture).state == "unavailable"
    assert capture.events == []


class TargetGate(EvidenceReportAuthorizer):
    denied: str | None = None

    def authorize_target(self, connection, binding, target):
        if isinstance(target, DocumentTarget) and target.document_id == self.denied:
            raise EvidenceServiceError("forbidden")
        super().authorize_target(connection, binding, target)


def family(env):
    saved = receipt(env.service.write(put(env.scope)))
    parent_collector = Collector("query", env.service.identity)
    child_collector = Collector("indexing", env.service.identity)
    gate = TargetGate(env.database)
    parent_facade = DiagnosticService(parent_collector, gate)
    child_facade = DiagnosticService(child_collector, gate)
    parent = begin(env, collector=parent_collector)
    parent.retain(ReportTargets(values=(DocumentTarget(document_id=saved.document_id),)))
    child = begin(env, collector=child_collector, group=parent.group)
    parent.append(EvidenceEvent(phase="read", decision="observed"))
    child.append(EvidenceEvent(phase="read", decision="observed"))
    child.finish("succeeded")
    return parent, child, parent_facade, child_facade, gate, saved


def test_child_provisional_and_one_parent_release(tmp_path):
    env = environment(tmp_path / "reports.db")
    parent, child, parents, children, _, _ = family(env)
    assert children.report(env.scope, child.report_id).state == "unavailable"
    assert children.recent(env.scope).entries == ()
    assert children._publish(child).state == "unavailable"
    assert parent.group.state == "provisional"
    parent.finish("succeeded")
    assert isinstance(parents._publish(parent), ExecutionReport)
    assert isinstance(children.report(env.scope, child.report_id), ExecutionReport)
    assert child.prepared.parent_execution_id == parent.execution_id


@pytest.mark.parametrize("failure", ["no_data", "close", "ipc_death"])
def test_terminal_no_data_redaction_after_rights_return_and_parent_eviction(tmp_path, failure):
    env = environment(tmp_path / "reports.db")
    parent, child, parents, children, gate, saved = family(env)
    if failure == "no_data":
        parent.group.redact("forbidden")
        parent.finish("failed", reason="forbidden")
    else:
        parent.group.close()
    assert parents._publish(parent).state == "redacted"
    gate.denied = saved.document_id
    assert children.report(env.scope, child.report_id).state == "unavailable"
    gate.denied = None
    parent.collector._remove(parent)
    result = children.report(env.scope, child.report_id)
    assert result.state == "redacted"
    assert "events" not in result.model_dump() and "captured_event_count" not in result.model_dump()
    assert child.events == [] and child.prepared is None
    assert children.recent(env.scope).entries == ()


def test_later_denial_redacts_headers_body_and_parent_dependencies_survive_eviction(tmp_path):
    env = environment(tmp_path / "reports.db")
    parent, child, parents, children, gate, saved = family(env)
    parent.finish("succeeded")
    parents._publish(parent)
    parent.collector._remove(parent)
    assert len(child.group.bindings) == 2
    assert isinstance(children.report(env.scope, child.report_id), ExecutionReport)
    gate.denied = saved.document_id
    assert children.recent(env.scope).entries == ()
    assert children.report(env.scope, child.report_id).state == "unavailable"
    gate.denied = None
    assert children.report(env.scope, child.report_id).state == "redacted"
    # A NEW authorized inspection cannot unlock the old execution.
    assert isinstance(
        env.service.document_explained(env.scope, saved.document_id).report, ExecutionReport
    )
    assert children.report(env.scope, child.report_id).state == "redacted"
    child.collector._remove(child)
    assert child.group.bindings == []
    assert child.group.members == []


def test_no_parent_admission_means_no_independent_child_discovery(tmp_path):
    env = environment(tmp_path / "reports.db")
    active = [begin(env) for _ in range(32)]
    missing_parent = begin(env)
    other = Collector("indexing", env.service.identity)
    child = begin(env, collector=other, group=missing_parent.group)
    assert isinstance(child, CaptureUnavailable)
    assert other.candidates() == ()
    assert len(active) == 32


def test_group_bookkeeping_does_not_grow_when_members_evicted(tmp_path):
    env = environment(tmp_path / "reports.db")
    root = begin(env)
    other = Collector("indexing", env.service.identity)
    for _ in range(31):
        child = begin(env, collector=other, group=root.group)
        assert isinstance(child, Capture)
        child.finish("succeeded")
        other._remove(child)
    assert len(root.group.bindings) == 32
    assert len(root.group.members) == 1
    assert isinstance(begin(env, collector=other, group=root.group), CaptureUnavailable)
    assert root.group.state == "unavailable"


def test_original_observer_invalidates_group_without_report_writes(tmp_path):
    env = environment(tmp_path / "reports.db")
    saved = receipt(env.service.write(put(env.scope)))
    deadline = Deadline(time.monotonic() + 30)
    budget = PrivateBudget(deadline)
    with observe(env.database, env.service.identity, env.scope, deadline, budget) as observer:
        reference = observer.retain()
        capture = begin(env, observer=reference)
        reference.close()
        capture.finish("succeeded")
        report = env.service.diagnostics._publish(capture)
        assert isinstance(report, ExecutionReport)
        assert not observer.changed()
    assert observer._references == 1
    env.service.write(put(env.scope, external="new"))
    assert env.service.diagnostics.report(env.scope, report.report_id).state == "unavailable"
    assert capture.prepared is None
    env.service._collector._remove(capture)
    assert observer._references == 0
    assert saved.document_id


def test_all_retained_evidence_targets_gate_header_and_body(tmp_path):
    env = environment(tmp_path / "reports.db")
    saved = receipt(env.service.write(put(env.scope)))
    view = env.service.anchors(
        env.scope,
        saved.document_id,
        saved.processing.state_version,
    ).entries[0]
    capture = begin(env)
    capture.retain(
        ReportTargets(
            values=(
                EvidenceTarget(
                    reference=view.reference,
                    citation=view.citation,
                ),
            )
        )
    )
    capture.finish("succeeded")
    report = env.service.diagnostics._publish(capture)
    assert isinstance(report, ExecutionReport)
    with env.database.transaction() as connection:
        connection.execute(
            "DELETE FROM policy_grant WHERE corpus_id=? AND namespace='markdown' "
            "AND grant_name='read'",
            (env.scope.corpus_id,),
        )
    assert not any(
        header.report_id == report.report_id
        for header in env.service.diagnostics.recent(env.scope).entries
    )
    assert env.service.diagnostics.report(env.scope, report.report_id).state == "unavailable"
