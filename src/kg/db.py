from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def migrate(self) -> None:
        with self.connection() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migration (
                    version INTEGER PRIMARY KEY,
                    name TEXT NOT NULL,
                    applied_at TEXT NOT NULL
                )
                """
            )
            applied = {
                row["version"]
                for row in connection.execute("SELECT version FROM schema_migration").fetchall()
            }
            migration_root = resources.files("kg").joinpath("migrations")
            migrations = sorted(
                (item for item in migration_root.iterdir() if item.name.endswith(".sql")),
                key=lambda item: item.name,
            )
            for migration in migrations:
                version_text, _, _ = migration.name.partition("_")
                version = int(version_text)
                if version in applied:
                    continue
                _apply_migration(
                    connection,
                    version,
                    migration.name,
                    migration.read_text(encoding="utf-8"),
                )

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()


def _apply_migration(
    connection: sqlite3.Connection,
    version: int,
    name: str,
    sql: str,
) -> None:
    escaped_name = name.replace("'", "''")
    script = (
        "BEGIN IMMEDIATE;\n"
        f"{sql}\n"
        "INSERT INTO schema_migration (version, name, applied_at) "
        f"VALUES ({version}, '{escaped_name}', datetime('now'));\n"
        "COMMIT;\n"
    )
    try:
        connection.executescript(script)
    except sqlite3.Error:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
