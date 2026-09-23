import json
from contextlib import contextmanager

import pytest
from support.graph import fixture, require_native

from kg._execution_budget import PrivateResourceStop
from kg.evidence._graph_observer import GraphSourceOperation
from kg.graph import LocalGraphSession, _relationships
from kg.graph.session import GraphReadContext, _size
from kg.models.foundation import AddAssertion, EntityObject, SourceSupport, StoredEntity
from kg.models.graph import GraphEntitySelector, GraphTraversalRequest


def request(env):
    return GraphTraversalRequest(
        request_id="bounded", scope=env.scope, start=GraphEntitySelector(entity_id=env.person),
        predicate="work:owns", direction="outgoing",
    )


def add_relationships(env, count):
    for offset in range(0, count, 20):
        env.write(tuple(AddAssertion(
            kind="assertion", local_id=f"edge-{i}", predicate="work:owns",
            subject=StoredEntity(kind="stored", entity_id=env.person),
            object=EntityObject(kind="entity", entity=StoredEntity(
                kind="stored", entity_id=env.projects[0],
            )),
            interpretation="explicit",
            support=SourceSupport(kind="source", evidence=(env.references[0],)),
        ) for i in range(offset, min(count, offset + 20))))


def test_actual_native_1000_complete_warm_and_1001_no_prefix(tmp_path):
    require_native()
    env = fixture(tmp_path / "source.sqlite", decisions=0)
    add_relationships(env, 990)
    with LocalGraphSession(
        env.database, env.identity, env.scope, graph_directory=tmp_path / "derived",
    ) as session:
        result = session.traverse(request(env))
        assert result.outcome == "complete", result.error
        assert len(result.paths) == 1000
        assert _size(result) <= 8 << 20
        assert session.traverse(request(env)) == result
        add_relationships(env, 1)
        assert session.refresh().state == "ready"
        overflow = session.traverse(request(env))
        assert overflow.outcome == "failed" and overflow.error.code == "resource_exhausted"
        assert overflow.generation is None and overflow.paths == ()


@pytest.mark.parametrize("failure", ["decode", "assembly", "retain", "fence"])
def test_canonical_custody_native_lifetime_and_failure_cleanup(tmp_path, monkeypatch, failure):
    require_native()
    env = fixture(tmp_path / "source.sqlite", decisions=0)
    with LocalGraphSession(
        env.database, env.identity, env.scope, graph_directory=tmp_path / "derived",
    ) as session:
        assert session.refresh().state == "ready"
        closed, stores, contexts = [], [], []
        real_schema, real_close = _relationships.Store.schema, _relationships.Store.close
        real_resolve = _relationships.KnowledgeReader.resolve_entity
        real_decode = _relationships.decode_relationship
        real_retain = GraphReadContext.retain

        def schema(store):
            stores.append(store)
            return real_schema(store)

        def resolve(reader, selector):
            cursor = real_resolve(reader, selector)
            stores.append(cursor.store)
            return cursor

        def close(store):
            closed.append(store)
            real_close(store)

        def decode(ctx, row, **kwargs):
            contexts.append(ctx)
            assert len(stores) == 2 and not closed
            assert all(s._scratch._exit_callbacks for s in stores)
            assert ctx.meter.private_budget._scratch > 0
            if failure == "decode":
                return real_decode(ctx, row[:3] + ("invalid-json",) + row[4:], **kwargs)
            if failure == "assembly":
                raise PrivateResourceStop()
            return real_decode(ctx, row, **kwargs)

        def retain(ctx, value):
            assert not closed and ctx.meter.private_budget._scratch > 0
            if failure == "retain":
                raise PrivateResourceStop()
            return real_retain(ctx, value)

        original_fence = GraphSourceOperation.release_fence
        @contextmanager
        def fence(operation):
            assert len(closed) == 2
            if failure == "fence":
                assert operation.budget._scratch > 0
                raise PrivateResourceStop()
            with original_fence(operation) as current:
                yield current

        monkeypatch.setattr(_relationships.Store, "schema", schema)
        monkeypatch.setattr(_relationships.Store, "close", close)
        monkeypatch.setattr(_relationships.KnowledgeReader, "resolve_entity", resolve)
        monkeypatch.setattr(_relationships, "decode_relationship", decode)
        monkeypatch.setattr(GraphReadContext, "retain", retain)
        monkeypatch.setattr(GraphSourceOperation, "release_fence", fence)
        result = session.traverse(request(env))
        assert result.outcome == "failed"
        assert result.error.code == (
            "invalid_projection" if failure == "decode" else "resource_exhausted"
        )
        assert not result.paths and result.generation is None
        assert all(s in closed for s in stores)
        assert contexts and contexts[0].meter.private_budget._scratch == 0
        assert session._generation is None


@pytest.mark.parametrize("mutation", [
    "id", "predicate", "schema", "subject", "selected", "root-basis", "support-scope", "duplicate",
])
def test_native_proof_corruption_cannot_return_partial(tmp_path, monkeypatch, mutation):
    require_native()
    env = fixture(tmp_path / "source.sqlite", decisions=0)
    real = _relationships.decode_relationship
    seen = []
    def decode(ctx, row, **kwargs):
        if mutation == "duplicate" and seen:
            row = seen[0]
        else:
            seen.append(row)
            row = list(row)
            if mutation in ("id", "subject"):
                row[2 if mutation == "id" else 0] = "wrong"
            elif mutation in ("predicate", "schema", "support-scope"):
                assertion = json.loads(row[3])
                if mutation == "support-scope":
                    assertion["support"][0]["captured"]["reference"]["corpus_id"] = "other"
                else:
                    field = "predicate" if mutation == "predicate" else "schema_version"
                    assertion[field] = "wrong"
                row[3] = json.dumps(assertion)
            elif mutation in ("selected", "root-basis"):
                witness = json.loads(row[4])
                witness["contribution_id"] = "different"
                row[4] = json.dumps(witness)
                if mutation == "root-basis":
                    assertion = json.loads(row[3])
                    assertion["subject_witness"] = witness
                    row[3] = json.dumps(assertion)
            row = tuple(row)
        return real(ctx, row, **kwargs)
    monkeypatch.setattr(_relationships, "decode_relationship", decode)
    with LocalGraphSession(
        env.database, env.identity, env.scope, graph_directory=tmp_path / "derived",
    ) as session:
        result = session.traverse(request(env))
        assert result.outcome == "failed" and result.error.code == "invalid_projection"
        assert not result.paths and result.generation is None
