"""Fixed retained-target authorization for knowledge and mixed evidence calls."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING

from kg._execution_budget import Deadline, PrivateBudget
from kg.diagnostics._collector import Capture, CaptureUnavailable
from kg.diagnostics._targets import (
    AuthorizationBinding,
    KnowledgeTarget,
    KnowledgeWriterTarget,
    ReportTarget,
    ReportTargets,
    SeedSetTarget,
)
from kg.evidence._authorization import authorize
from kg.evidence._diagnostic_authorization import EvidenceReportAuthorizer
from kg.evidence._read_context import ObserverReference, ReadSessionId, ReleaseFence
from kg.evidence.errors import EvidenceServiceError
from kg.knowledge._authorization import writer
from kg.knowledge._store import Store
from kg.models.evidence import LocalIdentity

if TYPE_CHECKING:
    from kg.knowledge._write import Manifest


def authorize_target(store: Store, identity: LocalIdentity, target: ReportTarget) -> None:
    if isinstance(target, (KnowledgeWriterTarget, SeedSetTarget)):
        writer(
            store.connection,
            identity,
            store.scope,
            target.namespace,
            target.owner_id,
            target.writer_id,
            seed=isinstance(target, SeedSetTarget),
        )
        if (
            isinstance(target, SeedSetTarget)
            and store.connection.execute(
                "SELECT 1 FROM seed_set WHERE corpus_id=? AND namespace=? AND owner_id=? "
                "AND writer_id=? AND seed_set_id=?",
                (
                    store.scope.corpus_id,
                    target.namespace,
                    target.owner_id,
                    target.writer_id,
                    target.seed_set_id,
                ),
            ).fetchone()
            is None
        ):
            raise EvidenceServiceError("not_found")
    elif isinstance(target, KnowledgeTarget):
        contribution = store.contribution(target.contribution_id, history=True)
        endpoints = {w.entity_id for w in contribution.witnesses}
        witnessed = set()
        for cid in target.witness_ids:
            row = store.row("contribution", cid)
            store.support(row)
            support = store.connection.execute(
                "SELECT entity_id FROM entity_support WHERE corpus_id=? AND contribution_id=?",
                (store.scope.corpus_id, cid),
            ).fetchone()
            if support is None or support[0] not in endpoints:
                raise EvidenceServiceError("not_found")
            witnessed.add(support[0])
        if not endpoints <= witnessed:
            raise EvidenceServiceError("not_found")
    else:
        raise EvidenceServiceError("unsupported")


def retain_manifest(capture: Capture | CaptureUnavailable, manifest: Manifest) -> None:
    with capture.guard():
        for start in range(0, len(manifest.targets), 100):
            capture.retain(ReportTargets(values=manifest.targets[start : start + 100]))


class KnowledgeReportAuthorizer(EvidenceReportAuthorizer):
    @contextmanager
    def fence(
        self,
        bindings: tuple[AuthorizationBinding, ...],
        observers: tuple[ObserverReference, ...],
    ) -> Iterator[ReleaseFence]:
        if not bindings:
            raise EvidenceServiceError("forbidden")
        budget = PrivateBudget(Deadline(time.monotonic() + 5)).limited(max_visits=10_000)
        with self.database.connection(budget=budget) as connection:
            connection.execute("BEGIN IMMEDIATE")
            fence = ReleaseFence(observers[0].observer.session_id if observers else ReadSessionId())
            try:
                connection._controls(participant=True, read_only=True)
                for binding in bindings:
                    authorize(connection, binding.identity, binding.scope, binding.required)
                    store = Store(connection, binding.scope, budget)
                    try:
                        for target in binding.targets:
                            if isinstance(
                                target, (KnowledgeTarget, KnowledgeWriterTarget, SeedSetTarget)
                            ):
                                authorize_target(store, binding.identity, target)
                            else:
                                self.authorize_target(connection, binding, target)
                    finally:
                        store.close()
                for reference in observers:
                    observer = reference.observer
                    if observer.database is not self.database or not any(
                        b.identity == observer.identity and b.scope == observer.scope
                        for b in bindings
                    ):
                        raise EvidenceServiceError("forbidden")
                    previous = observer._connection._budget
                    try:
                        observer._connection._budget = budget
                        if observer.changed():
                            raise EvidenceServiceError("state_changed")
                    finally:
                        observer._connection._budget = previous
                yield fence
            finally:
                fence._active = False
                connection._controls()
                connection.rollback()
