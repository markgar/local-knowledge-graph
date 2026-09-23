"""Exact owned assertion withdrawal inside the canonical owner's transaction."""

from datetime import datetime
from typing import Literal

from kg._execution_budget import PrivateBudget
from kg.diagnostics._targets import KnowledgeTarget, KnowledgeWriterTarget, ReportTarget
from kg.evidence._transactions import CanonicalWriteContext
from kg.evidence._values import token
from kg.evidence.errors import EvidenceServiceError
from kg.knowledge._authorization import writer
from kg.knowledge._store import Store
from kg.knowledge._write import Manifest, save
from kg.models.foundation import (
    AddAssertion,
    AssertionWithdrawalReceipt,
    WithdrawAssertion,
    WriteRequest,
)
from kg.models.knowledge import ContributionView


def authorize_target(
    context: CanonicalWriteContext, request: WriteRequest, budget: PrivateBudget,
) -> tuple[ContributionView, Manifest]:
    assert isinstance(request.payload, WithdrawAssertion)
    store = Store(context.connection, request.scope, budget)
    try:
        target = store.contribution(request.payload.contribution_id, history=True)
        if not isinstance(target.payload, AddAssertion):
            raise EvidenceServiceError("invalid_request")
        attribution = request.attribution
        if (target.attribution.owner_id, target.attribution.writer_id) != (
            attribution.owner_id, attribution.writer_id,
        ):
            raise EvidenceServiceError("forbidden")
        targets: list[ReportTarget] = []
        for namespace in sorted({e.reference.source_namespace for e in target.evidence}):
            writer(
                context.connection, context.identity, request.scope, namespace,
                attribution.owner_id, attribution.writer_id,
            )
            targets.append(KnowledgeWriterTarget(
                namespace=namespace, owner_id=attribution.owner_id, writer_id=attribution.writer_id,
            ))
        if store.schema().schema_version != target.schema_version:
            raise EvidenceServiceError("internal_error")
        targets.append(KnowledgeTarget(
            contribution_id=target.contribution_id,
            witness_ids=tuple(w.contribution_id for w in target.witnesses),
        ))
        return target, Manifest(
            owner_id=attribution.owner_id, writer_id=attribution.writer_id, targets=tuple(targets),
        )
    finally:
        store.close()


def apply(
    context: CanonicalWriteContext, request: WriteRequest, at: datetime, budget: PrivateBudget,
) -> tuple[AssertionWithdrawalReceipt, Literal["applied", "unchanged"], str, Manifest]:
    target, manifest = authorize_target(context, request, budget)
    key_id = token()
    status: Literal["applied", "unchanged"]
    if target.withdrawal is None:
        withdrawal_id = token()
        context.connection.execute(
            "INSERT INTO assertion_withdrawal VALUES (?,?,?,?)",
            (withdrawal_id, request.scope.corpus_id, target.contribution_id, key_id),
        )
        status = "applied"
    else:
        withdrawal_id = target.withdrawal.withdrawal_id
        status = "unchanged"
    receipt = AssertionWithdrawalReceipt(
        kind="assertion_withdrawal",
        contribution_id=target.contribution_id,
        withdrawal_id=withdrawal_id,
    )
    save(context, request, key_id, target.schema_version, receipt, manifest, status, at)
    return receipt, status, key_id, manifest
