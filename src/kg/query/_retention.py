"""Owner-thread support registry; serialized membership is never a live snapshot."""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from pydantic import BaseModel

from kg.evidence._read_context import ObserverReference
from kg.evidence._values import canonical
from kg.models.foundation import Scope, Value
from kg.query._dispatch import Stopped

SET_BYTES = 8 << 20
TOTAL_BYTES = 32 << 20
MAX_SETS = 32
TTL = 300
CHUNK = 4096


def utf8_size(text: str) -> int:
    return sum(
        1 if ord(char) < 128 else 2 if ord(char) < 2048 else 3 if ord(char) < 65536 else 4
        for char in text
    )


def size(value: object) -> int:
    """Exact compact UTF-8 JSON size, without a dump or encoded allocation."""
    if isinstance(value, BaseModel):
        return (
            2
            + sum(size(key) + 1 + size(getattr(value, key)) for key in type(value).model_fields)
            + max(0, len(type(value).model_fields) - 1)
        )
    if isinstance(value, dict):
        return 2 + sum(size(k) + 1 + size(v) for k, v in value.items()) + max(0, len(value) - 1)
    if isinstance(value, (tuple, list)):
        return 2 + sum(size(v) for v in value) + max(0, len(value) - 1)
    if isinstance(value, str):
        total = 2
        for char in value:
            code = ord(char)
            total += (
                2
                if char in '"\\\b\f\n\r\t'
                else 6
                if code < 32
                else 1
                if code < 128
                else 2
                if code < 2048
                else 3
                if code < 65536
                else 4
            )
        return total
    if value is None:
        return 4
    if type(value) is bool:
        return 4 if value else 5
    if isinstance(value, (int, float)):
        return len(str(value))
    raise TypeError("Unsupported retained value")


def encode(value: Value) -> str:
    return canonical(value.model_dump(mode="json"))


@dataclass
class RetainedSet:
    scope: Scope
    source_request_id: str
    read_state_id: str
    records_step_id: str
    exact: bool
    observer: ObserverReference
    members: list[str]
    payload_bytes: int
    expires: float = field(default_factory=lambda: time.monotonic() + TTL)


class Registry:
    def __init__(self) -> None:
        self.sets: dict[str, RetainedSet] = {}
        self.bytes = 0

    def expire(self) -> None:
        for key, item in tuple(self.sets.items()):
            if time.monotonic() >= item.expires:
                self.remove(key)

    def reserve(self, n: int) -> None:
        if n < 0 or self.bytes + n > TOTAL_BYTES:
            raise Stopped("budget_exceeded", "retention_limit")
        self.bytes += n

    def release(self, n: int) -> None:
        self.bytes -= n
        assert self.bytes >= 0

    def remove(self, key: str) -> None:
        item = self.sets.pop(key)
        self.release(item.payload_bytes)
        item.observer.close()

    def close(self) -> None:
        for key in tuple(self.sets):
            self.remove(key)


class Allocation:
    """Reserve simultaneous worker/IPC/parent copies before each transfer."""

    def __init__(self, registry: Registry) -> None:
        self.registry = registry
        self.held = 0

    def reserve(self, n: int) -> None:
        self.registry.reserve(n)
        self.held += n

    def close(self) -> None:
        self.registry.release(self.held)
        self.held = 0

    def publish(self, key: str, item: RetainedSet) -> None:
        self.registry.expire()
        if len(self.registry.sets) >= MAX_SETS or item.payload_bytes > SET_BYTES:
            raise Stopped("budget_exceeded", "retention_limit")
        assert self.held >= item.payload_bytes
        self.registry.sets[key] = item
        self.held -= item.payload_bytes
