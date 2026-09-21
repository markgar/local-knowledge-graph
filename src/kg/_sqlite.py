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
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    }


def legacy_format(connection: sqlite3.Connection, *, projection: bool = False) -> None:
    if connection.execute("PRAGMA application_id").fetchone()[0] != 0:
        raise sqlite3.DatabaseError("Incompatible database format; use a separate database file")
    names = tables(connection)
    expected = "projection_schema" if projection else "source_document"
    if names and expected not in names:
        raise sqlite3.DatabaseError("Incompatible database format; use a separate database file")


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
