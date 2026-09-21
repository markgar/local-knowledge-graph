from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Literal

from kg.evidence import _store
from kg.evidence._values import metadata_json, sha, timestamp, token
from kg.evidence.errors import EvidenceServiceError
from kg.ids import digest
from kg.models.foundation import CreateOnly, PutDocument, RemoveDocument, WriteRequest


def _content(
    connection: sqlite3.Connection,
    doc: str,
    payload: PutDocument,
    state: str,
    at: datetime,
) -> tuple[str, str]:
    content = payload.content.text.encode()
    revision_id = digest("e1-revision/1", doc, content)
    stored = connection.execute(
        "SELECT * FROM revision WHERE document_id=? AND content_hash=?",
        (doc, sha(content)),
    ).fetchone()
    if stored:
        if stored["revision_id"] != revision_id or stored["content"] != content:
            raise EvidenceServiceError("internal_error")
    else:
        sequence = connection.execute(
            "SELECT coalesce(max(sequence),0)+1 FROM revision WHERE document_id=?",
            (doc,),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO revision(revision_id,document_id,sequence,content_hash,content,"
            "byte_length,created_at) VALUES (?,?,?,?,?,?,?)",
            (revision_id, doc, sequence, sha(content), content, len(content), timestamp(at)),
        )
    anchors = sorted(payload.content.anchors, key=lambda item: item.local_id)
    ids = [
        digest("e1-anchor/1", revision_id, a.local_id, str(a.start), str(a.end), a.quote)
        for a in anchors
    ]
    set_id = digest("e1-anchor-set/1", revision_id, *ids)
    existing_set = connection.execute(
        "SELECT 1 FROM anchor_set WHERE set_id=?",
        (set_id,),
    ).fetchone()
    if existing_set:
        stored_ids = [
            row[0]
            for row in connection.execute(
                "SELECT anchor_id FROM anchor_set_member WHERE set_id=? ORDER BY local_id",
                (set_id,),
            )
        ]
        if stored_ids != ids:
            raise EvidenceServiceError("internal_error")
    else:
        connection.execute(
            "INSERT INTO anchor_set(set_id,revision_id) VALUES (?,?)", (set_id, revision_id)
        )
    ordinal = connection.execute(
        "SELECT coalesce(max(ordinal),0) FROM anchor WHERE revision_id=?",
        (revision_id,),
    ).fetchone()[0]
    for a, anchor_id in zip(anchors, ids, strict=True):
        stored_anchor = connection.execute(
            "SELECT * FROM anchor WHERE anchor_id=?",
            (anchor_id,),
        ).fetchone()
        if stored_anchor:
            expected = (
                doc,
                revision_id,
                a.local_id,
                a.start,
                a.end,
                a.quote,
                sha(a.quote.encode()),
                "supplied",
                anchor_id,
            )
            actual = tuple(
                stored_anchor[key]
                for key in (
                    "document_id",
                    "revision_id",
                    "local_id",
                    "start_offset",
                    "end_offset",
                    "quote",
                    "quote_hash",
                    "origin_kind",
                    "origin_key",
                )
            )
            if actual != expected:
                raise EvidenceServiceError("internal_error")
        else:
            ordinal += 1
            connection.execute(
                "INSERT INTO anchor(anchor_id,document_id,revision_id,local_id,ordinal,"
                "start_offset,end_offset,quote,quote_hash,origin_state,origin_kind,origin_key) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,'supplied',?)",
                (
                    anchor_id,
                    doc,
                    revision_id,
                    a.local_id,
                    ordinal,
                    a.start,
                    a.end,
                    a.quote,
                    sha(a.quote.encode()),
                    state,
                    anchor_id,
                ),
            )
    if not existing_set:
        ordered = sorted(
            zip(anchors, ids, strict=True),
            key=lambda pair: (
                pair[0].start,
                pair[0].end,
                pair[0].local_id,
            ),
        )
        connection.executemany(
            "INSERT INTO anchor_set_member(set_id,revision_id,anchor_id,local_id,ordinal) "
            "VALUES (?,?,?,?,?)",
            [
                (set_id, revision_id, anchor_id, anchor.local_id, index)
                for index, (anchor, anchor_id) in enumerate(ordered, start=1)
            ],
        )
    return revision_id, set_id


def apply(
    connection: sqlite3.Connection,
    request: WriteRequest,
    at: datetime,
) -> tuple[str, Literal["applied", "unchanged"]]:
    payload = request.payload
    if not isinstance(payload, (PutDocument, RemoveDocument)):
        raise EvidenceServiceError("unsupported")
    external = payload.document
    existing = connection.execute(
        "SELECT * FROM document WHERE corpus_id=? AND namespace=? AND external_id=?",
        (request.scope.corpus_id, external.source_namespace, external.external_id),
    ).fetchone()
    if existing and (
        existing["owner_id"] != request.attribution.owner_id
        or existing["synchronization_scope"] != external.synchronization_scope
    ):
        raise EvidenceServiceError("forbidden")
    previous = _store.head(connection, existing["document_id"]) if existing else None
    if isinstance(payload.precondition, CreateOnly):
        if existing:
            raise EvidenceServiceError("state_conflict")
    else:
        if previous is None:
            raise EvidenceServiceError("not_found")
        if previous["state_version"] != payload.precondition.state_version:
            raise EvidenceServiceError("state_conflict")
    namespace = connection.execute(
        "SELECT policy_token FROM source_namespace WHERE corpus_id=? AND namespace=?",
        (request.scope.corpus_id, external.source_namespace),
    ).fetchone()
    if namespace is None:
        raise EvidenceServiceError("forbidden")
    doc = existing["document_id"] if existing else token()
    state = token()
    if not existing:
        connection.execute(
            "INSERT INTO document(document_id,corpus_id,namespace,external_id,owner_id,"
            "synchronization_scope,current_state) VALUES (?,?,?,?,?,?,?)",
            (
                doc,
                request.scope.corpus_id,
                external.source_namespace,
                external.external_id,
                request.attribution.owner_id,
                external.synchronization_scope,
                state,
            ),
        )
    if isinstance(payload, PutDocument):
        revision_id, set_id = _content(connection, doc, payload, state, at)
        metadata = metadata_json(payload.metadata)
        source = "active"
        policy = payload.content.passage_policy
    else:
        if previous is None:
            raise EvidenceServiceError("not_found")
        revision_id, set_id = previous["revision_id"], previous["set_id"]
        metadata, source, policy = previous["metadata_json"], "inactive", previous["passage_policy"]
    if previous and (
        previous["revision_id"],
        previous["set_id"],
        previous["metadata_json"],
        previous["source"],
        previous["passage_policy"],
        previous["namespace_token"],
    ) == (revision_id, set_id, metadata, source, policy, namespace[0]):
        return doc, "unchanged"
    _store.add_state(
        connection,
        document_id=doc,
        revision_id=revision_id,
        set_id=set_id,
        metadata=metadata,
        source=source,
        namespace_token=namespace[0],
        passage_policy=policy,
        kind="put" if isinstance(payload, PutDocument) else "remove",
        at=at,
        state_version=state,
        previous=previous,
    )
    return doc, "applied"
