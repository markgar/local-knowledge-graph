from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from threading import Event

import pytest
from support.evidence import environment, put, receipt

from kg._execution_budget import (
    Deadline,
    PrivateBudget,
    PrivateResourceStop,
    _graph_build_operation,
    _selection_budget,
)
from kg.evidence import EvidenceDatabase, EvidenceServiceError, _format
from kg.evidence._transactions import writing
from kg.models.evidence import LocalIdentity


def pool():
    return PrivateBudget(Deadline(time.monotonic() + 30))


@pytest.mark.parametrize("text,batches", [("", 0), ("Synthetic source", 8)])
def test_lifecycle_and_batched_transactions_share_original_allowances(tmp_path, text, batches):
    env = environment(tmp_path / "lifecycle.db")
    saved = receipt(env.service.write(put(env.scope, text=text)))
    budget = pool()
    selection = budget.limited(max_visits=10_000)
    with sqlite3.connect(env.database.path) as connection:
        before = tuple(connection.iterdump())

    # Seven mandatory phases, followed by representative <=8-sequence staging reads.
    # This exercises the shipped owner seams, not the unshipped indexing service.
    for phase in range(7 + batches):
        with writing(env.database, env.service.identity, budget=budget) as context:
            assert context.connection._budget is budget
            with context.using_budget(selection):
                rows = context.connection.execute(
                    "SELECT document_id FROM document WHERE document_id=?",
                    (saved.document_id,),
                ).fetchall()
                assert len(rows) == 1
                if phase >= 7:
                    assert len(context.connection.execute(
                        "WITH RECURSIVE batch(n) AS "
                        "(VALUES(1) UNION ALL SELECT n+1 FROM batch WHERE n<8) SELECT n FROM batch"
                    ).fetchall()) == 8
            assert context.connection._budget is budget
        assert context.commit_outcome == "confirmed_committed"

    assert selection._root is budget and selection.deadline is budget.deadline
    assert selection._visits == 2 * (7 + batches) + 9 * batches
    assert 0 < budget._vm < 3_000_000
    assert selection._visits < budget._visits < 60_000
    assert budget._scratch == 0
    with sqlite3.connect(env.database.path) as connection:
        assert tuple(connection.iterdump()) == before


def test_fresh_admission_is_bounded_but_not_free(tmp_path):
    database = EvidenceDatabase(tmp_path / "format.db")
    database.initialize()
    budget = pool()
    with database.connection(budget=budget) as connection:
        vm, visits = budget._vm, budget._visits
        assert 0 < vm < 100_000 and 0 < visits < 2_000
        local = budget.limited(max_visits=10_000)
        with connection._using_budget(local):
            for _ in range(3):
                assert _format.check(connection)
        assert 0 < local._visits < 6_000
        assert budget._visits == visits + local._visits
        assert vm < budget._vm < 400_000


def test_each_metadata_batch_prepays_all_nested_engine_instructions(tmp_path, monkeypatch):
    database = EvidenceDatabase(tmp_path / "instructions.db")
    database.initialize()
    grouped = _format._grouped
    measured = []

    def compared(connection, sql):
        instructions = 0

        def count():
            nonlocal instructions
            instructions += 1
            return 0

        with sqlite3.connect(database.path) as reference:
            reference.execute("SELECT count(*) FROM sqlite_schema").fetchone()
            reference.set_progress_handler(count, 1)
            expected = grouped(reference, sql)
        before = connection._budget._vm
        actual = grouped(connection, sql)
        reserved = connection._budget._vm - before
        assert actual == expected
        assert 0 < instructions <= reserved
        measured.append((instructions, reserved))
        return actual

    monkeypatch.setattr(_format, "_grouped", compared)
    with database.connection(budget=pool()):
        pass
    assert len(measured) == 4


def test_repeated_admission_still_exhausts_original_pool_and_rolls_back(tmp_path):
    database = EvidenceDatabase(tmp_path / "bounded.db")
    database.initialize()
    budget = pool()
    identity = LocalIdentity(principal_id="synthetic")
    completed = 0
    with pytest.raises(PrivateResourceStop):
        for _ in range(100):
            with writing(database, identity, budget=budget) as context:
                context.connection.execute("INSERT INTO corpus VALUES ('transient','v','{}')")
                context.connection.execute("DELETE FROM corpus WHERE corpus_id='transient'")
            completed += 1
    assert 15 <= completed < 100
    assert budget._vm <= 10_000_000 and budget._visits <= 100_000
    with database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM corpus").fetchone()[0] == 0

    budget = pool()
    with (
        pytest.raises(PrivateResourceStop),
        writing(database, identity, budget=budget) as context,
    ):
        context.connection.execute("INSERT INTO corpus VALUES ('rolled-back','v','{}')")
        budget.reserve_vm(10_000_000 - budget._vm)
        context.connection.execute("SELECT 1")
    assert context.commit_outcome == "confirmed_rolled_back"
    with database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM corpus").fetchone()[0] == 0


def test_interleaved_cursor_cannot_resume_after_precise_admission_exhaustion(tmp_path):
    database = EvidenceDatabase(tmp_path / "interleaved.db")
    database.initialize()
    budget = pool()
    with database.connection(budget=budget) as connection:
        connection.execute("BEGIN")
        cursor = connection.execute(
            "WITH RECURSIVE n(x) AS (VALUES(1) UNION ALL SELECT x+1 FROM n WHERE x<10000) "
            "SELECT x FROM n"
        )
        budget.reserve_vm(10_000_000 - budget._vm - 30_000)
        with pytest.raises(PrivateResourceStop):
            _format.check(connection)
        assert budget._vm == 10_000_000
        assert connection._remaining == 0 and not connection._precise
        assert isinstance(connection._stop, PrivateResourceStop)
        with pytest.raises(PrivateResourceStop):
            cursor.fetchall()
        assert connection._remaining == 0
        connection.rollback()


@pytest.mark.parametrize(
    "alteration",
    [
        "PRAGMA application_id=0",
        "PRAGMA user_version=1",
        "UPDATE store_format SET schema_signature='forged'",
        "DELETE FROM schema_object_manifest WHERE name='corpus'",
        "ALTER TABLE corpus ADD COLUMN unexpected TEXT",
        "DROP INDEX assertion_subject_idx",
        "UPDATE sqlite_schema SET sql=replace(sql,'REFERENCES corpus','REFERENCES entity') "
        "WHERE name='processing_guard'",
        "UPDATE sqlite_schema SET sql=replace(sql,'epoch >= 0','epoch >= 1') "
        "WHERE name='processing_guard'",
    ],
)
def test_rechecks_reject_tampering_even_with_restored_schema_cookie(tmp_path, alteration):
    database = EvidenceDatabase(tmp_path / "tampered.db")
    database.initialize()
    budget = pool()
    with database.connection(budget=budget) as admitted:
        with sqlite3.connect(database.path) as attacker:
            cookie = attacker.execute("PRAGMA schema_version").fetchone()[0]
            attacker.execute("PRAGMA writable_schema=ON")
            attacker.execute(alteration)
            attacker.execute(f"PRAGMA schema_version={cookie}")
            attacker.commit()
            before = tuple(attacker.iterdump())
        with pytest.raises(EvidenceServiceError, match="unsupported"):
            _format.check(admitted)
        with (
            pytest.raises(EvidenceServiceError, match="unsupported"),
            database.connection(budget=budget),
        ):
            pytest.fail("Tampered database admitted on a new connection")
        with sqlite3.connect(database.path) as after:
            assert tuple(after.iterdump()) == before


@pytest.mark.parametrize(
    "ddl",
    [
        "CREATE TEMP TABLE corpus(corpus_id TEXT PRIMARY KEY,policy_version,registration_json)",
        "CREATE TEMP TABLE source_namespace(corpus_id TEXT NOT NULL,namespace TEXT NOT NULL,"
        "policy_token TEXT NOT NULL,PRIMARY KEY(corpus_id,namespace))",
        "CREATE TEMP TABLE corpus(corpus_id TEXT NOT NULL,policy_version TEXT NOT NULL,"
        "registration_json TEXT NOT NULL)",
    ],
)
def test_actual_pragma_structure_is_checked_even_when_catalog_and_manifest_match(tmp_path, ddl):
    database = EvidenceDatabase(tmp_path / "pragma.db")
    database.initialize()
    with database.connection(budget=pool()) as connection:
        catalog = _format._catalog(connection)
        connection.execute(ddl)
        assert _format._catalog(connection) == catalog
        with pytest.raises(EvidenceServiceError, match="unsupported"):
            _format.check(connection)


def test_batched_metadata_and_manifest_share_one_snapshot(tmp_path, monkeypatch):
    database = EvidenceDatabase(tmp_path / "snapshot.db")
    database.initialize()
    grouped = _format._grouped
    changed = False

    def competing(connection, sql):
        nonlocal changed
        result = grouped(connection, sql)
        if not changed:
            changed = True
            with sqlite3.connect(database.path) as writer:
                writer.execute("UPDATE store_format SET schema_signature='forged'")
        return result

    monkeypatch.setattr(_format, "_grouped", competing)
    with database.connection(budget=pool()) as connection:
        assert changed
        with pytest.raises(EvidenceServiceError, match="unsupported"):
            _format.check(connection)


def test_owner_rechecks_after_connection_admission_before_any_mutation(tmp_path, monkeypatch):
    database = EvidenceDatabase(tmp_path / "owner.db")
    database.initialize()
    original = database.connection

    @contextmanager
    def raced(*, budget=None):
        with original(budget=budget) as connection:
            with sqlite3.connect(database.path) as writer:
                writer.execute("UPDATE schema_object_manifest SET definition_hash='forged'")
            yield connection

    monkeypatch.setattr(database, "connection", raced)
    with (
        pytest.raises(EvidenceServiceError, match="unsupported"),
        writing(database, LocalIdentity(principal_id="synthetic"), budget=pool()),
    ):
        pytest.fail("Owner exposed a context before fresh locked admission")
    with sqlite3.connect(database.path) as connection:
        assert connection.execute("SELECT count(*) FROM corpus").fetchone()[0] == 0


def test_bulk_repeated_actual_admission_counts_work_without_hidden_interactive_ceiling(tmp_path):
    database = EvidenceDatabase(tmp_path / "bulk-admission.db")
    database.initialize()
    operation = _graph_build_operation(
        deadline=Deadline(time.monotonic() + 300), cancel=Event(),
    )
    budget = operation.budget
    child = _selection_budget(budget)
    with database.connection(budget=budget) as connection:
        for _ in range(150):
            with connection._using_budget(child):
                _format.check(connection)
        snapshot = operation.snapshot()
        assert snapshot.visits_reserved > 100_000
        assert snapshot.vm_instructions_reserved > 10_000_000
        assert child._visits > 10_000
        assert connection._budget is budget
        with sqlite3.connect(database.path) as writer:
            writer.execute("UPDATE store_format SET schema_signature='invalid'")
        with pytest.raises(EvidenceServiceError, match="unsupported"):
            _format.check(connection)
    assert operation.snapshot().visits_reserved > snapshot.visits_reserved
