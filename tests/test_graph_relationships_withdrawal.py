from contextlib import contextmanager
from uuid import uuid4

import pytest
from support.graph import fixture, require_native

from kg.evidence import EvidenceServiceError
from kg.evidence._graph_observer import GraphSourceOperation
from kg.graph import LocalGraphSession, _relationships
from kg.graph import session as session_module
from kg.graph._native import NativeError
from kg.knowledge import KnowledgeService
from kg.models.evidence import StoredCitation
from kg.models.foundation import (
    AddAssertion,
    ChangeSet,
    EntityObject,
    ExpectedState,
    ExternalDocument,
    PutDocument,
    SourceMetadata,
    SourceSupport,
    StoredEntity,
    SuppliedAnchor,
    SuppliedContent,
    WithdrawAssertion,
    WriteRequest,
)
from kg.models.graph import GraphEntitySelector, GraphTraversalRequest


def query(env, project=None):
    return GraphTraversalRequest(
        request_id="withdrawal-query", scope=env.scope,
        start=GraphEntitySelector(entity_id=project or env.person),
        predicate="work:owns", direction="incoming" if project else "outgoing",
    )


def withdrawal(env, target):
    return WriteRequest(
        contract_version="foundation/1", request_id=str(uuid4()), retry_key=str(uuid4()),
        scope=env.scope, attribution=env.attribution,
        payload=WithdrawAssertion(operation="withdraw_assertion", contribution_id=target),
    )


def test_public_withdrawal_preserves_history_and_exact_remaining_proofs(tmp_path, monkeypatch):
    require_native()
    env = fixture(tmp_path / "canonical.sqlite", decisions=0)
    knowledge = KnowledgeService(env.database, env.identity)
    with LocalGraphSession(
        env.database, env.identity, env.scope, graph_directory=tmp_path / "derived",
    ) as graph:
        before = graph.traverse(query(env))
        incoming = graph.traverse(query(env, env.projects[0]))
        assert before.outcome == incoming.outcome == "complete"
        assert len(before.paths) == 10 and len(incoming.paths) == 2
        target = incoming.paths[0].assertion.assertion_id
        original = knowledge.contribution(env.scope, target)
        citations = tuple(StoredCitation(
            reference=e.reference, metadata_snapshot_id=e.metadata_snapshot_id,
            state_version=e.dependency.state_version,
        ) for e in original.evidence)
        quotes = tuple(env.evidence.citation(env.scope, c).quote for c in citations)
        assert all(quotes)
        req = withdrawal(env, target)
        result = graph.write(req)
        assert result.status == "applied" and graph.status().state == "dirty"
        with pytest.raises(EvidenceServiceError, match="not_found"):
            knowledge.contribution(env.scope, target)
        history = knowledge.contribution(env.scope, target, mode="history")
        assert not history.is_current
        assert history.withdrawal.withdrawal_id == result.receipt.withdrawal_id
        assert (history.payload, history.evidence, history.attribution, history.witnesses) == (
            original.payload, original.evidence, original.attribution, original.witnesses,
        )
        assert tuple(env.evidence.citation(env.scope, c).quote for c in citations) == quotes

        with monkeypatch.context() as patch:
            def fail(*args, **kwargs):
                raise NativeError("native_error")
            patch.setattr(session_module, "build_graph", fail)
            assert graph.refresh().error.code == "native_error"
        assert env.evidence.write(req).receipt == result.receipt
        assert graph.refresh().state == "ready"
        after = graph.traverse(query(env))
        remaining = tuple(p for p in before.paths if p.assertion.assertion_id != target)
        assert after.outcome == "complete" and after.paths == remaining
        assert after.generation != before.generation
        assert all(knowledge.contribution(
            env.scope, p.assertion.assertion_id,
        ).is_current for p in after.paths)
        assert graph.traverse(query(env, env.projects[0])).paths == incoming.paths[1:]

        assert graph.write(req).receipt == result.receipt
        assert graph.status().state == "dirty"
        rebuilt = graph.traverse(query(env))
        assert rebuilt.outcome == "complete" and rebuilt.paths == remaining
        assert rebuilt.generation != after.generation
        unchanged = graph.write(withdrawal(env, target))
        assert unchanged.status == "unchanged" and unchanged.receipt == result.receipt
        assert graph.status().state == "dirty"
        assert graph.refresh().state == "ready"
        assert graph.traverse(query(env)).paths == remaining

        other = incoming.paths[1].assertion.assertion_id
        assert graph.write(withdrawal(env, other)).status == "applied"
        assert graph.refresh().state == "ready"
        empty = graph.traverse(query(env, env.projects[0]))
        assert empty.outcome == "empty" and empty.root_entity_id == env.projects[0]
        assert empty.paths == () and empty.generation is not None
        final = tuple(p for p in remaining if p.assertion.assertion_id != other)
        assert graph.traverse(query(env)).paths == final
    with LocalGraphSession(
        env.database, env.identity, env.scope, graph_directory=tmp_path / "derived",
    ) as restarted:
        assert restarted.traverse(query(env)).paths == final


@pytest.mark.parametrize("phase", ["idle", "decode", "fence"])
def test_public_external_withdrawal_never_releases_stale_paths(tmp_path, monkeypatch, phase):
    require_native()
    env = fixture(tmp_path / "canonical.sqlite", decisions=0)
    with LocalGraphSession(
        env.database, env.identity, env.scope, graph_directory=tmp_path / "derived",
    ) as graph:
        before = graph.traverse(query(env))
        assert before.outcome == "complete"
        req = withdrawal(env, before.paths[0].assertion.assertion_id)
        committed = []
        def withdraw():
            committed.append(env.evidence.write(req))
            assert committed[-1].status == "applied"
        with monkeypatch.context() as patch:
            if phase == "idle":
                withdraw()
                assert graph.status().state == "ready"
            elif phase == "decode":
                original = _relationships.decode_relationship
                def intercept(ctx, row, **kwargs):
                    proof = original(ctx, row, **kwargs)
                    if not committed:
                        withdraw()
                    return proof
                patch.setattr(_relationships, "decode_relationship", intercept)
            else:
                original = GraphSourceOperation.release_fence
                @contextmanager
                def intercept(operation):
                    withdraw()
                    with original(operation) as guard:
                        yield guard
                patch.setattr(GraphSourceOperation, "release_fence", intercept)
            failed = graph.traverse(query(env))
        assert committed
        assert failed.outcome == "failed" and failed.error.code == "state_changed"
        assert failed.paths == failed.candidate_ids == ()
        assert failed.root_entity_id is failed.generation is None
        assert graph.status().state == "dirty"
        assert graph.refresh().state == "ready"
        after = graph.traverse(query(env))
        assert after.outcome == "complete" and after.paths == before.paths[1:]
        assert after.generation != before.generation


def test_public_creation_replay_source_restore_and_new_assertion_do_not_resurrect(tmp_path):
    require_native()
    env = fixture(tmp_path / "canonical.sqlite", decisions=0)
    reference = env.references[2]
    dependency = env.dependencies[reference.document_id]
    change = AddAssertion(
        kind="assertion", local_id="extra", predicate="work:owns",
        subject=StoredEntity(kind="stored", entity_id=env.person),
        object=EntityObject(kind="entity", entity=StoredEntity(
            kind="stored", entity_id=env.projects[0],
        )),
        interpretation="explicit", support=SourceSupport(kind="source", evidence=(reference,)),
    )
    creation = WriteRequest(
        contract_version="foundation/1", request_id="extra", retry_key="extra",
        scope=env.scope, attribution=env.attribution,
        payload=ChangeSet(operation="enrich", dependencies=(dependency,), changes=(change,)),
    )
    created = env.evidence.write(creation)
    assert created.status == "applied"
    target = created.receipt.mappings[0].stored_id
    with LocalGraphSession(
        env.database, env.identity, env.scope, graph_directory=tmp_path / "derived",
    ) as graph:
        before = graph.traverse(query(env))
        assert before.outcome == "complete"
        assert target in {p.assertion.assertion_id for p in before.paths}
        receipt = graph.write(withdrawal(env, target))
        assert receipt.status == "applied"
        assert graph.refresh().state == "ready"
        assert graph.write(creation).receipt == created.receipt
        replayed = graph.traverse(query(env))
        assert replayed.outcome == "complete"
        assert replayed.paths == tuple(
            p for p in before.paths if p.assertion.assertion_id != target
        )

        original_text = env.evidence.content(
            env.scope, reference.document_id, reference.revision_id,
        ).text
        state = dependency.state_version
        for text in ("Changed source", original_text):
            update = graph.write(WriteRequest(
                contract_version="foundation/1", request_id=str(uuid4()), retry_key=str(uuid4()),
                scope=env.scope, attribution=env.attribution,
                payload=PutDocument(
                    operation="put_document", precondition=ExpectedState(
                        kind="match", state_version=state,
                    ),
                    document=ExternalDocument(
                        source_namespace="notes", synchronization_scope="local",
                        external_id="note-2",
                    ),
                    content=SuppliedContent(
                        text=text, passage_policy="supplied-anchors/1",
                        anchors=(SuppliedAnchor(local_id="whole", start=0, end=len(text),
                                                quote=text),),
                    ),
                    metadata=SourceMetadata(
                        title="Synthetic note 2", location="example://note/2",
                    ),
                ),
            ))
            assert update.status == "applied"
            state = update.receipt.processing.state_version
        assert graph.refresh().state == "ready"
        restored = graph.traverse(query(env))
        assert restored.outcome == "complete"
        assert target not in {p.assertion.assertion_id for p in restored.paths}
        knowledge = KnowledgeService(env.database, env.identity)
        history = knowledge.contribution(env.scope, target, mode="history")
        assert not history.is_current
        assert history.withdrawal.withdrawal_id == receipt.receipt.withdrawal_id
        captured = history.evidence[0]
        assert env.evidence.citation(env.scope, StoredCitation(
            reference=captured.reference, metadata_snapshot_id=captured.metadata_snapshot_id,
            state_version=captured.dependency.state_version,
        )).quote == original_text

        current_ref = env.evidence.anchors(
            env.scope, reference.document_id, state,
        ).entries[0].reference
        corrected = creation.model_copy(update={
            "request_id": "new-assertion", "retry_key": "new-assertion",
            "payload": ChangeSet(
                operation="enrich",
                dependencies=(dependency.model_copy(update={
                    "state_version": state, "revision_id": current_ref.revision_id,
                }),),
                changes=(change.model_copy(update={
                    "support": SourceSupport(kind="source", evidence=(current_ref,)),
                }),),
            ),
        })
        replacement = graph.write(corrected)
        assert replacement.status == "applied"
        replacement_id = replacement.receipt.mappings[0].stored_id
        assert replacement_id != target
        assert graph.refresh().state == "ready"
        final = graph.traverse(query(env))
        assert final.outcome == "complete"
        assert {p.assertion.assertion_id for p in final.paths} == {
            p.assertion.assertion_id for p in restored.paths
        } | {replacement_id}
        assert knowledge.contribution(env.scope, replacement_id).is_current
        with pytest.raises(EvidenceServiceError, match="not_found"):
            knowledge.contribution(env.scope, target)
