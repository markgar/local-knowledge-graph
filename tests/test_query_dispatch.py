import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from kg._execution_budget import Deadline, DeadlineStop
from kg.query._dispatch import Dispatcher, Stopped


def wait_for(predicate):
    until = time.monotonic() + 3
    while not predicate():
        assert time.monotonic() < until
        time.sleep(0.005)


def test_fifo_one_active_eight_waiters_and_queued_expiry_no_dispatch():
    gate, started = Event(), Event()
    dispatcher = Dispatcher(lambda: None)
    calls = []

    def active():
        started.set()
        gate.wait(5)

    try:
        with ThreadPoolExecutor(max_workers=10) as pool:
            first = pool.submit(dispatcher.call, active, Deadline(time.monotonic() + 5))
            assert started.wait(2)
            pending = []
            for index in range(8):
                pending.append(
                    pool.submit(
                        dispatcher.call,
                        lambda i=index: calls.append(i),
                        Deadline(time.monotonic() + 5),
                    )
                )
                wait_for(lambda i=index: len(dispatcher.queue) == i + 1)
            with pytest.raises(Stopped) as error:
                dispatcher.call(lambda: None, Deadline(time.monotonic() + 5))
            assert error.value.reason == "admission_limit"
            gate.set()
            first.result()
            for task in pending:
                task.result()
        assert calls == list(range(8))
    finally:
        gate.set()
        dispatcher.close()


def test_queued_deadline_removes_waiter_without_executing_it():
    gate, started = Event(), Event()
    dispatcher = Dispatcher(lambda: None)
    calls = []

    def active():
        started.set()
        gate.wait(5)

    try:
        with ThreadPoolExecutor() as pool:
            first = pool.submit(dispatcher.call, active, Deadline(time.monotonic() + 5))
            assert started.wait(2)
            with pytest.raises(DeadlineStop):
                dispatcher.call(lambda: calls.append(True), Deadline(time.monotonic() + 0.02))
            assert not calls and not dispatcher.queue
            gate.set()
            first.result()
    finally:
        gate.set()
        dispatcher.close()
