"""Trusted provisioning, deliberately outside scoped request/report APIs."""

import time
from collections.abc import Callable
from functools import wraps

from kg._execution_budget import Deadline, DeadlineStop, PrivateResourceStop
from kg.evidence._transactions import writing
from kg.evidence._values import canonical, sha, validated
from kg.evidence.database import EvidenceDatabase
from kg.evidence.errors import EvidenceServiceError
from kg.models.evidence import LocalAdminAuthority, LocalIdentity
from kg.models.processing import (
    PlanRegistration,
    PlanStateRequest,
    ProcessingCapabilities,
    RegistrationResult,
    WorkerRegistration,
)


def _budget_errors[**P, T](function: Callable[P, T]) -> Callable[P, T]:
    @wraps(function)
    def call(*args: P.args, **kwargs: P.kwargs) -> T:
        try:
            return function(*args, **kwargs)
        except (DeadlineStop, PrivateResourceStop):
            raise EvidenceServiceError("budget_exceeded") from None

    return call


class ProcessingAdministration:
    def __init__(self, database: EvidenceDatabase, authority: LocalAdminAuthority) -> None:
        self.database = database
        self.authority = validated(LocalAdminAuthority, authority)

    @_budget_errors
    def set_plan_enabled(self, request: PlanStateRequest) -> RegistrationResult:
        request = validated(PlanStateRequest, request)
        with writing(
            self.database,
            LocalIdentity(principal_id=self.authority.principal_id),
            deadline=Deadline(time.monotonic() + 5),
        ) as context:
            row = context.connection.execute(
                "SELECT enabled FROM processing_plan "
                "WHERE corpus_id=? AND plan_id=? AND plan_version=?",
                (request.corpus_id, request.plan_id, request.plan_version),
            ).fetchone()
            if row is None:
                raise EvidenceServiceError("not_found")
            if bool(row["enabled"]) != request.expected_enabled:
                raise EvidenceServiceError("state_conflict")
            if request.enabled == request.expected_enabled:
                return RegistrationResult(status="unchanged")
            changed = context.connection.execute(
                "UPDATE processing_plan SET enabled=? "
                "WHERE corpus_id=? AND plan_id=? AND plan_version=? AND enabled=?",
                (
                    int(request.enabled),
                    request.corpus_id,
                    request.plan_id,
                    request.plan_version,
                    int(request.expected_enabled),
                ),
            ).rowcount
            if changed != 1:
                raise EvidenceServiceError("state_conflict")
        return RegistrationResult(status="applied")

    @_budget_errors
    def register_plan(self, request: PlanRegistration) -> RegistrationResult:
        request = validated(PlanRegistration, request)
        digest = sha(canonical(request.model_dump(mode="json")).encode())
        with writing(
            self.database,
            LocalIdentity(principal_id=self.authority.principal_id),
            deadline=Deadline(time.monotonic() + 5),
        ) as context:
            connection = context.connection
            if (
                connection.execute(
                    "SELECT 1 FROM corpus WHERE corpus_id=?",
                    (request.corpus_id,),
                ).fetchone()
                is None
            ):
                raise EvidenceServiceError("not_found")
            old = connection.execute(
                "SELECT definition_hash FROM processing_plan "
                "WHERE corpus_id=? AND plan_id=? AND plan_version=?",
                (request.corpus_id, request.plan_id, request.plan_version),
            ).fetchone()
            if old is not None:
                if old["definition_hash"] != digest:
                    raise EvidenceServiceError("state_conflict")
                return RegistrationResult(status="unchanged")
            if (
                request.configuration_id is not None
                and connection.execute(
                    "SELECT 1 FROM index_configuration WHERE configuration_id=?",
                    (request.configuration_id,),
                ).fetchone()
                is None
            ):
                raise EvidenceServiceError("not_found")
            connection.execute(
                "INSERT INTO processing_plan VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    request.corpus_id,
                    request.plan_id,
                    request.plan_version,
                    request.kind,
                    request.producer,
                    request.producer_version,
                    request.configuration_id,
                    digest,
                    ProcessingCapabilities().model_dump_json(),
                    int(request.enabled),
                ),
            )
        return RegistrationResult(status="applied")

    @_budget_errors
    def register_worker(self, request: WorkerRegistration) -> RegistrationResult:
        request = validated(WorkerRegistration, request)
        s = request.selection
        values = (
            request.corpus_id,
            s.plan_id,
            s.plan_version,
            s.namespace,
            request.principal_id,
            s.worker_id,
            s.owner_id,
            s.writer_id,
        )
        with writing(
            self.database,
            LocalIdentity(principal_id=self.authority.principal_id),
            deadline=Deadline(time.monotonic() + 5),
        ) as context:
            connection = context.connection
            if (
                connection.execute(
                    "SELECT 1 FROM processing_plan p JOIN source_namespace n "
                    "ON n.corpus_id=p.corpus_id WHERE p.corpus_id=? AND p.plan_id=? "
                    "AND p.plan_version=? AND n.namespace=?",
                    values[:4],
                ).fetchone()
                is None
            ):
                raise EvidenceServiceError("not_found")
            changed = connection.execute(
                "INSERT INTO processing_worker_binding VALUES (?,?,?,?,?,?,?,?) "
                "ON CONFLICT DO NOTHING",
                values,
            ).rowcount
        return RegistrationResult(status="applied" if changed else "unchanged")
