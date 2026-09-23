"""Reuse Q1's FIFO thread, without changing its requests or process supervision."""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import suppress
from threading import Event, current_thread

from kg._execution_budget import Deadline
from kg.graph._session_types import GraphFailure, GraphSessionError
from kg.query._dispatch import Dispatcher, Stopped

LOGGER = logging.getLogger(__name__)


def cleanup_log(error: BaseException) -> None:
    # A broken log handler must not strand thread-affine resource custody.
    with suppress(Exception):
        LOGGER.error("Graph session cleanup class=%s", type(error).__name__)


class _SessionOwner:
    def __init__(self, cleanup: Callable[[], bool]) -> None:
        self._cleanup = cleanup
        self._retry = Event()
        self.dispatcher = Dispatcher(self._finish)

    def check_caller(self) -> None:
        if current_thread() is self.dispatcher.thread:
            raise GraphSessionError(GraphFailure("invalid_request"))

    def check_owner(self) -> None:
        if current_thread() is not self.dispatcher.thread:
            raise GraphSessionError(GraphFailure("invalid_request"))

    def call[T](self, function: Callable[[], T], deadline: Deadline) -> T:
        self.check_caller()
        try:
            return self.dispatcher.call(function, deadline)
        except Stopped as error:
            raise GraphSessionError(GraphFailure(
                "busy" if error.reason == "admission_limit" else "closed",
            )) from None

    def _finish(self) -> None:
        while True:
            try:
                if self._cleanup():
                    return
            except Exception as error:
                cleanup_log(error)
            self._retry.wait()
            self._retry.clear()

    def close(self) -> bool:
        self.check_caller()
        if self.dispatcher.closed.is_set():
            self._retry.set()
        try:
            self.dispatcher.close()
        except Stopped:
            return False
        return True
