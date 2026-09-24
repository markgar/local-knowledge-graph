"""Actual canonical schema evolution, authorization and historical knowledge behavior."""

import multiprocessing
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Barrier

import pytest
from pydantic import ValidationError
from support.classification import typed_entity
from support.evidence import environment, put, receipt
from support.knowledge import preset, revision
from support.query_knowledge import setup, write

from kg.evidence import EvidenceDatabase, EvidenceServiceError
from kg.evidence._sql import AccountedConnection
from kg.knowledge import KnowledgeAdministration, KnowledgeService
from kg.knowledge._registry import definition_json, proposal_digest
from kg.models.evidence import LocalAdminAuthority
from kg.models.foundation import (
    AddAssertion,
    Attribution,
    BooleanObject,
    ChangeSet,
    EntityObject,
    LocalEntity,
    SourceSupport,
    StoredEntity,
    StringObject,
    WithdrawAssertion,
    WriteRequest,
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
    WidenPredicate,
)


@pytest.fixture
def env(tmp_path):
    env = setup(tmp_path / "schema.sqlite")
    doc = receipt(
        env.service.write(
            put(
                env.scope,
                external="certificate",
                text="Project Atlas requires the archive signing certificate. "
                "The archive signing certificate is unavailable. "
                "The review decided: require the signed access inventory before approval.",
            )
        )
    )
    anchor = env.service.anchors(env.scope, doc.document_id, doc.processing.state_version).entries[
        0
    ]
    env.example = SchemaExample(
        example_id="source",
        reference=anchor.reference,
        state_version=doc.processing.state_version,
        explanation="Explicit certificate, availability and review-decision context.",
    )
    env.knowledge = KnowledgeService(env.database, env.service.identity)
    env.schema_admin = KnowledgeAdministration(
        env.database,
        LocalAdminAuthority(principal_id="principal"),
    )
    return env


def proposal(env, name="certificate"):
    review = TermReview(
        candidates=(
            ConsideredTerm(
                term=TermRef(kind="entity_type", name="project"),
                assessment="The certificate is not a project.",
            ),
        ),
        reuse_assessment="Existing project/person types would distort the source.",
        extension_rationale="The exact source identifies a certificate.",
        defer_assessment="No automatic facts or identities follow from this proposal.",
        example_ids=("source",),
    )
    return SchemaProposal(
        corpus_id=env.scope.corpus_id,
        base_revision=revision(env),
        attribution=Attribution(
            owner_id="owner",
            writer_id="writer",
            producer="schema-tests",
            producer_version="1",
        ),
        rationale="Admit explicit signing certificates.",
        add_entity_types=(
            AddEntityType(
                definition=EntityTypeDefinition(
                    name=name, description="An identified certificate."
                ),
                review=review,
            ),
        ),
        examples=(env.example,),
    )


def request(env, value, key="schema-key"):
    return SchemaApplyRequest(
        request_id="schema-request",
        retry_key=key,
        scope=env.scope,
        proposal=value,
        approved_proposal_digest=proposal_digest(value),
        approval=SchemaApproval(
            human_reviewed=True,
            rationale="Reviewed exact terms, reuse and metadata publication.",
        ),
    )


def counts(env):
    with env.database.connection() as conn:
        return tuple(
            conn.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
            for name in (
                "knowledge_schema_revision",
                "knowledge_schema_change",
                "knowledge_schema_receipt",
                "entity",
                "contribution",
            )
        )


@pytest.mark.service
def test_validate_apply_preserve_old_facts_and_record_new_property_and_relation(env):
    ids = write(
        env,
        (
            typed_entity(
                kind="entity",
                local_id="project",
                name="Atlas",
                entity_type="project",
                support=env.support,
            ),
        ),
    )
    old_id = ids["project"]
    before = env.knowledge.entity(env.scope, old_id)
    old_revision = revision(env)
    value = proposal(env)
    review = value.add_entity_types[0].review
    value = value.model_copy(
        update={
            "add_predicates": (
                AddPredicate(
                    definition=SchemaPredicateDefinition(
                        name="available",
                        description="Whether the subject is available.",
                        subject_types=("certificate",),
                        object_kind="boolean",
                    ),
                    review=review,
                ),
                AddPredicate(
                    definition=SchemaPredicateDefinition(
                        name="requires",
                        description="The subject explicitly requires the object.",
                        subject_types=("project",),
                        object_kind="entity",
                        object_types=("certificate",),
                    ),
                    review=review,
                ),
            )
        }
    )
    before_counts = counts(env)
    checked = env.knowledge.validate_schema(env.scope, value)
    assert checked.semantic_review_required
    assert counts(env) == before_counts
    applied = env.schema_admin.apply_schema(request(env, value))
    assert applied.status == "applied", applied
    assert counts(env)[3:] == before_counts[3:]
    assert env.knowledge.entity(env.scope, old_id) == before
    assert (
        env.knowledge.schema(env.scope, revision_id=old_revision.revision_id).revision
        == old_revision
    )
    support = SourceSupport(kind="source", evidence=(env.example.reference,))
    ids = write(
        env,
        (
            typed_entity(
                kind="entity",
                local_id="cert",
                name="archive signing certificate",
                entity_type="certificate",
                support=support,
            ),
            AddAssertion(
                kind="assertion",
                local_id="available",
                predicate="available",
                subject=LocalEntity(kind="local", local_id="cert"),
                object=BooleanObject(kind="boolean", value=False),
                interpretation="explicit",
                support=support,
            ),
            AddAssertion(
                kind="assertion",
                local_id="required",
                predicate="requires",
                subject=StoredEntity(kind="stored", entity_id=old_id),
                object=EntityObject(
                    kind="entity", entity=LocalEntity(kind="local", local_id="cert")
                ),
                interpretation="explicit",
                support=support,
            ),
        ),
        dependencies=value.dependencies(),
    )
    assert env.knowledge.contribution(env.scope, ids["available"]).payload.object.value is False
    assert (
        env.knowledge.contribution(env.scope, ids["required"]).payload.object.entity.entity_id
        == (ids["cert"])
    )
    assert env.knowledge.schema(env.scope).head == applied.receipt.revision
    page = env.knowledge.schema_history(env.scope, limit=1)
    assert page.has_more and page.next_after_sequence == 1
    assert env.knowledge.schema_history(env.scope, after_sequence=1).entries[0].sequence == 2


@pytest.mark.service
def test_endpoint_union_and_authored_revision_survive_withdrawal_and_query(env):
    project = write(
        env,
        (
            typed_entity(
                kind="entity",
                local_id="p",
                name="Atlas",
                entity_type="project",
                support=env.support,
            ),
        ),
    )["p"]
    decision = write(
        env,
        (
            AddAssertion(
                kind="assertion",
                local_id="d",
                predicate="work:decision",
                subject=StoredEntity(kind="stored", entity_id=project),
                object=StringObject(kind="string", value="Ship"),
                interpretation="explicit",
                support=env.support,
            ),
        ),
    )["d"]
    original = env.knowledge.contribution(env.scope, decision)
    value = proposal(env, "review")
    value = value.model_copy(
        update={
            "widen_predicates": (
                WidenPredicate(
                    name="work:decision",
                    add_subject_types=("review",),
                    review=value.add_entity_types[0].review,
                ),
            )
        }
    )
    checked = env.knowledge.validate_schema(env.scope, value)
    assert checked.widenings[0].new_endpoint_combinations == 1
    assert checked.widenings[0].previous_subject_types == ("project",)
    assert env.schema_admin.apply_schema(request(env, value)).status == "applied"
    assert env.knowledge.contribution(env.scope, decision) == original
    withdrawn = env.service.write(
        WriteRequest(
            contract_version="foundation/1",
            request_id="withdraw",
            retry_key="withdraw",
            scope=env.scope,
            attribution=original.attribution,
            payload=WithdrawAssertion(operation="withdraw_assertion", contribution_id=decision),
        )
    )
    assert withdrawn.status == "applied", withdrawn
    history = env.knowledge.contribution(env.scope, decision, mode="history")
    assert history.schema_version == original.schema_version and not history.is_current


@pytest.mark.service
def test_exact_head_fresh_write_and_successful_receipt_replay(env):
    authored = revision(env)
    fresh = WriteRequest(
        contract_version="foundation/1",
        request_id="facts",
        retry_key="facts",
        scope=env.scope,
        attribution=proposal(env).attribution,
        payload=ChangeSet(
            operation="enrich",
            expected_schema_revision=authored,
            dependencies=(env.dependency,),
            changes=typed_entity(
                kind="entity",
                local_id="p",
                name="Atlas",
                entity_type="project",
                support=env.support,
            ),
        ),
    )
    saved = env.service.write(fresh)
    assert saved.status == "applied"
    assert env.schema_admin.apply_schema(request(env, proposal(env))).status == "applied"
    assert env.service.write(fresh).receipt == saved.receipt
    stale = env.service.write(fresh.model_copy(update={"retry_key": "new"}))
    assert stale.error.code == "state_conflict"
    missing = fresh.model_copy(
        update={
            "retry_key": "missing",
            "payload": fresh.payload.model_copy(update={"expected_schema_revision": None}),
        }
    )
    assert env.service.write(missing).error.code == "invalid_request"


@pytest.mark.service
def test_apply_replay_changed_key_digest_and_source_history(env):
    value = proposal(env)
    prepared = request(env, value)
    applied = env.schema_admin.apply_schema(prepared)
    assert applied.status == "applied"
    receipt(
        env.service.write(
            put(
                env.scope,
                external="certificate",
                state=env.example.state_version,
                text="Updated exact source; old schema approval remains historical.",
            )
        )
    )
    reopened = KnowledgeAdministration(
        EvidenceDatabase(env.database.path),
        LocalAdminAuthority(principal_id="principal"),
    )
    assert reopened.apply_schema(prepared).receipt == applied.receipt
    changed = prepared.model_copy(
        update={
            "approval": SchemaApproval(
                human_reviewed=True, rationale="Changed approval rationale."
            ),
        }
    )
    assert reopened.apply_schema(changed).error.code == "retry_conflict"
    assert reopened.apply_schema(prepared.model_copy(update={"retry_key": "new"})).error.code == (
        "state_conflict"
    )
    assert env.knowledge.schema_change(env.scope, applied.receipt.revision.revision_id).proposal


@pytest.mark.service
@pytest.mark.parametrize("mutation", ["digest", "candidate", "unknown-type", "duplicate", "state"])
def test_invalid_units_are_atomic(env, mutation):
    value = proposal(env)
    prepared = request(env, value)
    if mutation == "digest":
        prepared = prepared.model_copy(update={"approved_proposal_digest": "0" * 64})
    elif mutation == "candidate":
        review = value.add_entity_types[0].review.model_copy(
            update={
                "candidates": (
                    ConsideredTerm(
                        term=TermRef(kind="entity_type", name="missing"), assessment="Unknown."
                    ),
                )
            }
        )
        value = value.model_copy(
            update={
                "add_entity_types": (
                    value.add_entity_types[0].model_copy(update={"review": review}),
                )
            }
        )
    elif mutation == "unknown-type":
        value = value.model_copy(
            update={
                "widen_predicates": (
                    WidenPredicate(
                        name="work:owns",
                        add_object_types=("missing",),
                        review=value.add_entity_types[0].review,
                    ),
                )
            }
        )
    elif mutation == "duplicate":
        value = value.model_copy(
            update={
                "add_entity_types": (
                    value.add_entity_types[0],
                    value.add_entity_types[0],
                )
            }
        )
    else:
        value = value.model_copy(
            update={"examples": (env.example.model_copy(update={"state_version": "missing"}),)}
        )
    before = counts(env)
    try:
        outcome = env.schema_admin.apply_schema(
            prepared
            if mutation == "digest"
            else prepared.model_copy(
                update={
                    "proposal": value,
                    "approved_proposal_digest": proposal_digest(value),
                }
            ),
        )
        assert outcome.error and outcome.receipt is None
        assert outcome.commit_outcome in {"confirmed_rolled_back", "not_attempted"}
    except EvidenceServiceError as error:
        assert error.failure.code == "invalid_request"
    assert counts(env) == before


@pytest.mark.process
def test_concurrent_proposals_and_identical_retry_have_single_commit(env):
    barrier = Barrier(2)
    value = proposal(env)
    competing = proposal(env, "artifact")

    def run(prepared):
        barrier.wait()
        admin = KnowledgeAdministration(
            EvidenceDatabase(env.database.path),
            LocalAdminAuthority(principal_id="principal"),
        )
        return admin.apply_schema(prepared)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (request(env, value, "a"), request(env, competing, "b"))))
    assert sorted(r.status for r in results) == ["applied", "conflict"]
    assert counts(env)[:3] == (2, 2, 1)
    value = proposal(env, "resource")
    prepared = request(env, value, "same")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, (prepared, prepared)))
    assert results[0].receipt == results[1].receipt
    assert counts(env)[:3] == (3, 3, 2)


@pytest.mark.service
@pytest.mark.parametrize(
    "table",
    [
        "knowledge_schema_revision",
        "knowledge_schema_change",
        "knowledge_schema_receipt",
    ],
)
def test_injected_storage_failure_rolls_back_revision_head_receipt(env, monkeypatch, table):
    execute = AccountedConnection.execute

    def fail(connection, sql, parameters=()):
        result = execute(connection, sql, parameters)
        if sql.startswith("INSERT INTO " + table):
            import sqlite3

            raise sqlite3.OperationalError("injected")
        return result

    before = counts(env)
    prepared = request(env, proposal(env))
    with monkeypatch.context() as patch:
        patch.setattr(AccountedConnection, "execute", fail)
        outcome = env.schema_admin.apply_schema(prepared)
    assert outcome.status == "failed" and outcome.error.code == "internal_error"
    assert outcome.commit_outcome == "confirmed_rolled_back"
    assert counts(env) == before


@pytest.mark.service
def test_unconfigured_bootstrap_legacy_rejection_and_preset_detail_policy(tmp_path):
    env = environment(tmp_path / "empty.sqlite")
    reader = KnowledgeService(env.database, env.service.identity)
    assert reader.schema(env.scope).status == "unconfigured"
    assert reader.capabilities(env.scope).change_kinds == ()
    doc = receipt(env.service.write(put(env.scope)))
    assert env.service.document(env.scope, doc.document_id)
    admin = KnowledgeAdministration(env.database, env.admin.authority)
    value = preset()
    installed = admin.register_knowledge_schema(value)
    assert admin.register_knowledge_schema(value).revision == installed.revision
    with pytest.raises(EvidenceServiceError, match="not_found"):
        reader.schema_change(env.scope, installed.revision.revision_id)
    from support.knowledge import schema

    with pytest.raises(EvidenceServiceError, match="invalid_request"):
        admin.register_knowledge_schema(schema())
    with pytest.raises(EvidenceServiceError, match="invalid_request"):
        KnowledgeAdministration(env.database, env.service.identity)
    assert definition_json(reader.schema(env.scope).definition) == definition_json(value.definition)


@pytest.mark.service
def test_wrong_admin_identity_and_forged_approval_cannot_apply(env):
    value = proposal(env)
    wrong = KnowledgeAdministration(env.database, LocalAdminAuthority(principal_id="untrusted"))
    before = counts(env)
    result = wrong.apply_schema(request(env, value))
    assert result.error.code == "forbidden"
    with pytest.raises(ValidationError):
        SchemaApproval(human_reviewed=False, rationale="Not reviewed.")
    forged = request(env, value).model_copy(
        update={
            "approval": {
                "human_reviewed": False,
                "rationale": "Forged.",
            }
        }
    )
    with pytest.raises(EvidenceServiceError, match="invalid_request"):
        env.schema_admin.apply_schema(forged)
    assert counts(env) == before


@pytest.mark.service
def test_source_and_policy_changes_block_new_validation_and_apply(env):
    prepared = request(env, proposal(env))
    receipt(
        env.service.write(
            put(
                env.scope,
                external="certificate",
                state=env.example.state_version,
                text="New source.",
            )
        )
    )
    assert env.schema_admin.apply_schema(prepared).error.code == "state_conflict"
    policy = env.policy.model_copy(
        update={
            "grants": tuple(
                g
                for g in env.policy.grants
                if not (g.namespace == "markdown" and g.grant == "read")
            )
        }
    )
    changed = env.admin.replace_policy(policy, env.scope.access.policy_version)
    scope = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(
                update={
                    "policy_version": changed.policy_version,
                }
            )
        }
    )
    assert env.schema_admin.apply_schema(
        prepared.model_copy(update={"scope": scope})
    ).error.code == ("forbidden")


def _apply_process(path, serialized, barrier, queue):
    admin = KnowledgeAdministration(
        EvidenceDatabase(path),
        LocalAdminAuthority(principal_id="principal"),
    )
    prepared = SchemaApplyRequest.model_validate_json(serialized)
    barrier.wait()
    queue.put(admin.apply_schema(prepared).model_dump_json())


@pytest.mark.process
@pytest.mark.parametrize("same", [False, True])
def test_multiprocess_proposal_apply_and_retry(env, same):
    from kg.models.schema import SchemaApplyOutcome

    first = request(env, proposal(env))
    second = first if same else request(env, proposal(env, "artifact"), "other-key")
    ctx = multiprocessing.get_context("spawn")
    barrier, queue = ctx.Barrier(2), ctx.Queue()
    workers = [
        ctx.Process(
            target=_apply_process,
            args=(env.database.path, value.model_dump_json(), barrier, queue),
        )
        for value in (first, second)
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(30)
        assert worker.exitcode == 0
    outcomes = [SchemaApplyOutcome.model_validate_json(queue.get(timeout=5)) for _ in workers]
    assert sorted(o.status for o in outcomes) == (
        ["applied", "applied"] if same else ["applied", "conflict"]
    )
    if same:
        assert outcomes[0].receipt == outcomes[1].receipt
    assert counts(env)[:3] == (2, 2, 1)


@pytest.mark.service
def test_evidence_backed_null_base_bootstrap_without_facts(env, tmp_path):
    fresh = environment(tmp_path / "new.sqlite")
    source = receipt(fresh.service.write(put(fresh.scope, text="The basalt specimen is porous.")))
    anchor = fresh.service.anchors(
        fresh.scope,
        source.document_id,
        source.processing.state_version,
    ).entries[0]
    value = proposal(env)
    review = value.add_entity_types[0].review.model_copy(
        update={
            "candidates": (),
            "no_existing_candidate_reason": "No vocabulary is configured.",
        }
    )
    value = value.model_copy(
        update={
            "base_revision": None,
            "rationale": "Explicit specimen vocabulary from a bounded supplied source.",
            "add_entity_types": (
                AddEntityType(
                    definition=EntityTypeDefinition(
                        name="specimen", description="An identified specimen."
                    ),
                    review=review,
                ),
            ),
            "examples": (
                SchemaExample(
                    example_id="source",
                    reference=anchor.reference,
                    state_version=source.processing.state_version,
                    explanation="The exact supplied source identifies a specimen.",
                ),
            ),
        }
    )
    reader = KnowledgeService(fresh.database, fresh.service.identity)
    assert reader.validate_schema(fresh.scope, value).base_revision is None
    assert reader.schema(fresh.scope).status == "unconfigured"
    admin = KnowledgeAdministration(
        fresh.database,
        LocalAdminAuthority(principal_id="principal"),
    )
    outcome = admin.apply_schema(request(fresh, value))
    assert outcome.receipt.sequence == 1 and outcome.receipt.previous_revision is None
    assert counts(fresh) == (1, 1, 1, 0, 0)
    assert reader.schema_change(fresh.scope, outcome.receipt.revision.revision_id).proposal == value


@pytest.mark.service
@pytest.mark.parametrize(
    "operation", ["schema", "schema_history", "validate_schema", "schema_change"]
)
@pytest.mark.parametrize("mutation", ["schema", "source", "policy"])
def test_schema_reads_reject_changes_at_release_fence(env, monkeypatch, operation, mutation):
    from kg.knowledge import service as module

    original = request(env, proposal(env))
    installed = env.schema_admin.apply_schema(original).receipt
    value = proposal(env, "artifact")
    pending = request(env, value, "next")
    fence = module.release_fence

    @contextmanager
    def intercept(*args, **kwargs):
        if mutation == "schema":
            assert env.schema_admin.apply_schema(pending).receipt is not None
        elif mutation == "source":
            receipt(
                env.service.write(
                    put(
                        env.scope,
                        external="certificate",
                        state=env.example.state_version,
                        text="Changed",
                    )
                )
            )
        else:
            with env.database.transaction() as connection:
                connection.execute("DELETE FROM policy_grant WHERE grant_name='read'")
        with fence(*args, **kwargs) as guard:
            yield guard

    monkeypatch.setattr(module, "release_fence", intercept)
    extra = {
        "validate_schema": (value,),
        "schema_change": (installed.revision.revision_id,),
    }.get(operation, ())
    with pytest.raises(EvidenceServiceError) as error:
        getattr(env.knowledge, operation)(env.scope, *extra)
    assert error.value.failure.code in {"state_changed", "forbidden"}


@pytest.mark.unit
def test_approval_requires_boolean_not_coercible_literal():
    for value in (False, 1, "true", None):
        with pytest.raises(ValidationError):
            SchemaApproval(human_reviewed=value, rationale="Explicit review.")


@pytest.mark.service
def test_metadata_discovery_is_readable_but_change_and_replay_require_original_scope(env):
    from kg.models.evidence import LocalIdentity, PolicyGrant

    prepared = request(env, proposal(env))
    saved = env.schema_admin.apply_schema(prepared).receipt
    policy = env.policy.model_copy(
        update={
            "grants": (
                *env.policy.grants,
                PolicyGrant(principal_id="limited", namespace="email", grant="read"),
            )
        }
    )
    installed = env.admin.replace_policy(policy, env.scope.access.policy_version)
    scope = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(
                update={
                    "principal_id": "limited",
                    "namespaces": ("email",),
                    "policy_version": installed.policy_version,
                    "grants": ("read",),
                }
            ),
        }
    )
    reader = KnowledgeService(env.database, LocalIdentity(principal_id="limited"))
    assert reader.schema(scope).revision == saved.revision
    assert len(reader.schema_history(scope).entries) == 2
    with pytest.raises(EvidenceServiceError):
        reader.schema_change(scope, saved.revision.revision_id)
    assert env.schema_admin.apply_schema(prepared).error.code == "forbidden"


@pytest.mark.service
def test_lost_commit_acknowledgement_is_unknown_and_exact_retry_recovers(env, monkeypatch):
    import sqlite3

    prepared = request(env, proposal(env))
    commit = AccountedConnection.commit

    def lost(connection):
        commit(connection)
        raise sqlite3.OperationalError("lost commit acknowledgement")

    with monkeypatch.context() as patch:
        patch.setattr(AccountedConnection, "commit", lost)
        outcome = env.schema_admin.apply_schema(prepared)
    assert outcome.status == "uncertain" and outcome.commit_outcome == "unknown"
    assert outcome.receipt is None
    recovered = env.schema_admin.apply_schema(prepared)
    assert recovered.status == "applied" and recovered.receipt.sequence == 2
    assert counts(env)[:3] == (2, 2, 1)


@pytest.mark.service
def test_schema_scratch_exhaustion_rolls_back_without_success_shape(env, monkeypatch):
    from kg._execution_budget import PrivateBudget, PrivateResourceStop

    prepared = request(env, proposal(env))
    before = counts(env)
    reserve = PrivateBudget.reserve_scratch
    pools = []

    def limited(budget, size_bytes, unit):
        pools.append(budget)
        if unit == "general":
            raise PrivateResourceStop()
        return reserve(budget, size_bytes, unit)

    with monkeypatch.context() as patch:
        patch.setattr(PrivateBudget, "reserve_scratch", limited)
        result = env.schema_admin.apply_schema(prepared)
        with pytest.raises(EvidenceServiceError, match="budget_exceeded"):
            env.knowledge.validate_schema(env.scope, prepared.proposal)
    assert result.error.code == "budget_exceeded" and result.receipt is None
    assert counts(env) == before
    assert all(pool._root._scratch == 0 for pool in pools)


@pytest.mark.service
def test_simultaneous_widening_discloses_full_cartesian_product_and_rejects_noops(env):
    value = proposal(env)
    widening = WidenPredicate(
        name="work:owns", add_subject_types=("project",), add_object_types=("person",),
        review=value.add_entity_types[0].review,
    )
    value = value.model_copy(update={"widen_predicates": (widening,)})
    checked = env.knowledge.validate_schema(env.scope, value)
    effect = checked.widenings[0]
    assert effect.new_endpoint_combinations == 3
    assert effect.subject_types == effect.object_types == ("person", "project")
    original = next(p for p in env.knowledge.schema(env.scope).definition.predicates
                    if p.name == "work:owns")
    accepted = next(p for p in checked.definition.predicates if p.name == "work:owns")
    assert original.model_dump(exclude={"subject_types", "object_types"}) == accepted.model_dump(
        exclude={"subject_types", "object_types"},
    )
    before = counts(env)
    for invalid in (
        widening.model_copy(update={"add_subject_types": ("person",)}),
        widening.model_copy(update={"name": "work:decision", "add_subject_types": ("person",)}),
        widening.model_copy(update={"name": "unknown"}),
    ):
        changed = value.model_copy(update={"widen_predicates": (invalid,)})
        assert env.schema_admin.apply_schema(request(env, changed)).error.code == "invalid_request"
        assert counts(env) == before


@pytest.mark.process
def test_schema_commit_invalidates_retained_query_but_fresh_query_keeps_authored_members(env):
    from support.query_knowledge import plan, produce

    from kg.models.query import SupportInspectionRequest
    from kg.query import QueryService

    subject, identifiers = produce(env, 3)
    req = plan(env, subject)
    with QueryService(env.database, env.service.identity) as query:
        old = query.execute(req).result
        assert old.error is None and old.data.count == 3
        assert env.schema_admin.apply_schema(request(env, proposal(env))).receipt is not None
        stale = query.inspect_support(SupportInspectionRequest(
            request_id="old", scope=env.scope, result_set_id=old.result_set_id,
            records_step_id="decisions", budget=req.budget,
        ))
        assert stale.error.code == "state_changed" and not stale.records
        fresh = query.execute(req).result
        assert fresh.error is None and fresh.data.count == 3
        page = query.inspect_support(SupportInspectionRequest(
            request_id="new", scope=env.scope, result_set_id=fresh.result_set_id,
            records_step_id="decisions", budget=req.budget,
        ))
        assert page.error is None and {r.record_id for r in page.records} == identifiers


@pytest.mark.service
@pytest.mark.parametrize("corruption", ["missing", "rewound"])
def test_missing_or_rewound_head_is_corrupt_not_unconfigured_or_writable(env, corruption):
    old = revision(env)
    prepared = request(env, proposal(env))
    assert env.schema_admin.apply_schema(prepared).receipt is not None
    with env.database.transaction() as connection:
        if corruption == "missing":
            connection.execute("DELETE FROM knowledge_schema_head WHERE corpus_id='work'")
        else:
            connection.execute(
                "UPDATE knowledge_schema_head SET revision_id=? WHERE corpus_id='work'",
                (old.revision_id,),
            )
    before = counts(env)
    for read in (env.knowledge.schema, env.knowledge.schema_history):
        with pytest.raises(EvidenceServiceError, match="internal_error"):
            read(env.scope)
    with pytest.raises(EvidenceServiceError, match="internal_error"):
        env.schema_admin.register_knowledge_schema(preset())
    result = env.schema_admin.apply_schema(prepared.model_copy(update={"retry_key": "fresh"}))
    assert result.error.code == "internal_error"
    fresh = env.service.write(WriteRequest(
        contract_version="foundation/1", request_id="corrupt-head", retry_key="corrupt-head",
        scope=env.scope, attribution=prepared.proposal.attribution,
        payload=ChangeSet(
            operation="enrich", expected_schema_revision=old,
            dependencies=(env.dependency,),
            changes=typed_entity(
                kind="entity", local_id="p", name="Atlas", entity_type="project",
                support=env.support,
            ),
        ),
    ))
    assert fresh.error.code == "internal_error"
    assert counts(env) == before
