from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING

from pydantic import TypeAdapter, ValidationError

from kg.evidence import _store
from kg.evidence._authorization import authorize
from kg.evidence._reporting import reported_read
from kg.evidence._values import sha, validated
from kg.evidence.database import EvidenceDatabase
from kg.evidence.errors import EvidenceServiceError
from kg.models.evidence import (
    AnchorEntry,
    AnchorPage,
    DocumentView,
    EvidenceView,
    LocalIdentity,
    RevisionAnchorPage,
    RevisionPage,
    RevisionView,
    StatePage,
    StoredCitation,
)
from kg.models.foundation import (
    ContentResult,
    EvidenceRef,
    ExternalDocument,
    MetadataSnapshot,
    ProcessingState,
    Scope,
    SourceMetadata,
    Token,
)

_TOKEN = TypeAdapter(Token)

if TYPE_CHECKING:
    from kg.diagnostics import DiagnosticService
    from kg.diagnostics._collector import Collector


def check_token(value: str) -> str:
    try:
        return _TOKEN.validate_python(value, strict=True)
    except ValidationError:
        raise EvidenceServiceError("invalid_request") from None


def _page(after: int, limit: int) -> None:
    if type(after) is not int or type(limit) is not int or after < 0 or not 1 <= limit <= 200:
        raise EvidenceServiceError("invalid_request")


def scoped_document(connection: sqlite3.Connection, scope: Scope, doc: str) -> sqlite3.Row:
    row = _store.document(connection, scope.corpus_id, check_token(doc))
    if row["namespace"] not in scope.access.namespaces:
        raise EvidenceServiceError("not_found")
    return row


def state_row(connection: sqlite3.Connection, doc: str, state: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT s.*, m.metadata_json, p.indexing, p.enrichment, p.indexing_reason, "
        "p.enrichment_reason FROM document_state s "
        "JOIN metadata_snapshot m ON m.snapshot_id=s.metadata_snapshot_id "
        "JOIN processing_state p ON p.state_version=s.state_version "
        "WHERE s.document_id=? AND s.state_version=?",
        (doc, check_token(state)),
    ).fetchone()
    if not isinstance(row, sqlite3.Row):
        raise EvidenceServiceError("not_found")
    return row


def metadata_snapshot(row: sqlite3.Row) -> MetadataSnapshot:
    return MetadataSnapshot(
        contract_version="foundation/1",
        document_id=row["document_id"],
        revision_id=row["revision_id"],
        state_version=row["state_version"],
        metadata_snapshot_id=row["metadata_snapshot_id"],
        metadata=SourceMetadata.model_validate_json(row["metadata_json"]),
    )


def document_view(connection: sqlite3.Connection, doc: sqlite3.Row, state: str) -> DocumentView:
    row = state_row(connection, doc["document_id"], state)
    return DocumentView(
        corpus_id=doc["corpus_id"],
        document=ExternalDocument(
            source_namespace=doc["namespace"],
            synchronization_scope=doc["synchronization_scope"],
            external_id=doc["external_id"],
        ),
        document_id=doc["document_id"],
        owner_id=doc["owner_id"],
        revision_id=row["revision_id"],
        state_version=state,
        sequence=row["sequence"],
        previous_state=row["previous_state"],
        change_kind=row["change_kind"],
        committed_at=datetime.fromisoformat(row["committed_at"]),
        processing=ProcessingState(
            state_version=state,
            source=row["source"],
            indexing=row["indexing"],
            enrichment=row["enrichment"],
        ),
        metadata=metadata_snapshot(row),
        anchor_set_id=row["set_id"],
        passage_policy=row["passage_policy"],
        is_latest_state=state == doc["current_state"],
        indexing_reason=row["indexing_reason"],
        enrichment_reason=row["enrichment_reason"],
    )


def evidence_view(
    connection: sqlite3.Connection,
    scope: Scope,
    reference: EvidenceRef,
    state_version: str | None = None,
) -> EvidenceView:
    if (
        reference.corpus_id != scope.corpus_id
        or reference.source_namespace not in scope.access.namespaces
    ):
        raise EvidenceServiceError("not_found")
    doc = scoped_document(connection, scope, reference.document_id)
    if doc["namespace"] != reference.source_namespace:
        raise EvidenceServiceError("not_found")
    if reference.passage_id is not None:
        raise EvidenceServiceError("unsupported")
    anchor = connection.execute(
        "SELECT * FROM anchor WHERE document_id=? AND revision_id=? AND anchor_id=?",
        (reference.document_id, reference.revision_id, reference.anchor_id),
    ).fetchone()
    if anchor is None:
        raise EvidenceServiceError("not_found")
    state = state_row(
        connection,
        reference.document_id,
        state_version if state_version is not None else anchor["origin_state"],
    )
    member = connection.execute(
        "SELECT 1 FROM anchor_set_member WHERE set_id=? AND anchor_id=? AND revision_id=?",
        (state["set_id"], reference.anchor_id, reference.revision_id),
    ).fetchone()
    if state["revision_id"] != reference.revision_id or member is None:
        raise EvidenceServiceError("not_found")
    text = _store.content_bytes(connection, reference.document_id, reference.revision_id).decode()
    if (
        anchor["end_offset"] > len(text)
        or text[anchor["start_offset"] : anchor["end_offset"]] != anchor["quote"]
        or sha(anchor["quote"].encode()) != anchor["quote_hash"]
    ):
        raise EvidenceServiceError("internal_error")
    current = connection.execute(
        "SELECT s.revision_id,s.source,s.set_id FROM document d "
        "JOIN document_state s ON s.document_id=d.document_id AND s.state_version=d.current_state "
        "WHERE d.document_id=?", (reference.document_id,),
    ).fetchone()
    if current is None:
        raise EvidenceServiceError("internal_error")
    current_member = (
        connection.execute(
            "SELECT 1 FROM anchor_set_member WHERE set_id=? AND anchor_id=?",
            (current["set_id"], reference.anchor_id),
        ).fetchone()
        is not None
    )
    active = current["source"] == "active"
    current_revision = current["revision_id"] == reference.revision_id
    snapshot = metadata_snapshot(state)
    return EvidenceView(
        reference=reference,
        citation=StoredCitation(
            reference=reference,
            metadata_snapshot_id=snapshot.metadata_snapshot_id,
            state_version=state["state_version"],
        ),
        local_id=anchor["local_id"],
        start=anchor["start_offset"],
        end=anchor["end_offset"],
        quote=anchor["quote"],
        quote_hash=anchor["quote_hash"],
        metadata=snapshot,
        state_version=state["state_version"],
        is_current_revision=current_revision,
        is_active=active,
        member_of_current_anchor_set=current_member,
        is_current_support=current_member and active and current_revision,
    )


class EvidenceReads:
    database: EvidenceDatabase
    identity: LocalIdentity
    _collector: Collector
    diagnostics: DiagnosticService

    @contextmanager
    def _read(self, scope: Scope) -> Iterator[sqlite3.Connection]:
        scope = validated(Scope, scope)
        with self.database.connection() as connection:
            connection.execute("BEGIN")
            authorize(connection, self.identity, scope, "read")
            yield connection
            with self.database.connection() as fresh:
                version = fresh.execute(
                    "SELECT policy_version FROM corpus WHERE corpus_id=?",
                    (scope.corpus_id,),
                ).fetchone()
                if version is None or version[0] != scope.access.policy_version:
                    raise EvidenceServiceError("state_changed")

    @reported_read("current")
    def current(self, scope: Scope, document: ExternalDocument) -> DocumentView:
        document = validated(ExternalDocument, document)
        with self._read(scope) as connection:
            if document.source_namespace not in scope.access.namespaces:
                raise EvidenceServiceError("not_found")
            doc = connection.execute(
                "SELECT * FROM document WHERE corpus_id=? AND namespace=? AND external_id=? "
                "AND synchronization_scope=?",
                (
                    scope.corpus_id,
                    document.source_namespace,
                    document.external_id,
                    document.synchronization_scope,
                ),
            ).fetchone()
            if doc is None:
                raise EvidenceServiceError("not_found")
            return document_view(connection, doc, doc["current_state"])

    @reported_read("document")
    def document(self, scope: Scope, document_id: str) -> DocumentView:
        with self._read(scope) as connection:
            doc = scoped_document(connection, scope, document_id)
            return document_view(connection, doc, doc["current_state"])

    @reported_read("state")
    def state(self, scope: Scope, document_id: str, state_version: str) -> DocumentView:
        with self._read(scope) as connection:
            doc = scoped_document(connection, scope, document_id)
            return document_view(connection, doc, check_token(state_version))

    @reported_read("content")
    def content(self, scope: Scope, document_id: str, revision_id: str) -> ContentResult:
        with self._read(scope) as connection:
            scoped_document(connection, scope, document_id)
            text = _store.content_bytes(connection, document_id, check_token(revision_id)).decode()
            return ContentResult(
                contract_version="foundation/1",
                document_id=document_id,
                revision_id=revision_id,
                state="available",
                text=text,
            )

    @reported_read("history")
    def history(
        self,
        scope: Scope,
        document_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> StatePage:
        _page(after_sequence, limit)
        with self._read(scope) as connection:
            doc = scoped_document(connection, scope, document_id)
            rows = connection.execute(
                "SELECT state_version FROM document_state WHERE document_id=? AND sequence>? "
                "ORDER BY sequence LIMIT ?",
                (document_id, after_sequence, limit + 1),
            ).fetchall()
            entries = tuple(document_view(connection, doc, row[0]) for row in rows[:limit])
            more = len(rows) > limit
            return StatePage(
                document_id=document_id,
                entries=entries,
                has_more=more,
                next_after_sequence=entries[-1].sequence if more else None,
            )

    @reported_read("revisions")
    def revisions(
        self,
        scope: Scope,
        document_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> RevisionPage:
        _page(after_sequence, limit)
        with self._read(scope) as connection:
            scoped_document(connection, scope, document_id)
            rows = connection.execute(
                "SELECT revision_id,sequence,content_hash,byte_length,created_at FROM revision "
                "WHERE document_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                (document_id, after_sequence, limit + 1),
            ).fetchall()
            entries = tuple(
                RevisionView(
                    revision_id=row["revision_id"],
                    sequence=row["sequence"],
                    content_hash=row["content_hash"],
                    byte_length=row["byte_length"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                )
                for row in rows[:limit]
            )
            more = len(rows) > limit
            return RevisionPage(
                document_id=document_id,
                entries=entries,
                has_more=more,
                next_after_sequence=entries[-1].sequence if more else None,
            )

    @reported_read("evidence")
    def evidence(
        self,
        scope: Scope,
        reference: EvidenceRef,
        *,
        state_version: str | None = None,
    ) -> EvidenceView:
        reference = validated(EvidenceRef, reference)
        with self._read(scope) as connection:
            return evidence_view(connection, scope, reference, state_version)

    @reported_read("citation")
    def citation(self, scope: Scope, citation: StoredCitation) -> EvidenceView:
        citation = validated(StoredCitation, citation)
        with self._read(scope) as connection:
            result = evidence_view(connection, scope, citation.reference, citation.state_version)
            if result.metadata.metadata_snapshot_id != citation.metadata_snapshot_id:
                raise EvidenceServiceError("not_found")
            return result

    @reported_read("anchors")
    def anchors(
        self,
        scope: Scope,
        document_id: str,
        state_version: str,
        *,
        after_ordinal: int = 0,
        limit: int = 100,
    ) -> AnchorPage:
        _page(after_ordinal, limit)
        with self._read(scope) as connection:
            doc = scoped_document(connection, scope, document_id)
            state = state_row(connection, document_id, state_version)
            rows = connection.execute(
                "SELECT anchor_id,ordinal FROM anchor_set_member "
                "WHERE set_id=? AND ordinal>? ORDER BY ordinal LIMIT ?",
                (state["set_id"], after_ordinal, limit + 1),
            ).fetchall()
            entries = tuple(
                AnchorEntry(
                    **evidence_view(
                        connection,
                        scope,
                        EvidenceRef(
                            corpus_id=scope.corpus_id,
                            source_namespace=doc["namespace"],
                            document_id=document_id,
                            revision_id=state["revision_id"],
                            anchor_id=row[0],
                        ),
                        state_version,
                    ).model_dump(),
                    ordinal=row[1],
                )
                for row in rows[:limit]
            )
            more = len(rows) > limit
            return AnchorPage(
                document_id=document_id,
                state_version=state_version,
                entries=entries,
                has_more=more,
                next_after_ordinal=entries[-1].ordinal if more else None,
            )

    @reported_read("revision_anchors")
    def revision_anchors(
        self,
        scope: Scope,
        document_id: str,
        revision_id: str,
        *,
        after_ordinal: int = 0,
        limit: int = 100,
    ) -> RevisionAnchorPage:
        _page(after_ordinal, limit)
        with self._read(scope) as connection:
            doc = scoped_document(connection, scope, document_id)
            _store.content_bytes(connection, document_id, check_token(revision_id))
            rows = connection.execute(
                "SELECT anchor_id,ordinal FROM anchor WHERE revision_id=? "
                "AND ordinal>? ORDER BY ordinal LIMIT ?",
                (revision_id, after_ordinal, limit + 1),
            ).fetchall()
            entries = tuple(
                AnchorEntry(
                    **evidence_view(
                        connection,
                        scope,
                        EvidenceRef(
                            corpus_id=scope.corpus_id,
                            source_namespace=doc["namespace"],
                            document_id=document_id,
                            revision_id=revision_id,
                            anchor_id=row[0],
                        ),
                    ).model_dump(),
                    ordinal=row[1],
                )
                for row in rows[:limit]
            )
            more = len(rows) > limit
            return RevisionAnchorPage(
                document_id=document_id,
                revision_id=revision_id,
                entries=entries,
                has_more=more,
                next_after_ordinal=entries[-1].ordinal if more else None,
            )
