"""E1's fixed report target checks under a fresh, read-only release fence."""

import time
from collections.abc import Iterator
from contextlib import contextmanager

from kg._execution_budget import Deadline, PrivateBudget
from kg.diagnostics._targets import (
    AuthorizationBinding,
    DocumentTarget,
    EvidenceTarget,
    ReportTarget,
    WriterTarget,
)
from kg.evidence._authorization import authorize, authorize_writer
from kg.evidence._read_context import ObserverReference, ReadSessionId, ReleaseFence
from kg.evidence._reads import scoped_document, state_row
from kg.evidence._sql import AccountedConnection
from kg.evidence.database import EvidenceDatabase
from kg.evidence.errors import EvidenceServiceError
from kg.models.foundation import Attribution


class EvidenceReportAuthorizer:
    def __init__(self, database: EvidenceDatabase) -> None:
        self.database = database

    def authorize_target(
        self,
        connection: AccountedConnection,
        binding: AuthorizationBinding,
        target: ReportTarget,
    ) -> None:
        scope = binding.scope
        if isinstance(target, WriterTarget):
            authorize_writer(
                connection,
                binding.identity,
                scope,
                Attribution(
                    owner_id=target.owner_id,
                    writer_id=target.writer_id,
                    producer="diagnostics",
                    producer_version="1",
                ),
                target.document,
            )
        elif isinstance(target, DocumentTarget):
            scoped_document(connection, scope, target.document_id)
            if target.state_version is not None:
                state = state_row(connection, target.document_id, target.state_version)
                if target.revision_id is not None and state["revision_id"] != target.revision_id:
                    raise EvidenceServiceError("not_found")
            elif (
                target.revision_id is not None
                and connection.execute(
                    "SELECT 1 FROM revision WHERE document_id=? AND revision_id=?",
                    (target.document_id, target.revision_id),
                ).fetchone()
                is None
            ):
                raise EvidenceServiceError("not_found")
        elif isinstance(target, EvidenceTarget):
            reference = target.reference
            document = scoped_document(connection, scope, reference.document_id)
            if (
                reference.corpus_id != scope.corpus_id
                or document["namespace"] != reference.source_namespace
            ):
                raise EvidenceServiceError("not_found")
            if reference.passage_id is not None:
                raise EvidenceServiceError("unsupported")
            row = connection.execute(
                "SELECT origin_state FROM anchor "
                "WHERE document_id=? AND revision_id=? AND anchor_id=?",
                (reference.document_id, reference.revision_id, reference.anchor_id),
            ).fetchone()
            if row is None:
                raise EvidenceServiceError("not_found")
            citation = target.citation
            if citation is not None and citation.reference != reference:
                raise EvidenceServiceError("not_found")
            state = state_row(
                connection,
                reference.document_id,
                citation.state_version if citation is not None else row[0],
            )
            if (
                state["revision_id"] != reference.revision_id
                or (
                    citation is not None
                    and state["metadata_snapshot_id"] != citation.metadata_snapshot_id
                )
                or connection.execute(
                    "SELECT 1 FROM anchor_set_member "
                    "WHERE set_id=? AND revision_id=? AND anchor_id=?",
                    (state["set_id"], reference.revision_id, reference.anchor_id),
                ).fetchone()
                is None
            ):
                raise EvidenceServiceError("not_found")
        else:
            # Future owners must implement their own exact target checks.
            raise EvidenceServiceError("unsupported")

    @contextmanager
    def fence(
        self,
        bindings: tuple[AuthorizationBinding, ...],
        observers: tuple[ObserverReference, ...],
    ) -> Iterator[ReleaseFence]:
        if not bindings:
            raise EvidenceServiceError("forbidden")
        with self.database.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            fence = ReleaseFence(
                observers[0].observer.session_id if observers else ReadSessionId(),
            )
            try:
                connection._controls(participant=True, read_only=True)
                for binding in bindings:
                    authorize(connection, binding.identity, binding.scope, binding.required)
                    for target in binding.targets:
                        self.authorize_target(connection, binding, target)
                for reference in observers:
                    observer = reference.observer
                    if observer.database is not self.database or not any(
                        binding.identity == observer.identity and binding.scope == observer.scope
                        for binding in bindings
                    ):
                        raise EvidenceServiceError("forbidden")
                    previous = observer._connection._budget
                    try:
                        observer._connection._budget = PrivateBudget(Deadline(time.monotonic() + 5))
                        if observer.changed():
                            raise EvidenceServiceError("state_changed")
                    finally:
                        observer._connection._budget = previous
                yield fence
            finally:
                fence._active = False
                connection._controls()
                connection.rollback()
