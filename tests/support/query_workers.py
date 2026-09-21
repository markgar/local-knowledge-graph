"""Controlled spawn targets for query supervision tests, not service adapters."""

import os
import struct
import time
from contextlib import contextmanager

from kg.evidence import EvidenceDatabase
from kg.evidence._read_context import read_context, read_evidence
from kg.query._meter import Frame, RemoteBudget, RemoteStep, send


def die_after_ack(connection, path, identity, scope, session, deadline, step):
    budget = RemoteBudget(connection, deadline)
    budget.rpc(Frame(action="begin"))
    budget.rpc(Frame(action="public"))
    os._exit(7)


def stall_after_ack(connection, path, identity, scope, session, deadline, step):
    budget = RemoteBudget(connection, deadline)
    budget.rpc(Frame(action="begin"))
    budget.rpc(Frame(action="public"))
    time.sleep(60)


def over_public(connection, path, identity, scope, session, deadline, step):
    budget = RemoteBudget(connection, deadline)
    budget.rpc(Frame(action="begin"))
    budget.rpc(Frame(action="public"))
    budget.rpc(Frame(action="public"))


def over_local(connection, path, identity, scope, session, deadline, step):
    budget = RemoteBudget(connection, deadline)
    budget.rpc(Frame(action="begin"))
    outer = budget.limited(max_visits=3)
    inner = outer.limited(max_visits=2)
    inner.reserve_visits(2)
    outer.reserve_visits()
    inner.reserve_visits()


def false_completion(connection, path, identity, scope, session, deadline, step):
    budget = RemoteBudget(connection, deadline)
    budget.rpc(Frame(action="begin"))
    budget.rpc(Frame(action="public"))
    send(connection, Frame(action="done"))
    os._exit(8)


def too_large(connection, *args):
    connection.send_bytes(b"x" * 65537)


def partial_frame(connection, *args):
    os.write(connection.fileno(), struct.pack("!i", 100))
    time.sleep(60)


@contextmanager
def _hydrate(connection, path, identity, scope, session, deadline, step):
    budget = RemoteBudget(connection, deadline)
    with read_context(
        EvidenceDatabase(path),
        identity,
        scope,
        session,
        deadline,
        RemoteStep(budget),
    ) as context:
        budget.rpc(Frame(action="begin"))
        read_evidence(context, step.evidence)
        yield


def die_after_hydration(connection, *args):
    with _hydrate(connection, *args):
        os._exit(7)


def partial_frame_after_hydration(connection, *args):
    with _hydrate(connection, *args):
        partial_frame(connection)


def repeated_local_hydration(connection, path, identity, scope, session, deadline, step):
    budget = RemoteBudget(connection, deadline)
    with read_context(
        EvidenceDatabase(path),
        identity,
        scope,
        session,
        deadline,
        RemoteStep(budget),
    ) as context:
        budget.rpc(Frame(action="begin"))
        outer = budget.limited(max_visits=100)
        inner = outer.limited(max_visits=100)
        with context.using_budget(inner):
            read_evidence(context, step.evidence)
        # The first read consumed this retained ancestor's allowance too.
        outer.reserve_visits(100)
