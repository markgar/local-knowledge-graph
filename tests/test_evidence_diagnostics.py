from __future__ import annotations

import sqlite3
from unittest.mock import patch

import pytest
from support.evidence import environment, put, receipt

from kg.diagnostics._collector import Collector
from kg.evidence import EvidenceService, EvidenceServiceError, _writer
from kg.evidence._sql import AccountedConnection
from kg.models.evidence import LocalIdentity
from kg.models.execution import ExecutionReport, ExplainOptions, ReportAvailability
from kg.models.execution_events import CommitEvent, EvidenceEvent
from kg.models.foundation import WriteBatch


def observed(report):
    assert isinstance(report, ExecutionReport), report
    return tuple(item.event for item in report.events)


def test_ordinary_discovery_headers_repeat_request_and_no_lookup_execution(tmp_path):
    env = environment(tmp_path / "evidence.db")
    request = put(env.scope)
    original = env.service.write(request)
    replay = env.service.write(request)
    assert original == replay
    with patch("kg.evidence._dispatch.write", side_effect=AssertionError("must not execute")):
        page = env.service.diagnostics.for_request(env.scope, request.request_id)
        assert len(page.entries) == 2
        assert len({header.execution_id for header in page.entries}) == 2
        assert all("events" not in header.model_dump() for header in page.entries)
        report = env.service.diagnostics.report(env.scope, page.entries[0].report_id)
        events = observed(report)
        assert EvidenceEvent(phase="replay", decision="replayed") in events
        assert not any(
            isinstance(event, EvidenceEvent) and event.phase == "mutation" for event in events
        )
        assert (
            CommitEvent(
                observation="confirmed_committed",
                observation_kind="retained_commit_fact",
            )
            in events
        )
        assert report.observation_kind == "captured_execution"


def test_explained_write_once_and_ordinary_shape_unchanged(tmp_path):
    env = environment(tmp_path / "evidence.db")
    with patch("kg.evidence._writer.apply", wraps=_writer.apply) as apply:
        result = env.service.write_explained(put(env.scope), ExplainOptions(detail="detailed"))
    assert apply.call_count == 1
    assert result.outcome.status == "applied"
    assert "report" not in result.outcome.model_dump()
    assert CommitEvent(observation="confirmed_committed") in observed(result.report)


def test_quote_opt_in_read_wrappers_single_read_and_current_inspection(tmp_path):
    env = environment(tmp_path / "evidence.db")
    secret = "SOURCE-CANARY-\u2603"
    saved = receipt(env.service.write(put(env.scope, text=secret)))
    page = env.service.anchors(env.scope, saved.document_id, saved.processing.state_version)
    citation = page.entries[0].citation
    with patch(
        "kg.evidence._reads.evidence_view",
        wraps=__import__("kg.evidence._reads", fromlist=["evidence_view"]).evidence_view,
    ) as read:
        default = env.service.citation_explained(env.scope, citation)
        assert read.call_count == 1
    assert default.outcome.quote == secret
    assert secret not in default.report.model_dump_json()
    detailed = env.service.citation_explained(
        env.scope,
        citation,
        ExplainOptions(detail="detailed", include_quotes=True),
    )
    assert detailed.outcome == default.outcome
    assert secret in detailed.report.model_dump_json()
    assert detailed.report.observation_kind == "current_inspection"
    assert env.service.diagnostics.recent(env.scope).entries[0].request_id is None


def test_full_read_surface_preserves_results(tmp_path):
    env = environment(tmp_path / "evidence.db")
    request = put(env.scope)
    saved = receipt(env.service.write(request))
    reference = (
        env.service.anchors(
            env.scope,
            saved.document_id,
            saved.processing.state_version,
        )
        .entries[0]
        .reference
    )
    calls = (
        ("current", (env.scope, request.payload.document), {}),
        ("document", (env.scope, saved.document_id), {}),
        ("state", (env.scope, saved.document_id, saved.processing.state_version), {}),
        ("content", (env.scope, saved.document_id, saved.revision_id), {}),
        ("history", (env.scope, saved.document_id), {"limit": 1}),
        ("revisions", (env.scope, saved.document_id), {"limit": 1}),
        ("evidence", (env.scope, reference), {}),
        ("anchors", (env.scope, saved.document_id, saved.processing.state_version), {"limit": 1}),
        ("revision_anchors", (env.scope, saved.document_id, saved.revision_id), {"limit": 1}),
    )
    for operation, args, kwargs in calls:
        plain = getattr(env.service, operation)(*args, **kwargs)
        wrapper = getattr(env.service, operation + "_explained")(*args, **kwargs)
        assert wrapper.outcome == plain
        assert isinstance(wrapper.report, ExecutionReport)
        assert wrapper.report.operation == operation
    assert (
        env.service.document(
            scope=env.scope,
            document_id=saved.document_id,
        )
        == env.service.document_explained(
            scope=env.scope,
            document_id=saved.document_id,
        ).outcome
    )


def test_batch_once_bounded_events_and_atomic_units(tmp_path):
    env = environment(tmp_path / "evidence.db")
    batch = WriteBatch(
        contract_version="foundation/1",
        batch_id="batch",
        items=tuple(put(env.scope, external=str(index)) for index in range(15)),
    )
    with patch("kg.evidence._writer.apply", wraps=_writer.apply) as apply:
        result = env.service.write_batch_explained(batch)
    assert apply.call_count == len(batch.items)
    assert result.outcome.status == "complete"
    assert isinstance(result.report, ExecutionReport)
    assert result.report.truncated
    assert result.report.captured_event_count == 32
    assert len(env.service.diagnostics.for_request(env.scope, "batch").entries) == 1


def test_mixed_scope_batch_allocation_failure_still_executes_every_unit(tmp_path, monkeypatch):
    from kg.diagnostics._collector import CaptureUnavailable, DisclosureGroup

    env = environment(tmp_path / "evidence.db")
    email_scope = env.scope.model_copy(update={
        "access": env.scope.access.model_copy(update={"namespaces": ("email",)}),
    })
    batch = WriteBatch(
        contract_version="foundation/1", batch_id="mixed",
        items=(
            put(env.scope, external="markdown"),
            put(email_scope, namespace="email", external="email"),
        ),
    )

    def fail(*args, **kwargs):
        raise MemoryError("MIXED-SCOPE-ALLOCATION-CANARY")

    monkeypatch.setattr(DisclosureGroup, "__init__", fail)
    monkeypatch.setattr(CaptureUnavailable, "__init__", fail)
    with patch("kg.evidence._writer.apply", wraps=_writer.apply) as apply:
        result = env.service.write_batch_explained(batch)
    assert apply.call_count == 2
    assert result.outcome.status == "complete"
    assert result.report == ReportAvailability(state="not_collected")
    assert len({item.receipt.document_id for item in result.outcome.outcomes}) == 2
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM write_response").fetchone()[0] == 2


def test_unknown_commit_is_not_rollback_or_original_trace(tmp_path, monkeypatch):
    env = environment(tmp_path / "evidence.db")
    commit = AccountedConnection.commit

    def lost_response(connection):
        commit(connection)
        raise sqlite3.OperationalError("RAW-ERROR-CANARY")

    monkeypatch.setattr(AccountedConnection, "commit", lost_response)
    result = env.service.write_explained(put(env.scope))
    assert result.outcome.error.code == "internal_error"
    events = observed(result.report)
    assert CommitEvent(observation="unknown") in events
    assert CommitEvent(observation="confirmed_rolled_back") not in events
    assert "RAW-ERROR-CANARY" not in result.model_dump_json()
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM document").fetchone()[0] == 1


def test_confirmed_rollback_and_diagnostic_correlation(tmp_path, monkeypatch):
    env = environment(tmp_path / "evidence.db")

    def fail(*args):
        raise EvidenceServiceError("state_conflict")

    monkeypatch.setattr(_writer, "apply", fail)
    result = env.service.write_explained(put(env.scope))
    assert CommitEvent(observation="confirmed_rolled_back") in observed(result.report)
    assert (
        env.service.diagnostics.report(
            env.scope,
            result.outcome.error.diagnostic_id,
        )
        == result.report
    )


def test_postcommit_retention_failure_does_not_corrupt_business_result(tmp_path, monkeypatch):
    env = environment(tmp_path / "evidence.db")

    def fail(*args):
        raise MemoryError("RETENTION-CANARY")

    monkeypatch.setattr(Collector, "_complete", fail)
    result = env.service.write_explained(put(env.scope))
    assert result.outcome.status == "applied"
    assert result.report == ReportAvailability(state="unavailable")
    assert env.service._collector.candidates() == ()
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM write_response").fetchone()[0] == 1


@pytest.mark.parametrize(
    "point",
    [
        "admission_size",
        "writer_size",
        "event_size",
        "receipt_size",
        "writer_construction",
        "receipt_construction",
        "event_construction",
        "commit_construction",
    ],
)
def test_diagnostic_allocation_failure_never_replaces_committed_success(
    tmp_path,
    monkeypatch,
    caplog,
    point,
):
    from kg.diagnostics import _collector, _targets
    from kg.evidence import _dispatch
    from kg.models.foundation import Scope

    env = environment(tmp_path / "evidence.db")
    original_size = _collector.bounded_size

    def fail(*args, **kwargs):
        raise MemoryError("PRIVATE-ALLOCATION-CANARY")

    size_types = {
        "admission_size": Scope,
        "writer_size": _targets.WriterTarget,
        "event_size": EvidenceEvent,
        "receipt_size": _targets.DocumentTarget,
    }
    if point in size_types:

        def measured(value, limit):
            if isinstance(value, size_types[point]):
                fail()
            return original_size(value, limit)

        monkeypatch.setattr(_collector, "bounded_size", measured)
    else:
        module, name = {
            "writer_construction": (_targets, "WriterTarget"),
            "receipt_construction": (_targets, "DocumentTarget"),
            "event_construction": (_dispatch, "EvidenceEvent"),
            "commit_construction": (_dispatch, "CommitEvent"),
        }[point]
        monkeypatch.setattr(module, name, fail)
    result = env.service.write_explained(put(env.scope))
    assert result.outcome.status == "applied"
    assert isinstance(result.report, ReportAvailability)
    assert result.report.state == ("not_collected" if point == "admission_size" else "unavailable")
    assert "PRIVATE-ALLOCATION-CANARY" not in caplog.text
    assert "capacity" in caplog.text
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM document").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM write_response").fetchone()[0] == 1


def test_read_target_construction_failure_preserves_ordinary_outcome(tmp_path, monkeypatch):
    from kg.diagnostics import _targets

    env = environment(tmp_path / "evidence.db")
    saved = receipt(env.service.write(put(env.scope)))
    ordinary = env.service.document(env.scope, saved.document_id)

    def fail(*args, **kwargs):
        raise MemoryError("READ-ALLOCATION-CANARY")

    monkeypatch.setattr(_targets, "DocumentTarget", fail)
    result = env.service.document_explained(env.scope, saved.document_id)
    assert result.outcome == ordinary
    assert result.report.state == "unavailable"


def test_business_allocation_failure_is_not_suppressed_by_diagnostic_guard(tmp_path, monkeypatch):
    env = environment(tmp_path / "evidence.db")

    def fail(*args):
        raise MemoryError("business allocation")

    monkeypatch.setattr(_writer, "apply", fail)
    with pytest.raises(MemoryError, match="business allocation"):
        env.service.write_explained(put(env.scope))
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM document").fetchone()[0] == 0
    assert all(not item.active for item in env.service._collector._captures.values())


def test_capture_and_lookup_no_canonical_mutations_or_receipt_clock(tmp_path, monkeypatch):
    env = environment(tmp_path / "evidence.db")
    saved = receipt(env.service.write(put(env.scope)))
    with env.database.connection() as observer:
        before_version = observer.execute("PRAGMA data_version").fetchone()[0]
        before_clock = observer.execute("SELECT watermark FROM receipt_clock").fetchone()[0]
        statements = []
        original = env.database._open

        def traced(**kwargs):
            connection = original(**kwargs)
            connection.set_trace_callback(statements.append)
            return connection

        monkeypatch.setattr(env.database, "_open", traced)
        result = env.service.document_explained(env.scope, saved.document_id)
        for _ in range(3):
            assert (
                env.service.diagnostics.report(env.scope, result.report.report_id) == result.report
            )
            env.service.diagnostics.recent(env.scope)
            env.service.diagnostics.for_request(env.scope, "missing")
        assert observer.execute("PRAGMA data_version").fetchone()[0] == before_version
        assert observer.execute("SELECT watermark FROM receipt_clock").fetchone()[0] == before_clock
        assert not any(
            sql.split()[0] in {"COMMIT", "INSERT", "UPDATE", "DELETE", "CREATE"}
            for sql in statements
        )
        assert len(env.service.diagnostics.recent(env.scope).entries) == 2


def test_foreign_scope_unknown_and_restart_indistinguishable(tmp_path):
    env = environment(tmp_path / "evidence.db")
    result = env.service.write_explained(put(env.scope))
    report_id = result.report.report_id
    foreign = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(update={"principal_id": "foreign"}),
        }
    )
    assert env.service.diagnostics.report(foreign, report_id) == ReportAvailability(
        state="unavailable"
    )
    assert env.service.diagnostics.report(env.scope, "unknown") == ReportAvailability(
        state="unavailable"
    )
    assert env.service.diagnostics.recent(foreign).entries == ()
    restarted = EvidenceService(env.database, LocalIdentity(principal_id="principal"))
    assert restarted.diagnostics.report(env.scope, report_id) == ReportAvailability(
        state="unavailable"
    )
    # A foreign lookup cannot redact the legitimate principal's report.
    assert env.service.diagnostics.report(env.scope, report_id) == result.report


@pytest.mark.parametrize("limit", [0, 33, -1, True, 1.0, "1"])
def test_header_list_limits(tmp_path, limit):
    env = environment(tmp_path / "evidence.db")
    with pytest.raises(EvidenceServiceError):
        env.service.diagnostics.recent(env.scope, limit=limit)
    with pytest.raises(EvidenceServiceError):
        env.service.diagnostics.for_request(env.scope, "request", limit=limit)
