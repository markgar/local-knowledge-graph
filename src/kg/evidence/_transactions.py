"""Internal transaction context shared by document writes and future K1 writes."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from kg.evidence._values import validated
from kg.evidence.database import EvidenceDatabase
from kg.models.evidence import LocalIdentity


@dataclass
class CanonicalWriteContext:
    connection: sqlite3.Connection
    identity: LocalIdentity
    active: bool = field(default=True, init=False)


@contextmanager
def writing(database: EvidenceDatabase, identity: LocalIdentity) -> Iterator[CanonicalWriteContext]:
    identity = validated(LocalIdentity, identity)
    with database.transaction() as connection:
        context = CanonicalWriteContext(connection, identity)
        try:
            yield context
        finally:
            context.active = False
