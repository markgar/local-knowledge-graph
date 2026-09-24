from __future__ import annotations

import multiprocessing
import sqlite3
import threading
import time
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

import pytest

from kg._sqlite import EVIDENCE_APPLICATION_ID, execute_schema, write_transaction
from kg.db import Database
from kg.evidence import EvidenceDatabase, EvidenceServiceError
from kg.retrieval.dense import DenseIndexError, DenseRetrievalService


def snapshot(path: Path) -> tuple[object, ...]:
    with sqlite3.connect(path) as connection:
        return (
            *(
                connection.execute(f"PRAGMA {name}").fetchone()[0]
                for name in (
                    "application_id",
                    "user_version",
                    "journal_mode",
                )
            ),
            tuple(connection.iterdump()),
        )


def test_fresh_format_reopen_and_transaction(tmp_path: Path) -> None:
    database = EvidenceDatabase(tmp_path / "evidence.db")
    database.initialize()
    before = snapshot(database.path)
    database.initialize()
    assert snapshot(database.path) == before
    assert before[:3] == (EVIDENCE_APPLICATION_ID, 4, "wal")
    with pytest.raises(EvidenceServiceError) as error, database.transaction() as connection:
        connection.execute("INSERT INTO corpus VALUES ('c', 'p', '{}')")
        connection.execute("INSERT INTO document VALUES ('d','c','n','e','o','s','state')")
    assert error.value.failure.code == "internal_error"
    assert snapshot(database.path) == before


@pytest.mark.parametrize("old", ["legacy", "unknown", "projection", "sqlitefoo", "view-only"])
def test_incompatible_store_does_not_mutate(tmp_path: Path, old: str) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        if old == "view-only":
            connection.execute("CREATE VIEW unrelated AS SELECT 1 AS retained")
        else:
            connection.execute(f"CREATE TABLE {old}(value TEXT)")
            connection.execute(f"INSERT INTO {old} VALUES ('retained')")
    before = snapshot(path)
    with pytest.raises(EvidenceServiceError) as error:
        EvidenceDatabase(path).initialize()
    assert error.value.failure.code == "unsupported"
    assert snapshot(path) == before
    with pytest.raises(sqlite3.DatabaseError):
        Database(path).initialize()
    assert snapshot(path) == before


def test_admission_snapshot_during_competing_evidence_commit(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "race.db"
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=WAL")
    database = EvidenceDatabase(path)
    competed = False

    class AdmissionCursor(sqlite3.Cursor):
        application_read = False

        def execute(self, sql, parameters=()):
            self.application_read = sql == "PRAGMA application_id"
            return super().execute(sql, parameters)

        def fetchone(self):
            nonlocal competed
            result = super().fetchone()
            if self.application_read and not competed:
                assert result[0] == 0
                competed = True
                EvidenceDatabase(path).initialize()
            return result

    class AdmissionConnection(sqlite3.Connection):
        def execute(self, sql, parameters=()):
            return self.cursor(factory=AdmissionCursor).execute(sql, parameters)

    def open_connection(*, create):
        connection = sqlite3.connect(path, factory=AdmissionConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    monkeypatch.setattr(database, "_open", open_connection)
    database.initialize()
    assert competed
    assert snapshot(path)[:3] == (EVIDENCE_APPLICATION_ID, 4, "wal")


def test_old_and_projection_connections_reject_evidence(tmp_path: Path) -> None:
    path = tmp_path / "new.db"
    EvidenceDatabase(path).initialize()
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
    before = snapshot(path)
    with pytest.raises(sqlite3.DatabaseError):
        Database(path).initialize()
    old = Database(tmp_path / "old.db")
    old.initialize()
    dense = DenseRetrievalService(old, "test", projection_path=path)
    with pytest.raises(DenseIndexError), dense._projection_connection():
        pytest.fail("Projection path must be checked before extension/schema work")
    assert snapshot(path) == before


def test_schema_execution_keeps_lock_and_rolls_back(tmp_path: Path) -> None:
    path = tmp_path / "ddl.db"
    connection = sqlite3.connect(path)
    with pytest.raises(sqlite3.OperationalError), write_transaction(connection):
        execute_schema(
            connection,
            "CREATE TABLE t(x TEXT); INSERT INTO t VALUES ('a;b'); INVALID SQL;",
        )
    assert not connection.execute("SELECT name FROM sqlite_master").fetchall()
    assert not connection.in_transaction
    connection.close()
    assert (
        "CREATE TABLE revision" in resources.files("kg.evidence").joinpath("schema.sql").read_text()
    )


@pytest.mark.parametrize("kind", ["evidence", "legacy"])
def test_wal_transition_waits_for_post_commit_writer(
    tmp_path: Path, monkeypatch, kind: str
) -> None:
    import kg.db as legacy
    import kg.evidence.database as evidence

    path = tmp_path / "wal-race.db"
    module = evidence if kind == "evidence" else legacy
    transaction = module.write_transaction
    held, release = threading.Event(), threading.Event()

    def writer():
        with sqlite3.connect(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            held.set()
            assert release.wait(5)
            connection.rollback()

    thread = threading.Thread(target=writer)
    timer = threading.Timer(0.1, release.set)

    @contextmanager
    def contended(connection):
        with transaction(connection):
            yield
        thread.start()
        assert held.wait(5)
        timer.start()

    monkeypatch.setattr(module, "write_transaction", contended)
    try:
        (EvidenceDatabase(path) if kind == "evidence" else Database(path)).initialize()
    finally:
        release.set()
        thread.join(5)
        timer.cancel()
    assert not thread.is_alive()
    assert snapshot(path)[2] == "wal"


def test_wal_contention_obeys_timeout_and_does_not_fall_back(tmp_path: Path) -> None:
    from kg._sqlite import enable_wal

    path = tmp_path / "timeout.db"
    with sqlite3.connect(path) as writer, sqlite3.connect(path) as waiting:
        writer.execute("CREATE TABLE retained(value)")
        writer.execute("BEGIN IMMEDIATE")
        waiting.execute("PRAGMA busy_timeout=20")
        started = time.monotonic()
        with pytest.raises(sqlite3.OperationalError) as error:
            enable_wal(waiting)
        assert error.value.sqlite_errorcode == sqlite3.SQLITE_BUSY
        assert time.monotonic() - started < 1
        assert waiting.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
        writer.rollback()


def initialize_worker(path: str, kind: str, ready: object = None) -> None:
    if kind == "evidence":
        EvidenceDatabase(Path(path)).initialize()
    elif kind == "legacy":
        Database(Path(path)).initialize()
    else:
        from kg.retrieval.dense import _initialize_projection_schema

        with sqlite3.connect(path) as connection:
            connection.row_factory = sqlite3.Row
            _initialize_projection_schema(connection)


@pytest.mark.parametrize("winner", ["evidence", "legacy", "projection"])
@pytest.mark.parametrize("loser", ["evidence", "legacy", "projection"])
def test_initializers_in_both_orders(tmp_path: Path, winner: str, loser: str) -> None:
    if winner != "evidence" and loser != "evidence":
        return
    path = tmp_path / "race.db"
    ctx = multiprocessing.get_context("spawn")
    first = ctx.Process(target=initialize_worker, args=(str(path), winner))
    first.start()
    first.join(30)
    assert first.exitcode == 0
    before = snapshot(path)
    if winner == loser:
        initialize_worker(str(path), loser)
    else:
        with pytest.raises((EvidenceServiceError, sqlite3.DatabaseError)):
            initialize_worker(str(path), loser)
    assert snapshot(path) == before
