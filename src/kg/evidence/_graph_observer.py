"""Private graph source custody, independent of each operation's accounting."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass
from threading import current_thread
from typing import Never, Self

from pydantic import ValidationError

from kg._execution_budget import (
    Deadline,
    DeadlineStop,
    PrivateBudget,
    PrivateResourceStop,
    StepMeter,
)
from kg.evidence._authorization import authorize
from kg.evidence._format import check
from kg.evidence._read_context import (
    CanonicalReadContext,
    ReadSessionId,
    ReleaseFence,
    _inherited,
    read_context,
)
from kg.evidence._sql import AccountedConnection
from kg.evidence._values import token, validated
from kg.evidence.database import EvidenceDatabase, _initialize_connection
from kg.evidence.errors import EvidenceServiceError, storage_error
from kg.models.evidence import LocalIdentity
from kg.models.foundation import Scope

_CONSTRUCTION_KEY = object()
_STORAGE_ERRORS = (sqlite3.Error, OSError, ValidationError)


@dataclass(frozen=True)
class GraphSourceBinding:
    source_id: str
    identity: LocalIdentity
    scope: Scope


class GraphSourceCleanupError(EvidenceServiceError):
    def __init__(
        self, source: GraphSourceObserver,
        original_failure: EvidenceServiceError | DeadlineStop | PrivateResourceStop,
        cleanup_failure: EvidenceServiceError,
    ) -> None:
        self.failure = cleanup_failure.failure
        self.source = source
        self.original_failure = original_failure
        Exception.__init__(self, str(cleanup_failure))


class GraphSourceObserver:
    def __init__(
        self, database: EvidenceDatabase, identity: LocalIdentity, scope: Scope, *,
        _key: object,
    ) -> None:
        if _key is not _CONSTRUCTION_KEY:
            raise EvidenceServiceError("invalid_request")
        self._database = database
        self._binding = GraphSourceBinding(token(), identity, scope)
        self._thread = current_thread()
        self._connection: AccountedConnection | None = None
        self._physical: AccountedConnection | None = None
        self._version: int | None = None
        self._usable = False
        self._close_confirmed = False
        self._operation: GraphSourceOperation | None = None

    def __copy__(self) -> Self:
        raise TypeError("Graph source ownership cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> Self:
        raise TypeError("Graph source ownership cannot be copied")

    def __reduce__(self) -> Never:
        raise TypeError("Graph source ownership cannot be serialized")

    @property
    def binding(self) -> GraphSourceBinding:
        if self._version is None:
            raise EvidenceServiceError("invalid_request")
        return self._binding

    def _check_thread(self) -> None:
        if current_thread() is not self._thread:
            raise EvidenceServiceError("invalid_request")

    def _live_connection(self) -> AccountedConnection:
        self._check_thread()
        connection = self._connection
        if (
            not self._usable or connection is None or connection is not self._physical
            or connection._closed
        ):
            self._usable = False
            raise EvidenceServiceError("state_changed")
        try:
            in_transaction = connection.in_transaction
        except sqlite3.ProgrammingError:
            self._usable = False
            raise EvidenceServiceError("state_changed") from None
        if in_transaction:
            self._usable = False
            raise EvidenceServiceError("state_changed")
        return connection

    @staticmethod
    def _data_version(connection: AccountedConnection) -> int:
        with closing(connection.cursor()) as cursor:
            cursor.execute("PRAGMA data_version")
            row = cursor.fetchone()
        if row is None or type(row[0]) is not int:
            raise EvidenceServiceError("internal_error")
        return int(row[0])

    @contextmanager
    def operation(
        self, binding: GraphSourceBinding, identity: LocalIdentity, scope: Scope,
        deadline: Deadline, budget: PrivateBudget,
    ) -> Iterator[GraphSourceOperation]:
        self._check_thread()
        if self._operation is not None or binding is not self._binding:
            raise EvidenceServiceError("invalid_request")
        identity, scope = validated(LocalIdentity, identity), validated(Scope, scope)
        if identity != binding.identity or scope != binding.scope:
            raise EvidenceServiceError("forbidden")
        _inherited(deadline, budget)
        connection = self._live_connection()
        operation = GraphSourceOperation(self, deadline, budget, _key=_CONSTRUCTION_KEY)
        self._operation = operation
        try:
            with connection._observer_operation_budget(budget), operation._resources:
                operation.check_current()
                try:
                    yield operation
                finally:
                    operation._active = False
        except _STORAGE_ERRORS as error:
            self._usable = False
            raise storage_error(error) from None
        finally:
            operation._active = False
            self._operation = None

    def close(self) -> None:
        self._check_thread()
        if self._operation is not None:
            raise EvidenceServiceError("invalid_request")
        self._usable = False
        if self._close_confirmed:
            return
        if self._physical is not None:
            try:
                self._physical._close_observer()
            except _STORAGE_ERRORS as error:
                raise storage_error(error) from None
        self._close_confirmed = True
        self._connection = self._physical = None


class GraphSourceOperation:
    def __init__(
        self, source: GraphSourceObserver, deadline: Deadline, budget: PrivateBudget, *,
        _key: object,
    ) -> None:
        if _key is not _CONSTRUCTION_KEY:
            raise EvidenceServiceError("invalid_request")
        self._source = source
        self._deadline = deadline
        self._budget = budget
        self._session_id = ReadSessionId()
        self._active = True
        self._aborted = False
        self._terminal = False
        self._snapshot_used = False
        self._snapshot_active = False
        self._fence_used = False
        self._fence_active = False
        self._resources = ExitStack()

    def __copy__(self) -> Self:
        raise TypeError("Graph operation ownership cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> Self:
        raise TypeError("Graph operation ownership cannot be copied")

    def __reduce__(self) -> Never:
        raise TypeError("Graph operation ownership cannot be serialized")

    @property
    def binding(self) -> GraphSourceBinding:
        return self._source.binding

    @property
    def session_id(self) -> ReadSessionId:
        return self._session_id

    @property
    def deadline(self) -> Deadline:
        return self._deadline

    @property
    def budget(self) -> PrivateBudget:
        return self._budget

    def _check_active(self) -> None:
        self._source._check_thread()
        if (
            not self._active or self._aborted or self._terminal
            or self._source._operation is not self
        ):
            raise EvidenceServiceError("invalid_request")
        self._source._live_connection()
        _inherited(self.deadline, self.budget)

    @contextmanager
    def _work(self) -> Iterator[None]:
        try:
            self._check_active()
            yield
        except _STORAGE_ERRORS as error:
            self._aborted = True
            self._source._usable = False
            raise storage_error(error) from None
        except EvidenceServiceError as error:
            self._aborted = True
            if error.failure.code in ("forbidden", "state_changed", "internal_error"):
                self._source._usable = False
            raise
        except BaseException:
            self._aborted = True
            raise

    def _poll(self) -> None:
        connection = self._source._live_connection()
        if self._source._data_version(connection) != self._source._version:
            self._source._usable = False
            raise EvidenceServiceError("state_changed")

    def check_current(self) -> None:
        with self._work():
            if self._fence_active:
                raise EvidenceServiceError("invalid_request")
            authorize(
                self._source._live_connection(), self.binding.identity, self.binding.scope, "read",
            )
            self._poll()

    @contextmanager
    def read_context(self, meter: StepMeter) -> Iterator[CanonicalReadContext]:
        with self._work():
            if (
                self._snapshot_used or self._fence_used
                or meter.private_budget is not self.budget
            ):
                raise EvidenceServiceError("invalid_request")
            self._snapshot_used = True
            with ExitStack() as snapshot:
                self._resources.callback(snapshot.close)
                context = snapshot.enter_context(read_context(
                    self._source._database, self.binding.identity, self.binding.scope,
                    self.session_id, self.deadline, meter,
                ))
                self._snapshot_active = True
                try:
                    yield context
                finally:
                    self._snapshot_active = False

    @contextmanager
    def release_fence(self) -> Iterator[ReleaseFence]:
        with self._work():
            if self._snapshot_active or self._fence_used:
                raise EvidenceServiceError("invalid_request")
            self._fence_used = True
            with ExitStack() as release:
                self._resources.callback(release.close)
                fence = release.enter_context(self._release_fence())
                yield fence
                self._check_active()
            self._terminal = True

    @contextmanager
    def _release_fence(self) -> Iterator[ReleaseFence]:
        with self._source._database.connection(budget=self.budget) as connection:
            fence = ReleaseFence(self.session_id)
            try:
                connection.execute("BEGIN IMMEDIATE")
                check(connection)
                authorize(connection, self.binding.identity, self.binding.scope, "read")
                self._poll()
                connection._controls(participant=True, read_only=True)
                self._fence_active = True
                yield fence
            finally:
                self._fence_active = False
                fence._active = False
                connection._controls()
                connection.rollback()


def open_graph_source(
    database: EvidenceDatabase, identity: LocalIdentity, scope: Scope,
    deadline: Deadline, budget: PrivateBudget,
) -> GraphSourceObserver:
    _inherited(deadline, budget)
    identity, scope = validated(LocalIdentity, identity), validated(Scope, scope)
    try:
        source = GraphSourceObserver(
            EvidenceDatabase(database.path.resolve()), identity, scope, _key=_CONSTRUCTION_KEY,
        )
    except _STORAGE_ERRORS as error:
        raise storage_error(error) from None
    try:
        try:
            source._connection = source._physical = source._database._acquire(create=False)
            _initialize_connection(source._connection, budget=budget)
            check(source._connection)
            source._version = source._data_version(source._connection)
            authorize(source._connection, identity, scope, "read")
            source._connection._park_observer()
            source._usable = True
        except _STORAGE_ERRORS as error:
            raise storage_error(error) from None
    except (EvidenceServiceError, DeadlineStop, PrivateResourceStop) as original:
        try:
            source.close()
        except EvidenceServiceError as cleanup:
            raise GraphSourceCleanupError(source, original, cleanup) from None
        raise
    except BaseException:
        source.close()
        raise
    return source
