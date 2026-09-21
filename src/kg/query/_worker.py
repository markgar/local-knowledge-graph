"""Spawn entry point: immutable values in, no captured text over the control pipe."""

from multiprocessing.connection import Connection
from pathlib import Path

from kg._execution_budget import Deadline, DeadlineStop, PrivateResourceStop, PublicBudgetStop
from kg.evidence._read_context import ReadSessionId, read_context, read_evidence
from kg.evidence.database import EvidenceDatabase
from kg.evidence.errors import EvidenceServiceError
from kg.models.evidence import LocalIdentity
from kg.models.foundation import EvidenceStep, Scope
from kg.query._meter import Frame, RemoteBudget, RemoteStep, send


def run(
    connection: Connection,
    path: Path,
    identity: LocalIdentity,
    scope: Scope,
    session: ReadSessionId,
    deadline: Deadline,
    step: EvidenceStep,
) -> None:
    budget = RemoteBudget(connection, deadline)
    try:
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
        budget.check_deadline()
        send(connection, Frame(action="done"))
    except EvidenceServiceError as error:
        send(connection, Frame(action="error", code=error.failure.code))
    except (DeadlineStop, PrivateResourceStop, PublicBudgetStop):
        send(connection, Frame(action="error", code="budget_exceeded"))
    finally:
        connection.close()
