"""Controlled spawn targets for query supervision tests, not service adapters."""

import os
import struct
import time

from kg.query._meter import Frame, RemoteBudget, send


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
