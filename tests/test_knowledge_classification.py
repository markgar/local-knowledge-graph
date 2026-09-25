from uuid import uuid4

import pytest
from support.private_knowledge import write as private_write
from test_knowledge_enrichment import env as env
from test_knowledge_enrichment import mappings, reader, request, source

from kg.evidence import EvidenceServiceError
from kg.knowledge import KnowledgeService
from kg.knowledge._write_models import (
    AddAssertion,
    AddClassification,
    CreateEntity,
    LocalClassificationRef,
    LocalEntity,
    LocalSelectionRef,
    SelectClassification,
)
from kg.models.foundation import (
    StoredClassificationRef,
    StoredEntity,
    StoredSelectionRef,
    StringObject,
    WithdrawClassification,
)

pytestmark = pytest.mark.service


def classified(support, name="Export"):
    return (
        CreateEntity(kind="entity", local_id="e", name=name, support=support),
        AddClassification(
            kind="classification",
            local_id="c",
            entity=LocalEntity(kind="local", local_id="e"),
            entity_type="project",
            interpretation="explicit",
            support=support,
        ),
        SelectClassification(
            kind="classification_selection",
            local_id="s",
            entity=LocalEntity(kind="local", local_id="e"),
            claim=LocalClassificationRef(kind="local", local_id="c"),
            expected_selection_id=None,
            reviewed_candidates_digest=None,
            reviewed_claim_ids=(),
            review_coverage="complete",
            accept_incomplete_review=False,
            rationale="Source classification.",
        ),
        AddAssertion(
            kind="assertion",
            local_id="a",
            subject=LocalEntity(kind="local", local_id="e"),
            predicate="work:decision",
            object=StringObject(kind="string", value="Ship"),
            interpretation="explicit",
            support=support,
            subject_classification=LocalSelectionRef(kind="local", local_id="s"),
        ),
    )


def selection(service, scope, entity_id, claim_id):
    review = service.classification_review(scope, entity_id)
    return SelectClassification(
        kind="classification_selection",
        local_id=str(uuid4()),
        entity=StoredEntity(kind="stored", entity_id=entity_id),
        claim=StoredClassificationRef(kind="stored", contribution_id=claim_id)
        if claim_id
        else None,
        expected_selection_id=review.selection_id,
        reviewed_candidates_digest=review.reviewed_candidates_digest,
        reviewed_claim_ids=review.reviewed_claim_ids,
        review_coverage=review.review_coverage,
        accept_incomplete_review=False,
        rationale="Deliberately choosing the supported claim.",
    )


def test_grounded_unresolved_identity_is_discoverable_without_edges(env):
    support, dependency = source(env)
    ids = mappings(
        private_write(
            env.service,
            request(
                env,
                (CreateEntity(kind="entity", local_id="e", name="the export", support=support),),
                (dependency,),
            ),
        )
    )
    service = KnowledgeService(env.database, env.service.identity)
    value = service.entity(env.scope, ids["e"])
    assert value.is_current and value.entity_type is None
    assert value.classification.status == "unresolved"
    assert len(service.entities(env.scope, name="the export").entries) == 1
    assert len(service.contributions(env.scope, ids["e"]).entries) == 1


def test_compound_explicit_classification_and_non_resurrection(env):
    support, dependency = source(env)
    submitted = request(env, classified(support), (dependency,))
    ids = mappings(private_write(env.service, submitted))
    service = KnowledgeService(env.database, env.service.identity)
    original = service.contribution(env.scope, ids["a"])
    assert original.classification_witnesses[0].selection_id == ids["s"]
    assert service.entity(env.scope, ids["e"]).entity_type == "project"
    clear = selection(service, env.scope, ids["e"], None)
    mappings(private_write(env.service, request(env, (clear,))))
    assert service.entity(env.scope, ids["e"]).is_current
    assert service.entity(env.scope, ids["e"]).entity_type is None
    with pytest.raises(EvidenceServiceError):
        service.contribution(env.scope, ids["a"])
    choose = selection(service, env.scope, ids["e"], ids["c"])
    mappings(private_write(env.service, request(env, (choose,))))
    history = service.contribution(env.scope, ids["a"], mode="history")
    assert history.eligibility == "classification_changed"
    assert history.classification_witnesses == original.classification_witnesses
    assert mappings(private_write(env.service, submitted)) == ids
    assert not service.contribution(env.scope, ids["a"], mode="history").is_current
    events = service.classification_history(env.scope, ids["e"])
    assert len(events.entries) == 4
    assert all("sequence" not in item.model_dump() for item in events.entries)


def test_stale_selection_is_atomic(env):
    support, dependency = source(env)
    ids = mappings(private_write(env.service, request(env, classified(support), (dependency,))))
    service = KnowledgeService(env.database, env.service.identity)
    stale = selection(service, env.scope, ids["e"], None)
    mappings(private_write(env.service, request(env, (stale,))))
    outcome = private_write(env.service, request(env, (stale,)))
    assert outcome.error.code == "state_conflict"
    assert len(service.classification_history(env.scope, ids["e"]).entries) == 3


def test_claim_withdrawal_preserves_identity_and_receipts(env):
    support, dependency = source(env)
    ids = mappings(private_write(env.service, request(env, classified(support), (dependency,))))
    service = KnowledgeService(env.database, env.service.identity)
    operation = request(
        env,
        (
            CreateEntity(
                kind="entity",
                local_id="unused",
                name="Unused",
                support=support,
            ),
        ),
        (dependency,),
    ).model_copy(
        update={
            "payload": WithdrawClassification(
                operation="withdraw_classification",
                contribution_id=ids["c"],
            )
        }
    )
    result = private_write(env.service, operation)
    assert result.status == "applied"
    assert private_write(env.service, operation).receipt == result.receipt
    assert service.entity(env.scope, ids["e"]).is_current
    assert service.entity(env.scope, ids["e"]).entity_type is None
    assert (
        service.contribution(env.scope, ids["a"], mode="history").eligibility
        == "classification_withdrawn"
    )
    assert service.contribution(env.scope, ids["c"], mode="history").withdrawal is not None
    attempted = selection(service, env.scope, ids["e"], ids["c"])
    # A withdrawn claim is not in the eligible review and cannot be selected.
    assert private_write(env.service, request(env, (attempted,))).error.code == "invalid_request"


def test_claim_alone_never_selects_and_duplicate_names_stay_distinct(env):
    support, dependency = source(env)
    changes = (
        CreateEntity(kind="entity", local_id="first", name="the export", support=support),
        CreateEntity(kind="entity", local_id="second", name="the export", support=support),
        AddClassification(
            kind="classification",
            local_id="c",
            entity=LocalEntity(kind="local", local_id="first"),
            entity_type="project",
            interpretation="inferred",
            support=support,
        ),
    )
    ids = mappings(private_write(env.service, request(env, changes, (dependency,))))
    service = KnowledgeService(env.database, env.service.identity)
    entities = service.entities(env.scope, name="the export").entries
    assert len(entities) == 2 and all(e.entity_type is None for e in entities)
    assert ids["first"] != ids["second"]
    review = service.classification_review(env.scope, ids["first"])
    assert len(review.claims) == 1 and review.selected is None


def test_decision_selection_captures_original_classification(env):
    support, dependency = source(env)
    ids = mappings(private_write(env.service, request(env, classified(support), (dependency,))))
    with reader(env) as (adapter, _, _):
        page = adapter.select_decisions(ids["e"]).read()
        assert len(page.items) == 1
        assert page.items[0].dependencies.subject_classification.selection_id == ids["s"]
        adapter.revalidate_member(page.items[0])
    service = KnowledgeService(env.database, env.service.identity)
    mappings(
        private_write(env.service, request(env, (selection(service, env.scope, ids["e"], None),)))
    )
    with reader(env) as (adapter, _, _):
        assert not adapter.select_decisions(ids["e"]).read().items


def test_explicit_subset_clear_and_same_type_claim_replacement(env):
    support, dependency = source(env)
    ids = mappings(private_write(env.service, request(env, classified(support), (dependency,))))
    service = KnowledgeService(env.database, env.service.identity)
    other = AddClassification(
        kind="classification",
        local_id="other",
        entity=StoredEntity(kind="stored", entity_id=ids["e"]),
        entity_type="project",
        interpretation="explicit",
        support=support,
    )
    cid = mappings(private_write(env.service, request(env, (other,), (dependency,))))["other"]
    reviewed = service.classification_review(env.scope, ids["e"], claim_ids=(cid,))
    choose = SelectClassification(
        kind="classification_selection",
        local_id="selection",
        entity=StoredEntity(kind="stored", entity_id=ids["e"]),
        claim=StoredClassificationRef(kind="stored", contribution_id=cid),
        expected_selection_id=reviewed.selection_id,
        reviewed_candidates_digest=reviewed.reviewed_candidates_digest,
        reviewed_claim_ids=reviewed.reviewed_claim_ids,
        review_coverage="selected_subset",
        accept_incomplete_review=True,
        rationale="Deliberate partial review of replacement support.",
    )
    selected = mappings(private_write(env.service, request(env, (choose,))))["selection"]
    assert service.entity(env.scope, ids["e"]).entity_type == "project"
    assert (
        service.contribution(env.scope, ids["a"], mode="history").eligibility
        == "classification_changed"
    )
    fresh = AddAssertion(
        kind="assertion",
        local_id="fresh",
        subject=StoredEntity(kind="stored", entity_id=ids["e"]),
        predicate="work:decision",
        object=StringObject(kind="string", value="Ship"),
        interpretation="explicit",
        support=support,
        subject_classification=StoredSelectionRef(kind="stored", event_id=selected),
    )
    new = mappings(private_write(env.service, request(env, (fresh,), (dependency,))))["fresh"]
    assert service.contribution(env.scope, new).is_current


def test_private_alternatives_protect_rationale_not_public_selected_type(env):
    support, dep = source(env)
    private, private_dep = source(env, external="private", namespace="email")
    ids = mappings(private_write(env.service, request(env, classified(support), (dep,))))
    service = KnowledgeService(env.database, env.service.identity)
    other = AddClassification(
        kind="classification",
        local_id="private",
        entity=StoredEntity(kind="stored", entity_id=ids["e"]),
        entity_type="person",
        interpretation="inferred",
        support=private,
    )
    cid = mappings(private_write(env.service, request(env, (other,), (private_dep,))))["private"]
    private_selection = selection(service, env.scope, ids["e"], cid)
    mappings(private_write(env.service, request(env, (private_selection,))))
    public_selection = selection(service, env.scope, ids["e"], ids["c"]).model_copy(
        update={"rationale": "Rejected the private alternative after reading its private support."},
    )
    saved_request = request(env, (public_selection,))
    public_event = mappings(private_write(env.service, saved_request))[public_selection.local_id]
    narrow = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(
                update={"namespaces": ("markdown",)},
            )
        }
    )
    assert service.entity(narrow, ids["e"]).entity_type == "project"
    review = service.classification_review(narrow, ids["e"])
    assert not review.conflicting_types and review.reviewed_claim_ids == (ids["c"],)
    history = service.classification_history(narrow, ids["e"])
    assert public_event not in {e.event_id for e in history.entries}
    assert "private alternative" not in history.model_dump_json()
    with pytest.raises(EvidenceServiceError, match="not_found"):
        service.classification_history(narrow, ids["e"], after_event_id=public_event)
    replay = private_write(env.service, saved_request.model_copy(update={"scope": narrow}))
    assert replay.error.code == "not_found"
    # Replaying an unchanged selection also retains newly reviewed private dependencies.
    unchanged = request(env, (selection(service, env.scope, ids["e"], ids["c"]),))
    assert private_write(env.service, unchanged).status == "unchanged"
    assert (
        private_write(env.service, unchanged.model_copy(update={"scope": narrow})).error.code
        == "not_found"
    )


def test_complete_review_saturation_has_explicit_bounded_owner_recovery(env):
    support, dep = source(env)
    ids = mappings(private_write(env.service, request(env, classified(support), (dep,))))
    service = KnowledgeService(env.database, env.service.identity)
    for batch in range(2):
        claims = tuple(
            AddClassification(
                kind="classification",
                local_id=f"claim-{batch}-{n}",
                entity=StoredEntity(kind="stored", entity_id=ids["e"]),
                entity_type="person",
                interpretation="inferred",
                support=support,
            )
            for n in range(100)
        )
        mappings(private_write(env.service, request(env, claims, (dep,))))
    with pytest.raises(EvidenceServiceError, match="budget_exceeded"):
        service.classification_review(env.scope, ids["e"])
    reviewed = service.classification_review(env.scope, ids["e"], claim_ids=())
    clear = SelectClassification(
        kind="classification_selection",
        local_id="clear",
        entity=StoredEntity(kind="stored", entity_id=ids["e"]),
        claim=None,
        expected_selection_id=reviewed.selection_id,
        reviewed_candidates_digest=reviewed.reviewed_candidates_digest,
        reviewed_claim_ids=(),
        review_coverage="selected_subset",
        accept_incomplete_review=True,
        rationale="Deliberately clear without reviewing all alternatives.",
    )
    mappings(private_write(env.service, request(env, (clear,))))
    assert service.entity(env.scope, ids["e"]).classification.status == "unresolved"


def test_other_writer_may_claim_but_only_original_owner_writer_selects(env):
    from kg.evidence import EvidenceService
    from kg.models.evidence import KnowledgeWriterBinding, LocalIdentity, PolicyGrant

    policy = env.policy.model_copy(
        update={
            "grants": env.policy.grants
            + tuple(
                PolicyGrant(principal_id="collaborator", namespace="markdown", grant=grant)
                for grant in ("read", "write_knowledge")
            ),
            "knowledge_bindings": env.policy.knowledge_bindings
            + (
                KnowledgeWriterBinding(
                    namespace="markdown",
                    principal_id="collaborator",
                    owner_id="other-owner",
                    writer_id="other-writer",
                ),
            ),
        }
    )
    updated = env.admin.replace_policy(policy, env.scope.access.policy_version)
    env.scope = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(
                update={"policy_version": updated.policy_version},
            )
        }
    )
    support, dep = source(env)
    ids = mappings(private_write(env.service, request(env, classified(support), (dep,))))
    collaborator = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(
                update={
                    "principal_id": "collaborator",
                    "namespaces": ("markdown",),
                    "grants": ("read", "write_knowledge"),
                }
            )
        }
    )
    author = request(
        env,
        (
            AddClassification(
                kind="classification",
                local_id="other",
                entity=StoredEntity(kind="stored", entity_id=ids["e"]),
                entity_type="person",
                interpretation="inferred",
                support=support,
            ),
        ),
        (dep,),
    )
    author = author.model_copy(
        update={
            "scope": collaborator,
            "attribution": author.attribution.model_copy(
                update={
                    "owner_id": "other-owner",
                    "writer_id": "other-writer",
                }
            ),
        }
    )
    other_service = EvidenceService(env.database, LocalIdentity(principal_id="collaborator"))
    claim_id = mappings(private_write(other_service, author))["other"]
    other_reader = KnowledgeService(env.database, other_service.identity)
    chosen = selection(other_reader, collaborator, ids["e"], claim_id)
    forbidden = request(env, (chosen,)).model_copy(
        update={
            "scope": collaborator,
            "attribution": author.attribution,
        }
    )
    assert private_write(other_service, forbidden).error.code == "forbidden"
    original_reader = KnowledgeService(env.database, env.service.identity)
    mappings(
        private_write(
            env.service, request(env, (selection(original_reader, env.scope, ids["e"], claim_id),))
        )
    )
    assert original_reader.entity(env.scope, ids["e"]).entity_type == "person"


def test_seed_deduplicated_local_alias_requires_existing_selection_review(env):
    from support.classification import typed_entity

    from kg.models.foundation import SeedSupport

    seed = SeedSupport(
        kind="seed",
        source_namespace="markdown",
        seed_set_id="explicit",
        seed_key="identity",
    )
    changes = typed_entity(local_id="e", name="Seeded thing", entity_type="project", support=seed)
    first = request(env, changes)
    saved = private_write(env.service, first)
    ids = mappings(saved)
    assert private_write(env.service, first).receipt == saved.receipt
    assert (
        private_write(env.service, first.model_copy(update={"retry_key": "fresh"})).error.code
        == "state_conflict"
    )
    reader = KnowledgeService(env.database, env.service.identity)
    reviewed = reader.classification_review(env.scope, ids["e"])
    selected = changes[-1].model_copy(
        update={
            "expected_selection_id": reviewed.selection_id,
            "reviewed_candidates_digest": reviewed.reviewed_candidates_digest,
            "reviewed_claim_ids": reviewed.reviewed_claim_ids,
        }
    )
    repeated = private_write(env.service, request(env, (*changes[:-1], selected)))
    assert repeated.status == "unchanged"
    assert mappings(repeated) == ids


def test_classification_source_aba_preserves_identity_without_resurrection(env):
    from support.evidence import put, receipt

    support, dep = source(env)
    classification_support, classification_dep = source(env, "classification")
    changes = list(classified(support))
    changes[1] = changes[1].model_copy(update={"support": classification_support})
    ids = mappings(
        private_write(env.service, request(env, tuple(changes), (dep, classification_dep)))
    )
    service = KnowledgeService(env.database, env.service.identity)
    before = service.contribution(env.scope, ids["a"])
    changed = receipt(
        private_write(
            env.service,
            put(
                env.scope,
                external="classification",
                state=classification_dep.state_version,
                title="Changed",
            ),
        )
    )
    assert service.entity(env.scope, ids["e"]).is_current
    assert service.entity(env.scope, ids["e"]).entity_type is None
    stale = service.contribution(env.scope, ids["a"], mode="history")
    assert stale.eligibility == "classification_stale"
    assert stale.classification_witnesses == before.classification_witnesses
    restored = receipt(
        private_write(
            env.service,
            put(
                env.scope,
                external="classification",
                state=changed.processing.state_version,
                title="Title",
            ),
        )
    )
    assert restored.processing.state_version != classification_dep.state_version
    with pytest.raises(EvidenceServiceError, match="not_found"):
        service.contribution(env.scope, ids["a"])
    with reader(env) as (adapter, _, _):
        assert not adapter.select_decisions(ids["e"]).read().items


def test_concurrent_selection_writers_have_one_cas_winner(env):
    from concurrent.futures import ThreadPoolExecutor

    support, dep = source(env)
    ids = mappings(private_write(env.service, request(env, classified(support), (dep,))))
    other = mappings(
        private_write(
            env.service,
            request(
                env,
                (
                    AddClassification(
                        kind="classification",
                        local_id="other",
                        entity=StoredEntity(kind="stored", entity_id=ids["e"]),
                        entity_type="person",
                        interpretation="inferred",
                        support=support,
                    ),
                ),
                (dep,),
            ),
        )
    )["other"]
    service = KnowledgeService(env.database, env.service.identity)
    requests = tuple(
        request(env, (selection(service, env.scope, ids["e"], claim_id),))
        for claim_id in (other, None)
    )
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(lambda value: private_write(env.service, value), requests))
    assert sorted(result.status for result in results) == ["applied", "conflict"]
    failed = next(result for result in results if result.error is not None)
    assert failed.error.code == "state_conflict"
    assert len(service.classification_history(env.scope, ids["e"]).entries) == 3


def test_claim_subset_does_not_reveal_hidden_nonclassification_ids(env):
    from kg.knowledge._write_models import AddAlias

    support, dep = source(env)
    private, private_dep = source(env, "private", "email")
    ids = mappings(private_write(env.service, request(env, classified(support), (dep,))))
    alias_id = mappings(
        private_write(
            env.service,
            request(
                env,
                (
                    AddAlias(
                        kind="alias",
                        local_id="private-alias",
                        entity=StoredEntity(kind="stored", entity_id=ids["e"]),
                        alias="Private name",
                        support=private,
                    ),
                ),
                (private_dep,),
            ),
        )
    )["private-alias"]
    narrow = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(
                update={"namespaces": ("markdown",)},
            )
        }
    )
    service = KnowledgeService(env.database, env.service.identity)
    for claim_id in (alias_id, "nonexistent-claim"):
        with pytest.raises(EvidenceServiceError, match="not_found"):
            service.classification_review(narrow, ids["e"], claim_ids=(claim_id,))
