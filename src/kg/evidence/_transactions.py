"""Internal transaction context shared by document writes and future K1 writes."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Literal

from kg._execution_budget import Deadline, PrivateBudget
from kg.evidence._format import check
from kg.evidence._sql import AccountedConnection
from kg.evidence._values import validated
from kg.evidence.database import EvidenceDatabase
from kg.evidence.errors import LOGGER, EvidenceServiceError
from kg.models.evidence import LocalIdentity


@dataclass
class CanonicalWriteContext:
    _connection: AccountedConnection
    identity: LocalIdentity
    active: bool = field(default=True, init=False)
    commit_outcome: Literal[
        "not_attempted", "confirmed_committed", "confirmed_rolled_back", "unknown",
    ] = field(default="not_attempted", init=False)

    def check_active(self) -> None:
        if not self.active or not self._connection.in_transaction:
            raise EvidenceServiceError("invalid_request")

    @contextmanager
    def using_budget(self, budget: PrivateBudget) -> Iterator[None]:
        self.check_active()
        with self._connection._using_budget(budget):
            yield

    @property
    def connection(self) -> AccountedConnection:
        self.check_active()
        return self._connection


@contextmanager
def writing(
    database: EvidenceDatabase, identity: LocalIdentity, *, deadline: Deadline | None = None,
    budget: PrivateBudget | None = None,
) -> Iterator[CanonicalWriteContext]:
    identity = validated(LocalIdentity, identity)
    if budget is not None:
        if deadline is not None and deadline != budget.deadline:
            raise EvidenceServiceError("invalid_request")
        budget.check_deadline()
    elif deadline is not None:
        budget = PrivateBudget(deadline)
    with database.connection(budget=budget) as connection:
        connection.execute("BEGIN IMMEDIATE")
        context = CanonicalWriteContext(connection, identity)
        try:
            check(connection)
            connection._controls(participant=True)
            try:
                yield context
            finally:
                context.active = False
                connection._controls()
            context.commit_outcome = "unknown"
            connection.commit()
            context.commit_outcome = "confirmed_committed"
        except BaseException:
            try:
                connection.rollback()
                if context.commit_outcome == "not_attempted":
                    context.commit_outcome = "confirmed_rolled_back"
            except BaseException as rollback_error:
                context.commit_outcome = "unknown"
                LOGGER.error("Canonical rollback failed class=%s", type(rollback_error).__name__)
            raise
        finally:
            context.active = False
