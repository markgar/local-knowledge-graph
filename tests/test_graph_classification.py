"""Real native classification proof binding; requires separately authorized native execution."""

import json
from uuid import uuid4

import pytest
from support.graph import fixture, require_native
from test_graph_decisions import request

from kg.graph import LocalGraphSession, _decisions
from kg.knowledge import KnowledgeService
from kg.models.foundation import (
    AddClassification,
    ChangeSet,
    SelectClassification,
    SourceSupport,
    StoredClassificationRef,
    StoredEntity,
    WriteRequest,
)


def choose(env, graph, claim_id):
    entity_id = env.projects[0]
    reviewed = KnowledgeService(env.database, env.identity).classification_review(
        env.scope,
        entity_id,
    )
    outcome = graph.write(
        WriteRequest(
            contract_version="foundation/1",
            request_id=str(uuid4()),
            retry_key=str(uuid4()),
            scope=env.scope,
            attribution=env.attribution,
            payload=ChangeSet(
                operation="enrich",
                expected_schema_revision=env.schema_revision,
                dependencies=(),
                changes=(
                    SelectClassification(
                        kind="classification_selection",
                        local_id="selection",
                        entity=StoredEntity(kind="stored", entity_id=entity_id),
                        claim=StoredClassificationRef(kind="stored", contribution_id=claim_id),
                        expected_selection_id=reviewed.selection_id,
                        reviewed_candidates_digest=reviewed.reviewed_candidates_digest,
                        reviewed_claim_ids=reviewed.reviewed_claim_ids,
                        review_coverage="complete",
                        accept_incomplete_review=False,
                        rationale="Explicitly change the synthetic selected supporting claim.",
                    ),
                ),
            ),
        )
    )
    assert outcome.receipt is not None, outcome.error
    return outcome.receipt.entity_classifications[0].selection_id


def test_native_same_type_selection_refresh_never_resurrects_old_edges(tmp_path):
    require_native()
    env = fixture(tmp_path / "source.sqlite", decisions=4)
    knowledge = KnowledgeService(env.database, env.identity)
    original = knowledge.entity(env.scope, env.projects[0]).classification.selected
    other = env.write(
        (
            AddClassification(
                kind="classification",
                local_id="other",
                entity=StoredEntity(kind="stored", entity_id=env.projects[0]),
                entity_type="project",
                interpretation="explicit",
                support=SourceSupport(kind="source", evidence=(env.references[0],)),
            ),
        )
    )["other"]
    with LocalGraphSession(
        env.database,
        env.identity,
        env.scope,
        graph_directory=tmp_path / "derived",
    ) as graph:
        assert graph.relationship_decisions(request(env)).count == 4
        first = choose(env, graph, other)
        assert graph.status().state == "dirty"
        assert graph.relationship_decisions(request(env)).count == 2
        restored = choose(env, graph, original.claim_id)
        assert restored not in {first, original.selection_id}
        assert graph.refresh().error is None
        result = graph.relationship_decisions(request(env))
        assert result.exact and result.count == 2
        assert knowledge.entity(env.scope, env.projects[0]).entity_type == "project"


@pytest.mark.parametrize("field", ["claim_id", "selection_id", "passage_set_id"])
def test_native_classification_substitution_cannot_release_a_count(tmp_path, monkeypatch, field):
    require_native()
    env = fixture(tmp_path / "source.sqlite", decisions=2)
    with LocalGraphSession(
        env.database,
        env.identity,
        env.scope,
        graph_directory=tmp_path / "derived",
    ) as graph:
        assert graph.relationship_decisions(request(env)).count == 2
        foreign = (
            KnowledgeService(env.database, env.identity)
            .entity(
                env.scope,
                env.projects[-1],
            )
            .classification.selected
        )
        decode = _decisions._decode_pair

        def substituted(ctx, row, **kwargs):
            changed = list(row)
            value = json.loads(changed[1])
            if field == "passage_set_id":
                with env.database.connection() as connection:
                    wrong_set = connection.execute(
                        "SELECT passage_set_id FROM passage WHERE passage_id=?",
                        (env.references[1].passage_id,),
                    ).fetchone()[0]
                for proof in value["classification_evidence"]:
                    if proof["passage_set_id"] is not None:
                        assert proof["passage_set_id"] != wrong_set
                        proof["passage_set_id"] = wrong_set
            else:
                value["classification_witnesses"][0][field] = getattr(foreign, field)
                value["decision_member"]["dependencies"]["subject_classification"][field] = getattr(
                    foreign,
                    field,
                )
            changed[1] = json.dumps(value)
            return decode(ctx, tuple(changed), **kwargs)

        monkeypatch.setattr(_decisions, "_decode_pair", substituted)
        result = graph.relationship_decisions(request(env))
        assert result.error.code == "invalid_projection"
        assert result.count is None and not result.members and not result.relationships


def test_native_pool_still_binds_every_distinct_assertion(tmp_path, monkeypatch):
    from kg.graph.session import GraphReadContext

    require_native()
    env = fixture(tmp_path / "source.sqlite", decisions=4)
    validated = set()
    original = GraphReadContext.require_classification_captures

    def observed(ctx, assertion_id, captures, evidence):
        original(ctx, assertion_id, captures, evidence)
        validated.add(assertion_id)

    monkeypatch.setattr(GraphReadContext, "require_classification_captures", observed)
    with LocalGraphSession(
        env.database,
        env.identity,
        env.scope,
        graph_directory=tmp_path / "derived",
    ) as graph:
        result = graph.relationship_decisions(request(env))
        assert result.count == 4 and result.exact
        expected = {m.decision.assertion_id for m in result.members}
        expected.update(p.assertion.assertion_id for p in result.relationships)
        assert validated == expected
