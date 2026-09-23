from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from support.evidence import environment, put, receipt

from kg.evidence import EvidenceService
from kg.models.foundation import ExpectedState


def test_replay_conflict_expiry_and_clock_rollback(tmp_path: Path) -> None:
    env = environment(tmp_path / "e.db")
    at = datetime(2026, 9, 21, tzinfo=UTC)
    env.service._clock = lambda: at
    request = put(env.scope, retry="stable-key")
    committed = env.service.write(request)
    first = receipt(committed)
    # A new process/service can recover the result after the original response was lost.
    reopened = EvidenceService(env.database, env.service.identity)
    reopened._clock = lambda: at
    replay = reopened.write(request.model_copy(update={"request_id": "fresh-correlation"}))
    assert replay.request_id == "fresh-correlation"
    assert replay.status == committed.status and replay.receipt == first
    change = put(env.scope, state=first.processing.state_version, text="new")
    changed = receipt(reopened.write(change))
    assert reopened.write(request).receipt == first
    assert (
        reopened.document(env.scope, first.document_id).state_version
        == changed.processing.state_version
    )
    different = request.model_copy(
        update={
            "payload": request.payload.model_copy(
                update={
                    "precondition": ExpectedState(
                        kind="match", state_version=first.processing.state_version
                    ),
                }
            )
        }
    )
    assert reopened.write(different).error.code == "retry_conflict"
    at += timedelta(days=30)
    assert reopened.write(request).error.code == "retry_expired"
    assert reopened.write(different).error.code == "retry_expired"
    at -= timedelta(days=31)
    assert reopened.write(request).error.code == "retry_expired"
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM write_key").fetchone()[0] == 2
        assert connection.execute("SELECT count(*) FROM write_response").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM revision").fetchone()[0] == 2


def test_revoked_retry_never_returns_receipt(tmp_path: Path) -> None:
    from kg.models.evidence import LocalPolicy

    env = environment(tmp_path / "e.db")
    request = put(env.scope)
    receipt(env.service.write(request))
    revoked = env.admin.replace_policy(
        LocalPolicy(
            corpus_id="work",
            bindings=env.policy.bindings,
            grants=tuple(g for g in env.policy.grants if g.grant != "write_documents"),
        ),
        env.scope.access.policy_version,
    )
    scope = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(
                update={
                    "policy_version": revoked.policy_version,
                }
            )
        }
    )
    result = env.service.write(request.model_copy(update={"scope": scope}))
    assert result.receipt is None
    assert result.error.code == "forbidden"
