"""Internal transaction context shared by document writes and future K1 writes."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

from kg.evidence.database import EvidenceDatabase
from kg.models.evidence import LocalIdentity


@dataclass(frozen=True)
class CanonicalWriteContext:
    connection: sqlite3.Connection
    identity: LocalIdentity


@contextmanager
def writing(database: EvidenceDatabase, identity: LocalIdentity) -> Iterator[CanonicalWriteContext]:
    with database.transaction() as connection:
        yield CanonicalWriteContext(connection, identity)
