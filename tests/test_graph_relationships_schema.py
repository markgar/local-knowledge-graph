"""Real native acceptance of mixed authoring revisions and exact-ID provenance."""

import json

import pytest
from support.graph import fixture, require_native

from kg.graph import LocalGraphSession, _decisions, _relationships
from kg.knowledge import KnowledgeAdministration, KnowledgeService
from kg.knowledge._registry import proposal_digest
from kg.models.evidence import LocalAdminAuthority
from kg.models.foundation import AddAssertion, SourceSupport, StoredEntity, StringObject
from kg.models.graph import GraphEntitySelector, GraphRelationshipDecisionsRequest
from kg.models.schema import (
    ConsideredTerm,
    SchemaApplyRequest,
    SchemaApproval,
    SchemaExample,
    SchemaProposal,
    TermRef,
    TermReview,
    WidenPredicate,
)


def evolve(env, predicate="work:decision"):
    ref = env.references[0]
    value = SchemaProposal(
        corpus_id=env.scope.corpus_id,
        base_revision=env.schema_revision,
        attribution=env.attribution,
        rationale="Synthetic schema lifecycle acceptance.",
        widen_predicates=(
            WidenPredicate(
                name=predicate,
                add_subject_types=("person" if predicate == "work:decision" else "project",),
                review=TermReview(
                    candidates=(
                        ConsideredTerm(
                            term=TermRef(kind="predicate", name=predicate),
                            assessment="Reuse the same predicate meaning and encoding.",
                        ),
                    ),
                    reuse_assessment="Preserve decision meaning rather than add a synonym.",
                    extension_rationale="Test person-subject admission in this synthetic fixture.",
                    defer_assessment="This mechanical test makes no semantic quality claim.",
                    example_ids=("fixture",),
                ),
            ),
        ),
        examples=(
            SchemaExample(
                example_id="fixture",
                reference=ref,
                state_version=env.dependencies[ref.document_id].state_version,
                explanation="Supplied synthetic graph test evidence.",
            ),
        ),
    )
    outcome = KnowledgeAdministration(
        env.database,
        LocalAdminAuthority(principal_id=env.identity.principal_id),
    ).apply_schema(
        SchemaApplyRequest(
            request_id=predicate,
            retry_key=predicate,
            scope=env.scope,
            proposal=value,
            approved_proposal_digest=proposal_digest(value),
            approval=SchemaApproval(
                human_reviewed=True, rationale="Reviewed synthetic test change."
            ),
        )
    )
    assert outcome.receipt is not None, outcome.error
    env.schema_revision = outcome.receipt.revision
    return outcome.receipt


def request(env):
    return GraphRelationshipDecisionsRequest(
        request_id="mixed",
        scope=env.scope,
        start=GraphEntitySelector(entity_id=env.person),
        predicate="work:owns",
        direction="outgoing",
    )


@pytest.mark.native
@pytest.mark.requires_native
def test_mixed_revision_native_proofs_refresh_and_canonical_history(tmp_path):
    require_native()
    env = fixture(tmp_path / "source.sqlite", decisions=4)
    old = env.schema_revision
    with LocalGraphSession(
        env.database,
        env.identity,
        env.scope,
        graph_directory=tmp_path / "derived",
    ) as graph:
        original = graph.relationship_decisions(request(env))
        assert original.count == 4
        evolve(env)
        stale = graph.relationship_decisions(request(env))
        assert stale.outcome == "failed" and stale.error.code == "state_changed"
        assert graph.refresh().state == "ready"
        renewed = graph.relationship_decisions(request(env))
        assert renewed.count == 4 and renewed.generation != original.generation
        assert renewed.members == original.members
        added = env.write(
            (
                AddAssertion(
                    kind="assertion",
                    local_id="later",
                    subject=StoredEntity(kind="stored", entity_id=env.projects[0]),
                    predicate="work:decision",
                    object=StringObject(kind="string", value="Later decision."),
                    interpretation="explicit",
                    support=SourceSupport(
                        kind="source",
                        evidence=(env.references[0],),
                    ),
                ),
            )
        )["later"]
        assert graph.refresh().state == "ready"
        mixed = graph.relationship_decisions(request(env))
        assert mixed.count == 5
        assert {m.decision.schema_version for m in mixed.members} == {
            old.revision_id,
            env.schema_revision.revision_id,
        }
        assert {p.assertion.schema_version for p in mixed.relationships} == {old.revision_id}
        reader = KnowledgeService(env.database, env.identity)
        assert (
            reader.contribution(env.scope, added).schema_version == env.schema_revision.revision_id
        )
        for member in original.members:
            assert (
                reader.contribution(
                    env.scope,
                    member.decision.assertion_id,
                ).schema_version
                == old.revision_id
            )


@pytest.mark.native
@pytest.mark.requires_native
@pytest.mark.parametrize("side", ["relationship", "decision"])
def test_valid_compatible_revision_substitution_rejected(tmp_path, monkeypatch, side):
    require_native()
    env = fixture(tmp_path / "source.sqlite", decisions=2)
    evolve(env)
    if side == "relationship":
        real = _relationships.decode_relationship

        def substitute(ctx, row, **kwargs):
            value = json.loads(row[3])
            value["schema_version"] = env.schema_revision.revision_id
            return real(ctx, (*row[:3], json.dumps(value), *row[4:]), **kwargs)

        monkeypatch.setattr(_decisions, "decode_relationship", substitute)
    else:
        real = _decisions._decode_pair

        def substitute(ctx, row, **kwargs):
            value = json.loads(row[1])
            value["schema_version"] = env.schema_revision.revision_id
            value["decision_member"]["schema_version"] = env.schema_revision.revision_id
            return real(ctx, (row[0], json.dumps(value), *row[2:]), **kwargs)

        monkeypatch.setattr(_decisions, "_decode_pair", substitute)
    with LocalGraphSession(
        env.database,
        env.identity,
        env.scope,
        graph_directory=tmp_path / "derived",
    ) as graph:
        result = graph.relationship_decisions(request(env))
        assert result.error.code == "invalid_projection"
        assert result.count is None and not result.members and not result.relationships
