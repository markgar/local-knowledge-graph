"""Private observer-before-snapshot admission and fresh, short release fences."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

from pydantic import ValidationError

from kg._execution_budget import Deadline, PrivateBudget, ScratchReservation, ScratchUnit, StepMeter
from kg.evidence._authorization import authorize
from kg.evidence._format import check
from kg.evidence._sql import AccountedConnection
from kg.evidence._values import token, validated
from kg.evidence.database import EvidenceDatabase
from kg.evidence.errors import EvidenceServiceError, storage_error
from kg.models.evidence import EvidenceView, LocalIdentity
from kg.models.foundation import EvidenceRef, Scope


@dataclass(frozen=True)
class ReadSessionId:
    value: str = field(default_factory=token)


@dataclass
class CanonicalReadContext:
    identity: LocalIdentity
    scope: Scope
    session_id: ReadSessionId
    deadline: Deadline
    meter: StepMeter
    _connection: AccountedConnection
    _active: bool = field(default=True, init=False)
    _scratch: list[ScratchReservation] = field(default_factory=list, init=False, repr=False)

    def check_active(self) -> None:
        if not self._active or not self._connection.in_transaction:
            raise EvidenceServiceError("invalid_request")
        self.meter.check_deadline()

    def check_session(self, session_id: ReadSessionId) -> None:
        self.check_active()
        if session_id != self.session_id:
            raise EvidenceServiceError("invalid_request")

    def _hold_scratch(self, size_bytes: int, unit: ScratchUnit) -> None:
        self.check_active()
        self._scratch.append(self.meter.reserve_scratch(max(1, size_bytes), unit))

    def _release_scratch(self) -> None:
        for reservation in self._scratch:
            reservation.release()
        self._scratch.clear()

    @property
    def connection(self) -> AccountedConnection:
        self.check_active()
        return self._connection


class ObserverReference:
    def __init__(self, observer: ReadObserver) -> None:
        self._observer: ReadObserver | None = observer

    @property
    def observer(self) -> ReadObserver:
        if self._observer is None:
            raise EvidenceServiceError("invalid_request")
        return self._observer

    def close(self) -> None:
        if self._observer is not None:
            self._observer._release()
            self._observer = None


class ReadObserver:
    def __init__(
        self, database: EvidenceDatabase, connection: AccountedConnection,
        identity: LocalIdentity, scope: Scope, budget: PrivateBudget,
    ) -> None:
        self.database = database
        self.identity = identity
        self.scope = scope
        self.budget = budget
        self.session_id = ReadSessionId()
        self._connection = connection
        self._references = 1
        self._owner_closed = False
        self._version = self._data_version()

    def _data_version(self) -> int:
        if not self._references:
            raise EvidenceServiceError("invalid_request")
        row = self._connection.execute("PRAGMA data_version").fetchone()
        assert row is not None
        return int(row[0])

    def changed(self) -> bool:
        return self._data_version() != self._version

    def retain(self) -> ObserverReference:
        if not self._references:
            raise EvidenceServiceError("invalid_request")
        self._references += 1
        return ObserverReference(self)

    def _release(self) -> None:
        self._references -= 1
        if self._references == 0:
            self._connection.close()

    def close(self) -> None:
        if not self._owner_closed:
            self._owner_closed = True
            self._release()


@dataclass
class ReleaseFence:
    session_id: ReadSessionId
    _active: bool = True

    def check_active(self) -> None:
        if not self._active:
            raise EvidenceServiceError("invalid_request")


def _inherited(deadline: Deadline, budget: PrivateBudget) -> None:
    if budget.deadline != deadline:
        raise EvidenceServiceError("invalid_request")
    budget.check_deadline()


@contextmanager
def observe(
    database: EvidenceDatabase, identity: LocalIdentity, scope: Scope,
    deadline: Deadline, budget: PrivateBudget,
) -> Iterator[ReadObserver]:
    _inherited(deadline, budget)
    identity, scope = validated(LocalIdentity, identity), validated(Scope, scope)
    # This connection may outlive the call when a retained support/report owns a reference.
    try:
        connection = database._open(create=False, budget=budget)
    except (sqlite3.Error, OSError, ValidationError) as error:
        raise storage_error(error) from None
    observer: ReadObserver | None = None
    try:
        check(connection)
        observer = ReadObserver(database, connection, identity, scope, budget)
        authorize(connection, identity, scope, "read")
        yield observer
    except (sqlite3.Error, OSError, ValidationError) as error:
        raise storage_error(error) from None
    finally:
        if observer is not None:
            observer.close()
        else:
            connection.close()


@contextmanager
def read_context(
    database: EvidenceDatabase, identity: LocalIdentity, scope: Scope,
    session_id: ReadSessionId, deadline: Deadline, meter: StepMeter,
) -> Iterator[CanonicalReadContext]:
    _inherited(deadline, meter.private_budget)
    identity, scope = validated(LocalIdentity, identity), validated(Scope, scope)
    with database.connection(budget=meter.private_budget) as connection:
        connection.execute("BEGIN")
        context = CanonicalReadContext(identity, scope, session_id, deadline, meter, connection)
        try:
            # Recheck under the actual snapshot, not only under admission's earlier snapshot.
            check(connection)
            authorize(connection, identity, scope, "read")
            _limit_temp(connection)
            connection._controls(participant=True, read_only=True)
            yield context
        finally:
            context._active = False
            connection._controls()
            try:
                connection.rollback()
            finally:
                context._release_scratch()


def _limit_temp(connection: AccountedConnection) -> None:
    row = connection.execute("PRAGMA temp.page_size").fetchone()
    if row is None or type(row[0]) is not int or row[0] <= 0:
        raise EvidenceServiceError("unsupported")
    pages = (128 << 20) // row[0]
    actual = connection.execute(f"PRAGMA temp.max_page_count={pages}").fetchone()
    verified = connection.execute("PRAGMA temp.max_page_count").fetchone()
    if actual is None or verified is None or actual[0] != pages or verified[0] != pages:
        raise EvidenceServiceError("unsupported")


@contextmanager
def release_fence(
    observer: ReadObserver, identity: LocalIdentity, scope: Scope, deadline: Deadline,
    *, budget: PrivateBudget | None = None,
) -> Iterator[ReleaseFence]:
    budget = observer.budget if budget is None else budget
    _inherited(deadline, budget)
    identity, scope = validated(LocalIdentity, identity), validated(Scope, scope)
    if identity != observer.identity or scope != observer.scope or not observer._references:
        raise EvidenceServiceError("invalid_request")
    with observer.database.connection(budget=budget) as connection:
        connection.execute("BEGIN IMMEDIATE")
        fence = ReleaseFence(observer.session_id)
        previous_budget = observer._connection._budget
        try:
            check(connection)
            authorize(connection, identity, scope, "read")
            observer._connection._budget = budget
            if observer.changed():
                raise EvidenceServiceError("state_changed")
            connection._controls(participant=True, read_only=True)
            yield fence
        finally:
            fence._active = False
            observer._connection._budget = previous_budget
            connection._controls()
            connection.rollback()


def read_evidence(
    context: CanonicalReadContext, reference: EvidenceRef, *, state_version: str | None = None,
) -> EvidenceView:
    from kg.evidence._reads import check_token, evidence_view

    context.check_active()
    reference = validated(EvidenceRef, reference)
    if (
        reference.corpus_id != context.scope.corpus_id
        or reference.source_namespace not in context.scope.access.namespaces
    ):
        raise EvidenceServiceError("not_found")
    if reference.passage_id is not None:
        raise EvidenceServiceError("unsupported")
    if state_version is not None:
        state_version = check_token(state_version)
    lengths = context.connection.execute(
        "SELECT r.byte_length,length(r.content),length(CAST(a.quote AS BLOB)),"
        "length(CAST(m.metadata_json AS BLOB)) "
        "FROM document d JOIN revision r ON r.document_id=d.document_id "
        "JOIN anchor a ON a.document_id=d.document_id AND a.revision_id=r.revision_id "
        "JOIN document_state s ON s.document_id=d.document_id AND s.revision_id=r.revision_id "
        "JOIN anchor_set_member member ON member.set_id=s.set_id AND member.anchor_id=a.anchor_id "
        "JOIN metadata_snapshot m ON m.snapshot_id=s.metadata_snapshot_id "
        "WHERE d.corpus_id=? AND d.namespace=? AND d.document_id=? AND r.revision_id=? "
        "AND a.anchor_id=? AND s.state_version=coalesce(?,a.origin_state)",
        (
            reference.corpus_id, reference.source_namespace, reference.document_id,
            reference.revision_id, reference.anchor_id, state_version,
        ),
    ).fetchone()
    if lengths is None:
        raise EvidenceServiceError("not_found")
    if any(type(size) is not int or size < 0 for size in lengths) or lengths[0] != lengths[1]:
        raise EvidenceServiceError("internal_error")
    context.meter.reserve_public("evidence_reference")
    content_bytes, _, quote_bytes, metadata_bytes = lengths
    # UTF-8 byte lengths safely bound Python's four-byte character representation,
    # including NULs. Also prepay simultaneous validation/slicing/JSON copies.
    context._hold_scratch(4 * content_bytes, "text")
    context._hold_scratch(4 * quote_bytes, "text")
    context._hold_scratch(4 * metadata_bytes, "context")
    context._hold_scratch(content_bytes + 5 * quote_bytes + 8 * metadata_bytes, "general")
    return evidence_view(context.connection, context.scope, reference, state_version)
