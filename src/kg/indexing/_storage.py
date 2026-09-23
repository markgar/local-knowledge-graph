"""Fenced owner transactions and bounded disposable payload reclamation."""

import sqlite3
from dataclasses import dataclass

from kg.evidence._authorization import authorize, authorize_writer
from kg.evidence._reads import scoped_document
from kg.evidence._values import now, sha, timestamp, token
from kg.evidence.errors import EvidenceServiceError
from kg.indexing import _configuration
from kg.indexing._passages import _authorized_state, current_state
from kg.models.evidence import LocalIdentity
from kg.models.foundation import Attribution, ExternalDocument, Scope
from kg.models.indexing import CleanupResult, IndexConfiguration


class Superseded(EvidenceServiceError):
    def __init__(self) -> None:
        super().__init__("state_conflict")


_STALE_POINTER = (
    "NOT EXISTS (SELECT 1 FROM document_projection p JOIN document d "
    "ON d.document_id=p.document_id JOIN document_state s "
    "ON s.document_id=d.document_id AND s.state_version=d.current_state "
    "JOIN source_namespace n ON n.corpus_id=d.corpus_id AND n.namespace=d.namespace "
    "WHERE p.projection_id=active_document_projection.projection_id "
    "AND p.state_version=s.state_version AND s.source='active' "
    "AND s.namespace_token=n.policy_token)"
)


@dataclass(frozen=True)
class Attempt:
    attempt_id: str
    document_id: str
    revision_id: str
    state_version: str
    namespace: str
    configuration_id: str
    fence: int


def owner(
    connection: sqlite3.Connection,
    identity: LocalIdentity,
    scope: Scope,
    attribution: Attribution,
    document_id: str,
) -> sqlite3.Row:
    authorize(connection, identity, scope, "read")
    doc = scoped_document(connection, scope, document_id)
    authorize_writer(
        connection,
        identity,
        scope,
        attribution,
        ExternalDocument(
            source_namespace=doc["namespace"],
            synchronization_scope=doc["synchronization_scope"],
            external_id=doc["external_id"],
        ),
    )
    if doc["owner_id"] != attribution.owner_id:
        raise EvidenceServiceError("forbidden")
    return doc


def admit(
    connection: sqlite3.Connection,
    identity: LocalIdentity,
    scope: Scope,
    attribution: Attribution,
    document_id: str,
    state_version: str,
    configuration: IndexConfiguration,
) -> Attempt:
    state = _authorized_state(
        connection,
        identity,
        scope,
        attribution,
        document_id,
        state_version,
    )
    doc = scoped_document(connection, scope, document_id)
    config_id = _configuration.configuration_id(configuration)
    descriptor = _configuration.descriptor(configuration)
    existing = connection.execute(
        "SELECT descriptor_json,descriptor_hash FROM index_configuration WHERE configuration_id=?",
        (config_id,),
    ).fetchone()
    if existing is None:
        connection.execute(
            "INSERT INTO index_configuration VALUES (?,?,?)",
            (config_id, descriptor, sha(descriptor.encode())),
        )
    elif tuple(existing) != (descriptor, sha(descriptor.encode())):
        raise EvidenceServiceError("internal_error")
    slot = connection.execute(
        "SELECT * FROM index_work_slot WHERE corpus_id=? AND document_id=? AND configuration_id=?",
        (scope.corpus_id, document_id, config_id),
    ).fetchone()
    fence = slot["fence"] + 1 if slot is not None else 1
    attempt_id = token()
    at = timestamp(now())
    if slot is None:
        connection.execute(
            "INSERT INTO index_work_slot VALUES (?,?,?,?,?,NULL)",
            (scope.corpus_id, doc["namespace"], document_id, config_id, 0),
        )
    else:
        connection.execute(
            "UPDATE index_attempt SET status='superseded',reason='superseded_attempt',"
            "completed_at=? "
            "WHERE attempt_id=? AND status IN ('admitted','staging')",
            (at, slot["latest_attempt_id"]),
        )
    connection.execute(
        "INSERT INTO index_attempt(attempt_id,corpus_id,namespace,document_id,revision_id,"
        "state_version,configuration_id,fence,status,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,'admitted',?)",
        (
            attempt_id,
            scope.corpus_id,
            doc["namespace"],
            document_id,
            state["revision_id"],
            state_version,
            config_id,
            fence,
            at,
        ),
    )
    connection.execute(
        "UPDATE index_work_slot SET fence=?,latest_attempt_id=? "
        "WHERE corpus_id=? AND document_id=? AND configuration_id=?",
        (fence, attempt_id, scope.corpus_id, document_id, config_id),
    )
    return Attempt(
        attempt_id,
        document_id,
        state["revision_id"],
        state_version,
        doc["namespace"],
        config_id,
        fence,
    )


def guard(
    connection: sqlite3.Connection,
    identity: LocalIdentity,
    scope: Scope,
    attribution: Attribution,
    attempt: Attempt,
) -> None:
    owner(connection, identity, scope, attribution, attempt.document_id)
    slot = connection.execute(
        "SELECT s.fence,s.latest_attempt_id,a.status FROM index_work_slot s "
        "JOIN index_attempt a ON a.attempt_id=s.latest_attempt_id "
        "WHERE s.corpus_id=? AND s.document_id=? AND s.configuration_id=?",
        (scope.corpus_id, attempt.document_id, attempt.configuration_id),
    ).fetchone()
    if (
        slot is None
        or slot["fence"] != attempt.fence
        or slot["latest_attempt_id"] != attempt.attempt_id
        or slot["status"] not in ("admitted", "staging")
    ):
        raise Superseded()
    _authorized_state(
        connection,
        identity,
        scope,
        attribution,
        attempt.document_id,
        attempt.state_version,
    )


def active(
    connection: sqlite3.Connection,
    scope: Scope,
    document_id: str,
    configuration_id: str,
) -> sqlite3.Row | None:
    row = connection.execute(
        "SELECT p.* FROM active_document_projection a JOIN document_projection p "
        "ON p.projection_id=a.projection_id WHERE a.corpus_id=? AND a.document_id=? "
        "AND a.configuration_id=?",
        (scope.corpus_id, document_id, configuration_id),
    ).fetchone()
    return row if isinstance(row, sqlite3.Row) else None


def cleanup(
    connection: sqlite3.Connection,
    scope: Scope,
    document_id: str,
    configuration_id: str,
    limit: int,
    *,
    preserve_previous: bool = False,
) -> CleanupResult:
    """Delete at most limit derived rows, retaining live staging and the active projection."""
    params = (scope.corpus_id, document_id, configuration_id)
    connection.execute(
        "UPDATE index_attempt SET status='stale',reason='state_changed',completed_at=? "
        "WHERE corpus_id=? AND document_id=? AND configuration_id=? "
        "AND status IN ('admitted','staging') AND NOT EXISTS ("
        "SELECT 1 FROM document d JOIN document_state s ON s.state_version=d.current_state "
        "JOIN source_namespace n ON n.corpus_id=d.corpus_id AND n.namespace=d.namespace "
        "WHERE d.document_id=index_attempt.document_id "
        "AND s.state_version=index_attempt.state_version AND s.source='active' "
        "AND s.namespace_token=n.policy_token)",
        (timestamp(now()), *params),
    )
    removed = 0
    for table, predicate in (
        (
            "active_document_projection",
            "corpus_id=? AND document_id=? AND configuration_id=? AND " + _STALE_POINTER,
        ),
        (
            "index_staging_member",
            "attempt_id IN (SELECT attempt_id FROM index_attempt "
            "WHERE corpus_id=? AND document_id=? AND configuration_id=? "
            "AND status NOT IN ('admitted','staging'))",
        ),
        (
            "projection_member",
            "projection_id IN (SELECT projection_id FROM document_projection "
            "WHERE corpus_id=? AND document_id=? AND configuration_id=? "
            "AND projection_id NOT IN (SELECT projection_id FROM active_document_projection))",
        ),
        (
            "document_projection",
            "corpus_id=? AND document_id=? AND configuration_id=? "
            "AND projection_id NOT IN (SELECT projection_id FROM active_document_projection) "
            "AND NOT EXISTS (SELECT 1 FROM projection_member m "
            "WHERE m.projection_id=document_projection.projection_id)",
        ),
        (
            "index_attempt",
            "attempt_id IN (SELECT attempt_id FROM index_attempt "
            "WHERE corpus_id=? AND document_id=? AND configuration_id=? "
            "AND status NOT IN ('admitted','staging') ORDER BY fence DESC LIMIT -1 OFFSET 100) "
            "AND attempt_id NOT IN (SELECT latest_attempt_id FROM index_work_slot "
            "WHERE latest_attempt_id IS NOT NULL) "
            "AND NOT EXISTS (SELECT 1 FROM index_staging_member m "
            "WHERE m.attempt_id=index_attempt.attempt_id)",
        ),
    ):
        if table == "active_document_projection" and preserve_previous:
            continue
        rows = connection.execute(
            f"SELECT rowid FROM {table} WHERE {predicate} LIMIT ?",
            (*params, limit - removed),
        ).fetchall()
        for row in rows:
            connection.execute(f"DELETE FROM {table} WHERE rowid=?", (row[0],))
        removed += len(rows)
        if removed == limit:
            break
    return CleanupResult(
        removed=removed,
        has_more=obsolete(
            connection,
            scope,
            document_id,
            configuration_id,
            preserve_previous=preserve_previous,
        ),
    )


def obsolete(
    connection: sqlite3.Connection,
    scope: Scope,
    document_id: str,
    configuration_id: str,
    *,
    preserve_previous: bool = False,
) -> bool:
    params = (scope.corpus_id, document_id, configuration_id)
    return bool(
        connection.execute(
            "SELECT EXISTS (SELECT 1 FROM index_staging_member m JOIN index_attempt a "
            "ON a.attempt_id=m.attempt_id WHERE a.corpus_id=? AND a.document_id=? "
            "AND a.configuration_id=? AND a.status NOT IN ('admitted','staging')) "
            "OR EXISTS (SELECT 1 FROM document_projection p WHERE p.corpus_id=? "
            "AND p.document_id=? AND p.configuration_id=? AND p.projection_id NOT IN "
            "(SELECT projection_id FROM active_document_projection)) "
            "OR (SELECT count(*) FROM index_attempt WHERE corpus_id=? AND document_id=? "
            "AND configuration_id=? AND status NOT IN ('admitted','staging'))>100 "
            "OR (? AND EXISTS (SELECT 1 FROM active_document_projection WHERE "
            "corpus_id=? AND document_id=? AND configuration_id=? AND " + _STALE_POINTER + "))",
            (*params, *params, *params, not preserve_previous, *params),
        ).fetchone()[0]
    )


def current_source(connection: sqlite3.Connection, doc: sqlite3.Row) -> tuple[sqlite3.Row, bool]:
    state = current_state(connection, doc["document_id"])
    namespace = connection.execute(
        "SELECT policy_token FROM source_namespace WHERE corpus_id=? AND namespace=?",
        (doc["corpus_id"], doc["namespace"]),
    ).fetchone()
    return state, namespace is not None and namespace[0] == state["namespace_token"]
