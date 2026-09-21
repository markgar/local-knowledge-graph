"""Synchronous bounded reservation RPC; the supervisor owns every allowance."""

from __future__ import annotations

from contextlib import suppress
from multiprocessing.connection import Connection
from typing import Literal

from pydantic import Field

from kg._execution_budget import (
    Deadline,
    DeadlineStop,
    PrivateBudget,
    PrivateResourceStop,
    PublicBudgetStop,
    ScratchReservation,
    ScratchUnit,
    SemanticStage,
)
from kg.models.foundation import ErrorCode, Value

FRAME_BYTES = 64 << 10


class Frame(Value):
    action: Literal[
        "begin",
        "public",
        "visits",
        "vm",
        "quantum",
        "scratch",
        "release",
        "limited",
        "done",
        "error",
    ]
    view: int = Field(default=0, ge=0, le=100_000)
    n: int = Field(default=1, ge=1, le=64 << 20)
    stage: SemanticStage = "evidence_reference"
    unit: ScratchUnit = "general"
    code: ErrorCode | None = None


class Reply(Value):
    state: Literal["ok", "public", "private", "deadline"]
    value: int = 0


def send(connection: Connection, value: Value) -> None:
    payload = value.model_dump_json().encode()
    if len(payload) > FRAME_BYTES:
        raise PrivateResourceStop()
    connection.send_bytes(payload)


class RemoteBudget(PrivateBudget):
    def __init__(
        self,
        connection: Connection,
        deadline: Deadline,
        view: int = 0,
        parent: RemoteBudget | None = None,
    ) -> None:
        self.connection = connection
        self.deadline = deadline
        self.view = view
        self._parent = parent
        self._root = parent._root if parent else self

    def rpc(self, frame: Frame) -> int:
        self.check_deadline()
        send(self.connection, frame)
        if not self.connection.poll(self.deadline.remaining()):
            raise DeadlineStop()
        reply = Reply.model_validate_json(self.connection.recv_bytes(FRAME_BYTES))
        if reply.state == "public":
            raise PublicBudgetStop()
        if reply.state == "private":
            raise PrivateResourceStop()
        if reply.state == "deadline":
            raise DeadlineStop()
        self.check_deadline()
        return reply.value

    def limited(self, *, max_visits: int) -> PrivateBudget:
        view = self.rpc(Frame(action="limited", view=self.view, n=max_visits))
        return RemoteBudget(self.connection, self.deadline, view, self)

    def reserve_visits(self, n: int = 1) -> None:
        self.rpc(Frame(action="visits", view=self.view, n=n))

    def reserve_vm(self, instructions: int) -> None:
        self.rpc(Frame(action="vm", view=self.view, n=instructions))

    def reserve_sql_quantum(self) -> int:
        return self.rpc(Frame(action="quantum", view=self.view))

    def reserve_scratch(self, size_bytes: int, unit: ScratchUnit) -> ScratchReservation:
        key = self.rpc(Frame(action="scratch", view=self.view, n=size_bytes, unit=unit))
        return RemoteScratch(self, key)


class RemoteScratch(ScratchReservation):
    def __init__(self, budget: RemoteBudget, key: int) -> None:
        self.remote = budget
        self.key = key
        self.released = False

    def release(self) -> None:
        if not self.released:
            self.released = True
            # The supervisor reclaims outstanding handles after process cleanup.
            with suppress(DeadlineStop, EOFError, OSError):
                self.remote.rpc(Frame(action="release", view=self.key))


class RemoteStep:
    def __init__(self, budget: RemoteBudget) -> None:
        self.private_budget = budget

    def reserve_public(self, stage: SemanticStage, n: int = 1) -> None:
        self.private_budget.rpc(Frame(action="public", stage=stage, n=n))

    def reserve_visits(self, n: int = 1) -> None:
        self.private_budget.reserve_visits(n)

    def check_deadline(self) -> None:
        self.private_budget.check_deadline()

    def reserve_scratch(self, size_bytes: int, unit: ScratchUnit) -> ScratchReservation:
        return self.private_budget.reserve_scratch(size_bytes, unit)


class Ledger:
    def __init__(self, budget: PrivateBudget, max_operations: int, max_records: int) -> None:
        self.budget = budget
        self.views = {0: budget}
        self.scratch: dict[int, ScratchReservation] = {}
        self.next_scratch = 1
        self.operations = 0
        self.records = 0
        self.max_operations = max_operations
        self.max_records = max_records

    def reserve(self, frame: Frame) -> Reply:
        try:
            self.budget.check_deadline()
            value = self._reserve(frame)
            return Reply(state="ok", value=value)
        except PublicBudgetStop:
            return Reply(state="public")
        except PrivateResourceStop:
            return Reply(state="private")
        except DeadlineStop:
            return Reply(state="deadline")

    def _reserve(self, frame: Frame) -> int:
        if frame.action == "release":
            self.scratch.pop(frame.view).release()
            return 0
        view = self.views[frame.view]
        if frame.action == "begin":
            if self.operations >= self.max_operations:
                raise PublicBudgetStop()
            self.operations += 1
        elif frame.action == "public":
            if not self.operations or frame.n > 64:
                raise ValueError("Invalid semantic reservation")
            if self.records + frame.n > self.max_records:
                raise PublicBudgetStop()
            self.records += frame.n
        elif frame.action == "visits":
            view.reserve_visits(frame.n)
        elif frame.action == "vm":
            view.reserve_vm(frame.n)
        elif frame.action == "quantum":
            return view.reserve_sql_quantum()
        elif frame.action == "limited":
            if len(self.views) >= 1000:
                raise PrivateResourceStop()
            key = len(self.views)
            self.views[key] = view.limited(max_visits=frame.n)
            return key
        elif frame.action == "scratch":
            if len(self.scratch) >= 1000 or self.next_scratch > 100_000:
                raise PrivateResourceStop()
            key = self.next_scratch
            self.next_scratch += 1
            self.scratch[key] = view.reserve_scratch(frame.n, frame.unit)
            return key
        else:
            raise ValueError("Unexpected control frame")
        return 0

    def close(self) -> None:
        for reservation in self.scratch.values():
            reservation.release()
        self.scratch.clear()
