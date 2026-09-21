"""Content-free state hooks and E1's bounded-owner absence mutation primitive."""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from kg.evidence._coordination import DocumentTarget
from kg.evidence._transactions import CanonicalWriteContext
from kg.evidence._values import now, timestamp, validated
from kg.evidence.errors import EvidenceServiceError
from kg.models.foundation import Attribution, DocumentReceipt, Value


class StateIntent(Value):
    target: DocumentTarget
    intent_sequence: int
    mutation_epoch: int


def state_created(
    context: CanonicalWriteContext, target: DocumentTarget, owner_id: str,
    synchronization_scope: str, change_kind: Literal["put", "remove", "policy"],
    committed_at: datetime,
) -> StateIntent:
    connection = context.connection
    document = connection.execute(
        "SELECT 1 FROM document d JOIN document_state s ON s.document_id=d.document_id "
        "WHERE d.corpus_id=? AND d.namespace=? AND d.document_id=? AND d.owner_id=? "
        "AND d.synchronization_scope=? AND d.current_state=? AND s.state_version=? "
        "AND s.revision_id=? AND s.namespace_token=?",
        (
            target.corpus_id, target.namespace, target.document_id, owner_id,
            synchronization_scope, target.state_version, target.state_version,
            target.revision_id, target.namespace_token,
        ),
    ).fetchone()
    if document is None:
        raise EvidenceServiceError("internal_error")
    scope = (target.corpus_id, target.namespace, owner_id, synchronization_scope)
    connection.execute(
        "INSERT INTO sync_scope(corpus_id,namespace,owner_id,synchronization_scope,"
        "generation,mutation_epoch) VALUES (?,?,?,?,0,1) "
        "ON CONFLICT(corpus_id,namespace,owner_id,synchronization_scope) "
        "DO UPDATE SET mutation_epoch=mutation_epoch+1",
        scope,
    )
    inserted = connection.execute(
        "INSERT INTO state_intent(corpus_id,namespace,document_id,revision_id,state_version,"
        "change_kind,created_at) VALUES (?,?,?,?,?,?,?)",
        (
            target.corpus_id, target.namespace, target.document_id, target.revision_id,
            target.state_version, change_kind, timestamp(committed_at),
        ),
    )
    row = connection.execute(
        "SELECT mutation_epoch FROM sync_scope WHERE corpus_id=? AND namespace=? "
        "AND owner_id=? AND synchronization_scope=?", scope,
    ).fetchone()
    assert row is not None and inserted.lastrowid is not None
    return StateIntent(
        target=target, intent_sequence=inserted.lastrowid, mutation_epoch=row[0],
    )


def deactivate_absent(
    context: CanonicalWriteContext, target: DocumentTarget, attribution: Attribution, run_id: str,
) -> DocumentReceipt:
    """E4 owns candidate bounds, run/epoch guards and final atomic finish acknowledgement."""
    from kg.evidence import _store
    from kg.evidence._authorization import authorize_writer
    from kg.models.foundation import AccessContext, ExternalDocument, Scope

    target = validated(DocumentTarget, target)
    attribution = validated(Attribution, attribution)
    connection = context.connection
    doc = _store.document(connection, target.corpus_id, target.document_id)
    old = _store.head(connection, target.document_id)
    run = connection.execute(
        "SELECT * FROM sync_run WHERE corpus_id=? AND run_id=?", (target.corpus_id, run_id),
    ).fetchone()
    policy = connection.execute(
        "SELECT policy_version FROM corpus WHERE corpus_id=?", (target.corpus_id,),
    ).fetchone()
    namespace = connection.execute(
        "SELECT policy_token FROM source_namespace WHERE corpus_id=? AND namespace=?",
        (target.corpus_id, target.namespace),
    ).fetchone()
    if (
        run is None or policy is None or namespace is None or run["status"] != "open"
        or run["principal_id"] != context.identity.principal_id
        or run["writer_id"] != attribution.writer_id
        or run["owner_id"] != attribution.owner_id or doc["owner_id"] != attribution.owner_id
        or run["namespace"] != target.namespace or doc["namespace"] != target.namespace
        or run["synchronization_scope"] != doc["synchronization_scope"]
        or old["state_version"] != target.state_version or old["revision_id"] != target.revision_id
        or old["namespace_token"] != target.namespace_token
        or namespace[0] != target.namespace_token
        or run["namespace_token"] != target.namespace_token
        or old["source"] != "active"
    ):
        raise EvidenceServiceError("state_conflict")
    authorize_writer(
        connection, context.identity,
        Scope(corpus_id=target.corpus_id, access=AccessContext(
            principal_id=context.identity.principal_id, policy_version=policy[0],
            namespaces=(target.namespace,), grants=("write_documents",),
        )), attribution,
        ExternalDocument(
            source_namespace=target.namespace, synchronization_scope=doc["synchronization_scope"],
            external_id=doc["external_id"],
        ),
    )
    state = _store.add_state(
        context, document_id=target.document_id, revision_id=target.revision_id,
        set_id=old["set_id"], metadata=old["metadata_json"], source="inactive",
        namespace_token=target.namespace_token, passage_policy=old["passage_policy"],
        kind="remove", at=now(), previous=old,
    )
    connection.execute(
        "INSERT INTO sync_removal_provenance(corpus_id,run_id,namespace,owner_id,"
        "synchronization_scope,document_id,state_version,attribution_json) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (
            target.corpus_id, run_id, target.namespace, attribution.owner_id,
            doc["synchronization_scope"], target.document_id, state, attribution.model_dump_json(),
        ),
    )
    return _store.receipt(connection, target.document_id)
