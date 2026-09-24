import json
import sys
from contextlib import contextmanager
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
from pydantic import ValidationError
from support.graph import fixture, require_native
from support.knowledge import preset
from support.query_knowledge import produce, setup, write
from support.withdrawal import withdrawal

from kg._execution_budget import PrivateResourceStop
from kg.evidence import EvidenceServiceError
from kg.evidence._graph_observer import GraphSourceOperation
from kg.graph import LocalGraphSession, _decisions, _relationships
from kg.graph._native import NativeError, NativeRows
from kg.graph._session_types import GraphSessionError
from kg.graph.session import GraphReadContext, _size
from kg.knowledge import KnowledgeAdministration, KnowledgeService
from kg.models.evidence import LocalIdentity, StoredCitation
from kg.models.foundation import (
    AddAlias,
    AddAssertion,
    ChangeSet,
    EntityObject,
    ExpectedState,
    ExternalDocument,
    PutDocument,
    SourceMetadata,
    SourceSupport,
    StoredEntity,
    StringObject,
    SuppliedAnchor,
    SuppliedContent,
    WriteRequest,
)
from kg.models.graph import (
    GraphEntitySelector,
    GraphRelationshipDecisionsRequest,
    GraphRelationshipDecisionsResult,
)
from kg.models.knowledge import KnowledgeSchema, PredicateDefinition, RecordProjection


def request(env, **kwargs):
    values = dict(
        request_id="joined", scope=env.scope,
        start=GraphEntitySelector(entity_id=env.person),
        predicate="work:owns", direction="outgoing",
    )
    values.update(kwargs)
    return GraphRelationshipDecisionsRequest(**values)


def assert_parity(env, selection):
    proofs = {p.assertion.assertion_id: p for p in selection.relationships}
    for member in selection.members:
        decision = member.decision
        expected = env.expected[decision.assertion_id]
        assert decision.subject_id == expected["subject"]
        assert decision.decision_text == expected["text"]
        assert decision.subject_witness == env.witnesses[decision.subject_id]
        assert tuple(e.captured.reference for e in decision.support) == expected["support"]
        assert member.relationship_ids == tuple(sorted(
            key for key, value in env.expected.items() if value["object"] == expected["subject"]
        ))
        for identifier in member.relationship_ids:
            relation = proofs[identifier].assertion
            expected_relation = env.expected[identifier]
            assert relation.subject_id == env.person
            assert relation.object_entity_id == decision.subject_id
            assert relation.subject_witness == env.witnesses[env.person]
            assert relation.object_witness == decision.subject_witness
            assert tuple(
                e.captured.reference for e in relation.support
            ) == expected_relation["support"]


def test_actual_native_join_and_full_proofs(tmp_path):
    require_native()
    env = fixture(tmp_path / "source.sqlite", decisions=12)
    with LocalGraphSession(
        env.database, env.identity, env.scope, graph_directory=tmp_path / "derived",
    ) as graph:
        result = graph.relationship_decisions(request(env, display_limit=2))
        assert result.outcome == "complete", result.error
        assert result.count == 12 and result.exact and result.display_truncated
        assert len(result.members) == 2
        selection = graph._relationship_decision_selection(request(env))
        assert selection.count == len(selection.members) == 12
        assert selection.generation == result.generation
        assert selection.members[:2] == result.members
        assert_parity(env, selection)
        assert GraphRelationshipDecisionsResult.model_validate_json(
            result.model_dump_json(),
        ) == result
        assert graph.capabilities().decision_count_identity == "submitted_assertion_id"
        assert not graph.capabilities().decision_retained_inspection
        for assertion in (
            selection.members[0].decision, selection.relationships[0].assertion,
        ):
            for evidence in assertion.support:
                capture = evidence.captured
                view = env.evidence.citation(env.scope, StoredCitation(
                    reference=capture.reference, state_version=capture.dependency.state_version,
                    metadata_snapshot_id=capture.metadata_snapshot_id,
                ))
                assert view.quote and "Synthetic note" in view.quote


@pytest.mark.parametrize("mixed_revisions", [False, True])
def test_realistic_1001_capacity(tmp_path, monkeypatch, mixed_revisions):
    require_native()
    if mixed_revisions:
        from support.graph import example
        from test_graph_relationships_schema import evolve

        original_write = example.Fixture.write
        applied = set()

        def write_across_revisions(env, changes):
            relationships = sum(value["object"] is not None for value in env.expected.values())
            decisions = len(env.expected) - relationships
            predicate = (
                "work:owns" if decisions >= 500
                else "work:decision" if relationships >= 100 else None
            )
            if predicate is not None and predicate not in applied:
                evolve(env, predicate)
                applied.add(predicate)
            return original_write(env, changes)

        monkeypatch.setattr(example.Fixture, "write", write_across_revisions)
    env = fixture(tmp_path / "source.sqlite", decisions=1001, varied=True)
    measurements = []
    original = GraphReadContext.retain
    contexts = []
    read = NativeRows.read
    page_limits = []

    def retain(ctx, value):
        assert isinstance(value, _decisions._GraphDecisionSelection)
        assert ctx._sizes == [0]  # One full selection admission, never repeated payload charging.
        before = ctx.meter.private_budget._scratch
        result = original(ctx, value)
        contexts.append(ctx)
        measurements.append({
            "conservative_bytes": _size(value),
            "serialized_bytes": len(value.model_dump_json().encode()),
            "scratch_before_retain": before,
            "scratch_after_retain": ctx.meter.private_budget._scratch,
            "cumulative_retained_bytes": ctx._sizes[0],
        })
        return result

    def native_read(rows, *, limit=200):
        page_limits.append(limit)
        return read(rows, limit=limit)

    monkeypatch.setattr(GraphReadContext, "retain", retain)
    with LocalGraphSession(
        env.database, env.identity, env.scope, graph_directory=tmp_path / "derived",
    ) as graph:
        started = monotonic()
        cold = graph.relationship_decisions(request(env))
        assert cold.outcome == "complete", cold.error
        assert cold.count == 1001 and len(cold.members) == 1000 and cold.display_truncated
        cold_seconds = monotonic() - started
        assert_parity(env, cold)
        started = monotonic()
        with monkeypatch.context() as patch:
            patch.setattr(NativeRows, "read", native_read)
            full = graph._relationship_decision_selection(request(env))
        warm_seconds = monotonic() - started
        assert full.count == len(full.members) == 1001
        assert {m.decision.assertion_id for m in full.members} == {
            key for key, value in env.expected.items() if value["object"] is None
        }
        assert all(len(member.relationship_ids) == 2 for member in full.members)
        if mixed_revisions:
            assert len({m.decision.schema_version for m in full.members}) == 2
            assert len({p.assertion.schema_version for p in full.relationships}) == 2
        assert full.members[:1000] == cold.members
        assert_parity(env, full)
        assert max(page_limits) == 32  # 200 large nine-column rows exceed G3's page-byte bound.
        for measurement, ctx in zip(measurements, contexts, strict=True):
            budget = ctx.meter.private_budget
            measurement.update(
                peak_scratch=budget._scratch_peak, final_scratch=budget._scratch,
                visits=budget._visits, vm=budget._vm,
            )
            assert budget._scratch == 0
            assert budget._scratch_peak <= 128 << 20
            assert measurement["conservative_bytes"] <= 8 << 20
        assert len(measurements) == 2
        print(json.dumps({
            "cold_seconds": cold_seconds, "warm_seconds": warm_seconds,
            "public_conservative_bytes": _size(cold),
            "public_serialized_bytes": len(cold.model_dump_json().encode()),
            "measurements": measurements,
        }))


@pytest.fixture
def native(tmp_path):
    require_native()
    env = fixture(tmp_path / "source.sqlite", decisions=12)
    with LocalGraphSession(
        env.database, env.identity, env.scope, graph_directory=tmp_path / "derived",
    ) as graph:
        yield env, graph


def test_ambiguity_empty_alias_preflight_and_no_traversal_loop(native, monkeypatch):
    env, graph = native
    monkeypatch.setattr(graph, "traverse", lambda *a, **kw: pytest.fail("two graph calls"))
    ambiguity = graph.relationship_decisions(request(env, start=GraphEntitySelector(name="Alice")))
    assert ambiguity.outcome == "ambiguous" and len(ambiguity.candidate_ids) == 2
    assert ambiguity.count is None and not ambiguity.members and not ambiguity.relationships
    other = next(i for i in ambiguity.candidate_ids if i != env.person)
    absent = graph.relationship_decisions(request(
        env, start=GraphEntitySelector(entity_id="absent"),
    ))
    empty = graph.relationship_decisions(request(env, start=GraphEntitySelector(entity_id=other)))
    assert absent.outcome == empty.outcome == "empty" and absent.count == empty.count == 0
    assert absent.root_entity_id is None and empty.root_entity_id == other
    for predicate in ("unknown", "work:decision"):
        failed = graph.relationship_decisions(request(
            env, predicate=predicate, start=GraphEntitySelector(entity_id="absent"),
        ))
        assert failed.error.code == "unsupported" and failed.count is None
    incoming = graph.relationship_decisions(request(env, direction="incoming"))
    assert incoming.error.code == "unsupported"
    env.write((AddAlias(
        kind="alias", local_id="alias", alias="Exact alias",
        entity=StoredEntity(kind="stored", entity_id=env.person),
        support=SourceSupport(kind="source", evidence=(env.references[0],)),
    ),))
    assert graph.refresh().state == "ready"
    assert graph.relationship_decisions(request(
        env, start=GraphEntitySelector(name="Exact alias"),
    )).count == 12
    assert graph.relationship_decisions(request(
        env, start=GraphEntitySelector(name="exact alias"),
    )).count == 0


def test_incoming_reached_endpoint_and_unrelated_decisions(tmp_path):
    require_native()
    env = setup(tmp_path / "source.sqlite", registered=False)
    KnowledgeAdministration(env.database, env.admin.authority).register_knowledge_schema(
        preset(KnowledgeSchema(
            corpus_id="work", schema_version="reverse/1", entity_types=("project",),
            predicates=(
                PredicateDefinition(name="related", subject_types=("project",),
                                    object_kind="entity", object_types=("project",)),
                PredicateDefinition(name="work:decision", subject_types=("project",),
                                    object_kind="string", record_projection=RecordProjection(
                                        encoding="direct-subject-decision/1")),
            ),
        )),
    )
    root, root_decisions = produce(env, 1, name="Root")
    reached, decisions = produce(env, 3, name="Reached")
    produce(env, 1, name="Unrelated")
    edges = write(env, tuple(AddAssertion(
        kind="assertion", local_id=f"edge-{i}", predicate="related",
        subject=StoredEntity(kind="stored", entity_id=reached),
        object=EntityObject(kind="entity", entity=StoredEntity(kind="stored", entity_id=root)),
        interpretation="explicit" if i < 2 else "inferred", support=env.support,
    ) for i in range(3)))
    with LocalGraphSession(
        env.database, LocalIdentity(principal_id="principal"), env.scope,
        graph_directory=tmp_path / "derived",
    ) as graph:
        req = GraphRelationshipDecisionsRequest(
            request_id="incoming", scope=env.scope, start=GraphEntitySelector(entity_id=root),
            predicate="related", direction="incoming",
        )
        result = graph.relationship_decisions(req)
        assert result.outcome == "complete" and result.count == 3
        assert {m.decision.assertion_id for m in result.members} == decisions
        assert all(m.relationship_ids == tuple(sorted(
            (edges["edge-0"], edges["edge-1"]),
        )) for m in result.members)
        assert all(p.path.entity_ids == (root, reached) for p in result.relationships)
        outgoing = graph.relationship_decisions(req.model_copy(update={
            "direction": "outgoing", "start": GraphEntitySelector(entity_id=reached),
        }))
        assert {m.decision.assertion_id for m in outgoing.members} == root_decisions


@pytest.mark.parametrize("field,value", [
    ("max_hops", True), ("max_hops", 2), ("max_hops", 1.0),
    ("display_limit", True), ("display_limit", 0), ("display_limit", 1001),
    ("direction", "both"),
])
def test_request_revalidation_before_admission(native, monkeypatch, field, value):
    env, graph = native
    with pytest.raises(ValidationError):
        request(env, **{field: value})
    monkeypatch.setattr(graph, "_admit", lambda **kw: pytest.fail("admitted invalid request"))
    with pytest.raises(GraphSessionError, match="invalid_request"):
        graph.relationship_decisions(request(env).model_copy(update={field: value}))


def test_withdrawal_exact_current_history_refresh_and_independent_paths(native, monkeypatch):
    env, graph = native
    before = graph.relationship_decisions(request(env))
    member = before.members[0]
    project = member.decision.subject_id
    first, last = member.relationship_ids
    knowledge = KnowledgeService(env.database, env.identity)
    withdraw = withdrawal(env, first, writer="example")
    receipt = graph.write(withdraw)
    assert receipt.status == "applied" and graph.status().state == "dirty"
    from kg.graph import session as session_module
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise NativeError("native_error")
        patch.setattr(session_module, "build_graph", fail)
        assert graph.refresh().error.code == "native_error"
    assert env.evidence.write(withdraw).receipt == receipt.receipt
    with pytest.raises(GraphSessionError, match="generation_invalid"):
        graph._run_read(env.scope, lambda ctx: (), expected_generation=before.generation)
    assert graph.refresh().state == "ready"
    one_edge = graph.relationship_decisions(request(env))
    assert one_edge.count == before.count
    assert all(m.relationship_ids == (last,) for m in one_edge.members
               if m.decision.subject_id == project)
    history = knowledge.contribution(env.scope, first, mode="history")
    assert not history.is_current
    assert history.withdrawal.withdrawal_id == receipt.receipt.withdrawal_id
    with pytest.raises(EvidenceServiceError, match="not_found"):
        knowledge.contribution(env.scope, first)
    assert graph.write(withdraw).receipt == receipt.receipt
    assert graph.refresh().state == "ready"
    assert graph.relationship_decisions(request(env)).members == one_edge.members

    decision = member.decision.assertion_id
    assert graph.write(withdrawal(env, decision, writer="example")).status == "applied"
    assert graph.refresh().state == "ready"
    removed = graph.relationship_decisions(request(env))
    assert removed.count == before.count - 1
    assert decision not in {m.decision.assertion_id for m in removed.members}
    captured = knowledge.contribution(env.scope, decision, mode="history").evidence[0]
    assert env.evidence.citation(env.scope, StoredCitation(
        reference=captured.reference, state_version=captured.dependency.state_version,
        metadata_snapshot_id=captured.metadata_snapshot_id,
    )).quote
    assert graph.write(withdrawal(env, last, writer="example")).status == "applied"
    assert graph.refresh().state == "ready"
    final = graph.relationship_decisions(request(env))
    assert final.members == tuple(m for m in removed.members if m.decision.subject_id != project)
    assert final.count == len(final.members)


@pytest.mark.parametrize("phase", ["idle", "decode", "fence", "cancel", "revoke"])
def test_joined_withdrawal_race_and_no_data(native, monkeypatch, phase):
    env, graph = native
    before = graph.relationship_decisions(request(env))
    target = before.members[0].decision.assertion_id
    cancel = Event()
    committed = []
    def mutate():
        if not committed:
            committed.append(env.evidence.write(withdrawal(env, target, writer="example")))
            assert committed[-1].status == "applied"
    if phase == "idle":
        mutate()
    elif phase in ("decode", "cancel"):
        original = _decisions._decode_pair
        def decode(ctx, row, **kwargs):
            value = original(ctx, row, **kwargs)
            if phase == "cancel":
                cancel.set()
            else:
                mutate()
            return value
        monkeypatch.setattr(_decisions, "_decode_pair", decode)
    else:
        original = GraphSourceOperation.release_fence
        @contextmanager
        def fence(operation):
            if phase == "revoke":
                with env.database.transaction() as conn:
                    conn.execute("DELETE FROM policy_grant WHERE grant_name='read'")
            else:
                mutate()
            with original(operation) as guard:
                yield guard
        monkeypatch.setattr(GraphSourceOperation, "release_fence", fence)
    failed = graph.relationship_decisions(request(env), cancel=cancel)
    assert failed.outcome == "failed"
    assert failed.error.code == {"cancel": "cancelled", "revoke": "forbidden"}.get(
        phase, "state_changed",
    )
    assert failed.count is failed.exact is failed.generation is failed.root_entity_id is None
    assert not failed.members and not failed.relationships and not failed.candidate_ids


@pytest.mark.parametrize("failure", ["decode", "retain", "final"])
def test_allocation_and_canonical_custody_failures(native, monkeypatch, failure):
    env, graph = native
    assert graph.refresh().state == "ready"
    stores, closed, contexts = [], [], []
    schema, close = _relationships.Store.schema, _relationships.Store.close
    def tracked_schema(store):
        stores.append(store)
        return schema(store)
    def tracked_close(store):
        closed.append(store)
        close(store)
    decode = _decisions._decode_pair
    def tracked_decode(ctx, row, **kwargs):
        contexts.append(ctx)
        assert not closed and all(s._scratch._exit_callbacks for s in stores)
        if failure == "decode":
            raise PrivateResourceStop()
        return decode(ctx, row, **kwargs)
    retain = GraphReadContext.retain
    def tracked_retain(ctx, value):
        assert not closed
        if failure == "retain":
            raise PrivateResourceStop()
        return retain(ctx, value)
    monkeypatch.setattr(_relationships.Store, "schema", tracked_schema)
    monkeypatch.setattr(_relationships.Store, "close", tracked_close)
    monkeypatch.setattr(_decisions, "_decode_pair", tracked_decode)
    monkeypatch.setattr(GraphReadContext, "retain", tracked_retain)
    if failure == "final":
        from kg.graph import session as module
        size = module._size
        def final_size(value, *args, **kwargs):
            if isinstance(value, GraphRelationshipDecisionsResult):
                assert closed
                raise PrivateResourceStop()
            return size(value, *args, **kwargs)
        monkeypatch.setattr(module, "_size", final_size)
    failed = graph.relationship_decisions(request(env, display_limit=1))
    assert failed.error.code == "resource_exhausted" and failed.count is None
    assert not failed.members and not failed.relationships
    assert contexts and all(store in closed for store in stores)
    assert contexts[0].meter.private_budget._scratch == 0


@pytest.mark.parametrize(
    "mutation", ["decision-id", "decision-witness", "relationship", "duplicate"],
)
def test_corrupt_join_row_never_releases_count(native, monkeypatch, mutation):
    env, graph = native
    real = _decisions._decode_pair
    seen = []
    def decode(ctx, row, **kwargs):
        if mutation == "duplicate" and seen:
            row = seen[0]
        else:
            seen.append(row)
            changed = list(row)
            if mutation == "decision-id":
                changed[0] = "wrong"
            elif mutation == "decision-witness":
                value = json.loads(changed[1])
                value["subject_witness"]["contribution_id"] = "wrong"
                changed[1] = json.dumps(value)
            elif mutation == "relationship":
                changed[5] = "wrong"
            row = tuple(changed)
        return real(ctx, row, **kwargs)
    monkeypatch.setattr(_decisions, "_decode_pair", decode)
    failed = graph.relationship_decisions(request(env))
    assert failed.outcome == "failed" and failed.error.code == "invalid_projection"
    assert failed.count is None and not failed.members and not failed.relationships


def test_source_restore_and_creation_replay_never_resurrect_withdrawn_decision(native):
    env, graph = native
    reference = env.references[2]
    dependency = env.dependencies[reference.document_id]
    change = AddAssertion(
        kind="assertion", local_id="extra", predicate="work:decision",
        subject=StoredEntity(kind="stored", entity_id=env.projects[0]),
        object=StringObject(kind="string", value="Withdraw this exact contribution"),
        interpretation="explicit", support=SourceSupport(kind="source", evidence=(reference,)),
    )
    creation = WriteRequest(
        contract_version="foundation/1", request_id="extra", retry_key="extra",
        scope=env.scope, attribution=env.attribution,
        payload=ChangeSet(
            expected_schema_revision=env.schema_revision, operation="enrich",
            dependencies=(dependency,), changes=(change,),
        ),
    )
    created = graph.write(creation)
    target = created.receipt.mappings[0].stored_id
    assert graph.refresh().state == "ready"
    assert target in {m.decision.assertion_id for m in graph.relationship_decisions(
        request(env),
    ).members}
    withdrawn = graph.write(withdrawal(env, target, writer="example"))
    assert withdrawn.status == "applied"
    assert graph.write(creation).receipt == created.receipt
    original = env.evidence.content(env.scope, reference.document_id, reference.revision_id).text
    state = dependency.state_version
    for text in ("Changed source", original):
        saved = graph.write(WriteRequest(
            contract_version="foundation/1", request_id=str(uuid4()), retry_key=str(uuid4()),
            scope=env.scope, attribution=env.attribution,
            payload=PutDocument(
                operation="put_document",
                precondition=ExpectedState(kind="match", state_version=state),
                document=ExternalDocument(source_namespace="notes", synchronization_scope="local",
                                          external_id="note-2"),
                content=SuppliedContent(text=text, passage_policy="supplied-anchors/1", anchors=(
                    SuppliedAnchor(local_id="whole", start=0, end=len(text), quote=text),
                )),
                metadata=SourceMetadata(title="Synthetic note 2", location="example://note/2"),
            ),
        ))
        assert saved.status == "applied"
        state = saved.receipt.processing.state_version
    assert graph.refresh().state == "ready"
    restored = graph.relationship_decisions(request(env))
    assert restored.outcome == "complete"
    assert target not in {m.decision.assertion_id for m in restored.members}
    history = KnowledgeService(env.database, env.identity).contribution(
        env.scope, target, mode="history",
    )
    assert history.withdrawal.withdrawal_id == withdrawn.receipt.withdrawal_id
    ref = env.evidence.anchors(env.scope, reference.document_id, state).entries[0].reference
    replacement = graph.write(creation.model_copy(update={
        "retry_key": "replacement", "request_id": "replacement",
        "payload": ChangeSet(
            expected_schema_revision=env.schema_revision,
            operation="enrich", dependencies=(dependency.model_copy(update={
            "state_version": state, "revision_id": ref.revision_id,
        }),), changes=(change.model_copy(update={
            "support": SourceSupport(kind="source", evidence=(ref,)),
        }),)),
    }))
    replacement_id = replacement.receipt.mappings[0].stored_id
    assert replacement_id != target
    assert graph.refresh().state == "ready"
    final = graph.relationship_decisions(request(env))
    assert {m.decision.assertion_id for m in final.members} == {
        m.decision.assertion_id for m in restored.members
    } | {replacement_id}


@pytest.mark.parametrize("mutation", ["count", "empty-addition"])
def test_count_and_eof_share_original_generation(tmp_path, monkeypatch, mutation):
    require_native()
    env = fixture(
        tmp_path / "canonical.sqlite", decisions=0 if mutation == "empty-addition" else 12,
    )
    with LocalGraphSession(
        env.database, env.identity, env.scope, graph_directory=tmp_path / "derived",
    ) as graph:
        assert graph.refresh().state == "ready"
        original = NativeRows.read
        fired = []
        def read(rows, *, limit=200):
            page = original(rows, limit=limit)
            if not fired and page and len(page[0]) == 1 and type(page[0][0]) is int:
                fired.append(True)
                if mutation == "count":
                    return ((page[0][0] + 1,),)
                env.write((AddAssertion(
                    kind="assertion", local_id="new", predicate="work:decision",
                    subject=StoredEntity(kind="stored", entity_id=env.projects[0]),
                    object=StringObject(kind="string", value="New decision"),
                    interpretation="explicit",
                    support=SourceSupport(kind="source", evidence=(env.references[0],)),
                ),))
            return page
        with monkeypatch.context() as patch:
            patch.setattr(NativeRows, "read", read)
            result = graph.relationship_decisions(request(env))
        assert fired and result.outcome == "failed" and result.count is None
        assert result.error.code == (
            "invalid_projection" if mutation == "count" else "state_changed"
        )
        if mutation == "empty-addition":
            assert graph.refresh().state == "ready"
            assert graph.relationship_decisions(request(env)).count == 1


def test_borrowed_proof_page_dropped_before_next_read_and_eof(native, monkeypatch):
    env, graph = native
    assert graph.refresh().state == "ready"
    read = NativeRows.read
    pages = []
    def tracked_read(rows, *, limit=200):
        if limit == 32:
            frame = sys._getframe(1)
            while frame.f_code.co_name != "_select_relationship_decisions":
                frame = frame.f_back
                assert frame is not None
            if pages:
                assert "page" not in frame.f_locals and "row" not in frame.f_locals
            page = read(rows, limit=2)  # Force many transitions without altering full results.
            pages.append(len(page))
            return page
        return read(rows, limit=limit)
    monkeypatch.setattr(NativeRows, "read", tracked_read)
    result = graph.relationship_decisions(request(env))
    assert result.count == 12 and len(pages) > 2 and pages[-1] == 0
