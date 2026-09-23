"""Spawn entry point: immutable values in, no captured text over the control pipe."""

from multiprocessing.connection import Connection
from pathlib import Path

from kg._execution_budget import Deadline, DeadlineStop, PrivateResourceStop, PublicBudgetStop
from kg.evidence._read_context import ReadSessionId, read_context, read_evidence
from kg.evidence.database import EvidenceDatabase
from kg.evidence.errors import EvidenceServiceError
from kg.models.evidence import LocalIdentity
from kg.models.foundation import EvidenceStep, QueryRequest, Scope
from kg.models.query import SupportInspectionRequest
from kg.query._dispatch import Stopped
from kg.query._meter import Frame, RemoteBudget, RemoteStep, send
from kg.query._search import SearchWork


def run(
    connection: Connection,
    path: Path,
    identity: LocalIdentity,
    scope: Scope,
    session: ReadSessionId,
    deadline: Deadline,
    step: EvidenceStep | QueryRequest | SupportInspectionRequest | SearchWork,
) -> None:
    budget = RemoteBudget(connection, deadline)
    database = EvidenceDatabase(path)
    try:
        with read_context(
            database,
            identity,
            scope,
            session,
            deadline,
            RemoteStep(budget),
        ) as context:
            if isinstance(step, EvidenceStep):
                budget.rpc(Frame(action="begin"))
                read_evidence(context, step.evidence)
            elif isinstance(step, SearchWork):
                from kg.query._search_worker import execute as search

                search(database, context, budget, step)
            else:
                from kg.query._plan_worker import execute, inspect

                if isinstance(step, QueryRequest):
                    execute(context, budget, step)
                else:
                    inspect(context, budget, step)
        budget.check_deadline()
        send(connection, Frame(action="done"))
    except EvidenceServiceError as error:
        send(connection, Frame(action="error", code=error.failure.code))
    except Stopped as error:
        send(connection, Frame(action="error", code=error.code, reason=error.reason))
    except (DeadlineStop, PrivateResourceStop, PublicBudgetStop):
        send(connection, Frame(action="error", code="budget_exceeded"))
    finally:
        connection.close()
