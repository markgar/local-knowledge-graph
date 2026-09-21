from __future__ import annotations

import logging
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from importlib import resources
from pathlib import Path

from kg._sqlite import execute_schema, legacy_format, write_transaction
from kg.aliases import matches_alias

LOGGER = logging.getLogger(__name__)


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path

    def connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        try:
            initialized = legacy_format(connection)
            connection.row_factory = sqlite3.Row
            connection.create_function("kg_matches_alias", 2, matches_alias, deterministic=True)
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("PRAGMA busy_timeout = 5000")
            if initialized:
                connection.execute("PRAGMA journal_mode = WAL")
        except BaseException:
            connection.close()
            raise
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        with self.connection() as connection:
            schema = resources.files("kg").joinpath("schema.sql")
            with write_transaction(connection):
                legacy_format(connection)
                execute_schema(connection, schema.read_text(encoding="utf-8"))
            connection.execute("PRAGMA journal_mode=WAL")
            LOGGER.debug("Initialized database schema from schema.sql")

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            legacy_format(connection)
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
