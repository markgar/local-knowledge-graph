"""Controlled participants test shared seams, not E4's eventual SQL acceptance."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from support.evidence import environment, put, receipt

from kg.evidence import EvidenceServiceError, _dispatch, _receipts
from kg.evidence._coordination import NewWork, SettledFailure, SettledSuccess, UnitIdentity
from kg.evidence._sql import AccountedConnection
from kg.evidence._transactions import writing
from kg.models.foundation import Failure, WriteBatch

AT = datetime(2026, 1, 1, tzinfo=UTC)


class Participant:
    def __init__(self, *, admission=None, fail=False):
        self.admission = admission or NewWork()
        self.fail = fail
        self.events = []
        self.context = None
        self.committed = None
        self.sql = []

    def classify_unit(self, context, unit, key):
        self.context = context
        context.connection.set_trace_callback(self.sql.append)
        self.events.append("classify")
        return self.admission

    def guard_new(self, context, unit, request):
        assert self.context is context
        self.events.append("guard")

    def acknowledge_write(self, context, unit, committed):
        assert self.context is context
        assert context.connection.execute(
            "SELECT count(*) FROM write_key WHERE key_id=?", (committed.key_id,),
        ).fetchone()[0] == 1
        self.events.append("ack")
        self.committed = committed
        if self.fail:
            raise EvidenceServiceError("state_conflict")
        self.sql.clear()


def coordinated(env, request, participant, *, at=AT):
    return _dispatch.write(
        env.database, env.service.identity, request, at,
        participant=participant,
        unit=UnitIdentity(
            corpus_id=env.scope.corpus_id, batch_id="batch", unit_id="unit", ordinal=0,
        ),
    )


def test_one_owner_transaction_ack_then_immediate_commit_and_lifetime(tmp_path) -> None:
    env = environment(tmp_path / "coord.db")
    participant = Participant()
    saved = receipt(coordinated(env, put(env.scope), participant))
    assert participant.events == ["classify", "guard", "ack"]
    assert participant.sql == ["COMMIT"]
    assert participant.context.commit_outcome == "confirmed_committed"
    assert participant.committed.outcome.receipt == saved
    with pytest.raises(EvidenceServiceError):
        participant.context.connection.execute("SELECT 1")
    with env.database.connection() as connection:
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT count(*) FROM state_intent").fetchone()[0] == 1


def test_ack_failure_rolls_back_state_intent_epoch_receipt_clock(tmp_path) -> None:
    env = environment(tmp_path / "rollback.db")
    participant = Participant(fail=True)
    result = coordinated(env, put(env.scope), participant)
    assert result.error.code == "state_conflict"
    assert participant.context.commit_outcome == "confirmed_rolled_back"
    with env.database.connection() as connection:
        for table in (
            "document", "revision", "document_state", "write_key", "write_response",
            "write_provenance", "state_intent", "sync_scope", "receipt_clock",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.parametrize(
    "action", ["commit", "rollback", "close", "COMMIT", "ROLLBACK", "SAVEPOINT x"],
)
def test_participant_cannot_own_transaction(tmp_path, action) -> None:
    env = environment(tmp_path / "ownership.db")
    with writing(env.database, env.service.identity) as context:
        connection = context.connection
        if action.islower():
            with pytest.raises(EvidenceServiceError):
                getattr(connection, action)()
        else:
            with pytest.raises(sqlite3.DatabaseError):
                connection.execute(action)
        assert connection.in_transaction


def test_settled_replay_is_historical_and_never_guarded_or_acknowledged(tmp_path) -> None:
    env = environment(tmp_path / "replay.db")
    env.service._clock = lambda: AT
    request = put(env.scope)
    first = Participant()
    saved = receipt(coordinated(env, request, first))
    env.service.write(put(env.scope, state=saved.processing.state_version, text="New"))
    settled = Participant(admission=SettledSuccess(canonical_key_id=first.committed.key_id))
    assert receipt(coordinated(env, request, settled)) == saved
    assert settled.events == ["classify"]
    assert settled.context.commit_outcome == "confirmed_committed"
    assert not any("INSERT INTO state_intent" in sql for sql in settled.sql)


def test_first_observation_requires_original_receipt_state_still_current(tmp_path) -> None:
    env = environment(tmp_path / "first.db")
    env.service._clock = lambda: AT
    request = put(env.scope)
    saved = receipt(env.service.write(request))
    first = Participant()
    assert receipt(coordinated(env, request, first)) == saved
    assert first.events == ["classify", "guard", "ack"]
    receipt(env.service.write(put(env.scope, state=saved.processing.state_version, text="Edited")))
    stale = Participant()
    result = coordinated(env, request, stale)
    assert result.error.code == "state_conflict"
    assert stale.events == ["classify", "guard"]


def test_linkage_mismatch_is_not_receipt_adoption(tmp_path) -> None:
    env = environment(tmp_path / "link.db")
    request = put(env.scope)
    participant = Participant()
    receipt(coordinated(env, request, participant))
    bad = Participant(admission=SettledSuccess(canonical_key_id="unlinked"))
    result = coordinated(env, request, bad)
    assert result.error.code == "state_conflict"
    assert bad.events == ["classify"]


def test_retained_authorization_precedes_clock_expiry_and_digest(tmp_path) -> None:
    env = environment(tmp_path / "auth.db")
    request = put(env.scope)
    participant = Participant()
    receipt(coordinated(env, request, participant))
    changed = request.model_copy(update={
        "attribution": request.attribution.model_copy(update={"owner_id": "not-the-owner"}),
    })
    settled = Participant(admission=SettledSuccess(canonical_key_id=participant.committed.key_id))
    result = coordinated(env, changed, settled, at=AT + timedelta(days=31))
    assert result.error.code == "forbidden"
    with env.database.connection() as connection:
        assert connection.execute("SELECT watermark FROM receipt_clock").fetchone()[0].startswith(
            "2026-01-01",
        )
        assert connection.execute("SELECT expired FROM write_key").fetchone()[0] == 0
    altered = request.model_copy(update={
        "payload": request.payload.model_copy(update={
            "metadata": request.payload.metadata.model_copy(update={"title": "Different"}),
        }),
    })
    result = coordinated(env, altered, settled, at=AT + timedelta(days=30))
    assert result.error.code == "retry_expired"
    result = coordinated(env, request, settled, at=AT - timedelta(days=1))
    assert result.error.code == "retry_expired"


def test_mixed_units_expiry_maintenance_survives_conflict_and_continues(tmp_path) -> None:
    env = environment(tmp_path / "mixed.db")
    early = put(env.scope, external="expired")
    later = put(env.scope, external="retained")
    p1, p2 = Participant(), Participant()
    receipt(coordinated(env, early, p1))
    retained = receipt(coordinated(env, later, p2, at=AT + timedelta(days=10)))
    conflict = put(env.scope, external="retained")
    batch = WriteBatch(
        contract_version="foundation/1", batch_id="batch", items=(early, conflict, later),
    )

    class Mixed(Participant):
        def classify_unit(self, context, unit, key):
            if unit.ordinal == 0:
                return SettledSuccess(canonical_key_id=p1.committed.key_id)
            if unit.ordinal == 2:
                return SettledSuccess(canonical_key_id=p2.committed.key_id)
            return NewWork()

        def guard_new(self, context, unit, request):
            raise EvidenceServiceError("state_conflict")

    result = _dispatch.write_batch(
        env.database, env.service.identity, batch, AT + timedelta(days=30),
        participant=Mixed(),
        units=tuple(
            UnitIdentity(corpus_id="work", batch_id="batch", unit_id=f"u{i}", ordinal=i)
            for i in range(3)
        ),
    )
    assert [item.error.code if item.error else "success" for item in result.outcomes] == [
        "retry_expired", "state_conflict", "success",
    ]
    assert result.outcomes[-1].receipt == retained
    with env.database.connection() as connection:
        assert connection.execute(
            "SELECT expired FROM write_key WHERE key_id=?", (p1.committed.key_id,),
        ).fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM write_response").fetchone()[0] == 1


def test_settled_failure_does_not_retry_or_ack(tmp_path) -> None:
    env = environment(tmp_path / "failed.db")
    failure = Failure(code="state_conflict", diagnostic_id="recorded-failure")
    participant = Participant(admission=SettledFailure(failure=failure))
    assert coordinated(env, put(env.scope), participant).error == failure
    assert participant.events == ["classify"]


def test_missing_unexpired_response_is_corruption_not_new_work(tmp_path) -> None:
    env = environment(tmp_path / "corrupt.db")
    request = put(env.scope)
    participant = Participant()
    receipt(coordinated(env, request, participant))
    with env.database.transaction() as connection:
        connection.execute("DELETE FROM write_response")
    result = coordinated(env, request, Participant(), at=AT + timedelta(days=31))
    assert result.error.code == "internal_error"
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM state_intent").fetchone()[0] == 1


@pytest.mark.parametrize("commit_happened", [False, True])
def test_commit_exception_is_unknown_even_when_rollback_returns(
    tmp_path, monkeypatch, commit_happened,
):
    env = environment(tmp_path / "unknown.db")
    original = AccountedConnection.commit
    participant = Participant()

    def uncertain(connection):
        if commit_happened:
            original(connection)
        raise sqlite3.OperationalError("commit outcome unknown")

    with monkeypatch.context() as patch:
        patch.setattr(AccountedConnection, "commit", uncertain)
        result = coordinated(env, put(env.scope), participant)
        assert result.error.code == "internal_error"
    assert participant.context.commit_outcome == "unknown"
    with env.database.connection() as connection:
        count = connection.execute("SELECT count(*) FROM document").fetchone()[0]
        assert count == int(commit_happened)


def test_rollback_error_preserves_primary_error_and_unknown_outcome(tmp_path, monkeypatch):
    env = environment(tmp_path / "rollback-error.db")

    def broken(connection):
        raise sqlite3.OperationalError("rollback failed")

    with (
        monkeypatch.context() as patch,
        pytest.raises(RuntimeError, match="primary"),
        writing(env.database, env.service.identity) as context,
    ):
        patch.setattr(AccountedConnection, "rollback", broken)
        raise RuntimeError("primary")
    assert context.commit_outcome == "unknown"


def test_read_only_receipt_seam_miss_does_not_allocate_key(tmp_path):
    env = environment(tmp_path / "missing.db")
    request = put(env.scope)
    with writing(env.database, env.service.identity) as context:
        result = _receipts.replay_only(context, env.service.identity, request, "missing", AT)
        assert isinstance(result, _receipts.ReplayMissing)
        assert context.connection.execute("SELECT count(*) FROM write_key").fetchone()[0] == 0
