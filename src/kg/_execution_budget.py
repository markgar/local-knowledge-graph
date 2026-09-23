"""Private inherited execution allowances and the shared semantic-meter ABI."""

from __future__ import annotations

import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Event, Lock
from typing import Literal, NoReturn, Protocol, Self

SemanticStage = Literal[
    "resolve_entity", "decision_record", "evidence_reference", "search_temp",
    "search_lexical", "search_vector", "search_rerank", "search_final_evidence", "support_member",
]
StopKind = Literal["eligible_eof", "public_budget_stop", "private_resource_stop", "deadline_stop"]
ScratchUnit = Literal["general", "text", "context", "vector", "reranker"]


class PublicBudgetStop(Exception):
    pass


class PrivateResourceStop(Exception):
    pass


class CancelledStop(PrivateResourceStop):
    pass


class DeadlineStop(Exception):
    pass


@dataclass(frozen=True)
class Deadline:
    expires_at_monotonic: float

    def __post_init__(self) -> None:
        if not math.isfinite(self.expires_at_monotonic):
            raise ValueError("Deadline must be finite")

    def remaining(self) -> float:
        remaining = self.expires_at_monotonic - time.monotonic()
        if remaining <= 0:
            raise DeadlineStop()
        return remaining


def _quantity(value: int, *, maximum: int | None = None) -> None:
    if type(value) is not int or value < 1 or (maximum is not None and value > maximum):
        raise ValueError("Invalid reservation")


def _semantic(stage: SemanticStage, n: int) -> None:
    _quantity(n, maximum=64)
    if stage not in {
        "resolve_entity", "decision_record", "evidence_reference", "search_temp",
        "search_lexical", "search_vector", "search_rerank",
        "search_final_evidence", "support_member",
    }:
        raise ValueError("Unknown semantic stage")


class ScratchReservation:
    def __init__(self, budget: PrivateBudget, size_bytes: int) -> None:
        self._budget = budget
        self._size = size_bytes
        self._released = False

    def __copy__(self) -> Self:
        raise TypeError("Scratch ownership cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> Self:
        raise TypeError("Scratch ownership cannot be copied")

    def release(self) -> None:
        # Cleanup is allowed after deadline; no caller can obtain a new reservation then.
        with self._budget._lock:
            if not self._released:
                self._budget._scratch -= self._size
                self._released = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.release()


class PrivateBudget:
    """One local pool per invocation; limited views retain that pool.

    Q1 owns the process-safe implementation of these operations, not serialized
    copies of this local implementation or fresh per-step limits.
    """

    _profile: Literal["interactive/1", "graph-build/1"] = "interactive/1"

    def __init__(self, deadline: Deadline) -> None:
        self.deadline = deadline
        self._lock = Lock()
        self._visits = 0
        self._vm = 0
        self._scratch = 0
        self._scratch_peak = 0
        self._semantic_items = 0
        self._cancel: Event | None = None
        self._stop_reason: Literal["cancelled", "deadline", "resource"] | None = None
        self._root = self
        self._parent: PrivateBudget | None = None
        self._max_visits: int | None = 100_000
        self._max_vm: int | None = 10_000_000

    @property
    def resource_profile(self) -> Literal["interactive/1", "graph-build/1"]:
        return self._root._profile

    def limited(self, *, max_visits: int) -> PrivateBudget:
        """Create a cumulative local visit cap, never another execution pool."""
        _quantity(max_visits, maximum=100_000)
        return self._child(max_visits)

    def _child(self, max_visits: int | None) -> PrivateBudget:
        self.check_deadline()
        child = object.__new__(PrivateBudget)
        child.deadline = self.deadline
        child._lock = self._lock
        child._root = self._root
        child._parent = self
        child._max_visits = max_visits
        child._visits = 0
        return child

    def inherits(self, budget: PrivateBudget) -> bool:
        current: PrivateBudget | None = self
        while current is not None:
            if current is budget:
                return True
            current = current._parent
        return False

    def __copy__(self) -> Self:
        raise TypeError("Execution pools must be inherited, not copied")

    def __deepcopy__(self, memo: dict[int, object]) -> Self:
        raise TypeError("Execution pools must be inherited, not copied")

    @contextmanager
    def _locked(self) -> Iterator[None]:
        if self.resource_profile == "graph-build/1":
            while True:
                self.check_deadline()
                if self._lock.acquire(timeout=min(.05, self._remaining())):
                    break
        elif not self._lock.acquire(timeout=self.deadline.remaining()):
            raise DeadlineStop()
        try:
            self.check_deadline()
            yield
        finally:
            self._lock.release()

    def check_deadline(self) -> None:
        if self.resource_profile == "interactive/1":
            self.deadline.remaining()
            return
        root = self._root
        if root._stop_reason is None:
            if root._cancel is not None and root._cancel.is_set():
                root._stop_reason = "cancelled"
            else:
                try:
                    self.deadline.remaining()
                except DeadlineStop:
                    root._stop_reason = "deadline"
        if root._stop_reason == "cancelled":
            raise CancelledStop()
        if root._stop_reason == "deadline":
            raise DeadlineStop()
        if root._stop_reason == "resource":
            raise PrivateResourceStop()

    def _remaining(self) -> float:
        self.check_deadline()
        try:
            return self.deadline.remaining()
        except DeadlineStop:
            if self.resource_profile == "graph-build/1":
                self._root._stop_reason = "deadline"
            raise

    def _resource_stop(self) -> NoReturn:
        if self.resource_profile == "graph-build/1":
            self.check_deadline()
            self._root._stop_reason = "resource"
        raise PrivateResourceStop()

    def reserve_visits(self, n: int = 1) -> None:
        _quantity(n)
        with self._locked():
            current: PrivateBudget | None = self
            while current is not None:
                if current._max_visits is not None and current._visits + n > current._max_visits:
                    self._resource_stop()
                current = current._parent
            current = self
            while current is not None:
                current._visits += n
                current = current._parent

    def reserve_vm(self, instructions: int) -> None:
        _quantity(instructions)
        with self._locked():
            maximum = self._root._max_vm
            if maximum is not None and self._root._vm + instructions > maximum:
                self._resource_stop()
            self._root._vm += instructions

    def reserve_sql_quantum(self) -> int:
        with self._locked():
            maximum = self._root._max_vm
            quantum = 1000 if maximum is None else min(1000, maximum - self._root._vm)
            if not quantum:
                self._resource_stop()
            self._root._vm += quantum
            return quantum

    def reserve_scratch(self, size_bytes: int, unit: ScratchUnit) -> ScratchReservation:
        _quantity(size_bytes)
        limits = {
            "general": 64 << 20, "text": 8 << 20, "context": 8 << 20,
            "vector": 16 << 20, "reranker": 8 << 20,
        }
        if unit not in limits:
            raise ValueError("Unknown scratch unit")
        with self._locked():
            if size_bytes > limits[unit] or self._root._scratch + size_bytes > 64 << 20:
                self._resource_stop()
            self._root._scratch += size_bytes
            self._root._scratch_peak = max(self._root._scratch_peak, self._root._scratch)
            return ScratchReservation(self._root, size_bytes)

    def reserve_provider(
        self, size_bytes: int, *, unit: Literal["vector", "reranker"],
        sequences: int, padded_token_positions: int,
    ) -> ScratchReservation:
        _quantity(sequences)
        _quantity(padded_token_positions)
        if unit not in ("vector", "reranker"):
            raise ValueError("Unknown provider scratch unit")
        if sequences > 8 or padded_token_positions > 8192:
            self._resource_stop()
        return self.reserve_scratch(size_bytes, unit)


def _selection_budget(budget: PrivateBudget) -> PrivateBudget:
    if budget.resource_profile == "graph-build/1":
        return budget._child(None)
    return budget.limited(max_visits=10_000)


@dataclass(frozen=True)
class PublicAccounting:
    operations_executed: int
    items_consumed: int


class StepMeter(Protocol):
    @property
    def private_budget(self) -> PrivateBudget: ...
    def reserve_public(self, stage: SemanticStage, n: int = 1) -> None: ...
    def reserve_visits(self, n: int = 1) -> None: ...
    def check_deadline(self) -> None: ...
    def reserve_scratch(self, size_bytes: int, unit: ScratchUnit) -> ScratchReservation: ...


class ExecutionMeter(Protocol):
    def begin_step(self, step_id: str) -> StepMeter: ...
    def public_accounting(self) -> PublicAccounting: ...


@dataclass(frozen=True)
class _BulkReadAccounting:
    profile: Literal["graph-build/1"]
    expires_at_monotonic: float
    visits_reserved: int
    vm_instructions_reserved: int
    semantic_items_reserved: int
    scratch_live_bytes: int
    scratch_peak_bytes: int
    stop_reason: Literal["cancelled", "deadline", "resource"] | None


@dataclass(frozen=True)
class _BulkReadOperation:
    budget: PrivateBudget
    meter: StepMeter
    cancel: Event

    def snapshot(self) -> _BulkReadAccounting:
        # Diagnostics and cleanup remain available after the operation stops.
        with self.budget._lock:
            return _BulkReadAccounting(
                profile="graph-build/1",
                expires_at_monotonic=self.budget.deadline.expires_at_monotonic,
                visits_reserved=self.budget._visits,
                vm_instructions_reserved=self.budget._vm,
                semantic_items_reserved=self.budget._semantic_items,
                scratch_live_bytes=self.budget._scratch,
                scratch_peak_bytes=self.budget._scratch_peak,
                stop_reason=self.budget._stop_reason,
            )


@dataclass(frozen=True)
class _BulkStep:
    private_budget: PrivateBudget

    def reserve_public(self, stage: SemanticStage, n: int = 1) -> None:
        _semantic(stage, n)
        with self.private_budget._locked():
            self.private_budget._semantic_items += n

    def reserve_visits(self, n: int = 1) -> None:
        self.private_budget.reserve_visits(n)

    def check_deadline(self) -> None:
        self.private_budget.check_deadline()

    def reserve_scratch(self, size_bytes: int, unit: ScratchUnit) -> ScratchReservation:
        return self.private_budget.reserve_scratch(size_bytes, unit)


def _graph_build_operation(*, deadline: Deadline, cancel: Event) -> _BulkReadOperation:
    if not isinstance(deadline, Deadline) or not isinstance(cancel, Event):
        raise ValueError("Invalid graph build operation")
    if deadline.remaining() > 300:
        raise ValueError("Graph build deadline exceeds 300 seconds")
    budget = PrivateBudget(deadline)
    budget._profile = "graph-build/1"
    budget._max_visits = None
    budget._max_vm = None
    budget._cancel = cancel
    budget.check_deadline()
    return _BulkReadOperation(budget, _BulkStep(budget), cancel)


class BudgetedStep:
    """Delegate semantic charges unchanged while narrowing private work."""

    def __init__(self, meter: StepMeter, budget: PrivateBudget) -> None:
        if not budget.inherits(meter.private_budget):
            raise ValueError("Budget must inherit the current allowance")
        self._meter = meter
        self._budget = budget

    @property
    def private_budget(self) -> PrivateBudget:
        return self._budget

    def reserve_public(self, stage: SemanticStage, n: int = 1) -> None:
        self._budget.check_deadline()
        self._meter.reserve_public(stage, n)

    def reserve_visits(self, n: int = 1) -> None:
        self._budget.reserve_visits(n)

    def check_deadline(self) -> None:
        self._budget.check_deadline()

    def reserve_scratch(self, size_bytes: int, unit: ScratchUnit) -> ScratchReservation:
        return self._budget.reserve_scratch(size_bytes, unit)


class LocalExecutionMeter:
    """Standalone implementation; there are no charges for readiness/fusion/count."""

    def __init__(self, budget: PrivateBudget, *, max_operations: int, max_items: int) -> None:
        _quantity(max_operations)
        _quantity(max_items)
        self._budget = budget
        self._max_operations = max_operations
        self._max_items = max_items
        self._operations = 0
        self._items = 0

    def begin_step(self, step_id: str) -> StepMeter:
        if not step_id:
            raise ValueError("Missing step identity")
        with self._budget._locked():
            if self._operations == self._max_operations:
                raise PublicBudgetStop()
            self._operations += 1
        return _LocalStep(self, step_id)

    def public_accounting(self) -> PublicAccounting:
        return PublicAccounting(self._operations, self._items)


class _LocalStep:
    def __init__(self, execution: LocalExecutionMeter, step_id: str) -> None:
        self._execution = execution
        self._step_id = step_id

    @property
    def private_budget(self) -> PrivateBudget:
        return self._execution._budget

    def reserve_public(self, stage: SemanticStage, n: int = 1) -> None:
        _semantic(stage, n)
        with self.private_budget._locked():
            if self._execution._items + n > self._execution._max_items:
                raise PublicBudgetStop()
            self._execution._items += n

    def reserve_visits(self, n: int = 1) -> None:
        self.private_budget.reserve_visits(n)

    def check_deadline(self) -> None:
        self.private_budget.check_deadline()

    def reserve_scratch(self, size_bytes: int, unit: ScratchUnit) -> ScratchReservation:
        return self.private_budget.reserve_scratch(size_bytes, unit)
