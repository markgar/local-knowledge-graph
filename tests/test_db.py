import sqlite3
from pathlib import Path

import pytest

from kg.db import _apply_migration


def test_failed_migration_is_atomic(tmp_path: Path) -> None:
    connection = sqlite3.connect(tmp_path / "migration.sqlite3")
    connection.execute(
        """
        CREATE TABLE schema_migration (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL
        )
        """
    )

    with pytest.raises(sqlite3.Error):
        _apply_migration(
            connection,
            99,
            "0099_broken.sql",
            "CREATE TABLE partial_state (id INTEGER); INVALID SQL;",
        )

    table = connection.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type = 'table' AND name = 'partial_state'
        """
    ).fetchone()
    version = connection.execute(
        "SELECT version FROM schema_migration WHERE version = 99"
    ).fetchone()
    connection.close()

    assert table is None
    assert version is None
