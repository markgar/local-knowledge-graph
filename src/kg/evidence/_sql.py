"""Connection-local accounting and owner controls, never a request SQL API."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Protocol, Self, overload

from kg._execution_budget import DeadlineStop, PrivateBudget, PrivateResourceStop
from kg.evidence.errors import EvidenceServiceError


class ParameterSequence(Protocol):
    def __len__(self) -> int: ...
    def __getitem__(self, index: int, /) -> object: ...


Parameters = ParameterSequence | Mapping[str, object]


class AccountedCursor(sqlite3.Cursor):
    connection: AccountedConnection

    def execute(self, sql: str, parameters: Parameters = ()) -> Self:
        self.connection._before_statement()
        try:
            super().execute(sql, parameters)
        except sqlite3.Error as error:
            self.connection._raise_stop(error)
            raise
        return self

    def executemany(self, sql: str, seq_of_parameters: Iterable[Parameters]) -> Self:
        if self.connection._budget is None:
            self.connection._before_statement()
            super().executemany(sql, seq_of_parameters)
        else:
            for parameters in seq_of_parameters:
                self.execute(sql, parameters)
        return self

    def _visit(self) -> None:
        self.connection._check_active()
        if self.connection._budget is not None:
            self.connection._budget.reserve_visits()

    def fetchone(self) -> Any:
        # Reserve before fetching/decoding, including bounded EOF lookahead.
        self._visit()
        try:
            return super().fetchone()
        except sqlite3.Error as error:
            self.connection._raise_stop(error)
            raise

    def fetchmany(self, size: int | None = None) -> list[Any]:
        size = self.arraysize if size is None else size
        if size < 0:
            raise ValueError("Unbounded fetch size")
        rows = []
        for _ in range(size):
            row = self.fetchone()
            if row is None:
                break
            rows.append(row)
        return rows

    def fetchall(self) -> list[Any]:
        return list(self)

    def __iter__(self) -> Self:
        return self

    def __next__(self) -> Any:
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row


class AccountedConnection(sqlite3.Connection):
    _budget: PrivateBudget | None = None
    _stop: DeadlineStop | PrivateResourceStop | None = None
    _closed = False
    _participant = False
    _read_only = False
    _precise = False
    _remaining = 0

    def _park_observer(self) -> None:
        try:
            self.set_progress_handler(None, 0)
        finally:
            self._stop = None
            self._remaining = 0
            self._precise = False
            self._budget = None

    @contextmanager
    def _observer_operation_budget(self, budget: PrivateBudget) -> Iterator[None]:
        self._check_active()
        if self.in_transaction or self._budget is not None:
            raise EvidenceServiceError("invalid_request")
        budget.check_deadline()
        self._budget = budget
        try:
            yield
        finally:
            self._park_observer()

    def _close_observer(self) -> None:
        if self._participant:
            raise EvidenceServiceError("invalid_request")
        self._closed = True
        self._budget = None
        # A previous attempt may have closed SQLite without confirming disposal.
        sqlite3.Connection.close(self)

    @contextmanager
    def _precise_progress(self) -> Iterator[None]:
        """Count nested PRAGMA VMs without losing each substatement's partial quantum."""
        self._check_active()
        previous = self._precise
        self._precise = True
        try:
            yield
        finally:
            self._precise = previous

    @contextmanager
    def _using_budget(self, budget: PrivateBudget) -> Iterator[None]:
        self._check_active()
        previous = self._budget
        if previous is None or not budget.inherits(previous):
            raise EvidenceServiceError("invalid_request")
        budget.check_deadline()
        self._budget = budget
        try:
            yield
        finally:
            self._budget = previous

    def _check_active(self) -> None:
        if self._closed:
            raise EvidenceServiceError("invalid_request")
        if self._budget is not None:
            self._budget.check_deadline()

    def _before_statement(self) -> None:
        self._check_active()
        if self._budget is not None:
            maximum = 100 if self._budget.resource_profile == "graph-build/1" else 5000
            milliseconds = max(0, min(maximum, int(self._budget._remaining() * 1000)))
            self._prepay_statement()
            try:
                super().execute(f"PRAGMA busy_timeout={milliseconds}")
            except sqlite3.Error as error:
                self._raise_stop(error)
                raise
            self._prepay_statement()

    def _prepay_statement(self) -> None:
        assert self._budget is not None
        try:
            quantum = self._budget.reserve_sql_quantum()
        except (DeadlineStop, PrivateResourceStop) as error:
            self._remaining = 0
            self._stop = error
            raise
        if self._precise:
            self._remaining = quantum
            self.set_progress_handler(self._precise_step, 1)
        else:
            self.set_progress_handler(self._progress, quantum)

    def _precise_step(self) -> int:
        if self._stop is not None:
            return 1
        self._remaining -= 1
        if self._remaining > 0:
            return 0
        try:
            self._prepay_statement()
            return 0
        except (DeadlineStop, PrivateResourceStop) as error:
            self._stop = error
            return 1

    def _progress(self) -> int:
        assert self._budget is not None
        try:
            quantum = self._budget.reserve_sql_quantum()
            self.set_progress_handler(self._progress, quantum)
            return 0
        except (DeadlineStop, PrivateResourceStop) as error:
            self._stop = error
            return 1

    def _raise_stop(self, error: sqlite3.Error) -> None:
        if self._stop is not None:
            raise self._stop
        if self._budget is not None:
            self._budget.check_deadline()
            if getattr(error, "sqlite_errorcode", None) in (
                sqlite3.SQLITE_FULL, sqlite3.SQLITE_TOOBIG,
            ):
                self._budget._resource_stop()

    def _authorize(
        self, action: int, arg1: str | None, arg2: str | None,
        database: str | None, trigger: str | None,
    ) -> int:
        if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
            return sqlite3.SQLITE_DENY
        if self._participant and action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT):
            return sqlite3.SQLITE_DENY
        if self._read_only:
            if action == sqlite3.SQLITE_PRAGMA and arg2 is not None and arg1 != "busy_timeout":
                return sqlite3.SQLITE_DENY
            allowed = {
                sqlite3.SQLITE_SELECT, sqlite3.SQLITE_READ, sqlite3.SQLITE_FUNCTION,
                sqlite3.SQLITE_RECURSIVE, sqlite3.SQLITE_PRAGMA,
            }
            if action not in allowed and database != "temp":
                return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def _controls(self, *, participant: bool = False, read_only: bool = False) -> None:
        self._participant = participant
        self._read_only = read_only
        self.set_authorizer(self._authorize)

    def execute(self, sql: str, parameters: Parameters = ()) -> AccountedCursor:
        return self.cursor().execute(sql, parameters)

    def executemany(self, sql: str, seq_of_parameters: Iterable[Parameters]) -> AccountedCursor:
        return self.cursor().executemany(sql, seq_of_parameters)

    def executescript(self, sql_script: str) -> sqlite3.Cursor:
        raise EvidenceServiceError("invalid_request")

    @overload
    def cursor(self, factory: None = None) -> AccountedCursor: ...

    @overload
    def cursor[C: sqlite3.Cursor](self, factory: Callable[[sqlite3.Connection], C]) -> C: ...

    def cursor(
        self, factory: Callable[[sqlite3.Connection], sqlite3.Cursor] | None = None,
    ) -> sqlite3.Cursor:
        self._check_active()
        cursor = super().cursor(factory or AccountedCursor)
        if not isinstance(cursor, AccountedCursor):
            cursor.close()
            raise EvidenceServiceError("invalid_request")
        return cursor

    def commit(self) -> None:
        if self._participant or self._read_only:
            raise EvidenceServiceError("invalid_request")
        # The owner commits immediately after acknowledgement, without a deadline/claim recheck.
        self.set_progress_handler(None, 0)
        super().commit()

    def rollback(self) -> None:
        if self._participant or self._read_only:
            raise EvidenceServiceError("invalid_request")
        self.set_progress_handler(None, 0)
        super().rollback()

    def close(self) -> None:
        if self._participant:
            raise EvidenceServiceError("invalid_request")
        self._closed = True
        self.set_progress_handler(None, 0)
        super().close()
