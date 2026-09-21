"""Private inherited execution allowances and the shared semantic-meter ABI."""

from __future__ import annotations

import math
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from threading import Lock
from typing import Literal, Protocol, Self

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

    def __init__(self, deadline: Deadline) -> None:
        self.deadline = deadline
        self._lock = Lock()
        self._visits = 0
        self._vm = 0
        self._scratch = 0
        self._root = self
        self._parent: PrivateBudget | None = None
        self._max_visits = 100_000

    def limited(self, *, max_visits: int) -> PrivateBudget:
        """Create a cumulative local visit cap, never another execution pool."""
        _quantity(max_visits, maximum=100_000)
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
        if not self._lock.acquire(timeout=self.deadline.remaining()):
            raise DeadlineStop()
        try:
            self.check_deadline()
            yield
        finally:
            self._lock.release()

    def check_deadline(self) -> None:
        self.deadline.remaining()

    def reserve_visits(self, n: int = 1) -> None:
        _quantity(n)
        with self._locked():
            current: PrivateBudget | None = self
            while current is not None:
                if current._visits + n > current._max_visits:
                    raise PrivateResourceStop()
                current = current._parent
            current = self
            while current is not None:
                current._visits += n
                current = current._parent

    def reserve_vm(self, instructions: int) -> None:
        _quantity(instructions)
        with self._locked():
            if self._root._vm + instructions > 10_000_000:
                raise PrivateResourceStop()
            self._root._vm += instructions

    def reserve_sql_quantum(self) -> int:
        with self._locked():
            quantum = min(1000, 10_000_000 - self._root._vm)
            if not quantum:
                raise PrivateResourceStop()
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
                raise PrivateResourceStop()
            self._root._scratch += size_bytes
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
            raise PrivateResourceStop()
        return self.reserve_scratch(size_bytes, unit)


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
        _quantity(n, maximum=64)
        if stage not in {
            "resolve_entity", "decision_record", "evidence_reference", "search_temp",
            "search_lexical", "search_vector", "search_rerank",
            "search_final_evidence", "support_member",
        }:
            raise ValueError("Unknown semantic stage")
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
