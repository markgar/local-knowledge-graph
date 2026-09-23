from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from support.query_knowledge import plan, produce, setup
from support.withdrawal import withdrawal

from kg.evidence import EvidenceService, EvidenceServiceError
from kg.knowledge import KnowledgeService
from kg.models.foundation import AssertionWithdrawalReceipt, WriteBatch
from kg.query import QueryService


def test_exact_withdrawal_history_replay_and_independent_members(tmp_path):
    env = setup(tmp_path / "withdraw.db")
    subject, ids = produce(env, 2)
    target, other = sorted(ids)
    knowledge = KnowledgeService(env.database, env.service.identity)
    original = knowledge.contribution(env.scope, target)
    request = withdrawal(env, target)
    result = env.service.write(request)
    assert result.status == "applied", result
    assert isinstance(result.receipt, AssertionWithdrawalReceipt)
    assert result.receipt.contribution_id == target
    assert env.service.write(request).receipt == result.receipt
    assert env.service.write(request).status == "applied"
    fresh = env.service.write(withdrawal(env, target))
    assert fresh.status == "unchanged"
    assert fresh.receipt == result.receipt
    with pytest.raises(EvidenceServiceError, match="not_found"):
        knowledge.contribution(env.scope, target)
    history = knowledge.contribution(env.scope, target, mode="history")
    assert not history.is_current
    assert history.withdrawal.withdrawal_id == result.receipt.withdrawal_id
    assert history.payload == original.payload
    assert history.evidence == original.evidence
    assert history.attribution == original.attribution
    assert knowledge.contribution(env.scope, other).is_current
    with QueryService(env.database, env.service.identity) as query:
        count = query.execute(plan(env, subject)).result
        assert count.error is None, count
        assert count.data.count == 1 and count.data.exact
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM assertion_withdrawal").fetchone()[0] == 1
        assert connection.execute(
            "SELECT count(*) FROM write_key WHERE operation='withdraw_assertion'"
        ).fetchone()[0] == 2


def test_target_authorization_batch_and_rollback(tmp_path):
    env = setup(tmp_path / "withdraw.db")
    subject, ids = produce(env, 2)
    first, second = sorted(ids)
    for target, kwargs, code in (
        ("missing", {}, "not_found"),
        (first, {"owner": "other"}, "forbidden"),
        (first, {"writer": "other"}, "forbidden"),
    ):
        outcome = env.service.write(withdrawal(env, target, **kwargs))
        assert outcome.error.code == code
    with env.database.connection() as c:
        support = c.execute(
            "SELECT contribution_id FROM entity_support WHERE entity_id=?", (subject,)
        ).fetchone()[0]
    assert env.service.write(withdrawal(env, support)).error.code == "invalid_request"
    batch = WriteBatch(
        contract_version="foundation/1", batch_id="batch",
        items=(withdrawal(env, first), withdrawal(env, second, owner="other")),
    )
    result = env.service.write_batch(batch)
    assert result.status == "partial"
    assert [o.status for o in result.outcomes] == ["applied", "rejected"]
    assert KnowledgeService(env.database, env.service.identity).contribution(
        env.scope, second,
    ).is_current


def test_expiry_preserves_event_and_authorizes_tombstone(tmp_path):
    env = setup(tmp_path / "withdraw.db")
    _, ids = produce(env, 1)
    request = withdrawal(env, next(iter(ids)))
    now = datetime(2026, 10, 1, tzinfo=UTC)
    service = EvidenceService(env.database, env.service.identity)
    service._clock = lambda: now
    original = service.write(request)
    assert original.error is None
    now += timedelta(days=30)
    assert service.write(request).error.code == "retry_expired"
    assert service.write(request).error.code == "retry_expired"
    fresh = service.write(withdrawal(env, request.payload.contribution_id))
    assert fresh.status == "unchanged"
    assert fresh.receipt == original.receipt
    changed = request.model_copy(update={
        "request_id": str(uuid4()),
        "payload": request.payload.model_copy(update={"contribution_id": "missing"}),
    })
    assert service.write(changed).error.code == "not_found"
