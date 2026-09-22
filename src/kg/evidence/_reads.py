from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import TYPE_CHECKING, Literal

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
from kg.models.indexing import PassageEntry, PassagePage

_TOKEN = TypeAdapter(Token)

if TYPE_CHECKING:
    from kg.diagnostics import DiagnosticService
    from kg.diagnostics._collector import Collector
    from kg.evidence._read_context import CanonicalReadContext


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
    from kg.indexing._inspection import effective_default

    row = state_row(connection, doc["document_id"], state)
    indexing, indexing_reason = effective_default(connection, doc, row)
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
            indexing=indexing,
            enrichment=row["enrichment"],
        ),
        metadata=metadata_snapshot(row),
        anchor_set_id=row["set_id"],
        passage_policy=row["passage_policy"],
        is_latest_state=state == doc["current_state"],
        indexing_reason=indexing_reason,
        enrichment_reason=row["enrichment_reason"],
    )


def evidence_member(
    connection: sqlite3.Connection, reference: EvidenceRef, state: sqlite3.Row,
    origin_kind: str,
) -> bool:
    if state["revision_id"] != reference.revision_id:
        return False
    if reference.passage_id is None and origin_kind == "supplied":
        return connection.execute(
            "SELECT 1 FROM anchor_set_member WHERE set_id=? AND anchor_id=? AND revision_id=?",
            (state["set_id"], reference.anchor_id, reference.revision_id),
        ).fetchone() is not None
    return connection.execute(
        "SELECT 1 FROM state_passage_set s JOIN passage p "
        "ON p.passage_set_id=s.passage_set_id AND p.corpus_id=s.corpus_id "
        "AND p.namespace=s.namespace AND p.document_id=s.document_id "
        "AND p.revision_id=s.revision_id JOIN passage_set published "
        "ON published.passage_set_id=s.passage_set_id AND published.policy_token=? "
        "JOIN passage_set_member m "
        "ON m.passage_set_id=p.passage_set_id AND m.passage_id=p.passage_id "
        "AND m.ordinal=p.ordinal WHERE s.state_version=? AND s.corpus_id=? "
        "AND s.namespace=? AND s.document_id=? AND s.revision_id=? AND p.anchor_id=? "
        "AND (? IS NULL OR p.passage_id=?)",
        (
            state["passage_policy"], state["state_version"],
            reference.corpus_id, reference.source_namespace,
            reference.document_id, reference.revision_id, reference.anchor_id,
            reference.passage_id, reference.passage_id,
        ),
    ).fetchone() is not None


def evidence_location(
    connection: sqlite3.Connection,
    scope: Scope,
    reference: EvidenceRef,
    state_version: str | None = None,
) -> tuple[sqlite3.Row, sqlite3.Row]:
    """Resolve the exact authorized chain without materializing source/quote/metadata."""
    if (
        reference.corpus_id != scope.corpus_id
        or reference.source_namespace not in scope.access.namespaces
    ):
        raise EvidenceServiceError("not_found")
    doc = scoped_document(connection, scope, reference.document_id)
    if doc["namespace"] != reference.source_namespace:
        raise EvidenceServiceError("not_found")
    anchor = connection.execute(
        "SELECT anchor_id,local_id,start_offset,end_offset,quote_hash,origin_state,origin_kind "
        "FROM anchor WHERE document_id=? AND revision_id=? AND anchor_id=?",
        (reference.document_id, reference.revision_id, reference.anchor_id),
    ).fetchone()
    if anchor is None:
        raise EvidenceServiceError("not_found")
    origin = anchor["origin_state"]
    if reference.passage_id is not None:
        passage = connection.execute(
            "SELECT origin_state FROM passage WHERE passage_id=? AND corpus_id=? "
            "AND namespace=? AND document_id=? AND revision_id=? AND anchor_id=?",
            (reference.passage_id, reference.corpus_id, reference.source_namespace,
             reference.document_id, reference.revision_id, reference.anchor_id),
        ).fetchone()
        if passage is None:
            raise EvidenceServiceError("not_found")
        origin = passage[0]
    state = connection.execute(
        "SELECT * FROM document_state WHERE document_id=? AND state_version=?",
        (reference.document_id,
         check_token(state_version) if state_version is not None else origin),
    ).fetchone()
    if state is None or not evidence_member(connection, reference, state, anchor["origin_kind"]):
        raise EvidenceServiceError("not_found")
    return anchor, state


def evidence_view(
    connection: sqlite3.Connection, scope: Scope, reference: EvidenceRef,
    state_version: str | None = None, *, context: CanonicalReadContext | None = None,
    stage: Literal["evidence_reference", "search_final_evidence"] = "evidence_reference",
) -> EvidenceView:
    if context is not None and (context.connection is not connection or context.scope != scope):
        raise EvidenceServiceError("invalid_request")
    anchor, state = evidence_location(connection, scope, reference, state_version)
    lengths = connection.execute(
        "SELECT r.byte_length,length(r.content),length(CAST(a.quote AS BLOB)),"
        "length(CAST(m.metadata_json AS BLOB)) FROM revision r JOIN anchor a "
        "ON a.revision_id=r.revision_id AND a.document_id=r.document_id "
        "JOIN metadata_snapshot m ON m.document_id=r.document_id AND m.revision_id=r.revision_id "
        "WHERE r.document_id=? AND r.revision_id=? AND a.anchor_id=? AND m.snapshot_id=? "
        "AND m.state_version=?",
        (reference.document_id, reference.revision_id, reference.anchor_id,
         state["metadata_snapshot_id"], state["state_version"]),
    ).fetchone()
    if (
        lengths is None or any(type(size) is not int or size < 0 for size in lengths)
        or lengths[0] != lengths[1] or lengths[0] > 5_000_000
        or lengths[2] > lengths[0]
    ):
        raise EvidenceServiceError("internal_error")
    if context is not None:
        context.meter.reserve_public(stage)
        content_bytes, _, quote_bytes, metadata_bytes = lengths
        context._hold_scratch(4 * content_bytes, "text")
        context._hold_scratch(4 * quote_bytes, "text")
        context._hold_scratch(4 * metadata_bytes, "context")
        context._hold_scratch(content_bytes + 5 * quote_bytes + 8 * metadata_bytes, "general")
    text = _store.content_bytes(connection, reference.document_id, reference.revision_id).decode()
    quote_row = connection.execute(
        "SELECT quote FROM anchor WHERE anchor_id=?", (reference.anchor_id,),
    ).fetchone()
    if quote_row is None:
        raise EvidenceServiceError("internal_error")
    quote = quote_row[0]
    if (
        not 0 <= anchor["start_offset"] < anchor["end_offset"] <= len(text)
        or text[anchor["start_offset"] : anchor["end_offset"]] != quote
        or sha(quote.encode()) != anchor["quote_hash"]
    ):
        raise EvidenceServiceError("internal_error")
    current = connection.execute(
        "SELECT s.* FROM document d "
        "JOIN document_state s ON s.document_id=d.document_id AND s.state_version=d.current_state "
        "WHERE d.document_id=?", (reference.document_id,),
    ).fetchone()
    if current is None:
        raise EvidenceServiceError("internal_error")
    supplied_member = (
        connection.execute(
            "SELECT 1 FROM anchor_set_member WHERE set_id=? AND anchor_id=?",
            (current["set_id"], reference.anchor_id),
        ).fetchone()
        is not None
    )
    current_member = evidence_member(connection, reference, current, anchor["origin_kind"])
    active = current["source"] == "active"
    current_revision = current["revision_id"] == reference.revision_id
    snapshot = metadata_snapshot(
        state_row(connection, reference.document_id, state["state_version"]),
    )
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
        quote=quote,
        quote_hash=anchor["quote_hash"],
        metadata=snapshot,
        state_version=state["state_version"],
        is_current_revision=current_revision,
        is_active=active,
        member_of_current_anchor_set=supplied_member,
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

    @reported_read("passages")
    def passages(
        self, scope: Scope, document_id: str, state_version: str, *,
        after_ordinal: int = 0, limit: int = 100,
    ) -> PassagePage:
        _page(after_ordinal, limit)
        with self._read(scope) as connection:
            doc = scoped_document(connection, scope, document_id)
            state = state_row(connection, document_id, state_version)
            published = connection.execute(
                "SELECT p.* FROM state_passage_set s JOIN passage_set p "
                "ON p.passage_set_id=s.passage_set_id AND p.corpus_id=s.corpus_id "
                "AND p.namespace=s.namespace AND p.document_id=s.document_id "
                "AND p.revision_id=s.revision_id WHERE s.state_version=? AND s.document_id=? "
                "AND s.corpus_id=? AND s.namespace=? AND s.revision_id=?",
                (state_version, document_id, scope.corpus_id,
                 doc["namespace"], state["revision_id"]),
            ).fetchone()
            entries: tuple[PassageEntry, ...] = ()
            more = False
            if published is not None:
                counts = connection.execute(
                    "SELECT (SELECT count(*) FROM passage_set_member WHERE passage_set_id=?),"
                    "(SELECT count(*) FROM passage WHERE passage_set_id=?)",
                    (published["passage_set_id"], published["passage_set_id"]),
                ).fetchone()
                if (
                    published["policy_token"] != state["passage_policy"] or counts is None
                    or tuple(counts) != (published["member_count"], published["member_count"])
                ):
                    raise EvidenceServiceError("internal_error")
                rows = connection.execute(
                    "SELECT p.passage_id,p.anchor_id,m.ordinal FROM passage_set_member m "
                    "JOIN passage p ON p.passage_set_id=m.passage_set_id "
                    "AND p.passage_id=m.passage_id AND p.ordinal=m.ordinal "
                    "WHERE m.passage_set_id=? AND m.ordinal>? ORDER BY m.ordinal LIMIT ?",
                    (published["passage_set_id"], after_ordinal, limit + 1),
                ).fetchall()
                entries = tuple(PassageEntry(
                    **evidence_view(connection, scope, EvidenceRef(
                        corpus_id=scope.corpus_id, source_namespace=doc["namespace"],
                        document_id=document_id, revision_id=state["revision_id"],
                        anchor_id=row["anchor_id"], passage_id=row["passage_id"],
                    ), state_version).model_dump(), ordinal=row["ordinal"],
                ) for row in rows[:limit])
                more = len(rows) > limit
            return PassagePage(
                document_id=document_id, revision_id=state["revision_id"],
                state_version=state_version, passage_policy=state["passage_policy"],
                status="complete" if published is not None else "not_processed",
                passage_set_id=published["passage_set_id"] if published is not None else None,
                entries=entries, has_more=more,
                next_after_ordinal=entries[-1].ordinal if more else None,
            )

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
