from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
from pydantic import ValidationError
from support.evidence import environment, put, receipt
from support.knowledge import preset

from kg.indexing._passages import produce
from kg.knowledge import KnowledgeAdministration, KnowledgeService, _record, _write
from kg.knowledge._authoring_models import (
    AuthoredAlias,
    AuthoredAssertion,
    AuthoredClassification,
    AuthoredEntity,
    AuthoredIdentifier,
    AuthoredMention,
    AuthoringEntityObject,
    ClassificationReviewWitness,
    ClassificationSelectionWitness,
    CurrentSelection,
    ExistingIdentity,
    NewIdentity,
    RecordAuthoringBatch,
    RecordAuthoringDocument,
    RecordAuthoringRequest,
    SeedCapture,
    SeedSlotIdentity,
    SelectLocalClaim,
    SourceCapture,
    StoredClaimSelection,
    SupportExistingIdentity,
    validate_request,
)
from kg.models.evidence import KnowledgeWriterBinding, LocalAdminAuthority, PolicyGrant
from kg.models.foundation import Attribution, StoredSelectionRef, StringObject
from kg.models.knowledge import KnowledgeSchema, PredicateDefinition


@pytest.fixture
def env(tmp_path):
    value = environment(tmp_path / "private-record.db")
    value.policy = value.policy.model_copy(
        update={
            "grants": value.policy.grants
            + tuple(
                PolicyGrant(
                    principal_id="principal",
                    namespace=namespace,
                    grant=grant,
                )
                for namespace in ("markdown", "email")
                for grant in ("write_knowledge", "seed")
            ),
            "knowledge_bindings": tuple(
                KnowledgeWriterBinding(
                    namespace=namespace,
                    principal_id="principal",
                    owner_id="owner",
                    writer_id="writer",
                )
                for namespace in ("markdown", "email")
            ),
        }
    )
    update = value.admin.replace_policy(value.policy, value.scope.access.policy_version)
    value.scope = value.scope.model_copy(
        update={
            "access": value.scope.access.model_copy(
                update={
                    "policy_version": update.policy_version,
                    "grants": ("read", "write_documents", "write_knowledge", "seed"),
                }
            )
        }
    )
    schema = KnowledgeSchema(
        corpus_id=value.scope.corpus_id,
        schema_version="inspection/1",
        entity_types=("device", "location", "inspection_event", "service_action"),
        identifier_schemes=("serial_number",),
        predicates=(
            PredicateDefinition(
                name="installed_at",
                subject_types=("device",),
                object_kind="entity",
                object_types=("location",),
            ),
            PredicateDefinition(
                name="calibration_status",
                subject_types=("device",),
                object_kind="string",
            ),
            PredicateDefinition(
                name="inspected",
                subject_types=("inspection_event",),
                object_kind="entity",
                object_types=("device",),
            ),
            PredicateDefinition(
                name="serviced",
                subject_types=("service_action",),
                object_kind="entity",
                object_types=("device",),
            ),
            PredicateDefinition(
                name="occurred_at",
                subject_types=("inspection_event",),
                object_kind="entity",
                object_types=("location",),
            ),
            PredicateDefinition(
                name="status",
                subject_types=("device",),
                object_kind="string",
            ),
            PredicateDefinition(
                name="result",
                subject_types=("inspection_event",),
                object_kind="string",
            ),
        ),
    )
    KnowledgeAdministration(
        value.database,
        LocalAdminAuthority(principal_id="admin"),
    ).register_knowledge_schema(preset(schema))
    return value


def authored_request(env, *, retry_key="record-key"):
    saved = receipt(env.service.write(put(env.scope, text="DX-42 is installed in Cold Room 3.")))
    anchor = env.service.anchors(
        env.scope,
        saved.document_id,
        saved.processing.state_version,
    ).entries[0]
    document = RecordAuthoringDocument(
        interface_version="record-authoring/1",
        expected_schema_revision=KnowledgeService(
            env.database,
            env.service.identity,
        ).schema(env.scope).head,
        support={
            "inspection": SourceCapture(
                kind="source",
                reference=anchor.reference,
                state_version=saved.processing.state_version,
            )
        },
        entities=(
            AuthoredEntity(
                local_id="thermometer",
                identity=NewIdentity(
                    kind="new",
                    name="Thermometer DX-42",
                    support=("inspection",),
                ),
                classifications=(
                    AuthoredClassification(
                        local_id="thermometer-device",
                        entity_type="device",
                        interpretation="explicit",
                        support=("inspection",),
                        select=SelectLocalClaim(rationale="The inspection identifies a device."),
                    ),
                ),
                identifiers=(
                    AuthoredIdentifier(
                        local_id="thermometer-serial",
                        scheme="serial_number",
                        value="DX-42",
                        support=("inspection",),
                    ),
                ),
            ),
            AuthoredEntity(
                local_id="cold-room",
                identity=NewIdentity(
                    kind="new",
                    name="Cold Room 3",
                    support=("inspection",),
                ),
                classifications=(
                    AuthoredClassification(
                        local_id="cold-room-location",
                        entity_type="location",
                        interpretation="explicit",
                        support=("inspection",),
                        select=SelectLocalClaim(
                            rationale="The inspection identifies a location.",
                        ),
                    ),
                ),
            ),
        ),
        assertions=(
            AuthoredAssertion(
                local_id="placement",
                subject="thermometer",
                predicate="installed_at",
                object=AuthoringEntityObject(kind="entity", entity="cold-room"),
                interpretation="explicit",
                support=("inspection",),
            ),
            AuthoredAssertion(
                local_id="calibration-note",
                subject="thermometer",
                predicate="calibration_status",
                object=StringObject(kind="string", value="Calibration due"),
                interpretation="explicit",
                support=("inspection",),
            ),
        ),
    )
    return RecordAuthoringRequest(
        interface_version="record-authoring/1",
        request_id=str(uuid4()),
        retry_key=retry_key,
        scope=env.scope,
        attribution=Attribution(
            owner_id="owner",
            writer_id="writer",
            producer="tests",
            producer_version="1",
        ),
        document=document,
    )


@pytest.mark.service
def test_private_record_compiles_exact_plan_and_authored_receipt(env, monkeypatch):
    request = authored_request(env)
    service = KnowledgeService(env.database, env.service.identity)
    captured = []
    original = _write.apply_plan

    def apply_plan(context, canonical_request, plan, at, budget, **kwargs):
        captured.append(plan)
        return original(context, canonical_request, plan, at, budget, **kwargs)

    monkeypatch.setattr(_write, "apply_plan", apply_plan)
    result = _record.record(service, request)
    assert result.error is None
    assert result.receipt is not None
    assert len(captured) == 1
    assert tuple(change.local_id for change in captured[0].changes) == (
        "entity/thermometer",
        "entity/cold-room",
        "classification/thermometer/thermometer-device",
        "classification/cold-room/cold-room-location",
        "classification-selection/thermometer",
        "classification-selection/cold-room",
        "thermometer-serial",
        "placement",
        "calibration-note",
    )
    assert result.receipt.derived_operations.model_dump() == {
        "entity": 2,
        "entity_support": 0,
        "classification": 2,
        "classification_selection": 2,
        "alias": 0,
        "identifier": 1,
        "mention": 0,
        "assertion": 2,
    }
    assert [entity.local_id for entity in result.receipt.entities] == [
        "thermometer",
        "cold-room",
    ]
    assert [item.local_id for item in result.receipt.assertions] == [
        "placement",
        "calibration-note",
    ]
    assert all(
        item.support_names == ("inspection",)
        for entity in result.receipt.entities
        for item in entity.items
    )
    assert all(
        item.subject_classification is not None for item in result.receipt.assertions
    )
    assert service.diagnostics.for_request(env.scope, request.request_id).entries


@pytest.mark.service
def test_complete_inspection_fixture_compiles_to_exact_19_operations(env, monkeypatch):
    fixture_root = Path(__file__).parent / "fixtures" / "record_authoring"
    template = RecordAuthoringDocument.model_validate_json(
        (fixture_root / "inspection-record-authoring.json").read_text(),
        strict=True,
    )
    saved = receipt(
        env.service.write(
            put(
                env.scope,
                external="complete-inspection",
                text="DX-42 was inspected in Cold Room 3 and requires service.",
            )
        )
    )
    anchor = env.service.anchors(
        env.scope,
        saved.document_id,
        saved.processing.state_version,
    ).entries[0]
    service = KnowledgeService(env.database, env.service.identity)
    document = template.model_copy(
        update={
            "expected_schema_revision": service.schema(env.scope).head,
            "support": {
                "inspection": SourceCapture(
                    kind="source",
                    reference=anchor.reference,
                    state_version=saved.processing.state_version,
                )
            },
        }
    )
    request = RecordAuthoringRequest(
        interface_version="record-authoring/1",
        request_id="complete-inspection",
        retry_key="complete-inspection",
        scope=env.scope,
        attribution=Attribution(
            owner_id="owner",
            writer_id="writer",
            producer="tests",
            producer_version="1",
        ),
        document=document,
    )
    captured = []
    original = _write.apply_plan

    def apply_plan(context, canonical_request, plan, at, budget, **kwargs):
        captured.append(plan)
        return original(context, canonical_request, plan, at, budget, **kwargs)

    monkeypatch.setattr(_write, "apply_plan", apply_plan)
    result = _record.record(service, request)
    assert result.status == "applied"
    expected = json.loads(
        (fixture_root / "inspection-canonical-operations.json").read_text()
    )
    assert [
        {"kind": change.kind, "local_id": change.local_id}
        for change in captured[0].changes
    ] == expected
    assert len(captured[0].changes) == 19


@pytest.mark.service
def test_replay_precedes_compilation_and_mutable_state_fences(env, monkeypatch):
    request = authored_request(env)
    service = KnowledgeService(env.database, env.service.identity)
    first = _record.record(service, request)
    assert first.receipt is not None
    source = next(iter(request.document.support.values()))
    assert isinstance(source, SourceCapture)
    env.service.write(
        put(
            env.scope,
            state=source.state_version,
            text="The inspection was replaced.",
            retry="source-update",
        )
    )
    fresh = request.model_copy(
        update={"request_id": str(uuid4()), "retry_key": "fresh-after-source-change"}
    )
    assert _record.record(service, fresh).error.code == "state_conflict"

    def forbidden_compile(*args, **kwargs):
        raise AssertionError("replay must not compile")

    monkeypatch.setattr(_record, "compile_authoring", forbidden_compile)
    replay = _record.record(service, request)
    assert replay.receipt == first.receipt
    changed = request.model_copy(
        update={
            "document": request.document.model_copy(
                update={
                    "assertions": (
                        request.document.assertions[0].model_copy(
                            update={
                                "object": AuthoringEntityObject(
                                    kind="entity",
                                    entity="thermometer",
                                )
                            }
                        ),
                    )
                }
            )
        }
    )
    assert _record.record(service, changed).error.code == "retry_conflict"


@pytest.mark.service
def test_replay_reauthorizes_all_disclosed_classification_provenance(env):
    request = authored_request(env)
    service = KnowledgeService(env.database, env.service.identity)
    first = _record.record(service, request)
    assert first.receipt is not None
    restricted = env.policy.model_copy(
        update={
            "grants": tuple(
                grant
                for grant in env.policy.grants
                if not (
                    grant.namespace == "markdown"
                    and grant.grant in {"read", "write_knowledge"}
                )
            ),
            "knowledge_bindings": tuple(
                binding
                for binding in env.policy.knowledge_bindings
                if binding.namespace != "markdown"
            ),
        }
    )
    updated = env.admin.replace_policy(restricted, env.scope.access.policy_version)
    narrowed = request.model_copy(
        update={
            "scope": request.scope.model_copy(
                update={
                    "access": request.scope.access.model_copy(
                        update={"policy_version": updated.policy_version}
                    )
                }
            )
        }
    )
    replay = _record.record(service, narrowed)
    assert replay.error is not None
    assert replay.error.code == "forbidden"


@pytest.mark.service
def test_persist_failure_rolls_back_and_exact_retry_can_commit(env, monkeypatch):
    request = authored_request(env)
    service = KnowledgeService(env.database, env.service.identity)
    original = _record._save

    def fail_after_save(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected persistence failure")

    monkeypatch.setattr(_record, "_save", fail_after_save)
    with pytest.raises(RuntimeError, match="injected"):
        _record.record(service, request)
    with env.database.connection() as connection:
        assert connection.execute(
            "SELECT count(*) FROM write_key WHERE operation='record_knowledge'"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM entity").fetchone()[0] == 0
    monkeypatch.setattr(_record, "_save", original)
    assert _record.record(service, request).status == "applied"


@pytest.mark.service
def test_seed_slot_authored_receipt_and_fresh_unchanged_retry(env):
    document = RecordAuthoringDocument(
        interface_version="record-authoring/1",
        expected_schema_revision=KnowledgeService(
            env.database,
            env.service.identity,
        ).schema(env.scope).head,
        support={
            "catalog": SeedCapture(
                kind="seed",
                source_namespace="markdown",
                seed_set_id="catalog",
                seed_key="device-dx-42",
            )
        },
        entities=(
            AuthoredEntity(
                local_id="device",
                identity=SeedSlotIdentity(
                    kind="seed_slot",
                    name="Thermometer DX-42",
                    support="catalog",
                ),
            ),
        ),
    )
    request = RecordAuthoringRequest(
        interface_version="record-authoring/1",
        request_id="seed-one",
        retry_key="seed-one",
        scope=env.scope,
        attribution=Attribution(
            owner_id="owner",
            writer_id="writer",
            producer="tests",
            producer_version="1",
        ),
        document=document,
    )
    service = KnowledgeService(env.database, env.service.identity)
    first = _record.record(service, request)
    assert first.status == "applied"
    assert first.receipt.entities[0].identity_contribution_id is not None
    second = _record.record(
        service,
        request.model_copy(update={"request_id": "seed-two", "retry_key": "seed-two"}),
    )
    assert second.status == "unchanged"
    assert second.receipt.entities[0].entity_id == first.receipt.entities[0].entity_id
    assert (
        second.receipt.entities[0].identity_contribution_id
        == first.receipt.entities[0].identity_contribution_id
    )


@pytest.mark.service
def test_existing_entity_local_selection_uses_exact_review_fence(env):
    service = KnowledgeService(env.database, env.service.identity)
    initial = _record.record(service, authored_request(env))
    entity_id = initial.receipt.entities[0].entity_id
    review = service.classification_review(env.scope, entity_id)
    source_saved = receipt(
        env.service.write(
            put(
                env.scope,
                external="classification-review",
                text="The thermometer is a device.",
            )
        )
    )
    anchor = env.service.anchors(
        env.scope,
        source_saved.document_id,
        source_saved.processing.state_version,
    ).entries[0]
    witness = ClassificationReviewWitness(
        interface_version="classification-review-witness/1",
        entity_id=entity_id,
        schema_revision=service.schema(env.scope).head,
        selection_id=review.selection_id,
        selected_claim_id=review.selected.claim_id,
        reviewed_claim_ids=review.reviewed_claim_ids,
        reviewed_candidates_digest=review.reviewed_candidates_digest,
        review_coverage=review.review_coverage,
        accept_incomplete_review=False,
    )
    document = RecordAuthoringDocument(
        interface_version="record-authoring/1",
        expected_schema_revision=service.schema(env.scope).head,
        support={
            "review": SourceCapture(
                kind="source",
                reference=anchor.reference,
                state_version=source_saved.processing.state_version,
            )
        },
        entities=(
            AuthoredEntity(
                local_id="existing-device",
                identity=ExistingIdentity(kind="existing", entity_id=entity_id),
                classifications=(
                    AuthoredClassification(
                        local_id="confirmed-device",
                        entity_type="device",
                        interpretation="explicit",
                        support=("review",),
                        select=SelectLocalClaim(
                            rationale="The new report confirms the device classification.",
                            review_witness=witness,
                        ),
                    ),
                ),
            ),
        ),
    )
    request = RecordAuthoringRequest(
        interface_version="record-authoring/1",
        request_id="classification-one",
        retry_key="classification-one",
        scope=env.scope,
        attribution=Attribution(
            owner_id="owner",
            writer_id="writer",
            producer="tests",
            producer_version="1",
        ),
        document=document,
    )
    tampered_classification = document.entities[0].classifications[0].model_copy(
        update={
            "select": document.entities[0].classifications[0].select.model_copy(
                update={
                    "review_witness": witness.model_copy(
                        update={"selected_claim_id": "different-claim"}
                    )
                }
            )
        }
    )
    tampered = request.model_copy(
        update={
            "request_id": "classification-tampered",
            "retry_key": "classification-tampered",
            "document": document.model_copy(
                update={
                    "entities": (
                        document.entities[0].model_copy(
                            update={"classifications": (tampered_classification,)}
                        ),
                    )
                }
            ),
        }
    )
    assert _record.record(service, tampered).error.code == "state_conflict"
    changed = _record.record(service, request)
    assert changed.status == "applied"
    assert changed.receipt.entities[0].classification.claim_id == (
        changed.receipt.entities[0].items[0].stored_id
    )
    stale = _record.record(
        service,
        request.model_copy(
            update={"request_id": "classification-two", "retry_key": "classification-two"}
        ),
    )
    assert stale.error.code == "state_conflict"
    assert _record.record(service, request).receipt == changed.receipt


@pytest.mark.service
def test_support_existing_receipt_keeps_entity_and_contribution_distinct(env):
    service = KnowledgeService(env.database, env.service.identity)
    initial = _record.record(service, authored_request(env))
    entity_id = initial.receipt.entities[0].entity_id
    saved = receipt(
        env.service.write(
            put(env.scope, external="identity-support", text="Thermometer DX-42 observed.")
        )
    )
    anchor = env.service.anchors(
        env.scope,
        saved.document_id,
        saved.processing.state_version,
    ).entries[0]
    document = RecordAuthoringDocument(
        interface_version="record-authoring/1",
        expected_schema_revision=service.schema(env.scope).head,
        support={
            "identity": SourceCapture(
                kind="source",
                reference=anchor.reference,
                state_version=saved.processing.state_version,
            )
        },
        entities=(
            AuthoredEntity(
                local_id="device",
                identity=SupportExistingIdentity(
                    kind="support_existing",
                    entity_id=entity_id,
                    name="Thermometer DX-42",
                    support=("identity",),
                ),
            ),
        ),
    )
    result = _record.record(
        service,
        RecordAuthoringRequest(
            interface_version="record-authoring/1",
            request_id="support-existing",
            retry_key="support-existing",
            scope=env.scope,
            attribution=Attribution(
                owner_id="owner",
                writer_id="writer",
                producer="tests",
                producer_version="1",
            ),
            document=document,
        ),
    )
    assert result.status == "applied"
    entity = result.receipt.entities[0]
    assert entity.entity_id == entity_id
    assert entity.identity_contribution_id != entity.entity_id
    assert entity.identity_support_names == ("identity",)


@pytest.mark.service
def test_current_selection_witness_compiles_only_authored_assertion(env, monkeypatch):
    service = KnowledgeService(env.database, env.service.identity)
    initial = _record.record(service, authored_request(env))
    entity = initial.receipt.entities[0]
    selected = entity.classification
    assert selected is not None
    saved = receipt(
        env.service.write(
            put(
                env.scope,
                external="status",
                namespace="email",
                text="Calibration remains current.",
            )
        )
    )
    anchor = env.service.anchors(
        env.scope,
        saved.document_id,
        saved.processing.state_version,
    ).entries[0]
    document = RecordAuthoringDocument(
        interface_version="record-authoring/1",
        expected_schema_revision=service.schema(env.scope).head,
        support={
            "status": SourceCapture(
                kind="source",
                reference=anchor.reference,
                state_version=saved.processing.state_version,
            )
        },
        entities=(
            AuthoredEntity(
                local_id="device",
                identity=ExistingIdentity(kind="existing", entity_id=entity.entity_id),
                selection=CurrentSelection(
                    kind="current",
                    expected_entity_type=selected.entity_type,
                    selection_witness=ClassificationSelectionWitness(
                        interface_version="classification-selection-witness/1",
                        entity_id=selected.entity_id,
                        schema_revision=service.schema(env.scope).head,
                        selection_id=selected.selection_id,
                        selected_claim_id=selected.claim_id,
                        entity_type=selected.entity_type,
                    ),
                ),
            ),
        ),
        assertions=(
            AuthoredAssertion(
                local_id="status",
                subject="device",
                predicate="calibration_status",
                object=StringObject(kind="string", value="Current"),
                interpretation="explicit",
                support=("status",),
            ),
        ),
    )
    request = RecordAuthoringRequest(
        interface_version="record-authoring/1",
        request_id="current",
        retry_key="current",
        scope=env.scope,
        attribution=Attribution(
            owner_id="owner",
            writer_id="writer",
            producer="tests",
            producer_version="1",
        ),
        document=document,
    )
    plans = []
    original = _write.apply_plan

    def apply_plan(context, canonical_request, plan, at, budget, **kwargs):
        plans.append(plan)
        return original(context, canonical_request, plan, at, budget, **kwargs)

    monkeypatch.setattr(_write, "apply_plan", apply_plan)
    result = _record.record(service, request)
    assert result.status == "applied"
    assert len(plans[0].changes) == 1
    assert isinstance(plans[0].changes[0].subject_classification, StoredSelectionRef)
    assert (
        plans[0].changes[0].subject_classification.event_id
        == selected.selection_id
    )
    narrowed = request.model_copy(
        update={
            "scope": request.scope.model_copy(
                update={
                    "access": request.scope.access.model_copy(
                        update={"namespaces": ("email",)}
                    )
                }
            )
        }
    )
    unauthorized = _record.record(service, narrowed)
    assert unauthorized.error is not None
    assert unauthorized.error.code == "not_found"


@pytest.mark.service
def test_passage_backed_mentions_preserve_exact_named_support(env):
    source_request = put(env.scope, external="passage", text="Observed device.")
    source_request = source_request.model_copy(
        update={
            "payload": source_request.payload.model_copy(
                update={
                    "content": source_request.payload.content.model_copy(
                        update={"passage_policy": "codepoint-window/1"}
                    )
                }
            )
        }
    )
    saved = receipt(env.service.write(source_request))
    produce(
        env.database,
        env.service.identity,
        env.scope,
        source_request.attribution,
        saved.document_id,
        saved.processing.state_version,
    )
    passage = env.service.passages(
        env.scope,
        saved.document_id,
        saved.processing.state_version,
    ).entries[0]
    service = KnowledgeService(env.database, env.service.identity)
    document = RecordAuthoringDocument(
        interface_version="record-authoring/1",
        expected_schema_revision=service.schema(env.scope).head,
        support={
            "passage": SourceCapture(
                kind="source",
                reference=passage.reference,
                state_version=saved.processing.state_version,
            )
        },
        entities=(
            AuthoredEntity(
                local_id="device",
                identity=NewIdentity(
                    kind="new",
                    name="Observed device",
                    support=("passage",),
                ),
                mentions=(
                    AuthoredMention(local_id="mention", support=("passage",)),
                ),
            ),
        ),
    )
    result = _record.record(
        service,
        RecordAuthoringRequest(
            interface_version="record-authoring/1",
            request_id="mention",
            retry_key="mention",
            scope=env.scope,
            attribution=source_request.attribution,
            document=document,
        ),
    )
    assert result.status == "applied"
    mention = result.receipt.entities[0].items[0]
    assert mention.local_id == "mention"
    assert mention.captures[0].evidence.reference == passage.reference
    assert mention.captures[0].passage_set_id is not None


@pytest.mark.service
def test_private_batch_preserves_independent_order_and_replay(env):
    one = authored_request(env, retry_key="one")
    two = one.model_copy(
        update={
            "request_id": str(uuid4()),
            "retry_key": "two",
            "document": one.document.model_copy(
                update={
                    "entities": (
                        one.document.entities[0].model_copy(
                            update={
                                "local_id": "sensor",
                                "identity": NewIdentity(
                                    kind="new",
                                    name="Sensor DX-43",
                                    support=("inspection",),
                                ),
                                "classifications": (
                                    one.document.entities[0].classifications[0].model_copy(
                                        update={"local_id": "sensor-device"}
                                    ),
                                ),
                                "identifiers": (),
                            }
                        ),
                    ),
                    "assertions": (),
                }
            ),
        }
    )
    batch = RecordAuthoringBatch(
        interface_version="record-authoring-batch/1",
        batch_id="batch",
        items=(one, two),
    )
    service = KnowledgeService(env.database, env.service.identity)
    result = _record.record_batch(service, batch)
    assert result.status == "complete"
    assert tuple(outcome.request_id for outcome in result.outcomes) == (
        one.request_id,
        two.request_id,
    )
    assert _record.record_batch(service, batch).outcomes == result.outcomes


@pytest.mark.service
def test_interrupted_batch_replays_prior_item_then_finishes(env, monkeypatch):
    one = authored_request(env, retry_key="batch-recovery-one")
    two = one.model_copy(
        update={
            "request_id": "batch-recovery-two",
            "retry_key": "batch-recovery-two",
            "document": one.document.model_copy(
                update={
                    "entities": (
                        one.document.entities[0].model_copy(
                            update={
                                "local_id": "second-device",
                                "identity": NewIdentity(
                                    kind="new",
                                    name="Second device",
                                    support=("inspection",),
                                ),
                                "classifications": (
                                    one.document.entities[0].classifications[0].model_copy(
                                        update={"local_id": "second-device-type"}
                                    ),
                                ),
                                "identifiers": (),
                            }
                        ),
                    ),
                    "assertions": (),
                }
            ),
        }
    )
    batch = RecordAuthoringBatch(
        interface_version="record-authoring-batch/1",
        batch_id="recover",
        items=(one, two),
    )
    service = KnowledgeService(env.database, env.service.identity)
    original = _record.record
    calls = 0

    def interrupted(owner, item, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("interrupted batch")
        return original(owner, item, **kwargs)

    monkeypatch.setattr(_record, "record", interrupted)
    with pytest.raises(RuntimeError, match="interrupted"):
        _record.record_batch(service, batch)
    monkeypatch.setattr(_record, "record", original)
    recovered = _record.record_batch(service, batch)
    assert recovered.status == "complete"
    assert recovered.outcomes[0].status == "applied"
    assert recovered.outcomes[1].status == "applied"
    with env.database.connection() as connection:
        assert connection.execute(
            "SELECT count(*) FROM write_key WHERE operation='record_knowledge'"
        ).fetchone()[0] == 2


@pytest.mark.unit
def test_private_validation_and_public_surface_are_not_compatibility_layers():
    with pytest.raises(ValidationError):
        RecordAuthoringDocument.model_validate(
            {
                "interface_version": "record-authoring/1",
                "expected_schema_revision": {
                    "revision_id": "r",
                    "definition_hash": "0" * 64,
                },
                "support": {
                    "e": {
                        "kind": "source",
                        "reference": {
                            "corpus_id": "c",
                            "source_namespace": "n",
                            "document_id": "d",
                            "revision_id": "r",
                            "anchor_id": "a",
                            "passage_id": None,
                        },
                        "state_version": "s",
                    }
                },
                "entities": [
                    {
                        "local_id": "known",
                        "identity": {"kind": "existing", "entity_id": "stored"},
                    }
                ],
                "assertions": [
                    {
                        "local_id": "bad",
                        "subject": "implicit",
                        "predicate": "observed_at",
                        "object": {"kind": "string", "value": "x"},
                        "interpretation": "explicit",
                        "support": ["e"],
                    }
                ],
                "changes": [],
            },
            strict=True,
        )
    assert not hasattr(KnowledgeService, "record")
    with pytest.raises(ModuleNotFoundError):
        __import__("kg.models.authoring")
    with pytest.raises(Exception) as error:
        validate_request({})
    assert error.value.code == "invalid_literal_shape"


@pytest.mark.unit
def test_domain_neutral_fixtures_and_private_engine_have_no_fixture_leakage():
    fixture_root = Path(__file__).parent / "fixtures" / "record_authoring"
    inspection = RecordAuthoringDocument.model_validate_json(
        (fixture_root / "inspection-record-authoring.json").read_text(),
        strict=True,
    )
    botanical = RecordAuthoringDocument.model_validate_json(
        (fixture_root / "botanical-record-authoring.json").read_text(),
        strict=True,
    )
    assert (
        len(inspection.entities)
        + sum(len(entity.identifiers) for entity in inspection.entities)
        + len(inspection.assertions)
        == 11
    )
    assert {entity.local_id for entity in botanical.entities} == {
        "specimen",
        "collection-site",
    }
    forbidden_fields = {
        "changes",
        "dependencies",
        "expected_selection_id",
        "reviewed_candidates_digest",
        "reviewed_claim_ids",
        "subject_classification",
        "object_classification",
    }

    def keys(value):
        if isinstance(value, dict):
            return set(value) | set().union(*(keys(item) for item in value.values()))
        if isinstance(value, list):
            return set().union(*(keys(item) for item in value))
        return set()

    assert not (keys(json.loads(inspection.model_dump_json())) & forbidden_fields)
    owned = (
        Path("src/kg/knowledge/_authoring_models.py"),
        Path("src/kg/knowledge/_authoring.py"),
        Path("src/kg/knowledge/_record.py"),
        *fixture_root.glob("*.json"),
    )
    text = "\n".join(path.read_text().lower() for path in owned)
    assert "atlas" not in text
    assert "--legacy" not in text
    assert "fallback parser" not in text


@pytest.mark.unit
def test_exact_derived_change_boundaries_and_empty_authoring():
    support = {
        "evidence": SourceCapture(
            kind="source",
            reference={
                "corpus_id": "c",
                "source_namespace": "n",
                "document_id": "d",
                "revision_id": "r",
                "anchor_id": "a",
                "passage_id": None,
            },
            state_version="s",
        )
    }
    revision = {
        "revision_id": "schema",
        "definition_hash": "2" * 64,
    }
    accepted = RecordAuthoringDocument(
        interface_version="record-authoring/1",
        expected_schema_revision=revision,
        support=support,
        entities=(
            AuthoredEntity(
                local_id="entity",
                identity=NewIdentity(
                    kind="new",
                    name="Entity",
                    support=("evidence",),
                ),
                aliases=tuple(
                    AuthoredAlias(
                        local_id=f"alias-{index}",
                        alias=f"Alias {index}",
                        support=("evidence",),
                    )
                    for index in range(99)
                ),
            ),
        ),
    )
    assert len(accepted.entities[0].aliases) == 99
    with pytest.raises(ValidationError, match="derived change limit"):
        RecordAuthoringDocument(
            interface_version="record-authoring/1",
            expected_schema_revision=revision,
            support=support,
            entities=(
                accepted.entities[0].model_copy(
                    update={
                        "aliases": (
                            *accepted.entities[0].aliases,
                            AuthoredAlias(
                                local_id="alias-99",
                                alias="Alias 99",
                                support=("evidence",),
                            ),
                        )
                    }
                ),
            ),
        )
    with pytest.raises(ValidationError, match="empty authoring"):
        RecordAuthoringDocument(
            interface_version="record-authoring/1",
            expected_schema_revision=revision,
            support=support,
            entities=(
                AuthoredEntity(
                    local_id="existing",
                    identity=ExistingIdentity(kind="existing", entity_id="stored"),
                ),
            ),
        )
    with pytest.raises(ValidationError, match="duplicate support name"):
        RecordAuthoringDocument(
            interface_version="record-authoring/1",
            expected_schema_revision=revision,
            support=support,
            entities=(
                AuthoredEntity(
                    local_id="entity",
                    identity=NewIdentity(
                        kind="new",
                        name="Entity",
                        support=("evidence", "evidence"),
                    ),
                ),
            ),
        )
    with pytest.raises(ValidationError, match="derived local ID exceeds"):
        RecordAuthoringDocument(
            interface_version="record-authoring/1",
            expected_schema_revision=revision,
            support=support,
            entities=(
                AuthoredEntity(
                    local_id="x" * 256,
                    identity=NewIdentity(
                        kind="new",
                        name="Entity",
                        support=("evidence",),
                    ),
                ),
            ),
        )
    witness = ClassificationReviewWitness(
        interface_version="classification-review-witness/1",
        entity_id="stored",
        schema_revision=revision,
        selection_id="selection",
        selected_claim_id=None,
        reviewed_claim_ids=(),
        reviewed_candidates_digest="3" * 64,
        review_coverage="complete",
        accept_incomplete_review=False,
    )
    with pytest.raises(ValidationError, match="cannot carry a review witness"):
        RecordAuthoringDocument(
            interface_version="record-authoring/1",
            expected_schema_revision=revision,
            support=support,
            entities=(
                AuthoredEntity(
                    local_id="entity",
                    identity=NewIdentity(
                        kind="new",
                        name="Entity",
                        support=("evidence",),
                    ),
                    classifications=(
                        AuthoredClassification(
                            local_id="type",
                            entity_type="device",
                            interpretation="explicit",
                            support=("evidence",),
                            select=SelectLocalClaim(
                                rationale="Explicit selection.",
                                review_witness=witness,
                            ),
                        ),
                    ),
                ),
            ),
        )
    with pytest.raises(ValidationError, match="entity-level selection"):
        AuthoredEntity(
            local_id="entity",
            identity=NewIdentity(
                kind="new",
                name="Entity",
                support=("evidence",),
            ),
            selection=StoredClaimSelection(
                kind="stored_claim",
                claim_id="claim",
                expected_entity_type="device",
                rationale="Stored claim.",
                review_witness=witness,
            ),
        )
    with pytest.raises(ValidationError, match="authored receipt mapping"):
        RecordAuthoringDocument(
            interface_version="record-authoring/1",
            expected_schema_revision=revision,
            support=support,
            entities=tuple(
                AuthoredEntity(
                    local_id=f"existing-{index}",
                    identity=ExistingIdentity(
                        kind="existing",
                        entity_id=f"stored-{index}",
                    ),
                )
                for index in range(100)
            ),
            assertions=(
                AuthoredAssertion(
                    local_id="assertion",
                    subject="existing-0",
                    predicate="observed",
                    object=StringObject(kind="string", value="value"),
                    interpretation="explicit",
                    support=("evidence",),
                ),
            ),
        )
