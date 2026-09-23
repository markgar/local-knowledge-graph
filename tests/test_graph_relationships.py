from threading import Event

import pytest
from pydantic import ValidationError
from support.graph import fixture, require_native

from kg.graph import LocalGraphSession
from kg.graph._session_types import GraphSessionError
from kg.models.evidence import StoredCitation
from kg.models.foundation import (
    AddAlias,
    AddAssertion,
    EntityObject,
    SourceSupport,
    StoredEntity,
)
from kg.models.graph import GraphEntitySelector, GraphTraversalRequest, GraphTraversalResult


def request(env, **changes):
    values = dict(
        request_id="relationship", scope=env.scope,
        start=GraphEntitySelector(entity_id=env.person), predicate="work:owns",
        direction="outgoing",
    )
    values.update(changes)
    return GraphTraversalRequest(**values)


@pytest.fixture
def native(tmp_path):
    require_native()
    env = fixture(tmp_path / "source.sqlite", decisions=0)
    with LocalGraphSession(
        env.database, env.identity, env.scope, graph_directory=tmp_path / "derived",
    ) as session:
        yield env, session


def test_native_public_directions_exact_full_proof_and_historical_citations(native):
    env, session = native
    assert session.capabilities().runtime == "available"
    assert session.status().state == "unbuilt"
    result = session.traverse(request(env))
    assert result.outcome == "complete" and len(result.paths) == 10
    assert result.generation == session._generation
    assert {p.path.entity_ids[1] for p in result.paths} == set(env.projects)
    assert [p.assertion.assertion_id for p in result.paths] == sorted(env.expected)
    for proof in result.paths:
        a = proof.assertion
        assert a.subject_witness == env.witnesses[env.person]
        assert a.object_witness == env.witnesses[a.object_entity_id]
        assert proof.path.support.evidence == env.expected[a.assertion_id]["support"]
        for item in a.support:
            captured = item.captured
            citation = env.evidence.citation(env.scope, StoredCitation(
                reference=captured.reference,
                metadata_snapshot_id=captured.metadata_snapshot_id,
                state_version=captured.dependency.state_version,
            ))
            assert citation
    decoded = GraphTraversalResult.model_validate_json(result.model_dump_json())
    assert decoded == result
    incoming = session.traverse(request(
        env, start=GraphEntitySelector(entity_id=env.projects[0]), direction="incoming",
    ))
    assert incoming.outcome == "complete" and len(incoming.paths) == 2
    assert incoming.generation == result.generation
    assert all(p.path.entity_ids == (env.projects[0], env.person) for p in incoming.paths)
    assert session.traverse(request(env)) == result


def test_native_ambiguity_absence_preflight_alias_and_strict_direction(native):
    env, session = native
    ambiguous = session.traverse(request(env, start=GraphEntitySelector(name="Alice")))
    assert ambiguous.outcome == "ambiguous" and len(ambiguous.candidate_ids) == 2
    assert tuple(sorted(ambiguous.candidate_ids)) == ambiguous.candidate_ids
    assert not ambiguous.paths and ambiguous.root_entity_id is None
    absent = session.traverse(request(env, start=GraphEntitySelector(entity_id="absent")))
    assert absent.outcome == "empty" and absent.root_entity_id is None
    for predicate in ("unknown", "work:decision"):
        invalid = session.traverse(request(
            env, predicate=predicate, start=GraphEntitySelector(entity_id="absent"),
        ))
        assert invalid.outcome == "failed" and invalid.error.code == "unsupported"
        assert invalid.generation is None
    wrong_side = session.traverse(request(env, direction="incoming"))
    assert wrong_side.outcome == "failed" and wrong_side.error.code == "unsupported"
    other_person = next(i for i in ambiguous.candidate_ids if i != env.person)
    empty = session.traverse(request(env, start=GraphEntitySelector(entity_id=other_person)))
    assert empty.outcome == "empty" and empty.root_entity_id == other_person
    env.write((AddAlias(
        kind="alias", local_id="alias", entity=StoredEntity(kind="stored", entity_id=env.person),
        alias="Exact alias", support=SourceSupport(kind="source", evidence=(env.references[0],)),
    ),))
    stale = session.traverse(request(env))
    assert stale.outcome == "failed" and stale.error.code == "state_changed"
    assert session.refresh().state == "ready"
    alias = session.traverse(request(env, start=GraphEntitySelector(name="Exact alias")))
    assert alias.outcome == "complete" and alias.root_entity_id == env.person
    assert session.traverse(request(
        env, start=GraphEntitySelector(name="exact alias"),
    )).outcome == "empty"


def test_native_explicit_filter_and_cancel_closed(native):
    env, session = native
    env.write((AddAssertion(
        kind="assertion", local_id="inferred", predicate="work:owns",
        subject=StoredEntity(kind="stored", entity_id=env.person),
        object=EntityObject(kind="entity", entity=StoredEntity(
            kind="stored", entity_id=env.projects[0],
        )),
        interpretation="inferred", support=SourceSupport(
            kind="source", evidence=(env.references[0],),
        ),
    ),))
    assert len(session.traverse(request(env)).paths) == 10
    cancel = Event()
    cancel.set()
    failed = session.traverse(request(env), cancel=cancel)
    assert failed.outcome == "failed" and failed.error.code == "cancelled"
    assert not failed.paths and failed.generation is None
    session.close()
    assert session.traverse(request(env)).error.code == "closed"
    with pytest.raises(GraphSessionError, match="closed"):
        session.capabilities()


@pytest.mark.parametrize("hops", [True, False, 1.0, "1", 0, 2, 3, None])
def test_strict_one_hop_rejected_before_admission(tmp_path, monkeypatch, hops):
    env = fixture(tmp_path / "source.sqlite", decisions=0)
    with pytest.raises(ValidationError):
        request(env, max_hops=hops)
    with LocalGraphSession(
        env.database, env.identity, env.scope, graph_directory=tmp_path / "derived",
    ) as session:
        monkeypatch.setattr(session, "_admit", lambda **kw: pytest.fail("request admitted"))
        forged = request(env).model_copy(update={"max_hops": hops})
        with pytest.raises(GraphSessionError, match="invalid_request"):
            session.traverse(forged)


def test_content_free_unavailable_capability_and_optional_import(tmp_path, monkeypatch):
    from kg.graph import _native
    from kg.graph._native import NativeError
    env = fixture(tmp_path / "source.sqlite", decisions=0)
    def unavailable():
        raise NativeError("graph_unavailable")
    monkeypatch.setattr(_native, "_engine", unavailable)
    with LocalGraphSession(
        env.database, env.identity, env.scope, graph_directory=tmp_path / "derived",
    ) as session:
        caps = session.capabilities()
        assert caps.runtime == "unavailable"
        assert caps.operations == ("traverse", "relationship_decisions")
        assert caps.unavailable_reason == "graph_unavailable"
        assert session.status().state == "unbuilt"
        assert not (tmp_path / "derived").exists()
        failed = session.traverse(request(env))
        assert failed.outcome == "failed" and failed.error.code == "graph_unavailable"
        assert failed.generation is None and not failed.paths
