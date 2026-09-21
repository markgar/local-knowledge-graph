from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

from kg._sqlite import EVIDENCE_APPLICATION_ID, execute_schema, tables, write_transaction
from kg.evidence.errors import EvidenceServiceError, storage_error


def _check(connection: sqlite3.Connection, *, allow_empty: bool = False) -> bool:
    application = connection.execute("PRAGMA application_id").fetchone()[0]
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    names = tables(connection)
    if allow_empty and application == 0 and version == 0 and not names:
        return False
    if application != EVIDENCE_APPLICATION_ID or version != 1 or "store_format" not in names:
        raise EvidenceServiceError("unsupported")
    rows = connection.execute("SELECT format FROM store_format").fetchall()
    if [row[0] for row in rows] != ["evidence-store/1"]:
        raise EvidenceServiceError("unsupported")
    return True


class EvidenceDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _open(self, *, create: bool) -> sqlite3.Connection:
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        uri = self.path.resolve().as_uri() + ("?mode=rwc" if create else "?mode=rw")
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def initialize(self) -> None:
        try:
            connection = self._open(create=True)
            try:
                _check(connection, allow_empty=True)
                with write_transaction(connection):
                    if not _check(connection, allow_empty=True):
                        execute_schema(
                            connection,
                            resources.files("kg.evidence").joinpath("schema.sql").read_text(),
                        )
                        connection.execute(f"PRAGMA application_id={EVIDENCE_APPLICATION_ID}")
                        connection.execute("PRAGMA user_version=1")
                    _check(connection)
                connection.execute("PRAGMA journal_mode=WAL")
            finally:
                connection.close()
        except (sqlite3.Error, OSError) as error:
            raise storage_error(error) from None

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        try:
            connection = self._open(create=False)
            try:
                _check(connection)
                yield connection
            finally:
                connection.close()
        except (sqlite3.Error, OSError) as error:
            raise storage_error(error) from None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection, write_transaction(connection):
            _check(connection)
            yield connection
