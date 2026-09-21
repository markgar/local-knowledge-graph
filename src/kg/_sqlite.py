"""Small SQLite primitives shared by otherwise incompatible storage formats."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

EVIDENCE_APPLICATION_ID = 0x4B474531


def tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT GLOB 'sqlite_*'"
        )
    }


def has_user_schema(connection: sqlite3.Connection) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE name NOT GLOB 'sqlite_*' LIMIT 1"
        ).fetchone()
        is not None
    )


@contextmanager
def read_snapshot(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        yield
    else:
        connection.execute("BEGIN")
        try:
            yield
        finally:
            connection.rollback()


def legacy_format(connection: sqlite3.Connection, *, projection: bool = False) -> bool:
    with read_snapshot(connection):
        if connection.execute("PRAGMA application_id").fetchone()[0] != 0:
            raise sqlite3.DatabaseError(
                "Incompatible database format; use a separate database file"
            )
        expected = "projection_schema" if projection else "source_document"
        nonempty = has_user_schema(connection)
        if nonempty and expected not in tables(connection):
            raise sqlite3.DatabaseError(
                "Incompatible database format; use a separate database file"
            )
        return nonempty


def execute_schema(connection: sqlite3.Connection, sql: str) -> None:
    """Execute complete SQLite statements without executescript's implicit commit."""
    if not connection.in_transaction:
        raise RuntimeError("Schema execution requires an owned write transaction")
    statement = ""
    for character in sql:
        statement += character
        if character == ";" and sqlite3.complete_statement(statement):
            connection.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("Incomplete schema statement")


@contextmanager
def write_transaction(connection: sqlite3.Connection) -> Iterator[None]:
    if connection.in_transaction:
        raise RuntimeError("Nested transaction owner")
    connection.execute("BEGIN IMMEDIATE")
    try:
        yield
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
