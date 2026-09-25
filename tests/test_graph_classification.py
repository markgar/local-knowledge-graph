"""Graph-session authoring uses the concise public record contract."""

import json
from uuid import uuid4

import pytest
from support.graph import fixture, require_native
from test_graph_decisions import request
from test_record_authoring_private import authored_request
from test_record_authoring_private import env as env

from kg.graph import LocalGraphSession, _decisions
from kg.knowledge import KnowledgeService
from kg.knowledge._write_models import AddClassification
from kg.models.authoring import (
    AuthoredEntity,
    ClassificationReviewWitness,
    ExistingIdentity,
    RecordAuthoringDocument,
    RecordAuthoringRequest,
    SourceCapture,
    StoredClaimSelection,
)
from kg.models.foundation import SourceSupport, StoredEntity


@pytest.mark.service
def test_graph_session_records_public_authoring_without_native_write_shape(env, tmp_path) -> None:
    request = authored_request(env)
    with LocalGraphSession(
        env.database,
        env.service.identity,
        env.scope,
        graph_directory=tmp_path / "derived",
    ) as graph:
        outcome = graph.record(request)

    assert outcome.receipt is not None
    assert outcome.receipt.interface_version == "record-authoring/1"


def choose(env, graph, claim_id):
    entity_id = env.projects[0]
    knowledge = KnowledgeService(env.database, env.identity)
    reviewed = knowledge.classification_review(env.scope, entity_id)
    reference = env.references[0]
    witness = ClassificationReviewWitness(
        interface_version="classification-review-witness/1",
        entity_id=entity_id,
        schema_revision=env.schema_revision,
        selection_id=reviewed.selection_id,
        selected_claim_id=reviewed.selected.claim_id,
        reviewed_claim_ids=reviewed.reviewed_claim_ids,
        reviewed_candidates_digest=reviewed.reviewed_candidates_digest,
        review_coverage=reviewed.review_coverage,
        accept_incomplete_review=False,
    )
    outcome = graph.record(
        RecordAuthoringRequest(
            interface_version="record-authoring/1",
            request_id=str(uuid4()),
            retry_key=str(uuid4()),
            scope=env.scope,
            attribution=env.attribution,
            document=RecordAuthoringDocument(
                interface_version="record-authoring/1",
                expected_schema_revision=env.schema_revision,
                support={
                    "source": SourceCapture(
                        kind="source",
                        reference=reference,
                        state_version=env.dependencies[reference.document_id].state_version,
                    )
                },
                entities=(
                    AuthoredEntity(
                        local_id="project",
                        identity=ExistingIdentity(kind="existing", entity_id=entity_id),
                        selection=StoredClaimSelection(
                            kind="stored_claim",
                            claim_id=claim_id,
                            expected_entity_type="project",
                            rationale="Explicitly change the selected supporting claim.",
                            review_witness=witness,
                        ),
                    ),
                ),
            ),
        )
    )
    assert outcome.receipt is not None, outcome.error
    return outcome.receipt.entities[0].selection_id


@pytest.mark.native
@pytest.mark.requires_native
def test_native_same_type_selection_refresh_never_resurrects_old_edges(tmp_path):
    require_native()
    native_env = fixture(tmp_path / "source.sqlite", decisions=4)
    knowledge = KnowledgeService(native_env.database, native_env.identity)
    original = knowledge.entity(
        native_env.scope,
        native_env.projects[0],
    ).classification.selected
    other = native_env.write(
        (
            AddClassification(
                kind="classification",
                local_id="other",
                entity=StoredEntity(kind="stored", entity_id=native_env.projects[0]),
                entity_type="project",
                interpretation="explicit",
                support=SourceSupport(
                    kind="source",
                    evidence=(native_env.references[0],),
                ),
            ),
        )
    )["other"]
    with LocalGraphSession(
        native_env.database,
        native_env.identity,
        native_env.scope,
        graph_directory=tmp_path / "derived",
    ) as graph:
        assert graph.relationship_decisions(request(native_env)).count == 4
        first = choose(native_env, graph, other)
        assert graph.status().state == "dirty"
        assert graph.relationship_decisions(request(native_env)).count == 2
        restored = choose(native_env, graph, original.claim_id)
        assert restored not in {first, original.selection_id}
        assert graph.refresh().error is None
        result = graph.relationship_decisions(request(native_env))
        assert result.exact and result.count == 2
        assert knowledge.entity(native_env.scope, native_env.projects[0]).entity_type == "project"


@pytest.mark.native
@pytest.mark.requires_native
@pytest.mark.parametrize("field", ["claim_id", "selection_id", "passage_set_id"])
def test_native_classification_substitution_cannot_release_a_count(
    tmp_path,
    monkeypatch,
    field,
):
    require_native()
    native_env = fixture(tmp_path / "source.sqlite", decisions=2)
    with LocalGraphSession(
        native_env.database,
        native_env.identity,
        native_env.scope,
        graph_directory=tmp_path / "derived",
    ) as graph:
        assert graph.relationship_decisions(request(native_env)).count == 2
        foreign = (
            KnowledgeService(native_env.database, native_env.identity)
            .entity(native_env.scope, native_env.projects[-1])
            .classification.selected
        )
        decode = _decisions._decode_pair

        def substituted(ctx, row, **kwargs):
            changed = list(row)
            value = json.loads(changed[1])
            if field == "passage_set_id":
                with native_env.database.connection() as connection:
                    wrong_set = connection.execute(
                        "SELECT passage_set_id FROM passage WHERE passage_id=?",
                        (native_env.references[1].passage_id,),
                    ).fetchone()[0]
                for proof in value["classification_evidence"]:
                    if proof["passage_set_id"] is not None:
                        assert proof["passage_set_id"] != wrong_set
                        proof["passage_set_id"] = wrong_set
            else:
                value["classification_witnesses"][0][field] = getattr(foreign, field)
                value["decision_member"]["dependencies"]["subject_classification"][field] = getattr(
                    foreign, field
                )
            changed[1] = json.dumps(value)
            return decode(ctx, tuple(changed), **kwargs)

        monkeypatch.setattr(_decisions, "_decode_pair", substituted)
        result = graph.relationship_decisions(request(native_env))
        assert result.error.code == "invalid_projection"
        assert result.count is None and not result.members and not result.relationships


@pytest.mark.native
@pytest.mark.requires_native
def test_native_binds_every_distinct_assertion(tmp_path, monkeypatch):
    from kg.graph.session import GraphReadContext

    require_native()
    native_env = fixture(tmp_path / "source.sqlite", decisions=4)
    validated = set()
    original = GraphReadContext.require_classification_captures

    def observed(ctx, assertion_id, captures, evidence):
        original(ctx, assertion_id, captures, evidence)
        validated.add(assertion_id)

    monkeypatch.setattr(GraphReadContext, "require_classification_captures", observed)
    with LocalGraphSession(
        native_env.database,
        native_env.identity,
        native_env.scope,
        graph_directory=tmp_path / "derived",
    ) as graph:
        result = graph.relationship_decisions(request(native_env))
        assert result.count == 4 and result.exact
        expected = {member.decision.assertion_id for member in result.members}
        expected.update(path.assertion.assertion_id for path in result.relationships)
        assert validated == expected
