"""Local graph lifecycle values and operation-local accounting."""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Event
from typing import Literal
from uuid import uuid4

from kg._execution_budget import (
    CancelledStop,
    PrivateBudget,
    ScratchReservation,
    ScratchUnit,
    SemanticStage,
    _semantic,
)
from kg.graph._build import GraphBuildCode

type GraphState = Literal["unbuilt", "ready", "dirty", "error"]
type GraphCode = GraphBuildCode | Literal[
    "closed", "busy", "generation_invalid", "unsupported", "close_pending",
]
type Retry = Literal["none", "refresh", "finish_import_then_refresh", "reopen"]


@dataclass(frozen=True)
class GraphGeneration:
    session_id: str
    ordinal: int


@dataclass(frozen=True)
class GraphFailure:
    code: GraphCode
    retry: Retry = "none"
    diagnostic_id: str = field(default_factory=lambda: str(uuid4()))


class GraphSessionError(Exception):
    def __init__(self, failure: GraphFailure) -> None:
        self.failure = failure
        super().__init__(f"{failure.code} ({failure.diagnostic_id})")


@dataclass(frozen=True)
class SessionStatus:
    state: GraphState
    closed: bool = False
    cleanup_pending: bool = False
    last_error: GraphFailure | None = None
    cleanup_error: GraphFailure | None = None


@dataclass(frozen=True)
class RefreshResult:
    state: Literal["ready", "dirty", "error"]
    error: GraphFailure | None


class _WarmReadMeter:
    def __init__(self, budget: PrivateBudget, cancel: Event) -> None:
        self._budget, self._cancel = budget, cancel
        self._cancelled = False
        self.counts: dict[SemanticStage, int] = {}

    @property
    def private_budget(self) -> PrivateBudget:
        return self._budget

    def check_deadline(self) -> None:
        self._cancelled |= self._cancel.is_set()
        if self._cancelled:
            raise CancelledStop()
        self._budget.check_deadline()

    def reserve_public(self, stage: SemanticStage, n: int = 1) -> None:
        _semantic(stage, n)
        self.check_deadline()
        self.counts[stage] = self.counts.get(stage, 0) + n

    def reserve_visits(self, n: int = 1) -> None:
        self.check_deadline()
        self._budget.reserve_visits(n)

    def reserve_scratch(self, size_bytes: int, unit: ScratchUnit) -> ScratchReservation:
        self.check_deadline()
        return self._budget.reserve_scratch(size_bytes, unit)
