"""Explicit trusted-local schema administration, never ordinary writer authority."""

import time
from typing import Literal

from kg._execution_budget import Deadline, DeadlineStop, PrivateBudget, PrivateResourceStop
from kg.evidence._transactions import writing
from kg.evidence._values import validated
from kg.evidence.database import EvidenceDatabase
from kg.evidence.errors import EvidenceServiceError
from kg.knowledge import _schema_operations
from kg.models.evidence import LocalAdminAuthority, LocalIdentity
from kg.models.schema import (
    SchemaApplyOutcome,
    SchemaApplyRequest,
    SchemaPresetRegistration,
    SchemaPresetRequest,
)


class KnowledgeAdministration:
    def __init__(self, database: EvidenceDatabase, authority: LocalAdminAuthority) -> None:
        self.database = database
        self.authority = validated(LocalAdminAuthority, authority)

    def register_knowledge_schema(self, schema: SchemaPresetRequest) -> SchemaPresetRegistration:
        schema = validated(SchemaPresetRequest, schema)
        authority = validated(LocalAdminAuthority, self.authority)
        identity = LocalIdentity(principal_id=authority.principal_id)
        budget = PrivateBudget(Deadline(time.monotonic() + 30))
        try:
            with writing(self.database, identity, budget=budget) as context:
                result = _schema_operations.preset(context, schema, budget)
            return result
        except (PrivateResourceStop, DeadlineStop):
            raise EvidenceServiceError("budget_exceeded") from None

    def apply_schema(self, request: SchemaApplyRequest) -> SchemaApplyOutcome:
        request = validated(SchemaApplyRequest, request)
        authority = validated(LocalAdminAuthority, self.authority)
        identity = LocalIdentity(principal_id=authority.principal_id)
        budget = PrivateBudget(Deadline(time.monotonic() + 30))
        context = None
        try:
            with writing(self.database, identity, budget=budget) as context:
                receipt = _schema_operations.apply(context, request, budget)
            return SchemaApplyOutcome(
                request_id=request.request_id, status="applied",
                commit_outcome="confirmed_committed", receipt=receipt,
            )
        except (EvidenceServiceError, PrivateResourceStop, DeadlineStop) as error:
            failure = (
                error.failure if isinstance(error, EvidenceServiceError)
                else EvidenceServiceError("budget_exceeded").failure
            )
            commit = context.commit_outcome if context is not None else "not_attempted"
            if commit == "unknown":
                return SchemaApplyOutcome(
                    request_id=request.request_id, status="uncertain", commit_outcome=commit,
                    error=EvidenceServiceError("internal_error").failure,
                )
            status: Literal["conflict", "failed", "rejected"]
            if failure.code in {"state_conflict", "state_changed", "retry_conflict"}:
                status = "conflict"
            elif failure.code in {"internal_error", "budget_exceeded"}:
                status = "failed"
            else:
                status = "rejected"
            return SchemaApplyOutcome(
                request_id=request.request_id, status=status, commit_outcome=commit, error=failure,
            )
