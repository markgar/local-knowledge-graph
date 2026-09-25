"""Exact owned assertion withdrawal inside the canonical owner's transaction."""

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import Literal

from kg._execution_budget import PrivateBudget
from kg.diagnostics._targets import (
    KnowledgeTarget,
    KnowledgeWriterTarget,
    ReportTarget,
    SeedSetTarget,
)
from kg.evidence._transactions import CanonicalWriteContext
from kg.evidence._values import token
from kg.evidence.errors import EvidenceServiceError
from kg.knowledge._authorization import writer
from kg.knowledge._store import Store
from kg.knowledge._write import Manifest, save
from kg.models.foundation import (
    AssertionWithdrawalReceipt,
    ClassificationWithdrawalReceipt,
    SeedSupport,
    WithdrawAssertion,
    WithdrawClassification,
    WriteRequest,
)
from kg.models.knowledge import AssertionPayload, ClassificationPayload, ContributionView


@contextmanager
def authorize_target(
    context: CanonicalWriteContext, request: WriteRequest, budget: PrivateBudget,
) -> Iterator[tuple[ContributionView, Manifest]]:
    assert isinstance(request.payload, (WithdrawAssertion, WithdrawClassification))
    store = Store(context.connection, request.scope, budget)
    try:
        target = store.contribution(request.payload.contribution_id, history=True)
        expected = (
            ClassificationPayload
            if isinstance(request.payload, WithdrawClassification)
            else AssertionPayload
        )
        if not isinstance(target.payload, expected):
            raise EvidenceServiceError("invalid_request")
        attribution = request.attribution
        if (target.attribution.owner_id, target.attribution.writer_id) != (
            attribution.owner_id, attribution.writer_id,
        ):
            raise EvidenceServiceError("forbidden")
        targets: list[ReportTarget] = [KnowledgeTarget(
            contribution_id=target.contribution_id,
            witness_ids=tuple(w.contribution_id for w in target.witnesses),
        )]
        seed = isinstance(target.payload.support, SeedSupport)
        namespaces = {e.reference.source_namespace for e in target.evidence}
        if isinstance(target.payload.support, SeedSupport):
            namespaces.add(target.payload.support.source_namespace)
            targets.append(SeedSetTarget(
                namespace=target.payload.support.source_namespace,
                owner_id=attribution.owner_id, writer_id=attribution.writer_id,
                seed_set_id=target.payload.support.seed_set_id,
            ))
        for namespace in sorted(namespaces):
            writer(
                context.connection, context.identity, request.scope, namespace,
                attribution.owner_id, attribution.writer_id, seed=seed,
            )
            targets.append(KnowledgeWriterTarget(
                namespace=namespace, owner_id=attribution.owner_id, writer_id=attribution.writer_id,
            ))
        store.registry.authored(target.schema_version)
        yield target, Manifest(
            owner_id=attribution.owner_id, writer_id=attribution.writer_id, targets=tuple(targets),
        )
    finally:
        store.close()


def apply(
    context: CanonicalWriteContext, request: WriteRequest, at: datetime, budget: PrivateBudget,
) -> tuple[
    AssertionWithdrawalReceipt | ClassificationWithdrawalReceipt,
    Literal["applied", "unchanged"], str, Manifest,
]:
    with authorize_target(context, request, budget) as (target, manifest):
        key_id = token()
        status: Literal["applied", "unchanged"]
        classification = isinstance(request.payload, WithdrawClassification)
        table = "classification_withdrawal" if classification else "assertion_withdrawal"
        if target.withdrawal is None:
            withdrawal_id = token()
            context.connection.execute(
                f"INSERT INTO {table} VALUES (?,?,?,?)",
                (withdrawal_id, request.scope.corpus_id, target.contribution_id, key_id),
            )
            status = "applied"
        else:
            withdrawal_id = target.withdrawal.withdrawal_id
            status = "unchanged"
        receipt = (
            ClassificationWithdrawalReceipt(
                kind="classification_withdrawal",
                contribution_id=target.contribution_id, withdrawal_id=withdrawal_id,
            ) if classification else AssertionWithdrawalReceipt(
                kind="assertion_withdrawal",
                contribution_id=target.contribution_id, withdrawal_id=withdrawal_id,
            )
        )
        save(context, request, key_id, target.schema_version, receipt, manifest, status, at)
        return receipt, status, key_id, manifest
