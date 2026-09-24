"""Grounded representations are explicit test input, not an extraction algorithm."""

from pathlib import Path

from support.classification import typed_entity
from support.evidence import put, receipt
from support.knowledge import revision
from support.query_knowledge import plan, setup, write

from kg.knowledge import KnowledgeAdministration, KnowledgeService
from kg.knowledge._registry import proposal_digest
from kg.models.evidence import LocalAdminAuthority
from kg.models.foundation import (
    AddAssertion,
    Attribution,
    BooleanObject,
    DocumentDependency,
    EntityObject,
    LocalEntity,
    SourceSupport,
    StringObject,
)
from kg.models.schema import (
    AddEntityType,
    AddPredicate,
    ConsideredTerm,
    EntityTypeDefinition,
    SchemaApplyRequest,
    SchemaApproval,
    SchemaExample,
    SchemaPredicateDefinition,
    SchemaProposal,
    TermRef,
    TermReview,
    UnresolvedConcept,
    WidenPredicate,
)
from kg.query import QueryService


def capture(env, key, text):
    saved = receipt(env.service.write(put(env.scope, external=key, text=text)))
    anchor = env.service.anchors(
        env.scope, saved.document_id, saved.processing.state_version
    ).entries[0]
    assert env.service.evidence(env.scope, anchor.reference).quote == text
    return SchemaExample(
        example_id=key,
        reference=anchor.reference,
        state_version=saved.processing.state_version,
        explanation="Explicitly supplied source; interpretation is outside the engine.",
    )


def supported(example):
    return SourceSupport(kind="source", evidence=(example.reference,))


def record(env, changes, examples):
    dependencies = tuple(
        DocumentDependency(
            source_namespace=e.reference.source_namespace,
            document_id=e.reference.document_id,
            revision_id=e.reference.revision_id,
            state_version=e.state_version,
        )
        for e in examples
    )
    return write(env, changes, dependencies=dependencies)


def review(*example_ids):
    return TermReview(
        candidates=(
            ConsideredTerm(
                term=TermRef(kind="entity_type", name="project"),
                assessment="A review, artifact or certificate is not a project.",
            ),
        ),
        reuse_assessment="Reuse project/person for genuine projects/people, not these concepts.",
        extension_rationale="Admit only the documented concepts and explicit value kinds.",
        defer_assessment=(
            "Do not resolve cross-document identity or infer ownership from task labels."
        ),
        example_ids=example_ids,
    )


def apply(env, value):
    reader = KnowledgeService(env.database, env.service.identity)
    checked = reader.validate_schema(env.scope, value)
    assert not reader.entities(env.scope).entries
    outcome = KnowledgeAdministration(
        env.database,
        LocalAdminAuthority(principal_id=env.service.identity.principal_id),
    ).apply_schema(
        SchemaApplyRequest(
            request_id="apply",
            retry_key="apply",
            scope=env.scope,
            proposal=value,
            approved_proposal_digest=proposal_digest(value),
            approval=SchemaApproval(
                human_reviewed=True, rationale="Reviewed exact fixture evidence."
            ),
        )
    )
    assert outcome.receipt is not None, outcome.error
    assert outcome.receipt.revision.definition_hash == checked.definition_hash
    assert not reader.entities(env.scope).entries
    return reader


def test_fixture_coverage_review_decision_export_and_certificate_without_false_ownership(tmp_path):
    env = setup(tmp_path / "coverage.sqlite")
    root = Path(__file__).parents[1] / "corpora/fixtures/atlas-vault"
    examples = tuple(
        capture(env, key, (root / filename).read_text())
        for key, filename in (
            ("audit", "04-audit-trail.md"),
            ("review", "03-security-review.md"),
            ("planning", "05-atlascope-planning.md"),
            ("integration", "06-integration-notes.md"),
        )
    )
    audit, meeting, planning, integration = examples
    proposal = SchemaProposal(
        corpus_id=env.scope.corpus_id,
        base_revision=revision(env),
        attribution=Attribution(
            owner_id="owner", writer_id="writer", producer="reviewed-fixtures", producer_version="1"
        ),
        rationale="Represent omitted supported concepts without synthetic project typing.",
        add_entity_types=tuple(
            AddEntityType(
                definition=EntityTypeDefinition(name=name, description=description),
                review=review(*ids),
            )
            for name, description, ids in (
                ("review", "An explicitly identified review context.", ("review",)),
                (
                    "artifact",
                    "An identified produced information artifact.",
                    ("audit", "integration"),
                ),
                ("certificate", "An identified signing or identity certificate.", ("audit",)),
            )
        ),
        add_predicates=(
            AddPredicate(
                definition=SchemaPredicateDefinition(
                    name="requires",
                    description="The subject explicitly requires the object.",
                    subject_types=("review",),
                    object_kind="entity",
                    object_types=("artifact",),
                ),
                review=review("review"),
            ),
            AddPredicate(
                definition=SchemaPredicateDefinition(
                    name="available",
                    description="The source explicitly states availability.",
                    subject_types=("certificate",),
                    object_kind="boolean",
                ),
                review=review("audit"),
            ),
        ),
        widen_predicates=(
            WidenPredicate(
                name="work:decision",
                add_subject_types=("review",),
                review=review("review"),
            ),
        ),
        examples=examples,
        unresolved_concepts=(
            UnresolvedConcept(
                concept="Cross-document export identity and task ownership",
                explanation="Do not merge local exports or infer project ownership from tasks.",
                reason="ambiguity",
                example_ids=("planning", "integration"),
            ),
        ),
    )
    reader = apply(env, proposal)
    creations = [
        typed_entity(kind="entity", local_id=key, name=name, entity_type=kind, support=supported(e))
        for key, name, kind, e in (
            ("meeting", "Security Review", "review", meeting),
            ("export", "Required export", "artifact", meeting),
            ("certificate", "Archive signing certificate", "certificate", audit),
            ("atlascope", "Atlascope", "project", planning),
            ("lee", "Lee", "person", planning),
        )
    ]
    changes = (
        *creations,
        AddAssertion(
            kind="assertion",
            local_id="decision",
            subject=LocalEntity(kind="local", local_id="meeting"),
            predicate="work:decision",
            object=StringObject(
                kind="string", value="Require the signed access inventory before approval."
            ),
            interpretation="explicit",
            support=supported(meeting),
        ),
        AddAssertion(
            kind="assertion",
            local_id="requirement",
            subject=LocalEntity(kind="local", local_id="meeting"),
            predicate="requires",
            object=EntityObject(kind="entity", entity=LocalEntity(kind="local", local_id="export")),
            interpretation="explicit",
            support=supported(meeting),
        ),
        AddAssertion(
            kind="assertion",
            local_id="availability",
            subject=LocalEntity(kind="local", local_id="certificate"),
            predicate="available",
            object=BooleanObject(kind="boolean", value=False),
            interpretation="explicit",
            support=supported(audit),
        ),
    )
    ids = record(env, changes, (audit, meeting, planning))
    assert len(reader.entities(env.scope).entries) == 5
    assert reader.contribution(env.scope, ids["availability"]).payload.object.value is False
    assert reader.contribution(env.scope, ids["requirement"]).payload.predicate == "requires"
    with QueryService(env.database, env.service.identity) as query:
        result = query.execute(plan(env, ids["meeting"])).result
        assert result.error is None and result.data.count == 1 and result.data.exact
    with env.database.connection() as conn:
        predicates = {row[0] for row in conn.execute("SELECT predicate FROM assertion")}
        assert predicates == {"work:decision", "requires", "available"}
    assert not reader.entities(env.scope, name="Example").entries


def test_non_atlas_document_local_same_names_keep_distinct_ids_and_no_edges(tmp_path):
    env = setup(tmp_path / "local-identities.sqlite")
    first = capture(env, "north", "The north collection labels its basalt specimen Core.")
    second = capture(env, "south", "The south collection labels its granite specimen Core.")
    value = SchemaProposal(
        corpus_id=env.scope.corpus_id,
        base_revision=revision(env),
        attribution=Attribution(
            owner_id="owner", writer_id="writer", producer="reviewed-fixtures", producer_version="1"
        ),
        rationale="Explicit specimen vocabulary; names are not globally unique.",
        add_entity_types=(
            AddEntityType(
                definition=EntityTypeDefinition(
                    name="specimen", description="An identified sample."
                ),
                review=review("north", "south"),
            ),
        ),
        examples=(first, second),
    )
    reader = apply(env, value)
    ids = record(
        env,
        tuple(
            typed_entity(
                kind="entity",
                local_id=e.example_id,
                name="Core",
                entity_type="specimen",
                support=supported(e),
            )
            for e in (first, second)
        ),
        (first, second),
    )
    assert ids["north"] != ids["south"]
    assert len(reader.entities(env.scope, name="Core").entries) == 2
    with env.database.connection() as conn:
        assert conn.execute("SELECT count(*) FROM assertion").fetchone()[0] == 0
