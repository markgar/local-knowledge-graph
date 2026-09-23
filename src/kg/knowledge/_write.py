"""Atomic enrichment kernel; the evidence dispatcher owns commit and coordination."""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Literal

from kg._execution_budget import PrivateBudget
from kg.diagnostics._collector import Capture, CaptureUnavailable
from kg.diagnostics._targets import (
    KnowledgeTarget,
    KnowledgeWriterTarget,
    ReportTarget,
    SeedSetTarget,
)
from kg.evidence import _receipts
from kg.evidence._support import TransactionEvidence
from kg.evidence._transactions import CanonicalWriteContext
from kg.evidence._values import canonical, sha, timestamp, token
from kg.evidence.errors import EvidenceServiceError
from kg.knowledge._authorization import writer
from kg.knowledge._store import Store
from kg.models.foundation import (
    AddAlias,
    AddAssertion,
    AddEntitySupport,
    AddIdentifier,
    AddMention,
    AssertionWithdrawalReceipt,
    Change,
    ChangeSet,
    ChangeSetReceipt,
    CreateEntity,
    EntityObject,
    EntityRef,
    IDMapping,
    LocalEntity,
    SeedSupport,
    SourceSupport,
    StoredEntity,
    Value,
    WithdrawAssertion,
    WriteRequest,
)
from kg.models.knowledge_events import KnowledgeEncoding, KnowledgeValidation


def _observed(
    capture: Capture | CaptureUnavailable | None,
    event: KnowledgeEncoding | KnowledgeValidation,
) -> None:
    if capture is not None:
        with capture.guard():
            capture.append(event)


class Manifest(Value):
    owner_id: str
    writer_id: str
    targets: tuple[ReportTarget, ...]


def digest(request: WriteRequest) -> str:
    return sha(
        canonical(
            {
                "corpus_id": request.scope.corpus_id,
                "attribution": request.attribution.model_dump(mode="json"),
                "payload": request.payload.model_dump(mode="json"),
            }
        ).encode()
    )


def writer_targets(request: WriteRequest) -> tuple[KnowledgeWriterTarget | SeedSetTarget, ...]:
    assert isinstance(request.payload, ChangeSet)
    targets: dict[KnowledgeWriterTarget | SeedSetTarget, None] = {}
    for change in request.payload.changes:
        support = change.support
        if isinstance(support, SeedSupport):
            target = SeedSetTarget(
                namespace=support.source_namespace,
                owner_id=request.attribution.owner_id,
                writer_id=request.attribution.writer_id,
                seed_set_id=support.seed_set_id,
            )
            targets[target] = None
        else:
            for reference in support.evidence:
                targets[
                    KnowledgeWriterTarget(
                        namespace=reference.source_namespace,
                        owner_id=request.attribution.owner_id,
                        writer_id=request.attribution.writer_id,
                    )
                ] = None
    return tuple(targets)


def authorize_new(context: CanonicalWriteContext, request: WriteRequest) -> None:
    for target in writer_targets(request):
        writer(
            context.connection,
            context.identity,
            request.scope,
            target.namespace,
            target.owner_id,
            target.writer_id,
            seed=isinstance(target, SeedSetTarget),
        )


def _resolved(change: Change, ids: dict[str, str]) -> Change:
    def ref(value: EntityRef) -> StoredEntity:
        return StoredEntity(
            kind="stored",
            entity_id=ids[value.local_id] if isinstance(value, LocalEntity) else value.entity_id,
        )

    if isinstance(change, (AddAlias, AddIdentifier, AddMention)):
        return change.model_copy(update={"entity": ref(change.entity)})
    if isinstance(change, AddAssertion):
        obj = change.object
        if isinstance(obj, EntityObject):
            obj = obj.model_copy(update={"entity": ref(obj.entity)})
        return change.model_copy(update={"subject": ref(change.subject), "object": obj})
    return change


def _comparable(change: Change, entity_id: str | None = None) -> dict[str, object]:
    result = change.model_dump(mode="json", exclude={"local_id"})
    if isinstance(change, CreateEntity):
        result["kind"] = "entity_support"
        result["entity"] = {"kind": "stored", "entity_id": entity_id}
    return result


def apply(
    context: CanonicalWriteContext,
    request: WriteRequest,
    at: datetime,
    budget: PrivateBudget,
    *,
    capture: Capture | CaptureUnavailable | None = None,
) -> tuple[ChangeSetReceipt, Literal["applied", "unchanged"], str, Manifest]:
    assert isinstance(request.payload, ChangeSet)
    connection, scope, attribution = context.connection, request.scope, request.attribution
    store = Store(connection, scope, budget)
    try:
        authorize_new(context, request)
        _observed(capture, KnowledgeValidation(phase="binding", status="passed"))
        schema = store.schema()
        _observed(capture, KnowledgeValidation(phase="schema", status="passed"))
        changes = request.payload.changes
        references = tuple(
            dict.fromkeys(
                r
                for c in changes
                if isinstance(c.support, SourceSupport)
                for r in c.support.evidence
            )
        )
        # Common E1 validator remains authoritative. Preflight/check each exact
        # source once with private scratch before it hydrates the bounded unit.
        dependencies = {d.document_id: d for d in request.payload.dependencies}
        TransactionEvidence(context).validate_current(scope, request.payload.dependencies, ())
        for reference in references:
            _, current = store.proof(reference, dependencies[reference.document_id].state_version)
            if not current:
                raise EvidenceServiceError("state_conflict")
        total = 1
        working = 1
        for reference in references:
            size = connection.execute(
                "SELECT length(CAST(a.quote AS BLOB)),r.byte_length,"
                "length(CAST(m.metadata_json AS BLOB)) FROM anchor a "
                "JOIN revision r ON r.revision_id=a.revision_id AND r.document_id=a.document_id "
                "JOIN metadata_snapshot m ON m.document_id=r.document_id "
                "AND m.revision_id=r.revision_id WHERE a.anchor_id=? AND m.state_version=?",
                (reference.anchor_id, dependencies[reference.document_id].state_version),
            ).fetchone()
            if size is None:
                raise EvidenceServiceError("internal_error")
            total += size[0] * 4
            working = max(working, size[1] * 5 + size[0] * 5 + size[2] * 12)
        store.hold(total)
        with budget.reserve_scratch(working, "general"):
            validated = TransactionEvidence(context).validate_current(
                scope,
                request.payload.dependencies,
                references,
            )
        _observed(capture, KnowledgeValidation(phase="support", status="passed"))
        proofs = {p.reference: p for p in validated}
        key_id = token()
        next_entity = connection.execute(
            "SELECT COALESCE(MAX(creation_sequence),0)+1 FROM entity WHERE corpus_id=?",
            (scope.corpus_id,),
        ).fetchone()[0]
        next_contribution = connection.execute(
            "SELECT COALESCE(MAX(sequence),0)+1 FROM contribution WHERE corpus_id=?",
            (scope.corpus_id,),
        ).fetchone()[0]
        ids: dict[str, str] = {}
        old_slots: dict[str, tuple[str, str | None]] = {}
        seed_keys: set[tuple[str, str, str]] = set()
        for change in changes:
            if isinstance(change.support, SeedSupport):
                s = change.support
                slot_key = s.source_namespace, s.seed_set_id, s.seed_key
                if slot_key in seed_keys:
                    raise EvidenceServiceError("invalid_request")
                seed_keys.add(slot_key)
                slot = connection.execute(
                    "SELECT * FROM seed_slot WHERE corpus_id=? AND namespace=? AND owner_id=? "
                    "AND writer_id=? AND seed_set_id=? AND seed_key=?",
                    (
                        scope.corpus_id,
                        s.source_namespace,
                        attribution.owner_id,
                        attribution.writer_id,
                        s.seed_set_id,
                        s.seed_key,
                    ),
                ).fetchone()
                if slot is not None and slot["current_contribution_id"] is not None:
                    old_slots[change.local_id] = (
                        slot["current_contribution_id"],
                        slot["entity_id"],
                    )
                elif slot is not None:
                    raise EvidenceServiceError("state_conflict")
            if isinstance(change, CreateEntity):
                if change.entity_type not in schema.entity_types:
                    raise EvidenceServiceError("invalid_request")
                prior = old_slots.get(change.local_id)
                ids[change.local_id] = prior[1] if prior and prior[1] else token()
                if prior is None:
                    connection.execute(
                        "INSERT INTO entity VALUES (?,?,?,?,?,0)",
                        (
                            ids[change.local_id],
                            scope.corpus_id,
                            change.name,
                            change.entity_type,
                            next_entity,
                        ),
                    )
                    next_entity += 1
            elif isinstance(change, AddEntitySupport):
                entity = store.row("entity", change.entity.entity_id)
                store.require_entity(change.entity.entity_id, history=True)
                if entity["retired"] or (entity["name"], entity["entity_type"]) != (
                    change.name,
                    change.entity_type,
                ):
                    raise EvidenceServiceError("invalid_request")
        planned = tuple(_resolved(c, ids) for c in changes)
        predicates = {p.name: p for p in schema.predicates}
        for change in planned:
            endpoints = []
            if isinstance(change, (AddAlias, AddIdentifier, AddMention)):
                assert isinstance(change.entity, StoredEntity)
                endpoints.append(change.entity.entity_id)
            elif isinstance(change, AddAssertion):
                assert isinstance(change.subject, StoredEntity)
                endpoints.append(change.subject.entity_id)
                if isinstance(change.object, EntityObject):
                    assert isinstance(change.object.entity, StoredEntity)
                    endpoints.append(change.object.entity.entity_id)
            for endpoint in endpoints:
                if endpoint not in ids.values():
                    store.require_entity(endpoint, history=True)
            if isinstance(change, AddIdentifier) and change.scheme not in schema.identifier_schemes:
                raise EvidenceServiceError("invalid_request")
            if isinstance(change, AddAssertion):
                assert isinstance(change.subject, StoredEntity)
                predicate = predicates.get(change.predicate)
                subject = store.row("entity", change.subject.entity_id)
                if (
                    predicate is None
                    or subject["entity_type"] not in predicate.subject_types
                    or change.object.kind != predicate.object_kind
                ):
                    raise EvidenceServiceError("invalid_request")
                if isinstance(change.object, EntityObject):
                    assert isinstance(change.object.entity, StoredEntity)
                    object_entity = store.row("entity", change.object.entity.entity_id)
                    if object_entity["entity_type"] not in predicate.object_types:
                        raise EvidenceServiceError("invalid_request")
                if predicate.record_projection and change.interpretation != "explicit":
                    raise EvidenceServiceError("invalid_request")
                _observed(
                    capture,
                    KnowledgeEncoding(
                        encoding=(
                            "direct-subject-decision/1"
                            if predicate.record_projection
                            else "ordinary"
                        ),
                        status="accepted",
                        configuration_id=schema.schema_version,
                    ),
                )
        _observed(capture, KnowledgeValidation(phase="type", status="passed"))
        mappings: dict[str, str] = {}
        contributions: dict[str, str] = {}
        generations: dict[tuple[str, str], int] = {}
        active_slots: dict[tuple[str, str], int] = {}
        # Creation support precedes endpoint checks, allowing forward attestations
        # to reactivate an historically visible stored identity atomically.
        ordered = sorted(planned, key=lambda c: not isinstance(c, (CreateEntity, AddEntitySupport)))
        for change in ordered:
            prior = old_slots.get(change.local_id)
            if prior:
                old = store.contribution(prior[0], history=True)
                if (
                    _comparable(old.payload) != _comparable(change, prior[1])
                    or old.attribution != attribution
                ):
                    raise EvidenceServiceError("state_conflict")
                mappings[change.local_id] = (
                    ids[change.local_id] if isinstance(change, CreateEntity) else prior[0]
                )
                contributions[change.local_id] = prior[0]
                continue
            cid = token()
            contributions[change.local_id] = cid
            mappings[change.local_id] = (
                ids[change.local_id] if isinstance(change, CreateEntity) else cid
            )
            kind = "entity_support" if isinstance(change, CreateEntity) else change.kind
            connection.execute(
                "INSERT INTO contribution VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    cid,
                    scope.corpus_id,
                    next_contribution,
                    kind,
                    key_id,
                    change.local_id,
                    attribution.owner_id,
                    attribution.writer_id,
                    attribution.producer,
                    attribution.producer_version,
                    attribution.model_id,
                    attribution.configuration_id,
                    schema.schema_version,
                    timestamp(at),
                    change.support.kind,
                ),
            )
            next_contribution += 1
            if isinstance(change, (CreateEntity, AddEntitySupport)):
                entity_id = (
                    ids[change.local_id]
                    if isinstance(change, CreateEntity)
                    else change.entity.entity_id
                )
                connection.execute(
                    "INSERT INTO entity_support VALUES (?,?,'entity_support',?,?,?)",
                    (scope.corpus_id, cid, entity_id, change.name, change.entity_type),
                )
            elif isinstance(change, AddMention):
                assert isinstance(change.entity, StoredEntity)
                connection.execute(
                    "INSERT INTO mention VALUES (?,?,'mention',?)",
                    (scope.corpus_id, cid, change.entity.entity_id),
                )
            elif isinstance(change, (AddAlias, AddIdentifier)):
                assert isinstance(change.entity, StoredEntity)
                if isinstance(change, AddAlias):
                    connection.execute(
                        "INSERT INTO alias VALUES (?,?,'alias',?,?)",
                        (scope.corpus_id, cid, change.entity.entity_id, change.alias),
                    )
                else:
                    connection.execute(
                        "INSERT INTO identifier VALUES (?,?,'identifier',?,?,?)",
                        (
                            scope.corpus_id,
                            cid,
                            change.entity.entity_id,
                            change.scheme,
                            change.value,
                        ),
                    )
            elif isinstance(change, AddAssertion):
                assert isinstance(change.subject, StoredEntity)
                values: dict[str, object] = dict.fromkeys(
                    ("entity", "string", "integer", "boolean", "timestamp"),
                )
                obj = change.object
                if isinstance(obj, EntityObject):
                    assert isinstance(obj.entity, StoredEntity)
                    values["entity"] = obj.entity.entity_id
                elif obj.kind == "timestamp":
                    values[obj.kind] = timestamp(obj.value)
                elif obj.kind == "integer":
                    values[obj.kind] = str(obj.value)
                else:
                    values[obj.kind] = obj.value
                connection.execute(
                    "INSERT INTO assertion VALUES (?,?,'assertion',?,?,?,?,?,?,?,?,?)",
                    (
                        scope.corpus_id,
                        cid,
                        change.subject.entity_id,
                        change.predicate,
                        change.interpretation,
                        obj.kind,
                        *values.values(),
                    ),
                )
            else:
                raise EvidenceServiceError("unsupported")
            if isinstance(change.support, SourceSupport):
                for ordinal, reference in enumerate(change.support.evidence, 1):
                    proof = proofs[reference]
                    passage_set_id = None
                    if reference.passage_id is not None:
                        # Membership was validated by E3 on this same owner transaction.
                        passage = connection.execute(
                            "SELECT passage_set_id FROM passage WHERE passage_id=?",
                            (reference.passage_id,),
                        ).fetchone()
                        if passage is None:
                            raise EvidenceServiceError("internal_error")
                        passage_set_id = passage[0]
                    connection.execute(
                        "INSERT INTO contribution_evidence "
                        "VALUES (?,?,?,'source',?,?,?,?,?,?,?,?)",
                        (
                            scope.corpus_id,
                            cid,
                            ordinal,
                            reference.source_namespace,
                            reference.document_id,
                            reference.revision_id,
                            reference.anchor_id,
                            proof.state_version,
                            proof.metadata_snapshot_id,
                            passage_set_id,
                            reference.passage_id,
                        ),
                    )
            else:
                s = change.support
                set_key = s.source_namespace, s.seed_set_id
                args = (
                    scope.corpus_id,
                    s.source_namespace,
                    attribution.owner_id,
                    attribution.writer_id,
                    s.seed_set_id,
                )
                if set_key not in generations:
                    previous = connection.execute(
                        "SELECT generation FROM seed_set WHERE corpus_id=? AND namespace=? "
                        "AND owner_id=? AND writer_id=? AND seed_set_id=?",
                        args,
                    ).fetchone()
                    generation = previous[0] + 1 if previous else 1
                    connection.execute(
                        "INSERT INTO seed_set VALUES (?,?,?,?,?,?,?) ON CONFLICT "
                        "(corpus_id,namespace,owner_id,writer_id,seed_set_id) "
                        "DO UPDATE SET generation=excluded.generation",
                        (*args, generation, timestamp(at)),
                    )
                    generations[set_key] = generation
                    active_slots[set_key] = len(
                        connection.execute(
                            "SELECT seed_key FROM seed_slot WHERE corpus_id=? AND namespace=? "
                            "AND owner_id=? AND writer_id=? AND seed_set_id=? "
                            "AND current_contribution_id IS NOT NULL LIMIT 101",
                            args,
                        ).fetchall()
                    )
                if active_slots[set_key] >= 100:
                    raise EvidenceServiceError("invalid_request")
                active_slots[set_key] += 1
                connection.execute(
                    "INSERT INTO seed_slot VALUES (?,?,?,?,?,?,?,?)",
                    (*args, s.seed_key, ids.get(change.local_id), cid),
                )
                connection.execute(
                    "INSERT INTO contribution_seed VALUES (?,?,'seed',?,?,?,?,?)",
                    (
                        scope.corpus_id,
                        cid,
                        s.source_namespace,
                        attribution.owner_id,
                        attribution.writer_id,
                        s.seed_set_id,
                        s.seed_key,
                    ),
                )
                seq = connection.execute(
                    "SELECT COALESCE(MAX(sequence),0)+1 FROM seed_membership_event "
                    "WHERE corpus_id=? "
                    "AND namespace=? AND owner_id=? AND writer_id=? AND seed_set_id=?",
                    args,
                ).fetchone()[0]
                connection.execute(
                    "INSERT INTO seed_membership_event "
                    "VALUES (?,?,?,?,?,?,?,?,?,'activated',?,?,?,?)",
                    (
                        token(),
                        *args,
                        s.seed_key,
                        generations[set_key],
                        seq,
                        cid,
                        key_id,
                        attribution.model_dump_json(),
                        timestamp(at),
                    ),
                )
        store.refresh_entities()
        targets: list[ReportTarget] = list(writer_targets(request))
        for change in planned:
            cid = contributions[change.local_id]
            try:
                contribution = store.contribution(cid)
            except EvidenceServiceError as error:
                if error.failure.code == "not_found":
                    # Distinguish stale readable endpoints from hidden identities.
                    store.contribution(cid, history=True)
                    raise EvidenceServiceError("state_conflict") from None
                raise
            targets.append(
                KnowledgeTarget(
                    contribution_id=cid,
                    witness_ids=tuple(w.contribution_id for w in contribution.witnesses),
                )
            )
        receipt = ChangeSetReceipt(
            kind="enrichment",
            mappings=tuple(
                IDMapping(local_id=c.local_id, stored_id=mappings[c.local_id]) for c in changes
            ),
        )
        status: Literal["applied", "unchanged"] = (
            "unchanged" if len(old_slots) == len(changes) else "applied"
        )
        manifest = Manifest(
            owner_id=attribution.owner_id,
            writer_id=attribution.writer_id,
            targets=tuple(targets),
        )
        save(context, request, key_id, schema.schema_version, receipt, manifest, status, at)
        return receipt, status, key_id, manifest
    finally:
        store.close()


def save(
    context: CanonicalWriteContext,
    request: WriteRequest,
    key_id: str,
    schema_version: str,
    receipt: ChangeSetReceipt | AssertionWithdrawalReceipt,
    manifest: Manifest,
    status: Literal["applied", "unchanged"],
    at: datetime,
) -> None:
    connection = context.connection
    connection.execute(
        "INSERT INTO write_key VALUES (?,?,?,?,?,?,?,?,?,?,0)",
        (
            key_id,
            request.scope.corpus_id,
            request.attribution.writer_id,
            request.payload.operation,
            sha(request.retry_key.encode()),
            digest(request),
            "k1-request-digest/1",
            status,
            timestamp(at),
            timestamp(at + timedelta(days=30)),
        ),
    )
    connection.execute(
        "INSERT INTO knowledge_write_response VALUES (?,?,?,?,?)",
        (
            key_id, request.scope.corpus_id, receipt.kind,
            receipt.model_dump_json(), manifest.model_dump_json(),
        ),
    )
    connection.execute(
        "INSERT INTO knowledge_write_provenance VALUES (?,?,?,?)",
        (key_id, request.scope.corpus_id, schema_version, request.attribution.model_dump_json()),
    )


def replay(
    context: CanonicalWriteContext,
    request: WriteRequest,
    key_id: str,
    at: datetime,
    budget: PrivateBudget,
) -> tuple[_receipts.ReplayResult, Manifest | None]:
    from kg.knowledge._reports import authorize_target

    key = _receipts.lookup_key(context, _receipts.canonical_key(request))
    if key is None or key["key_id"] != key_id:
        raise EvidenceServiceError("state_conflict")
    response = context.connection.execute(
        "SELECT * FROM knowledge_write_response WHERE key_id=?",
        (key_id,),
    ).fetchone()
    manifest = None
    if response is not None:
        manifest = Manifest.model_validate_json(response["authorization_json"])
        if (manifest.owner_id, manifest.writer_id) != (
            request.attribution.owner_id,
            request.attribution.writer_id,
        ):
            raise EvidenceServiceError("forbidden")
        store = Store(context.connection, request.scope, budget)
        try:
            for target in manifest.targets:
                authorize_target(store, context.identity, target)
        finally:
            store.close()
    else:
        if isinstance(request.payload, WithdrawAssertion):
            from kg.knowledge._withdraw import authorize_target as authorize_withdrawal

            with authorize_withdrawal(context, request, budget):
                pass
        else:
            authorize_new(context, request)
        if not key["expired"]:
            raise EvidenceServiceError("internal_error")
    observed = _receipts.clock(context.connection, at)
    if key["expired"] or timestamp(observed) >= key["expires_at"]:
        context.connection.execute("DELETE FROM knowledge_write_response WHERE key_id=?", (key_id,))
        context.connection.execute("UPDATE write_key SET expired=1 WHERE key_id=?", (key_id,))
        return _receipts.ReplayExpired(EvidenceServiceError("retry_expired").failure), manifest
    if digest(request) != key["digest"]:
        return _receipts.ReplayConflict(EvidenceServiceError("retry_conflict").failure), manifest
    assert response is not None
    receipt: ChangeSetReceipt | AssertionWithdrawalReceipt
    if isinstance(request.payload, WithdrawAssertion):
        receipt = AssertionWithdrawalReceipt.model_validate_json(response["receipt_json"])
        if (
            response["receipt_kind"] != receipt.kind
            or receipt.contribution_id != request.payload.contribution_id
        ):
            raise EvidenceServiceError("internal_error")
    else:
        receipt = ChangeSetReceipt.model_validate_json(response["receipt_json"])
        if response["receipt_kind"] != receipt.kind:
            raise EvidenceServiceError("internal_error")
    return _receipts.ReplaySuccess(key_id, key["status"], receipt), manifest
