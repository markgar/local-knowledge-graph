"""FIFO owner-thread dispatch keeps retained SQLite observers thread-affine."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from concurrent.futures import Future, TimeoutError
from threading import Condition, Event, Thread

from kg._execution_budget import Deadline, DeadlineStop
from kg.models.foundation import ErrorCode
from kg.models.query import StopReason


class Stopped(Exception):
    def __init__(self, code: ErrorCode, reason: StopReason) -> None:
        self.code, self.reason = code, reason
        super().__init__(reason)


class Dispatcher:
    def __init__(self, cleanup: Callable[[], None]) -> None:
        self.condition = Condition()
        self.closed = Event()
        self.queue: deque[Callable[[], None]] = deque()
        self.active = False
        self.cleanup = cleanup
        self.thread = Thread(target=self._loop, name="kg-query-owner", daemon=True)
        self.thread.start()

    def call[T](self, function: Callable[[], T], deadline: Deadline) -> T:
        future: Future[T] = Future()

        def invoke() -> None:
            if not future.set_running_or_notify_cancel():
                return
            try:
                deadline.remaining()
                if self.closed.is_set():
                    raise Stopped("unsupported", "service_closed")
                future.set_result(function())
            except Exception as error:
                future.set_exception(error)

        with self.condition:
            if self.closed.is_set():
                raise Stopped("unsupported", "service_closed")
            if len(self.queue) >= (8 if self.active else 9):
                raise Stopped("budget_exceeded", "admission_limit")
            self.queue.append(invoke)
            self.condition.notify()
        try:
            return future.result(timeout=deadline.remaining())
        except (TimeoutError, DeadlineStop):
            if future.cancel():
                with self.condition:
                    if invoke in self.queue:
                        self.queue.remove(invoke)
                raise DeadlineStop() from None
            # Only the owner can decide release versus redaction for an active call.
            return future.result()

    def _loop(self) -> None:
        try:
            while True:
                with self.condition:
                    while not self.queue and not self.closed.is_set():
                        self.condition.wait()
                    if not self.queue:
                        return
                    function = self.queue.popleft()
                    self.active = True
                try:
                    function()
                finally:
                    with self.condition:
                        self.active = False
        finally:
            self.cleanup()

    def close(self) -> None:
        with self.condition:
            self.closed.set()
            self.condition.notify_all()
        self.thread.join(timeout=32)
        if self.thread.is_alive():
            raise Stopped("internal_error", "internal_error")
