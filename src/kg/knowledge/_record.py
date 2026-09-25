"""Private record-authoring dispatcher and canonical transaction owner adapter."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Literal

from kg._execution_budget import Deadline, DeadlineStop, PrivateBudget, PrivateResourceStop
from kg.diagnostics._targets import ClassificationEventTarget, ClassificationTarget
from kg.evidence import _receipts
from kg.evidence import _reporting as reporting
from kg.evidence._authorization import authorize
from kg.evidence._coordination import CanonicalKey
from kg.evidence._transactions import CanonicalWriteContext, writing
from kg.evidence._values import canonical, now, sha, timestamp, validated
from kg.evidence.errors import EvidenceServiceError
from kg.knowledge import _classification
from kg.knowledge._authoring import CompiledAuthoring, compile_authoring
from kg.knowledge._authoring_models import (
    AuthoredItemReceipt,
    CaptureReceipt,
    DerivedOperationCounts,
    EntityAuthoringReceipt,
    RecordAuthoringBatch,
    RecordAuthoringBatchResult,
    RecordAuthoringOutcome,
    RecordAuthoringReceipt,
    RecordAuthoringRequest,
    SeedCapture,
    SeedCaptureReceipt,
    SourceCapture,
    SourceCaptureReceipt,
    SupportCapture,
)
from kg.knowledge._reports import retain_manifest
from kg.knowledge._store import Store
from kg.knowledge._write import Manifest
from kg.knowledge._write_models import ChangeSetReceipt
from kg.models.execution_events import CommitEvent, EvidenceEvent
from kg.models.foundation import (
    Failure,
    WriteRequest,
)
from kg.models.knowledge import AssertionPayload
from kg.models.knowledge_events import KnowledgeValidation

if TYPE_CHECKING:
    from kg.diagnostics._collector import Capture, CaptureUnavailable
    from kg.knowledge.service import KnowledgeService


def _capture_key(capture: SupportCapture) -> tuple[str, ...]:
    if isinstance(capture, SourceCapture):
        reference = capture.reference
        return (
            reference.corpus_id,
            reference.source_namespace,
            reference.document_id,
            reference.revision_id,
            reference.anchor_id,
            reference.passage_id or "",
            capture.state_version,
        )
    return capture.source_namespace, capture.seed_set_id, capture.seed_key


def digest(request: RecordAuthoringRequest) -> str:
    document = request.document.model_dump(mode="json")
    support = request.document.support

    def normalized(names: list[str]) -> list[str]:
        return sorted(
            names,
            key=lambda name: _capture_key(support[name]),
        )

    for entity in document["entities"]:
        identity = entity["identity"]
        if isinstance(identity.get("support"), list):
            identity["support"] = normalized(identity["support"])
        for collection in ("classifications", "aliases", "identifiers", "mentions"):
            for item in entity.get(collection, []):
                if isinstance(item.get("support"), list):
                    item["support"] = normalized(item["support"])
    for assertion in document["assertions"]:
        assertion["support"] = normalized(assertion["support"])
    return sha(
        canonical(
            {
                "domain": "record-authoring-request/1",
                "corpus_id": request.scope.corpus_id,
                "attribution": request.attribution.model_dump(mode="json"),
                "document": document,
            }
        ).encode()
    )


def key(request: RecordAuthoringRequest) -> CanonicalKey:
    return CanonicalKey(
        corpus_id=request.scope.corpus_id,
        writer_id=request.attribution.writer_id,
        operation="record_knowledge",
        retry_key_hash=sha(request.retry_key.encode()),
    )


def failed(request_id: str, failure: Failure) -> RecordAuthoringOutcome:
    return RecordAuthoringOutcome(
        request_id=request_id,
        status="conflict"
        if failure.code in {"state_conflict", "retry_conflict"}
        else "failed"
        if failure.code in {"internal_error", "budget_exceeded"}
        else "rejected",
        error=failure,
    )


def _contribution_id(
    context: CanonicalWriteContext,
    request: RecordAuthoringRequest,
    compiled: CompiledAuthoring,
    key_id: str,
    local_id: str,
) -> str:
    row = context.connection.execute(
        "SELECT contribution_id FROM contribution "
        "WHERE corpus_id=? AND key_id=? AND local_id=?",
        (request.scope.corpus_id, key_id, local_id),
    ).fetchone()
    if row is not None:
        return str(row[0])
    names = compiled.support_names.get(local_id, ())
    if len(names) == 1:
        capture = request.document.support[names[0]]
        if isinstance(capture, SeedCapture):
            row = context.connection.execute(
                "SELECT current_contribution_id FROM seed_slot "
                "WHERE corpus_id=? AND namespace=? AND owner_id=? AND writer_id=? "
                "AND seed_set_id=? AND seed_key=?",
                (
                    request.scope.corpus_id,
                    capture.source_namespace,
                    request.attribution.owner_id,
                    request.attribution.writer_id,
                    capture.seed_set_id,
                    capture.seed_key,
                ),
            ).fetchone()
            if row is not None and row[0] is not None:
                return str(row[0])
    raise EvidenceServiceError("internal_error")


def _source_capture(
    store: Store,
    name: str,
    capture: SourceCapture,
) -> SourceCaptureReceipt:
    proof, current = store.proof(capture.reference, capture.state_version)
    if not current:
        raise EvidenceServiceError("state_conflict")
    passage_set_id = None
    if capture.reference.passage_id is not None:
        row = store.connection.execute(
            "SELECT passage_set_id FROM passage WHERE passage_id=?",
            (capture.reference.passage_id,),
        ).fetchone()
        if row is None:
            raise EvidenceServiceError("internal_error")
        passage_set_id = str(row[0])
    return SourceCaptureReceipt(
        name=name,
        evidence=proof,
        passage_set_id=passage_set_id,
    )


def _build_receipt(
    context: CanonicalWriteContext,
    request: RecordAuthoringRequest,
    compiled: CompiledAuthoring,
    canonical_receipt: ChangeSetReceipt,
    key_id: str,
    budget: PrivateBudget,
) -> RecordAuthoringReceipt:
    mapping = {item.local_id: item.stored_id for item in canonical_receipt.mappings}
    store = Store(context.connection, request.scope, budget)
    try:
        captures: dict[str, CaptureReceipt] = {}
        for name in sorted(request.document.support):
            capture = request.document.support[name]
            if isinstance(capture, SourceCapture):
                captures[name] = _source_capture(store, name, capture)
                continue
            assert isinstance(capture, SeedCapture)
            plan_local = next(
                (
                    local_id
                    for local_id, names in compiled.support_names.items()
                    if names == (name,)
                ),
                None,
            )
            if plan_local is None:
                raise EvidenceServiceError("internal_error")
            contribution_id = _contribution_id(
                context, request, compiled, key_id, plan_local,
            )
            row = store.row("contribution", contribution_id)
            witness, current = store.support(row)
            if not current or witness.kind != "seed":
                raise EvidenceServiceError("state_conflict")
            captures[name] = SeedCaptureReceipt(name=name, witness=witness)

        def item(local_id: str, *, assertion: bool = False) -> AuthoredItemReceipt:
            names = compiled.support_names.get(local_id, ())
            subject_classification = None
            object_classification = None
            if assertion:
                contribution = store.contribution(mapping[local_id])
                if not isinstance(contribution.payload, AssertionPayload):
                    raise EvidenceServiceError("internal_error")
                witnesses = {
                    witness.entity_id: witness
                    for witness in contribution.classification_witnesses
                }
                subject_classification = witnesses.get(
                    contribution.payload.subject.entity_id,
                )
                if contribution.payload.object.kind == "entity":
                    object_classification = witnesses.get(
                        contribution.payload.object.entity.entity_id,
                    )
            return AuthoredItemReceipt(
                local_id=compiled.authored_ids[local_id],
                stored_id=mapping[local_id],
                support_names=names,
                captures=tuple(captures[name] for name in names),
                subject_classification=subject_classification,
                object_classification=object_classification,
            )

        entities: list[EntityAuthoringReceipt] = []
        for authored in request.document.entities:
            plan_id = compiled.entity_plan_ids[authored.local_id]
            if plan_id is None:
                identity = authored.identity
                if not hasattr(identity, "entity_id"):
                    raise EvidenceServiceError("internal_error")
                entity_id = identity.entity_id
                identity_contribution_id = None
                identity_support_names: tuple[str, ...] = ()
            else:
                identity = authored.identity
                entity_id = (
                    identity.entity_id
                    if hasattr(identity, "entity_id")
                    else mapping[plan_id]
                )
                identity_contribution_id = _contribution_id(
                    context, request, compiled, key_id, plan_id,
                )
                identity_support_names = compiled.support_names[plan_id]
            head = _classification.head(store, entity_id)
            selected = _classification.selected(store, entity_id)
            entities.append(
                EntityAuthoringReceipt(
                    local_id=authored.local_id,
                    entity_id=entity_id,
                    identity_contribution_id=identity_contribution_id,
                    identity_support_names=identity_support_names,
                    identity_captures=tuple(
                        captures[name] for name in identity_support_names
                    ),
                    classification=selected,
                    selection_id=head["event_id"],
                    selected_claim_id=head["claim_id"],
                    items=tuple(
                        item(local_id)
                        for local_id in compiled.entity_item_ids[authored.local_id]
                    ),
                )
            )
        assertions = tuple(
            item(assertion.local_id, assertion=True)
            for assertion in request.document.assertions
        )
        counts = compiled.operation_counts
        receipt = RecordAuthoringReceipt(
            request_digest=digest(request),
            schema_revision=request.document.expected_schema_revision,
            receipt_id=key_id,
            support=tuple(captures[name] for name in sorted(captures)),
            entities=tuple(entities),
            assertions=assertions,
            derived_operations=DerivedOperationCounts(
                entity=counts["entity"],
                entity_support=counts["entity_support"],
                classification=counts["classification"],
                classification_selection=counts["classification_selection"],
                alias=counts["alias"],
                identifier=counts["identifier"],
                mention=counts["mention"],
                assertion=counts["assertion"],
            ),
            classification_reviews=compiled.review_receipts,
        )
        return receipt
    finally:
        store.close()


def _save(
    context: CanonicalWriteContext,
    request: RecordAuthoringRequest,
    key_id: str,
    schema_version: str,
    receipt: RecordAuthoringReceipt,
    manifest: Manifest,
    status: Literal["applied", "unchanged"],
    at: datetime,
) -> None:
    context.connection.execute(
        "INSERT INTO write_key VALUES (?,?,?,?,?,?,?,?,?,?,0)",
        (
            key_id,
            request.scope.corpus_id,
            request.attribution.writer_id,
            "record_knowledge",
            sha(request.retry_key.encode()),
            digest(request),
            "k1-record-authoring-digest/1",
            status,
            timestamp(at),
            timestamp(at + timedelta(days=30)),
        ),
    )
    context.connection.execute(
        "INSERT INTO knowledge_write_response VALUES (?,?,?,?,?)",
        (
            key_id,
            request.scope.corpus_id,
            receipt.kind,
            receipt.model_dump_json(),
            manifest.model_dump_json(),
        ),
    )
    context.connection.execute(
        "INSERT INTO knowledge_write_provenance VALUES (?,?,?,?)",
        (
            key_id,
            request.scope.corpus_id,
            schema_version,
            request.attribution.model_dump_json(),
        ),
    )


def _replay(
    context: CanonicalWriteContext,
    request: RecordAuthoringRequest,
    key_id: str,
    at: datetime,
    budget: PrivateBudget,
) -> tuple[RecordAuthoringOutcome, Manifest | None]:
    from kg.knowledge._reports import authorize_target

    stored = _receipts.lookup_key(context, key(request))
    if stored is None or stored["key_id"] != key_id:
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
    elif not stored["expired"]:
        raise EvidenceServiceError("internal_error")
    observed = _receipts.clock(context.connection, at)
    if stored["expired"] or timestamp(observed) >= stored["expires_at"]:
        context.connection.execute(
            "DELETE FROM knowledge_write_response WHERE key_id=?",
            (key_id,),
        )
        context.connection.execute(
            "UPDATE write_key SET expired=1 WHERE key_id=?",
            (key_id,),
        )
        return failed(
            request.request_id,
            EvidenceServiceError("retry_expired").failure,
        ), manifest
    if digest(request) != stored["digest"]:
        return failed(
            request.request_id,
            EvidenceServiceError("retry_conflict").failure,
        ), manifest
    if response is None or response["receipt_kind"] != "record_authoring":
        raise EvidenceServiceError("internal_error")
    receipt = RecordAuthoringReceipt.model_validate_json(response["receipt_json"])
    if receipt.receipt_id != key_id or receipt.request_digest != stored["digest"]:
        raise EvidenceServiceError("internal_error")
    return RecordAuthoringOutcome(
        request_id=request.request_id,
        status=stored["status"],
        receipt=receipt,
    ), manifest


def _run(
    service: KnowledgeService,
    request: RecordAuthoringRequest,
    observed_at: datetime,
    capture: Capture | CaptureUnavailable,
    budget: PrivateBudget | None = None,
) -> RecordAuthoringOutcome:
    context: CanonicalWriteContext | None = None
    try:
        root = budget or PrivateBudget(Deadline(time.monotonic() + 30))
        limited = root.limited(max_visits=10_000)
        with writing(service.database, service.identity, budget=limited) as context:
            authorize(
                context.connection,
                service.identity,
                request.scope,
                "write_knowledge",
            )
            authorize(context.connection, service.identity, request.scope, "read")
            with capture.guard():
                capture.append(EvidenceEvent(phase="authorization", decision="passed"))
            stored = _receipts.lookup_key(context, key(request))
            if stored is not None:
                outcome, manifest = _replay(
                    context,
                    request,
                    stored["key_id"],
                    observed_at,
                    limited,
                )
                if manifest is not None:
                    retain_manifest(capture, manifest)
                replay_decision: Literal["replayed", "retry_conflict", "retry_expired"] = (
                    "replayed"
                    if outcome.receipt is not None
                    else "retry_expired"
                    if outcome.error is not None and outcome.error.code == "retry_expired"
                    else "retry_conflict"
                )
                with capture.guard():
                    capture.append(
                        EvidenceEvent(
                            phase="replay",
                            decision=replay_decision,
                        )
                    )
                return outcome
            at = _receipts.clock(context.connection, observed_at)
            with capture.guard():
                capture.append(EvidenceEvent(phase="replay", decision="fresh"))
            compiled = compile_authoring(context, request, limited)
            with capture.guard():
                capture.append(KnowledgeValidation(phase="type", status="passed"))
            canonical_request = WriteRequest.model_construct(
                contract_version="foundation/1",
                request_id=request.request_id,
                retry_key=request.retry_key,
                scope=request.scope,
                attribution=request.attribution,
                payload=compiled.plan,
            )
            authored: list[tuple[RecordAuthoringReceipt, Manifest]] = []

            def persist(
                write_context: CanonicalWriteContext,
                _: WriteRequest,
                key_id: str,
                schema_version: str,
                canonical_receipt: ChangeSetReceipt,
                manifest: Manifest,
                status: Literal["applied", "unchanged"],
                committed_at: datetime,
            ) -> None:
                receipt = _build_receipt(
                    write_context,
                    request,
                    compiled,
                    canonical_receipt,
                    key_id,
                    limited,
                )
                targets = list(manifest.targets)
                for entity in receipt.entities:
                    targets.append(
                        ClassificationEventTarget(
                            entity_id=entity.entity_id,
                            event_id=entity.selection_id,
                        )
                    )
                    if entity.selected_claim_id is not None:
                        targets.append(
                            ClassificationTarget(
                                contribution_ids=(entity.selected_claim_id,),
                            )
                        )
                for assertion in receipt.assertions:
                    claims = tuple(
                        witness.claim_id
                        for witness in (
                            assertion.subject_classification,
                            assertion.object_classification,
                        )
                        if witness is not None
                    )
                    if claims:
                        targets.append(
                            ClassificationTarget(
                                contribution_ids=tuple(sorted(set(claims))),
                            )
                        )
                authored_manifest = manifest.model_copy(update={"targets": tuple(targets)})
                _save(
                    write_context,
                    request,
                    key_id,
                    schema_version,
                    receipt,
                    authored_manifest,
                    status,
                    committed_at,
                )
                authored.append((receipt, authored_manifest))

            from kg.knowledge import _write

            _, status, _, _ = _write.apply_plan(
                context,
                canonical_request,
                compiled.plan,
                at,
                limited,
                capture=capture,
                persist=persist,
            )
            if len(authored) != 1:
                raise EvidenceServiceError("internal_error")
            receipt, manifest = authored[0]
            retain_manifest(capture, manifest)
            with capture.guard():
                capture.append(EvidenceEvent(phase="mutation", decision=status))
            return RecordAuthoringOutcome(
                request_id=request.request_id,
                status=status,
                receipt=receipt,
            )
    except EvidenceServiceError as error:
        return failed(request.request_id, error.failure)
    except (PrivateResourceStop, DeadlineStop):
        return failed(
            request.request_id,
            EvidenceServiceError("budget_exceeded").failure,
        )
    finally:
        with capture.guard():
            capture.append(
                CommitEvent(
                    observation=(
                        context.commit_outcome if context is not None else "not_attempted"
                    )
                )
            )


def record(
    service: KnowledgeService,
    request: RecordAuthoringRequest,
    *,
    budget: PrivateBudget | None = None,
) -> RecordAuthoringOutcome:
    request = validated(RecordAuthoringRequest, request)
    capture = service._collector.begin_capture(
        "write",
        request.scope,
        required="write_knowledge",
        request_id=request.request_id,
    )
    try:
        outcome = _run(service, request, now(), capture, budget)
    except BaseException:
        capture.group.close()
        raise
    capture.finish(
        outcome.status,
        reason=outcome.error.code if outcome.error else None,
        diagnostic_id=outcome.error.diagnostic_id if outcome.error else None,
    )
    reporting.deliver(service.diagnostics._publish(capture))
    return outcome


def record_batch(
    service: KnowledgeService,
    batch: RecordAuthoringBatch,
) -> RecordAuthoringBatchResult:
    batch = validated(RecordAuthoringBatch, batch)
    outcomes = tuple(record(service, item) for item in batch.items)
    successes = sum(outcome.receipt is not None for outcome in outcomes)
    return RecordAuthoringBatchResult(
        batch_id=batch.batch_id,
        outcomes=outcomes,
        status=(
            "complete"
            if successes == len(outcomes)
            else "partial"
            if successes
            else "failed"
        ),
    )
