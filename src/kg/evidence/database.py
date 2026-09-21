from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pydantic import ValidationError

from kg._execution_budget import PrivateBudget
from kg._sqlite import (
    EVIDENCE_APPLICATION_ID,
    enable_wal,
    execute_schema,
    write_transaction,
)
from kg.evidence._format import USER_VERSION, install_manifest, schema_sql
from kg.evidence._format import check as _check
from kg.evidence._sql import AccountedConnection
from kg.evidence.errors import EvidenceServiceError, storage_error


class EvidenceDatabase:
    def __init__(self, path: Path) -> None:
        self.path = path

    def _open(self, *, create: bool, budget: PrivateBudget | None = None) -> AccountedConnection:
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        uri = self.path.resolve().as_uri() + ("?mode=rwc" if create else "?mode=rw")
        connection = sqlite3.connect(uri, uri=True, factory=AccountedConnection)
        connection._budget = budget
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            row = connection.execute("PRAGMA foreign_keys").fetchone()
            if row is None or row[0] != 1:
                raise EvidenceServiceError("unsupported")
            if budget is None:
                connection.execute("PRAGMA busy_timeout=5000")
        except BaseException:
            connection.close()
            raise
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
    def connection(self, *, budget: PrivateBudget | None = None) -> Iterator[AccountedConnection]:
        try:
            connection = self._open(create=False, budget=budget)
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
