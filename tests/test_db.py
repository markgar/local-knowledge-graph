
from pathlib import Path

import pytest

from kg.db import Database


@pytest.mark.service
def test_schema_initialization_is_idempotent(tmp_path: Path) -> None:
    database = Database(tmp_path / "index.sqlite3")

    database.initialize()
    database.initialize()

    with database.connection() as connection:
        tables = {
            row["name"]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
            ).fetchall()
        }

    assert {"source_document", "source_revision", "source_anchor", "passage_fts"} <= tables
