"""Fixed Q1 authorization using shared E1 checks, never an independent resolver."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from kg._execution_budget import PrivateBudget
from kg.diagnostics._targets import AuthorizationBinding, EvidenceTarget
from kg.evidence._authorization import authorize
from kg.evidence._diagnostic_authorization import EvidenceReportAuthorizer
from kg.evidence._read_context import ObserverReference, ReleaseFence, release_fence
from kg.evidence.database import EvidenceDatabase
from kg.evidence.errors import EvidenceServiceError


class QueryAuthorizer:
    """Runs only on the service's owner thread, including retained lookups."""

    def __init__(self, database: EvidenceDatabase) -> None:
        self.database = database
        self.targets = EvidenceReportAuthorizer(database)
        self.lookup_budget: PrivateBudget | None = None
        self.active: (
            tuple[
                tuple[AuthorizationBinding, ...],
                tuple[ObserverReference, ...],
                ReleaseFence,
            ]
            | None
        ) = None

    @contextmanager
    def release(
        self,
        bindings: tuple[AuthorizationBinding, ...],
        observers: tuple[ObserverReference, ...],
        budget: PrivateBudget,
    ) -> Iterator[ReleaseFence]:
        if not bindings or not observers:
            raise EvidenceServiceError("forbidden")
        with self.database.connection(budget=budget) as connection:
            connection.execute("BEGIN")
            try:
                for binding in bindings:
                    authorize(connection, binding.identity, binding.scope, binding.required)
                    for target in binding.targets:
                        if not isinstance(target, EvidenceTarget):
                            raise EvidenceServiceError("unsupported")
                        self.targets.authorize_target(connection, binding, target)
            finally:
                connection.rollback()
        first = observers[0].observer
        with release_fence(
            first,
            first.identity,
            first.scope,
            budget.deadline,
            budget=budget,
        ) as fence:
            for binding in bindings:
                if binding.identity != first.identity or binding.scope != first.scope:
                    raise EvidenceServiceError("forbidden")
            for reference in observers:
                observer = reference.observer
                if (
                    observer.database.path != self.database.path
                    or observer.identity != first.identity
                    or observer.scope != first.scope
                ):
                    raise EvidenceServiceError("forbidden")
                previous = observer._connection._budget
                try:
                    observer._connection._budget = budget
                    if observer.changed():
                        raise EvidenceServiceError("state_changed")
                finally:
                    observer._connection._budget = previous
            self.active = (bindings, observers, fence)
            try:
                yield fence
            finally:
                self.active = None

    @contextmanager
    def fence(
        self,
        bindings: tuple[AuthorizationBinding, ...],
        observers: tuple[ObserverReference, ...],
    ) -> Iterator[ReleaseFence]:
        if self.active is not None:
            expected_bindings, expected_observers, fence = self.active
            if any(binding not in expected_bindings for binding in bindings) or any(
                observer not in expected_observers for observer in observers
            ):
                raise EvidenceServiceError("forbidden")
            fence.check_active()
            yield fence
        else:
            if self.lookup_budget is None:
                raise EvidenceServiceError("invalid_request")
            with self.release(bindings, observers, self.lookup_budget) as fence:
                yield fence
