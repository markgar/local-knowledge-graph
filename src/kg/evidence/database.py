from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import ValidationError

from kg._sqlite import (
    EVIDENCE_APPLICATION_ID,
    enable_wal,
    execute_schema,
    write_transaction,
)
from kg.evidence._format import USER_VERSION, install_manifest, schema_sql
from kg.evidence._format import check as _check
from kg.evidence.errors import EvidenceServiceError, storage_error


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
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            connection.close()
            raise EvidenceServiceError("unsupported")
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def initialize(self) -> None:
        try:
            connection = self._open(create=True)
            try:
                _check(connection, allow_empty=True)
                with write_transaction(connection):
                    if not _check(connection, allow_empty=True):
                        execute_schema(connection, schema_sql())
                        install_manifest(connection)
                        connection.execute(f"PRAGMA application_id={EVIDENCE_APPLICATION_ID}")
                        connection.execute(f"PRAGMA user_version={USER_VERSION}")
                    _check(connection)
                enable_wal(connection)
            finally:
                connection.close()
        except (sqlite3.Error, OSError, ValidationError) as error:
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
        except (sqlite3.Error, OSError, ValidationError) as error:
            raise storage_error(error) from None

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        with self.connection() as connection, write_transaction(connection):
            _check(connection)
            yield connection
