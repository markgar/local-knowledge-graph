from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from support.evidence import put
from support.knowledge import preset
from support.query_knowledge import plan, produce, setup, write
from support.withdrawal import withdrawal

from kg.evidence import EvidenceService, EvidenceServiceError
from kg.knowledge import KnowledgeService
from kg.models.foundation import (
    AddAssertion,
    AssertionWithdrawalReceipt,
    StoredEntity,
    StringObject,
    WriteBatch,
)
from kg.models.query import SupportInspectionRequest
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


def test_expiry_precedes_digest_and_clock_rollback_after_reopen(tmp_path):
    env = setup(tmp_path / "clock.db")
    _, ids = produce(env, 2)
    target, other = sorted(ids)
    request = withdrawal(env, target)
    clock = datetime(2030, 1, 1, tzinfo=UTC)
    env.service._clock = lambda: clock
    saved = env.service.write(request)
    changed = request.model_copy(update={
        "payload": request.payload.model_copy(update={"contribution_id": other}),
    })
    assert env.service.write(changed).error.code == "retry_conflict"
    clock += timedelta(days=31)
    assert env.service.write(changed).error.code == "retry_expired"
    reopened = EvidenceService(env.database, env.service.identity)
    reopened._clock = lambda: clock - timedelta(days=60)
    assert reopened.write(request).error.code == "retry_expired"
    fresh = reopened.write(withdrawal(env, target))
    assert fresh.status == "unchanged" and fresh.receipt == saved.receipt
    history = KnowledgeService(env.database, env.service.identity).contribution(
        env.scope, target, mode="history",
    )
    timestamp = datetime.fromisoformat(history.withdrawal.committed_at)
    assert timestamp == datetime(2030, 1, 1, tzinfo=UTC)


def test_lost_response_after_commit_reopens_to_identical_receipt(tmp_path, monkeypatch):
    from kg.evidence import _dispatch

    env = setup(tmp_path / "lost.db")
    _, ids = produce(env, 1)
    request = withdrawal(env, next(iter(ids)))
    original, committed = _dispatch.write, []
    def lose(*args, **kwargs):
        committed.append(original(*args, **kwargs))
        raise ConnectionError("simulated response loss after commit")
    with monkeypatch.context() as patch:
        patch.setattr(_dispatch, "write", lose)
        with pytest.raises(ConnectionError):
            env.service.write(request)
    reopened = EvidenceService(env.database, env.service.identity)
    replay = reopened.write(request)
    assert replay.status == "applied" and replay.receipt == committed[0].receipt
    with env.database.connection() as c:
        assert c.execute("SELECT count(*) FROM assertion_withdrawal").fetchone()[0] == 1


def test_all_assertion_object_kinds_and_incoming_outgoing_pages(tmp_path):
    from support.knowledge import schema

    from kg.knowledge import KnowledgeAdministration
    from kg.models.foundation import (
        BooleanObject,
        CreateEntity,
        EntityObject,
        IntegerObject,
        TimestampObject,
    )
    from kg.models.knowledge import PredicateDefinition

    env = setup(tmp_path / "kinds.db", registered=False)
    definition = schema()
    KnowledgeAdministration(env.database, env.admin.authority).register_knowledge_schema(
        preset(definition.model_copy(update={
            "predicates": definition.predicates + tuple(
                PredicateDefinition(
                    name=f"work:{kind}", subject_types=("project",), object_kind=kind,
                ) for kind in ("integer", "boolean", "timestamp")
            ),
        })),
    )
    project, _ = produce(env, 0)
    person = write(env, (CreateEntity(
        kind="entity", local_id="person", name="Person", entity_type="person", support=env.support,
    ),))["person"]
    values = (
        ("work:decision", StringObject(kind="string", value="Decision"), project),
        ("work:integer", IntegerObject(kind="integer", value=42), project),
        ("work:boolean", BooleanObject(kind="boolean", value=True), project),
        ("work:timestamp", TimestampObject(kind="timestamp", value=datetime.now(UTC)), project),
        ("work:owns", EntityObject(
            kind="entity", entity=StoredEntity(kind="stored", entity_id=project),
        ), person),
    )
    reader = KnowledgeService(env.database, env.service.identity)
    for predicate, value, subject in values:
        target = write(env, (AddAssertion(
            kind="assertion", local_id="value",
            subject=StoredEntity(kind="stored", entity_id=subject),
            predicate=predicate, object=value, interpretation="explicit", support=env.support,
        ),))["value"]
        endpoints = (person, project) if predicate == "work:owns" else (project,)
        for endpoint in endpoints:
            assert target in {
                c.contribution_id for c in reader.contributions(
                    env.scope, endpoint, kind="assertion",
                ).entries
            }
        assert env.service.write(withdrawal(env, target)).status == "applied"
        for endpoint in endpoints:
            assert target not in {
                c.contribution_id for c in reader.contributions(
                    env.scope, endpoint, kind="assertion",
                ).entries
            }
            history = reader.contributions(
                env.scope, endpoint, kind="assertion", mode="history",
            )
            captured = next(c for c in history.entries if c.contribution_id == target)
            assert captured.withdrawal is not None and not captured.is_current


@pytest.mark.parametrize("same_key", [False, True])
def test_concurrent_requests_have_one_immutable_event(tmp_path, same_key):
    env = setup(tmp_path / "concurrent.db")
    _, ids = produce(env, 1)
    first = withdrawal(env, next(iter(ids)))
    second = first if same_key else withdrawal(env, first.payload.contribution_id)
    def execute(request):
        return EvidenceService(env.database, env.service.identity).write(request)
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(execute, (first, second)))
    assert {o.receipt.withdrawal_id for o in outcomes} == {outcomes[0].receipt.withdrawal_id}
    assert sorted(o.status for o in outcomes) == (
        ["applied", "applied"] if same_key else ["applied", "unchanged"]
    )
    with env.database.connection() as c:
        assert c.execute("SELECT count(*) FROM assertion_withdrawal").fetchone()[0] == 1


def test_save_failure_rolls_back_event_key_provenance_and_clock(tmp_path, monkeypatch):
    from kg.knowledge import _withdraw

    env = setup(tmp_path / "rollback.db")
    _, ids = produce(env, 1)
    request = withdrawal(env, next(iter(ids)))
    with env.database.connection() as c:
        before = tuple(c.execute("SELECT * FROM receipt_clock").fetchone())
        keys = c.execute("SELECT count(*) FROM write_key").fetchone()[0]
    original = _withdraw.save
    def fail_after_save(*args, **kwargs):
        original(*args, **kwargs)
        raise EvidenceServiceError("internal_error")
    monkeypatch.setattr(_withdraw, "save", fail_after_save)
    assert env.service.write(request).error.code == "internal_error"
    with env.database.connection() as c:
        assert c.execute("SELECT count(*) FROM assertion_withdrawal").fetchone()[0] == 0
        assert c.execute("SELECT count(*) FROM write_key").fetchone()[0] == keys
        assert tuple(c.execute("SELECT * FROM receipt_clock").fetchone()) == before
        assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    monkeypatch.setattr(_withdraw, "save", original)
    assert env.service.write(request).status == "applied"


def test_stale_historical_target_and_source_restore_never_resurrect(tmp_path):
    env = setup(tmp_path / "stale.db")
    _, ids = produce(env, 1)
    target = next(iter(ids))
    update = put(env.scope, text="Changed source", state=env.dependency.state_version)
    changed = env.service.write(update)
    assert changed.error is None, changed
    outcome = env.service.write(withdrawal(env, target))
    assert outcome.status == "applied", outcome
    original_text = env.service.content(
        env.scope, env.dependency.document_id, env.dependency.revision_id,
    ).text
    restored = env.service.write(put(
        env.scope, text=original_text, state=changed.receipt.processing.state_version,
    ))
    assert restored.error is None
    history = KnowledgeService(env.database, env.service.identity).contribution(
        env.scope, target, mode="history",
    )
    assert not history.is_current and history.withdrawal is not None
    assert env.service.evidence(env.scope, history.evidence[0].reference).quote


def test_creation_replay_does_not_revive_and_fresh_correction_is_independent(tmp_path):
    env = setup(tmp_path / "creation.db")
    subject, _ = produce(env, 0)
    change = AddAssertion(
        kind="assertion", local_id="decision",
        subject=StoredEntity(kind="stored", entity_id=subject),
        predicate="work:decision", object=StringObject(kind="string", value="Incorrect"),
        interpretation="explicit", support=env.support,
    )
    target = write(env, (change,), retry="creation")["decision"]
    env.service.write(withdrawal(env, target))
    assert write(env, (change,), retry="creation")["decision"] == target
    corrected = change.model_copy(update={"object": StringObject(kind="string", value="Correct")})
    replacement = write(env, (corrected,))["decision"]
    reader = KnowledgeService(env.database, env.service.identity)
    assert replacement != target and reader.contribution(env.scope, replacement).is_current
    assert not reader.contribution(env.scope, target, mode="history").is_current


def test_revocation_and_original_namespace_conjunction_on_replay(tmp_path):
    env = setup(tmp_path / "auth.db")
    _, ids = produce(env, 1)
    target = next(iter(ids))
    inaccessible = env.scope.model_copy(update={
        "access": env.scope.access.model_copy(update={"namespaces": ("email",)}),
    })
    assert env.service.write(withdrawal(env, target, scope=inaccessible)).error.code == "not_found"
    request = withdrawal(env, target)
    assert env.service.write(request).status == "applied"
    policy = env.policy.model_copy(update={"knowledge_bindings": ()})
    version = env.admin.replace_policy(policy, env.scope.access.policy_version).policy_version
    scope = env.scope.model_copy(update={
        "access": env.scope.access.model_copy(update={"policy_version": version}),
    })
    assert env.service.write(request.model_copy(update={"scope": scope})).error.code == "forbidden"
    assert env.service.write(withdrawal(env, target, scope=scope)).error.code == "forbidden"


def test_exact_retry_conflict_and_knowledge_only_batch_reports(tmp_path):
    env = setup(tmp_path / "retry.db")
    _, ids = produce(env, 2)
    target, other = sorted(ids)
    scope = env.scope.model_copy(update={
        "access": env.scope.access.model_copy(update={"grants": ("read", "write_knowledge")}),
    })
    first = withdrawal(env, target, scope=scope)
    original = env.service.write_explained(first)
    assert original.outcome.status == "applied"
    assert original.report.state == "collected"
    changed = first.model_copy(update={
        "payload": first.payload.model_copy(update={"contribution_id": "nonexistent"}),
    })
    # Replay authorizes the retained target, not the substituted request's target.
    assert env.service.write(changed).error.code == "retry_conflict"
    batch = WriteBatch(
        contract_version="foundation/1", batch_id="knowledge-only",
        items=(first, withdrawal(env, other, scope=scope)),
    )
    outcome = env.service.write_batch_explained(batch)
    assert outcome.outcome.status == "complete"
    assert outcome.report.state == "collected"


def test_authorized_other_corpus_cannot_address_target(tmp_path):
    from kg.models.evidence import CorpusRegistration

    env = setup(tmp_path / "corpus.db")
    _, ids = produce(env, 1)
    registration = env.admin.register(CorpusRegistration(
        corpus_id="other", namespaces=env.registration.namespaces,
        policy=env.policy.model_copy(update={"corpus_id": "other"}),
    ))
    scope = env.scope.model_copy(update={
        "corpus_id": "other",
        "access": env.scope.access.model_copy(update={
            "policy_version": registration.policy_version,
        }),
    })
    result = env.service.write(withdrawal(env, next(iter(ids)), scope=scope))
    assert result.error.code == "not_found"


@pytest.mark.parametrize("phase", ["between_steps", "release", "inspection"])
def test_withdrawal_invalidates_direct_query_inflight_proof(tmp_path, monkeypatch, phase):
    from kg.query._meter import Ledger

    env = setup(tmp_path / "query-race.db")
    subject, ids = produce(env, 2)
    req, written = plan(env, subject), []
    def withdraw_once():
        if not written:
            written.append(env.service.write(withdrawal(env, min(ids))))
            assert written[0].status == "applied"
    with QueryService(env.database, env.service.identity) as query:
        if phase == "between_steps":
            reserve = Ledger.reserve
            def between(ledger, frame):
                if frame.action == "begin" and frame.step_id == "decisions":
                    withdraw_once()
                return reserve(ledger, frame)
            monkeypatch.setattr(Ledger, "reserve", between)
        else:
            run = query._run_worker
            def before_release(*args):
                result = run(*args)
                withdraw_once()
                return result
            if phase == "inspection":
                old = query.execute(req).result
                assert old.data.count == 2
                monkeypatch.setattr(query, "_run_worker", before_release)
                result = query.inspect_support(SupportInspectionRequest(
                    request_id="inspection", scope=env.scope, result_set_id=old.result_set_id,
                    records_step_id="decisions", budget=req.budget,
                ))
                assert result.error.code == "state_changed" and not result.records
                return
            monkeypatch.setattr(query, "_run_worker", before_release)
        result = query.execute(req).result
        assert result.error.code == "state_changed" and result.data is None
        assert not query._support.sets and query._support.bytes == 0


def test_coordinated_withdrawal_rejects_before_participant_or_clock(tmp_path):
    from kg.evidence._coordination import UnitIdentity
    from kg.evidence._dispatch import write as dispatch

    env = setup(tmp_path / "coordinated.db")
    _, ids = produce(env, 1)
    request = withdrawal(env, next(iter(ids)))
    class Participant:
        def classify_unit(self, *args):
            pytest.fail("withdrawal must not classify coordinated units")
    for replay in (False, True):
        if replay:
            assert env.service.write(request).status == "applied"
        with env.database.connection() as c:
            before = tuple(c.execute("SELECT * FROM receipt_clock").fetchone())
        outcome = dispatch(
            env.database, env.service.identity, request, env.service._clock(),
            participant=Participant(),
            unit=UnitIdentity(
                corpus_id=env.scope.corpus_id, batch_id="batch", unit_id="unit", ordinal=0,
            ),
        )
        assert outcome.error.code == "unsupported"
        with env.database.connection() as c:
            assert tuple(c.execute("SELECT * FROM receipt_clock").fetchone()) == before


@pytest.mark.parametrize("count", [25, 1001])
def test_full_count_and_inspected_members_after_withdrawal(tmp_path, count):
    env = setup(tmp_path / "count.db")
    subject, ids = produce(env, count)
    request = plan(env, subject)
    with QueryService(env.database, env.service.identity) as query:
        old = query.execute(request).result
        assert old.error is None
        withdrawn = min(ids)
        assert env.service.write(withdrawal(env, withdrawn)).status == "applied"
        stale = query.inspect_support(SupportInspectionRequest(
            request_id="old", scope=env.scope, result_set_id=old.result_set_id,
            records_step_id="decisions", budget=request.budget,
        ))
        assert stale.error.code == "state_changed"
        new = query.execute(request).result
        assert new.error is None and new.data.exact and new.data.count == count - 1
        page = query.inspect_support(SupportInspectionRequest(
            request_id="new", scope=env.scope, result_set_id=new.result_set_id,
            records_step_id="decisions", budget=request.budget,
        ))
        assert page.error is None
        assert {r.record_id for r in page.records} == ids - {withdrawn}
