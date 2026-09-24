from contextlib import contextmanager
from threading import Event
from uuid import uuid4

import pytest
from support.graph import fixture, require_native

from kg.evidence._graph_observer import GraphSourceOperation
from kg.graph import LocalGraphSession, _relationships
from kg.models.foundation import (
    AddAssertion,
    AddEntitySupport,
    EntityObject,
    ExpectedState,
    ExternalDocument,
    RemoveDocument,
    SourceSupport,
    StoredEntity,
    WriteRequest,
)
from kg.models.graph import GraphEntitySelector, GraphTraversalRequest


def request(env):
    return GraphTraversalRequest(
        request_id="race", scope=env.scope, start=GraphEntitySelector(entity_id=env.person),
        predicate="work:owns", direction="outgoing",
    )


def remove(env):
    reference = env.references[0]
    outcome = env.evidence.write(WriteRequest(
        contract_version="foundation/1", request_id=str(uuid4()), retry_key=str(uuid4()),
        scope=env.scope, attribution=env.attribution,
        payload=RemoveDocument(
            operation="remove_document",
            document=ExternalDocument(
                source_namespace="notes", external_id="note-0", synchronization_scope="local",
            ),
            precondition=ExpectedState(
                kind="match", state_version=env.dependencies[reference.document_id].state_version,
            ),
        ),
    ))
    assert outcome.receipt is not None, outcome.error


@pytest.mark.parametrize("phase", ["idle", "decode", "fence", "revoke", "cancel"])
def test_native_mutation_and_release_never_return_provisional_paths(tmp_path, monkeypatch, phase):
    require_native()
    env = fixture(tmp_path / "source.sqlite", decisions=0)
    with LocalGraphSession(
        env.database, env.identity, env.scope, graph_directory=tmp_path / "derived",
    ) as session:
        original = session.traverse(request(env))
        assert original.outcome == "complete"
        cancel = Event()
        if phase == "idle":
            remove(env)
        elif phase in ("decode", "cancel"):
            decode = _relationships.decode_relationship
            calls = []
            def intercept(ctx, row, **kwargs):
                proof = decode(ctx, row, **kwargs)
                if not calls:
                    calls.append(True)
                    if phase == "decode":
                        remove(env)
                    else:
                        cancel.set()
                return proof
            monkeypatch.setattr(_relationships, "decode_relationship", intercept)
        else:
            fence = GraphSourceOperation.release_fence
            @contextmanager
            def intercept(operation):
                if phase == "revoke":
                    with env.database.transaction() as connection:
                        connection.execute("DELETE FROM policy_grant WHERE grant_name='read'")
                else:
                    remove(env)
                with fence(operation) as guard:
                    yield guard
            monkeypatch.setattr(GraphSourceOperation, "release_fence", intercept)
        failed = session.traverse(request(env), cancel=cancel)
        assert failed.outcome == "failed"
        expected = {"revoke": "forbidden", "cancel": "cancelled"}.get(phase, "state_changed")
        assert failed.error.code == expected
        assert failed.paths == () and failed.generation is None
        assert original.paths and original.generation
        monkeypatch.undo()
        if phase not in ("revoke", "cancel"):
            assert session.refresh().state == "ready"
            current = session.traverse(request(env))
            assert current.outcome == "empty" and current.root_entity_id is None
            assert current.generation != original.generation


def test_valid_alternative_root_basis_is_not_selected_basis(tmp_path, monkeypatch):
    import json

    from kg.knowledge._selection import EntityWitness, SourceWitness
    require_native()
    env = fixture(tmp_path / "source.sqlite", decisions=0)
    mapping = env.write((AddEntitySupport(
        kind="entity_support", local_id="alternative",
        entity=StoredEntity(kind="stored", entity_id=env.person),
        name="Alice",
        support=SourceSupport(kind="source", evidence=(env.references[2],)),
    ),))
    decode = _relationships.decode_relationship
    def substitute(ctx, row, **kwargs):
        from contextlib import closing

        from kg.knowledge._store import Store
        with closing(Store(
            ctx.canonical.connection, ctx.canonical.scope, ctx.meter.private_budget,
        )) as store:
            contribution = store.row("contribution", mapping["alternative"])
            basis, current = store.support(contribution)
            assert current and isinstance(basis, SourceWitness)
            alternative = EntityWitness(
                entity_id=env.person, contribution_id=mapping["alternative"],
                contribution_sequence=contribution["sequence"], basis=basis,
            )
            assert alternative != kwargs["root"].witness
            assertion = json.loads(row[3])
            assertion["subject_witness"] = alternative.model_dump(mode="json")
            return decode(ctx, (
                *row[:3], json.dumps(assertion), alternative.model_dump_json(), row[5],
            ), **kwargs)
    monkeypatch.setattr(_relationships, "decode_relationship", substitute)
    with LocalGraphSession(
        env.database, env.identity, env.scope, graph_directory=tmp_path / "derived",
    ) as session:
        result = session.traverse(request(env))
        assert result.error.code == "invalid_projection" and not result.paths


def test_custom_predicate_self_loop_and_scope_isolation(tmp_path, monkeypatch):
    from kg.knowledge import KnowledgeAdministration
    from kg.models.schema import SchemaPredicateDefinition
    require_native()
    register = KnowledgeAdministration.register_knowledge_schema
    def custom(admin, schema):
        schema = schema.model_copy(update={"definition": schema.definition.model_copy(update={
            "predicates": (*schema.definition.predicates, SchemaPredicateDefinition(
                name="team:related", description="An explicitly related person.",
                subject_types=("person",), object_kind="entity", object_types=("person",),
            )),
        })})
        return register(admin, schema)
    with monkeypatch.context() as patch:
        patch.setattr(KnowledgeAdministration, "register_knowledge_schema", custom)
        env = fixture(tmp_path / "source.sqlite", decisions=0)
    env.write((AddAssertion(
        kind="assertion", local_id="loop", predicate="team:related",
        subject=StoredEntity(kind="stored", entity_id=env.person),
        object=EntityObject(kind="entity", entity=StoredEntity(
            kind="stored", entity_id=env.person,
        )),
        interpretation="explicit", support=SourceSupport(
            kind="source", evidence=(env.references[0],),
        ),
    ),))
    scope = env.scope.model_copy(update={"access": env.scope.access.model_copy(
        update={"namespaces": ("notes",)},
    )})
    with LocalGraphSession(
        env.database, env.identity, scope, graph_directory=tmp_path / "derived",
    ) as session:
        req = request(env).model_copy(update={"scope": scope, "predicate": "team:related"})
        out = session.traverse(req)
        incoming = session.traverse(req.model_copy(update={"direction": "incoming"}))
        assert len(out.paths) == len(incoming.paths) == 1
        assert out.paths == incoming.paths
        assert out.paths[0].path.entity_ids == (env.person, env.person)
        scoped = session.traverse(req.model_copy(update={"predicate": "work:owns"}))
        assert scoped.outcome == "complete" and len(scoped.paths) < 10
        assert all(ref.source_namespace == "notes"
                   for proof in scoped.paths for ref in proof.path.support.evidence)


def test_warm_cancel_during_canonical_resolution_keeps_latched_reason(tmp_path, monkeypatch):
    from kg.knowledge._reader import Cursor
    require_native()
    env = fixture(tmp_path / "source.sqlite", decisions=0)
    with LocalGraphSession(
        env.database, env.identity, env.scope, graph_directory=tmp_path / "derived",
    ) as session:
        assert session.traverse(request(env)).outcome == "complete"
        cancel = Event()
        read = Cursor.read
        def cancelled(cursor, **kwargs):
            cancel.set()
            page = read(cursor, **kwargs)
            cancel.clear()
            return page
        monkeypatch.setattr(Cursor, "read", cancelled)
        result = session.traverse(request(env), cancel=cancel)
        assert result.outcome == "failed" and result.error.code == "cancelled"
        assert not result.paths and result.generation is None
