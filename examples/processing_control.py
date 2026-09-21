"""A trusted embedding example: claim/renew only, never process or mark ready."""

import argparse
from pathlib import Path

from evidence_intake import run as supply_example

from kg.evidence import EvidenceDatabase
from kg.models.evidence import LocalAdminAuthority, LocalIdentity
from kg.models.foundation import AccessContext, Scope
from kg.models.processing import (
    DocumentTarget,
    HeartbeatRequest,
    JobRequest,
    PlanRegistration,
    ScheduleRequest,
    WorkerRegistration,
    WorkerRequest,
    WorkerSelection,
)
from kg.processing import ProcessingAdministration, ProcessingService


def run(path: Path) -> str:
    supply_example(path)
    database = EvidenceDatabase(path)
    identity = LocalIdentity(principal_id="me")
    # The trusted embedding constructs targets from canonical metadata only.
    # Scheduling rechecks all authority/currentness inside its own transaction.
    with database.connection() as connection:
        version = connection.execute(
            "SELECT policy_version FROM corpus WHERE corpus_id='demo'",
        ).fetchone()[0]
        row = connection.execute(
            "SELECT d.document_id,s.revision_id,s.state_version,s.namespace_token "
            "FROM document d JOIN document_state s ON s.state_version=d.current_state "
            "WHERE d.corpus_id='demo' AND d.namespace='notes' AND d.external_id='note-1'",
        ).fetchone()
        target = DocumentTarget(**dict(row))
    scope = Scope(
        corpus_id="demo",
        access=AccessContext(
            principal_id="me",
            policy_version=version,
            namespaces=("notes",),
            grants=("read",),
        ),
    )
    selection = WorkerSelection(
        namespace="notes",
        owner_id="me",
        writer_id="example",
        plan_id="document-index",
        plan_version="1",
        worker_id="local-worker",
    )
    admin = ProcessingAdministration(
        database,
        LocalAdminAuthority(principal_id="trusted-local-app"),
    )
    admin.register_plan(
        PlanRegistration(
            corpus_id="demo",
            plan_id=selection.plan_id,
            plan_version="1",
            producer="example",
            producer_version="1",
        )
    )
    admin.register_worker(
        WorkerRegistration(
            corpus_id="demo",
            principal_id="me",
            selection=selection,
        )
    )
    service = ProcessingService(database, identity)
    scheduled = service.schedule(
        ScheduleRequest(
            scope=scope,
            selection=selection,
            request_id="example-schedule",
            retry_key="example-schedule-1",
            target=target,
        )
    )
    claimed = service.claim(
        WorkerRequest(
            scope=scope,
            selection=selection,
            request_id="example-claim",
        )
    )
    if claimed.claim is not None:
        service.heartbeat(
            HeartbeatRequest(
                scope=scope,
                selection=selection,
                request_id="example-heartbeat",
                job_id=claimed.claim.job_id,
                claim_fence=claimed.claim.claim_fence,
            )
        )
    return service.job(
        JobRequest(
            scope=scope,
            selection=selection,
            request_id="example-status",
            job_id=scheduled.job_id,
        )
    ).model_dump_json(indent=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    print(run(parser.parse_args().database))
