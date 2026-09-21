from __future__ import annotations

import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from support.evidence import environment, put, receipt

from kg._execution_budget import (
    Deadline,
    DeadlineStop,
    LocalExecutionMeter,
    PrivateBudget,
    PrivateResourceStop,
    PublicBudgetStop,
)
from kg.evidence import EvidenceServiceError
from kg.evidence._read_context import (
    ReadSessionId,
    _limit_temp,
    observe,
    read_context,
    read_evidence,
    release_fence,
)
from kg.evidence._sql import AccountedConnection
from kg.indexing._selection import ProjectionHandle
from kg.models.evidence import PolicyGrant


def execution():
    deadline = Deadline(time.monotonic() + 30)
    budget = PrivateBudget(deadline)
    meter = LocalExecutionMeter(budget, max_operations=16, max_items=100)
    return deadline, budget, meter


@pytest.mark.parametrize("option", ["TEMP_STORE=3", "TEMP_STORE=unknown", None])
def test_temp_rejects_forced_memory_or_unverifiable_builds(monkeypatch, option):
    original = AccountedConnection.execute

    def execute(connection, sql, parameters=()):
        if sql == "PRAGMA compile_options":
            return original(
                connection, "SELECT ? WHERE ? IS NOT NULL", (option, option),
            )
        return original(connection, sql, parameters)

    monkeypatch.setattr(AccountedConnection, "execute", execute)
    connection = sqlite3.connect(":memory:", factory=AccountedConnection)
    try:
        with pytest.raises(EvidenceServiceError) as error:
            _limit_temp(connection)
        assert error.value.failure.code == "unsupported"
    finally:
        connection.close()


@pytest.mark.parametrize("mode", [None, 0, 2])
def test_temp_rejects_missing_or_incorrect_file_mode_readback(monkeypatch, mode):
    original = AccountedConnection.execute

    def execute(connection, sql, parameters=()):
        if sql == "PRAGMA temp_store":
            return original(connection, "SELECT ? WHERE ? IS NOT NULL", (mode, mode))
        return original(connection, sql, parameters)

    monkeypatch.setattr(AccountedConnection, "execute", execute)
    connection = sqlite3.connect(":memory:", factory=AccountedConnection)
    try:
        with pytest.raises(EvidenceServiceError) as error:
            _limit_temp(connection)
        assert error.value.failure.code == "unsupported"
    finally:
        connection.close()


def test_temp_file_mode_is_selected_before_controls_and_page_limit_is_real(tmp_path):
    env = environment(tmp_path / "temp.db")
    deadline, _, meter = execution()
    with read_context(
        env.database, env.service.identity, env.scope, ReadSessionId(),
        deadline, meter.begin_step("temp"),
    ) as context:
        connection = context.connection
        assert connection.execute("PRAGMA temp_store").fetchone()[0] == 1
        connection.execute("CREATE TEMP TABLE spill(payload BLOB)")
        # Exceeds the usual TEMP page cache; FILE mode still permits caching.
        connection.execute("INSERT INTO spill VALUES (zeroblob(4 * 1024 * 1024))")
        assert connection.execute("SELECT length(payload) FROM spill").fetchone()[0] == 4 << 20
        with pytest.raises(PrivateResourceStop):
            connection.execute("INSERT INTO spill VALUES (zeroblob(128 * 1024 * 1024))")


def test_read_local_cap_uses_same_snapshot_meter_and_accounted_helpers(tmp_path):
    env = environment(tmp_path / "local.db")
    saved = receipt(env.service.write(put(env.scope)))
    reference = env.service.anchors(
        env.scope, saved.document_id, saved.processing.state_version,
    ).entries[0].reference
    deadline, budget, meter = execution()
    step = meter.begin_step("read")
    with read_context(
        env.database, env.service.identity, env.scope, ReadSessionId(), deadline, step,
    ) as context:
        connection = context.connection
        local = budget.limited(max_visits=2)
        with context.using_budget(local):
            assert context.connection is connection
            assert context.meter.private_budget is local
            cursor = connection.execute("SELECT 1 UNION ALL SELECT 2")
            assert next(cursor)[0] == 1
            with context.using_budget(local.limited(max_visits=1)):
                assert cursor.fetchone()[0] == 2
                with pytest.raises(PrivateResourceStop):
                    context.meter.reserve_visits()
            with pytest.raises(PrivateResourceStop):
                cursor.fetchone()
        assert context.meter is step and connection._budget is budget
        with context.using_budget(local), pytest.raises(PrivateResourceStop):
            cursor.fetchone()
        # The public adapter's nested SQL cannot escape a cap on the connection.
        with (
            context.using_budget(budget.limited(max_visits=1)),
            pytest.raises(PrivateResourceStop),
        ):
            read_evidence(context, reference)
        assert meter.public_accounting().items_consumed == 1
        assert context.connection is connection and connection.in_transaction
    with pytest.raises(EvidenceServiceError), context.using_budget(local):
        pytest.fail("Expired context admitted a cap")


def test_observer_snapshot_temp_and_fresh_release_have_no_canonical_writes(tmp_path) -> None:
    env = environment(tmp_path / "read.db")
    saved = receipt(env.service.write(put(env.scope)))
    reference = env.service.anchors(
            env.scope, saved.document_id, saved.processing.state_version,
    ).entries[0].reference
    deadline, budget, meter = execution()
    with env.database.connection() as verify:
        version = verify.execute("PRAGMA data_version").fetchone()[0]
        with observe(env.database, env.service.identity, env.scope, deadline, budget) as observer:
            with read_context(
                env.database, env.service.identity, env.scope,
                observer.session_id, deadline, meter.begin_step("evidence"),
            ) as context:
                value = read_evidence(context, reference)
                assert value.quote == "A\r\nCafe\u0301 \U0001f680"
                connection = context.connection
                assert connection.execute("PRAGMA temp_store").fetchone()[0] == 1
                connection.execute("CREATE TEMP TABLE candidates(id TEXT)")
                connection.execute("INSERT INTO temp.candidates VALUES (?)", (reference.anchor_id,))
                assert connection.execute("SELECT count(*) FROM candidates").fetchone()[0] == 1
                assert connection.execute("PRAGMA temp.max_page_count").fetchone()[0] * (
                    connection.execute("PRAGMA temp.page_size").fetchone()[0]
                ) == 128 << 20
                handle = ProjectionHandle(observer.session_id, env.scope, "config", ())
                handle.check(context)
                for sql in (
                    "UPDATE corpus SET policy_version='forged'",
                    "CREATE TABLE main.unwanted(id TEXT)", "COMMIT", "ROLLBACK",
                    "SAVEPOINT escaped", "PRAGMA user_version=3",
                    "PRAGMA temp.max_page_count=999999", "ATTACH ':memory:' AS escaped",
                    "PRAGMA temp_store=MEMORY",
                ):
                    with pytest.raises(sqlite3.DatabaseError):
                        connection.execute(sql)
                with pytest.raises(EvidenceServiceError):
                    connection.commit()
                with pytest.raises(EvidenceServiceError):
                    ProjectionHandle(ReadSessionId(), env.scope, "config", ()).check(context)
            with pytest.raises(EvidenceServiceError):
                handle.check(context)
            with pytest.raises(EvidenceServiceError):
                connection.execute("SELECT 1")
            assert not observer.changed()
            with release_fence(observer, env.service.identity, env.scope, deadline) as fence:
                fence.check_active()
            with pytest.raises(EvidenceServiceError):
                fence.check_active()
        assert verify.execute("PRAGMA data_version").fetchone()[0] == version
    assert meter.public_accounting().items_consumed == 1
    assert budget._visits > 0 and budget._vm > 0


def test_commit_in_observer_snapshot_gap_is_rejected(tmp_path) -> None:
    env = environment(tmp_path / "gap.db")
    barrier = Barrier(2)

    def writer():
        barrier.wait(timeout=10)
        result = env.service.write(put(env.scope))
        barrier.wait(timeout=10)
        return result

    deadline, budget, meter = execution()
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(writer)
        with observe(env.database, env.service.identity, env.scope, deadline, budget) as observer:
            barrier.wait(timeout=10)
            barrier.wait(timeout=10)
            with read_context(
                env.database, env.service.identity, env.scope,
                observer.session_id, deadline, meter.begin_step("read"),
            ) as context:
                count = context.connection.execute("SELECT count(*) FROM document").fetchone()[0]
                assert count == 1
            with (
                pytest.raises(EvidenceServiceError) as error,
                release_fence(observer, env.service.identity, env.scope, deadline),
            ):
                pytest.fail("Changed generation released")
            assert error.value.failure.code == "state_changed"
        assert future.result().status == "applied"


def test_snapshot_is_shared_and_fence_blocks_writer(tmp_path) -> None:
    env = environment(tmp_path / "snapshot.db")
    deadline, budget, meter = execution()
    with observe(env.database, env.service.identity, env.scope, deadline, budget) as observer:
        with read_context(
            env.database, env.service.identity, env.scope,
            observer.session_id, deadline, meter.begin_step("read"),
        ) as context:
            assert context.connection.execute("SELECT count(*) FROM document").fetchone()[0] == 0
            env.service.write(put(env.scope))
            assert context.connection.execute("SELECT count(*) FROM document").fetchone()[0] == 0
        assert observer.changed()
    deadline, budget, meter = execution()
    with observe(env.database, env.service.identity, env.scope, deadline, budget) as observer:
        with (
            release_fence(observer, env.service.identity, env.scope, deadline),
            env.database.connection() as writer,
        ):
            writer.execute("PRAGMA busy_timeout=1")
            with pytest.raises(sqlite3.OperationalError):
                writer.execute("BEGIN IMMEDIATE")
        with env.database.transaction() as writer:
            writer.execute("UPDATE processing_guard SET epoch=epoch+1")
        assert observer.changed()


def test_revoked_policy_fresh_fence_and_retained_reference_lifetime(tmp_path) -> None:
    env = environment(tmp_path / "policy.db")
    deadline, budget, _ = execution()
    with observe(env.database, env.service.identity, env.scope, deadline, budget) as observer:
        retained = observer.retain()
    assert not retained.observer.changed()
    changed = env.policy.model_copy(update={"grants": (
        *env.policy.grants, PolicyGrant(principal_id="someone", namespace="email", grant="read"),
    )})
    env.admin.replace_policy(changed, env.scope.access.policy_version)
    with (
        pytest.raises(EvidenceServiceError) as error,
        release_fence(retained.observer, env.service.identity, env.scope, deadline),
    ):
        pytest.fail("Revoked policy released")
    assert error.value.failure.code == "forbidden"
    retained.close()
    retained.close()
    with pytest.raises(EvidenceServiceError):
        observer.changed()
    with pytest.raises(EvidenceServiceError):
        _ = retained.observer


def test_expired_context_and_deadline_cannot_be_reset(tmp_path, monkeypatch) -> None:
    env = environment(tmp_path / "expired.db")
    deadline, budget, meter = execution()
    with (
        observe(env.database, env.service.identity, env.scope, deadline, budget) as observer,
        read_context(
            env.database, env.service.identity, env.scope,
            observer.session_id, deadline, meter.begin_step("read"),
        ) as context,
    ):
        monkeypatch.setattr(time, "monotonic", lambda: deadline.expires_at_monotonic)
        with pytest.raises(DeadlineStop):
            context.check_active()
        with pytest.raises(DeadlineStop):
            context._connection.execute("SELECT 1")
    monkeypatch.undo()
    with pytest.raises(EvidenceServiceError), read_context(
        env.database, env.service.identity, env.scope, ReadSessionId(),
        Deadline(time.monotonic() + 60), meter.begin_step("new"),
    ):
        pass


@pytest.mark.parametrize("exhausted", ["scratch", "public"])
def test_evidence_preflight_reserves_before_content_or_quote_decode(
    tmp_path, monkeypatch, exhausted,
) -> None:
    from kg.evidence import _reads

    env = environment(tmp_path / "preflight.db")
    saved = receipt(env.service.write(put(env.scope, text="x" * (1 << 20))))
    reference = env.service.anchors(
    env.scope, saved.document_id, saved.processing.state_version,
    ).entries[0].reference
    deadline, budget, meter = execution()
    step = meter.begin_step("read")
    expected = PrivateResourceStop if exhausted == "scratch" else PublicBudgetStop
    with read_context(
        env.database, env.service.identity, env.scope, ReadSessionId(), deadline, step,
    ) as context:
        def unexpected(*args, **kwargs):
            pytest.fail("Evidence decoded before reservation")

        monkeypatch.setattr(_reads, "evidence_view", unexpected)
        if exhausted == "scratch":
            held = budget.reserve_scratch(64 << 20, "general")
        else:
            step.reserve_public("resolve_entity", 64)
            step.reserve_public("resolve_entity", 36)
        with pytest.raises(expected):
            read_evidence(context, reference)
        if exhausted == "scratch":
            held.release()


def test_evidence_scratch_lives_until_snapshot_exit(tmp_path) -> None:
    env = environment(tmp_path / "scratch-lifetime.db")
    saved = receipt(env.service.write(put(env.scope)))
    reference = env.service.anchors(
        env.scope, saved.document_id, saved.processing.state_version,
    ).entries[0].reference
    deadline, budget, meter = execution()
    with read_context(
        env.database, env.service.identity, env.scope, ReadSessionId(),
        deadline, meter.begin_step("read"),
    ) as context:
        read_evidence(context, reference)
        assert budget._scratch > 0
        first = budget._scratch
        read_evidence(context, reference)
        assert budget._scratch == 2 * first
    assert budget._scratch == 0
