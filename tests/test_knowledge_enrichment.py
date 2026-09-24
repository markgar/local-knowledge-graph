import time
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from support.evidence import environment, put, receipt
from support.knowledge import preset, revision, schema

from kg._execution_budget import (
    Deadline,
    LocalExecutionMeter,
    PrivateBudget,
    _graph_build_operation,
)
from kg.evidence import EvidenceServiceError
from kg.evidence._read_context import observe, read_context
from kg.knowledge import KnowledgeAdministration, KnowledgeService
from kg.knowledge._reader import KnowledgeReader
from kg.knowledge._selection import EligibleEOF, EntitySelector, SelectionStopped
from kg.models.evidence import KnowledgeWriterBinding, LocalAdminAuthority, PolicyGrant
from kg.models.foundation import (
    AddAlias,
    AddAssertion,
    AddEntitySupport,
    AddIdentifier,
    Attribution,
    ChangeSet,
    ChangeSetReceipt,
    CreateEntity,
    DocumentDependency,
    EntityObject,
    LocalEntity,
    SeedSupport,
    SourceSupport,
    StoredEntity,
    StringObject,
    WriteBatch,
    WriteRequest,
)


@pytest.fixture
def env(tmp_path):
    env = environment(tmp_path / "knowledge.db")
    env.policy = env.policy.model_copy(
        update={
            "grants": env.policy.grants
            + tuple(
                PolicyGrant(principal_id="principal", namespace=ns, grant=grant)
                for ns in ("markdown", "email")
                for grant in ("write_knowledge", "seed")
            ),
            "knowledge_bindings": tuple(
                KnowledgeWriterBinding(
                    namespace=ns,
                    principal_id="principal",
                    owner_id="owner",
                    writer_id="writer",
                )
                for ns in ("markdown", "email")
            ),
        }
    )
    update = env.admin.replace_policy(env.policy, env.scope.access.policy_version)
    env.scope = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(
                update={
                    "policy_version": update.policy_version,
                    "grants": ("read", "write_documents", "write_knowledge", "seed"),
                }
            )
        }
    )
    KnowledgeAdministration(
        env.database,
        LocalAdminAuthority(principal_id="admin"),
    ).register_knowledge_schema(preset(schema()))
    return env


def source(env, external="doc", namespace="markdown"):
    doc = receipt(env.service.write(put(env.scope, external=external, namespace=namespace)))
    anchor = env.service.anchors(env.scope, doc.document_id, doc.processing.state_version).entries[
        0
    ]
    return SourceSupport(kind="source", evidence=(anchor.reference,)), DocumentDependency(
        source_namespace=namespace,
        document_id=doc.document_id,
        revision_id=doc.revision_id,
        state_version=doc.processing.state_version,
    )


def request(env, changes, deps=(), retry=None):
    return WriteRequest(
        contract_version="foundation/1",
        request_id=str(uuid4()),
        retry_key=retry or str(uuid4()),
        scope=env.scope,
        attribution=Attribution(
            owner_id="owner",
            writer_id="writer",
            producer="tests",
            producer_version="1",
        ),
        payload=ChangeSet(
            expected_schema_revision=revision(env), operation="enrich",
            changes=tuple(changes), dependencies=tuple(deps),
        ),
    )


def mappings(result):
    assert result.error is None, result.error
    assert isinstance(result.receipt, ChangeSetReceipt)
    return {m.local_id: m.stored_id for m in result.receipt.mappings}


def entity(support, local="project", name="Project"):
    return CreateEntity(
        kind="entity", local_id=local, name=name, entity_type="project", support=support
    )


def decision(support, target, local="decision", text="Ship"):
    return AddAssertion(
        kind="assertion",
        local_id=local,
        subject=target,
        predicate="work:decision",
        object=StringObject(kind="string", value=text),
        interpretation="explicit",
        support=support,
    )


@contextmanager
def reader(env, max_items=100_000):
    budget = PrivateBudget(Deadline(time.monotonic() + 30))
    meter = LocalExecutionMeter(budget, max_operations=10, max_items=max_items)
    with (
        observe(env.database, env.service.identity, env.scope, budget.deadline, budget) as observer,
        read_context(
            env.database,
            env.service.identity,
            env.scope,
            observer.session_id,
            budget.deadline,
            meter.begin_step("knowledge"),
        ) as context,
    ):
        yield KnowledgeReader(context, env.service.identity), meter, context


@pytest.mark.service
def test_real_atomic_producer_reads_replay_and_cursor(env):
    support, dep = source(env)
    local = LocalEntity(kind="local", local_id="project")
    req = request(
        env,
        (
            decision(support, local),
            AddAlias(kind="alias", local_id="alias", entity=local, alias="P", support=support),
            AddIdentifier(
                kind="identifier",
                local_id="id",
                entity=local,
                scheme="ticket",
                value="P-1",
                support=support,
            ),
            entity(support),
        ),
        (dep,),
    )
    result = env.service.write(req)
    ids = mappings(result)
    assert tuple(m.local_id for m in result.receipt.mappings) == (
        "decision",
        "alias",
        "id",
        "project",
    )
    service = KnowledgeService(env.database, env.service.identity)
    assert service.entity(env.scope, ids["project"]).name == "Project"
    assert service.contribution(env.scope, ids["decision"]).payload.object.value == "Ship"
    assert len(service.entities(env.scope, name="P").entries) == 1
    assert len(service.entities(env.scope, scheme="ticket", identifier="P-1").entries) == 1
    assert len(service.contributions(env.scope, ids["project"]).entries) == 4
    assert env.service.write(req).receipt == result.receipt
    with reader(env) as (adapter, meter, context):
        resolved = adapter.resolve_entity(EntitySelector(name="P")).read()
        assert resolved.items[0].entity_id == ids["project"]
        cursor = adapter.select_decisions(ids["project"])
        page = cursor.read(limit=1)
        assert page.items[0].record.record_id == ids["decision"]
        assert page.items[0].dependencies.subject_witness == resolved.items[0].witness
        assert isinstance(cursor.read().terminal, EligibleEOF)
        assert meter.public_accounting().items_consumed == 2
        adapter.revalidate_member(page.items[0])
    with pytest.raises(EvidenceServiceError):
        cursor.read()


@pytest.mark.service
def test_seed_add_unchanged_is_owned_and_omission_does_not_withdraw(env):
    support = SeedSupport(
        kind="seed",
        source_namespace="markdown",
        seed_set_id="catalog",
        seed_key="p",
    )
    first = request(env, (entity(support),))
    ids = mappings(env.service.write(first))
    repeated = env.service.write(first.model_copy(update={"retry_key": "other"}))
    assert repeated.status == "unchanged"
    assert mappings(repeated) == ids
    service = KnowledgeService(env.database, env.service.identity)
    assert service.entity(env.scope, ids["project"]).is_current
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM contribution").fetchone()[0] == 1
        assert connection.execute("SELECT generation FROM seed_set").fetchone()[0] == 1
    conflict = request(env, (entity(support, name="Changed"),))
    assert env.service.write(conflict).error.code == "state_conflict"


@pytest.mark.service
def test_failed_last_change_rolls_back_entities_seeds_and_key(env):
    support, dep = source(env)
    bad = decision(support, LocalEntity(kind="local", local_id="project")).model_copy(
        update={"interpretation": "inferred"},
    )
    req = request(env, (entity(support), bad), (dep,))
    assert env.service.write(req).error.code == "invalid_request"
    with env.database.connection() as connection:
        for table in ("entity", "contribution", "knowledge_write_response"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    corrected = req.model_copy(
        update={
            "payload": req.payload.model_copy(
                update={
                    "changes": (
                        entity(support),
                        bad.model_copy(update={"interpretation": "explicit"}),
                    ),
                }
            )
        }
    )
    mappings(env.service.write(corrected))


@pytest.mark.service
def test_metadata_rotation_full_support_history_and_replay(env):
    support, dep = source(env)
    other, other_dep = source(env, "other", "email")
    both = SourceSupport(kind="source", evidence=(*support.evidence, *other.evidence))
    req = request(
        env,
        (
            entity(both),
            decision(
                both,
                LocalEntity(kind="local", local_id="project"),
            ),
        ),
        (dep, other_dep),
    )
    result = env.service.write(req)
    ids = mappings(result)
    receipt(env.service.write(put(env.scope, state=dep.state_version, title="changed")))
    service = KnowledgeService(env.database, env.service.identity)
    with pytest.raises(EvidenceServiceError, match="not_found"):
        service.contribution(env.scope, ids["decision"])
    assert not service.contribution(env.scope, ids["decision"], mode="history").is_current
    assert env.service.write(req).receipt == result.receipt
    assert (
        env.service.write(req.model_copy(update={"retry_key": "fresh"})).error.code
        == "state_conflict"
    )


@pytest.mark.service
def test_knowledge_only_grants_and_reports(env):
    support, dep = source(env)
    scope = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(
                update={
                    "grants": ("read", "write_knowledge"),
                }
            )
        }
    )
    req = request(env, (entity(support),), (dep,)).model_copy(update={"scope": scope})
    explained = env.service.write_explained(req)
    ids = mappings(explained.outcome)
    assert explained.report.state == "collected"
    assert env.service.diagnostics.for_request(scope, req.request_id).entries
    service = KnowledgeService(env.database, env.service.identity)
    result = service.entity_explained(scope, ids["project"])
    assert result.report.state == "collected"


@pytest.mark.service
def test_expiry_precedes_changed_digest_and_clock_rollback(env):
    support, dep = source(env)
    at = datetime(2030, 1, 1, tzinfo=UTC)
    env.service._clock = lambda: at
    req = request(env, (entity(support),), (dep,))
    mappings(env.service.write(req))
    changed = req.model_copy(
        update={
            "payload": req.payload.model_copy(
                update={
                    "changes": (entity(support, name="changed"),),
                }
            )
        }
    )
    assert env.service.write(changed).error.code == "retry_conflict"
    env.service._clock = lambda: at + timedelta(days=30)
    assert env.service.write(changed).error.code == "retry_expired"
    env.service._clock = lambda: at
    assert env.service.write(req).error.code == "retry_expired"


@pytest.mark.service
def test_mixed_batch_and_distinct_decision_submissions(env):
    support, dep = source(env)
    ids = mappings(env.service.write(request(env, (entity(support),), (dep,))))
    target = StoredEntity(kind="stored", entity_id=ids["project"])
    one = request(env, (decision(support, target),), (dep,))
    two = request(env, (decision(support, target),), (dep,))
    batch = WriteBatch(
        contract_version="foundation/1",
        batch_id="mixed",
        items=(
            one,
            put(env.scope, external="another"),
            two,
        ),
    )
    result = env.service.write_batch(batch)
    assert result.status == "complete"
    assert mappings(result.outcomes[0]) != mappings(result.outcomes[2])
    with reader(env, 1) as (adapter, meter, _):
        page = adapter.select_decisions(ids["project"]).read()
        assert len(page.items) == 1
        assert isinstance(page.terminal, SelectionStopped)
        assert page.terminal.kind == "public_budget_stop"


@pytest.mark.acceptance
def test_actual_1001_decisions_across_pages_and_private_local_cap(env):
    support, dep = source(env)
    ids = mappings(env.service.write(request(env, (entity(support),), (dep,))))
    target = StoredEntity(kind="stored", entity_id=ids["project"])
    expected = set()
    for start in range(0, 1001, 100):
        changes = tuple(
            decision(support, target, local=f"d{n}") for n in range(start, min(start + 100, 1001))
        )
        expected.update(mappings(env.service.write(request(env, changes, (dep,)))).values())
    with reader(env) as (adapter, meter, context):
        cursor = adapter.select_decisions(ids["project"])
        actual = []
        while True:
            page = cursor.read(limit=37)
            actual.extend(item.record.record_id for item in page.items)
            if page.terminal:
                assert isinstance(page.terminal, EligibleEOF), page.terminal
                break
        assert actual == sorted(expected)
        assert meter.public_accounting().items_consumed == 1001
        assert cursor.budget._visits < 10_000
    with reader(env) as (adapter, meter, _):
        cursor = adapter.select_decisions(ids["project"])
        assert len(cursor.read(limit=1).items) == 1
        cursor.budget.reserve_visits(10_000 - cursor.budget._visits)
        page = cursor.read(limit=1)
        assert page.items == ()
        assert page.terminal.kind == "private_resource_stop"
        assert meter.public_accounting().items_consumed == 1


@pytest.mark.acceptance
def test_bulk_actual_3001_varied_decisions_exceed_nested_visit_guard(env):
    supports = [source(env, external=f"bulk-{n}") for n in range(16)]
    support, dep = supports[0]
    ids = mappings(env.service.write(request(env, (entity(support),), (dep,))))
    target = StoredEntity(kind="stored", entity_id=ids["project"])
    expected = set()
    for start in range(0, 3001, 100):
        changes = tuple(
            decision(supports[n % 16][0], target, local=f"d{n}")
            for n in range(start, min(start + 100, 3001))
        )
        dependencies = tuple(
            item[1] for item in supports
            if any(item[0] == change.support for change in changes)
        )
        expected.update(mappings(env.service.write(request(env, changes, dependencies))).values())
    operation = _graph_build_operation(
        deadline=Deadline(time.monotonic() + 300), cancel=Event(),
    )
    budget = operation.budget
    with (
        observe(env.database, env.service.identity, env.scope, budget.deadline, budget) as observer,
        read_context(
            env.database, env.service.identity, env.scope, observer.session_id,
            budget.deadline, operation.meter,
        ) as context,
    ):
        adapter = KnowledgeReader(context, env.service.identity)
        cursor = adapter.select_decisions(ids["project"])
        actual = []
        previous = operation.snapshot()
        try:
            while True:
                page = cursor.read(limit=137)
                actual.extend(item.record.record_id for item in page.items)
                current = operation.snapshot()
                assert current.visits_reserved > previous.visits_reserved
                assert current.vm_instructions_reserved >= previous.vm_instructions_reserved
                previous = current
                if page.terminal is not None:
                    assert isinstance(page.terminal, EligibleEOF), page.terminal
                    break
            assert actual == sorted(expected)
            assert cursor.budget._visits > 10_000
            assert cursor.budget.deadline is budget.deadline
            assert operation.snapshot().semantic_items_reserved == 3001
            assert operation.snapshot().scratch_peak_bytes < 64 << 20
        finally:
            cursor.close()
            adapter.close()
    assert operation.snapshot().scratch_live_bytes == 0
    with reader(env) as (adapter, meter, _):
        cursor = adapter.select_decisions(ids["project"])
        try:
            while True:
                page = cursor.read(limit=137)
                if page.terminal is not None:
                    assert isinstance(page.terminal, SelectionStopped)
                    assert page.terminal.kind == "private_resource_stop"
                    assert page.items == ()
                    break
            assert meter.public_accounting().items_consumed < 3001
        finally:
            cursor.close()


@pytest.mark.service
def test_bulk_seed_witness_and_exact_revalidation_do_not_charge_nested_evidence(env):
    seed = SeedSupport(
        kind="seed", source_namespace="markdown", seed_set_id="bulk", seed_key="project",
    )
    ids = mappings(env.service.write(request(env, (entity(seed),))))
    evidence, dependency = source(env)
    target = StoredEntity(kind="stored", entity_id=ids["project"])
    decision_ids = mappings(env.service.write(
        request(env, (decision(evidence, target),), (dependency,)),
    ))
    operation = _graph_build_operation(
        deadline=Deadline(time.monotonic() + 300), cancel=Event(),
    )
    budget = operation.budget
    with (
        observe(env.database, env.service.identity, env.scope, budget.deadline, budget) as observer,
        read_context(
            env.database, env.service.identity, env.scope, observer.session_id,
            budget.deadline, operation.meter,
        ) as context,
    ):
        adapter = KnowledgeReader(context, env.service.identity)
        cursor = adapter.select_decisions(ids["project"])
        try:
            page = cursor.read()
            assert isinstance(page.terminal, EligibleEOF)
            member = page.items[0]
            assert member.record.record_id == decision_ids["decision"]
            assert member.dependencies.subject_witness.basis.kind == "seed"
            assert member.dependencies.subject_witness.basis.generation == 1
            before = operation.snapshot()
            adapter.revalidate_member(member)
            after = operation.snapshot()
            assert after.semantic_items_reserved == before.semantic_items_reserved == 1
            assert after.visits_reserved > before.visits_reserved
        finally:
            cursor.close()
            adapter.close()
    assert operation.snapshot().scratch_live_bytes == 0


@pytest.mark.service
def test_forward_independent_support_reactivates_but_old_assertion_stays_stale(env):
    support, dep = source(env)
    ids = mappings(
        env.service.write(
            request(
                env,
                (
                    entity(support),
                    decision(support, LocalEntity(kind="local", local_id="project")),
                ),
                (dep,),
            )
        )
    )
    receipt(env.service.write(put(env.scope, state=dep.state_version, title="updated")))
    fresh, fresh_dep = source(env, "fresh")
    stored = StoredEntity(kind="stored", entity_id=ids["project"])
    new = request(
        env,
        (
            decision(fresh, stored, local="new"),
            AddEntitySupport(
                kind="entity_support",
                local_id="attestation",
                entity=stored,
                name="Project",
                entity_type="project",
                support=fresh,
            ),
        ),
        (fresh_dep,),
    )
    new_ids = mappings(env.service.write(new))
    with reader(env) as (adapter, _, _):
        page = adapter.select_decisions(ids["project"]).read()
        assert [item.record.record_id for item in page.items] == [new_ids["new"]]
        assert page.items[0].dependencies.subject_witness.contribution_id == new_ids["attestation"]


@pytest.mark.service
def test_full_conjunction_and_hidden_endpoint_prevent_alias_activation(env):
    support, dep = source(env)
    other, other_dep = source(env, "foreign", "email")
    ids = mappings(
        env.service.write(
            request(
                env,
                (
                    entity(other),
                    AddAlias(
                        kind="alias",
                        local_id="a",
                        alias="Visible",
                        support=support,
                        entity=LocalEntity(kind="local", local_id="project"),
                    ),
                ),
                (dep, other_dep),
            )
        )
    )
    narrow = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(
                update={
                    "namespaces": ("markdown",),
                }
            )
        }
    )
    service = KnowledgeService(env.database, env.service.identity)
    for mode in ("current", "history"):
        with pytest.raises(EvidenceServiceError, match="not_found"):
            service.contribution(narrow, ids["a"], mode=mode)
    assert service.entities(narrow, name="Visible").entries == ()
    forged = request(
        env,
        (
            decision(
                support,
                StoredEntity(
                    kind="stored",
                    entity_id=ids["project"],
                ),
            ),
        ),
        (dep,),
    ).model_copy(update={"scope": narrow})
    assert env.service.write(forged).error.code == "not_found"


@pytest.mark.service
def test_entity_object_typed_endpoints_and_complete_owned_manifest(env):
    support, dep = source(env)
    person = CreateEntity(
        kind="entity",
        local_id="person",
        name="Someone",
        entity_type="person",
        support=support,
    )
    relation = AddAssertion(
        kind="assertion",
        local_id="edge",
        subject=LocalEntity(kind="local", local_id="person"),
        predicate="work:owns",
        object=EntityObject(
            kind="entity",
            entity=LocalEntity(kind="local", local_id="project"),
        ),
        interpretation="explicit",
        support=support,
    )
    ids = mappings(env.service.write(request(env, (relation, person, entity(support)), (dep,))))
    service = KnowledgeService(env.database, env.service.identity)
    assert len(service.contribution(env.scope, ids["edge"]).witnesses) == 2
    assert ids["edge"] in {
        c.contribution_id for c in service.contributions(env.scope, ids["project"]).entries
    }
    assert ids["edge"] in {
        c.contribution_id for c in service.contributions(env.scope, ids["person"]).entries
    }


@pytest.mark.service
def test_failed_ack_rollback_and_unlinked_ordinary_receipt_not_adopted(env):
    from kg.evidence._coordination import NewWork, UnitIdentity
    from kg.evidence._dispatch import write

    support, dep = source(env)
    req = request(env, (entity(support),), (dep,))
    unit = UnitIdentity(corpus_id="work", batch_id="batch", unit_id="unit", ordinal=0)

    class Participant:
        def classify_unit(self, context, unit, key):
            return NewWork()

        def guard_new(self, context, unit, request):
            pass

        def acknowledge_write(self, context, unit, committed):
            raise EvidenceServiceError("state_conflict")

    result = write(
        env.database,
        env.service.identity,
        req,
        env.service._clock(),
        participant=Participant(),
        unit=unit,
    )
    assert result.error.code == "state_conflict"
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM entity").fetchone()[0] == 0
    ordinary = env.service.write(req)
    mappings(ordinary)
    result = write(
        env.database,
        env.service.identity,
        req,
        env.service._clock(),
        participant=Participant(),
        unit=unit,
    )
    assert result.error.code == "state_conflict"
    assert env.service.write(req).receipt == ordinary.receipt


@pytest.mark.service
def test_sequence_not_lexicographic_witness_and_no_reselect_on_revalidation(env, monkeypatch):
    import kg.knowledge._write as write_module

    support, dep = source(env)
    ids = mappings(env.service.write(request(env, (entity(support),), (dep,))))
    service = KnowledgeService(env.database, env.service.identity)
    first = service.entity(env.scope, ids["project"]).witness
    original_token = write_module.token
    calls = 0

    def ordered_token():
        nonlocal calls
        calls += 1
        return "000-earlier-id-later-sequence" if calls == 2 else original_token()

    monkeypatch.setattr(write_module, "token", ordered_token)
    new = mappings(
        env.service.write(
            request(
                env,
                (
                    AddEntitySupport(
                        kind="entity_support",
                        local_id="extra",
                        entity=StoredEntity(kind="stored", entity_id=ids["project"]),
                        name="Project",
                        entity_type="project",
                        support=support,
                    ),
                ),
                (dep,),
            )
        )
    )
    assert new["extra"] < first.contribution_id
    assert service.entity(env.scope, ids["project"]).witness == first


@pytest.mark.process
def test_concurrent_same_retry_converges_and_failed_unknown_type_has_no_key(env):
    from concurrent.futures import ThreadPoolExecutor

    support, dep = source(env)
    req = request(env, (entity(support),), (dep,))
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = tuple(pool.map(env.service.write, (req, req)))
    assert mappings(results[0]) == mappings(results[1])
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM entity").fetchone()[0] == 1


@pytest.mark.service
def test_report_binding_revocation_is_irreversible_and_no_data(env):
    support, dep = source(env)
    req = request(env, (entity(support),), (dep,))
    explained = env.service.write_explained(req)
    mappings(explained.outcome)
    old_report = explained.report.report_id
    policy = env.policy.model_copy(update={"knowledge_bindings": ()})
    env.admin.replace_policy(policy, env.scope.access.policy_version)
    assert env.service.diagnostics.report(env.scope, old_report).state == "unavailable"
    assert env.service.write(req).error.code == "forbidden"


@pytest.mark.service
@pytest.mark.parametrize(
    "field,value",
    [
        ("owner_id", "other-owner"),
        ("writer_id", "other-writer"),
    ],
)
def test_exact_writer_binding_required(env, field, value):
    support, dep = source(env)
    req = request(env, (entity(support),), (dep,))
    req = req.model_copy(update={"attribution": req.attribution.model_copy(update={field: value})})
    assert env.service.write(req).error.code == "forbidden"
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM entity").fetchone()[0] == 0


@pytest.mark.service
def test_empty_unregistered_decision_capability_is_not_empty_selection(env):
    other = environment(env.database.path.parent / "no-decisions.db")
    KnowledgeAdministration(
        other.database,
        LocalAdminAuthority(principal_id="admin"),
    ).register_knowledge_schema(preset(schema().model_copy(update={"predicates": ()})))
    with reader(other) as (adapter, meter, _):
        page = adapter.select_decisions("missing").read()
        assert page.terminal.failure.code == "unsupported"
        assert page.items == ()
        assert meter.public_accounting().items_consumed == 0


@pytest.mark.service
def test_retained_reader_deadline_and_inherited_write_exhaustion(env):
    from kg.evidence._dispatch import write

    support, dep = source(env)
    req = request(env, (entity(support),), (dep,))
    budget = PrivateBudget(Deadline(time.monotonic() + 30))
    budget.reserve_visits(100_000)
    outcome = write(env.database, env.service.identity, req, env.service._clock(), budget=budget)
    assert outcome.error.code == "budget_exceeded"
    assert outcome.status == "failed"
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM entity").fetchone()[0] == 0
    with reader(env) as (adapter, _, context):
        cursor = adapter.resolve_entity(EntitySelector(name="Project"))
        object.__setattr__(context.deadline, "expires_at_monotonic", time.monotonic() - 1)
        page = cursor.read()
        assert page.items == ()
        assert page.terminal.kind == "deadline_stop"
        cursor.close()


@pytest.mark.service
def test_fabricated_passage_and_mention_reject_atomically(env):
    from kg.models.foundation import AddMention

    support, dep = source(env)
    fake_passage = support.model_copy(
        update={
            "evidence": (support.evidence[0].model_copy(update={"passage_id": "not-a-passage"}),),
        }
    )
    for changes in (
        (entity(fake_passage),),
        (
            entity(support),
            AddMention(
                kind="mention",
                local_id="mention",
                entity=LocalEntity(kind="local", local_id="project"),
                support=fake_passage,
            ),
        ),
    ):
        assert env.service.write(request(env, changes, (dep,))).error.code == "not_found"
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM entity").fetchone()[0] == 0
    caps = KnowledgeService(env.database, env.service.identity).capabilities(env.scope)
    assert "mention" in caps.change_kinds
    assert "mention" not in caps.unsupported


@pytest.mark.service
def test_whole_batch_forged_shape_rejected_before_item_zero(env):
    support, dep = source(env)
    good = request(env, (entity(support),), (dep,))
    bad = good.model_copy(
        update={
            "request_id": "bad",
            "payload": good.payload.model_copy(update={"changes": ()}),
        }
    )
    batch = WriteBatch.model_construct(
        contract_version="foundation/1",
        batch_id="forged",
        items=(good, bad),
    )
    with pytest.raises(EvidenceServiceError, match="invalid_request"):
        env.service.write_batch(batch)
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM entity").fetchone()[0] == 0


@pytest.mark.acceptance
def test_seed_maximum_active_slots_and_generation_per_unit(env):
    def seed(n):
        return SeedSupport(
            kind="seed",
            source_namespace="markdown",
            seed_set_id="bounded",
            seed_key=f"key-{n}",
        )

    first = request(env, tuple(entity(seed(n), local=f"p{n}") for n in range(100)))
    assert len(mappings(env.service.write(first))) == 100
    rejected = env.service.write(request(env, (entity(seed(100)),)))
    assert rejected.error.code == "invalid_request"
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM entity").fetchone()[0] == 100
        assert connection.execute("SELECT generation FROM seed_set").fetchone()[0] == 1


@pytest.mark.process
def test_source_write_waits_for_atomic_enrichment_commit_then_invalidates(env, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    import kg.knowledge._write as writes

    support, dep = source(env)
    req = request(env, (entity(support),), (dep,))
    staged, proceed = Event(), Event()
    original = writes.apply

    def paused(*args, **kwargs):
        result = original(*args, **kwargs)
        staged.set()
        assert proceed.wait(10)
        return result

    monkeypatch.setattr(writes, "apply", paused)
    with ThreadPoolExecutor(max_workers=2) as pool:
        enrichment = pool.submit(env.service.write, req)
        assert staged.wait(10)
        update = pool.submit(
            env.service.write, put(env.scope, state=dep.state_version, title="New")
        )
        assert not update.done()
        proceed.set()
        ids = mappings(enrichment.result(timeout=10))
        receipt(update.result(timeout=10))
    service = KnowledgeService(env.database, env.service.identity)
    assert not service.entity(env.scope, ids["project"], mode="history").is_current


@pytest.mark.service
def test_linked_settled_expiry_maintenance_never_checks_new_work(env):
    from kg.evidence._coordination import SettledSuccess, UnitIdentity
    from kg.evidence._dispatch import write, write_batch

    support, dep = source(env)
    at = datetime(2030, 1, 1, tzinfo=UTC)
    env.service._clock = lambda: at
    req = request(env, (entity(support),), (dep,))
    original = env.service.write(req)
    mappings(original)
    with env.database.connection() as connection:
        key_id = connection.execute(
            "SELECT key_id FROM write_key WHERE operation='enrich'",
        ).fetchone()[0]

    class Settled:
        def classify_unit(self, context, unit, key):
            return SettledSuccess(canonical_key_id=key_id)

        def guard_new(self, *args):
            pytest.fail("settled receipt must not enter new-work guards")

        def acknowledge_write(self, *args):
            pytest.fail("settled receipt must not acknowledge again")

    unit = UnitIdentity(corpus_id="work", batch_id="settled", unit_id="one", ordinal=0)
    changed = req.model_copy(
        update={
            "payload": req.payload.model_copy(
                update={
                    "changes": (entity(support, name="changed"),),
                }
            )
        }
    )
    before = write(
        env.database, env.service.identity, changed, at, participant=Settled(), unit=unit
    )
    assert before.error.code == "retry_conflict"
    batch = WriteBatch(contract_version="foundation/1", batch_id="settled", items=(changed,))
    expired = write_batch(
        env.database,
        env.service.identity,
        batch,
        at + timedelta(days=30),
        participant=Settled(),
        units=(unit,),
    )
    assert expired.outcomes[0].error.code == "retry_expired"
    assert env.service.write(req).error.code == "retry_expired"


@pytest.mark.functional
@pytest.mark.parametrize("passages", [False, True])
def test_example_reopens_and_replays_exact_record(tmp_path, passages):
    import json
    import subprocess
    import sys

    command = [
        sys.executable,
        "examples/knowledge_enrichment.py",
        "--database",
        str(tmp_path / "example.db"),
    ]
    if passages:
        command.append("--passages")
    first = json.loads(subprocess.check_output(command))
    second = json.loads(subprocess.check_output(command))
    assert first == second
    assert first["payload"]["predicate"] == "work:decision"
    assert first["evidence"][0]["reference"]["anchor_id"]
    assert bool(first["evidence"][0]["reference"]["passage_id"]) == passages


@pytest.mark.service
def test_every_typed_scalar_round_trips_without_bool_integer_coercion(env):
    from kg.models.evidence import CorpusRegistration
    from kg.models.foundation import BooleanObject, IntegerObject, TimestampObject
    from kg.models.knowledge import PredicateDefinition

    corpus = "typed"
    registration = env.admin.register(
        CorpusRegistration(
            corpus_id=corpus,
            namespaces=("markdown", "email"),
            policy=env.policy.model_copy(update={"corpus_id": corpus}),
        )
    )
    env.scope = env.scope.model_copy(
        update={
            "corpus_id": corpus,
            "access": env.scope.access.model_copy(
                update={"policy_version": registration.policy_version}
            ),
        }
    )
    values = (
        StringObject(kind="string", value="exact"),
        IntegerObject(kind="integer", value=10**40),
        BooleanObject(kind="boolean", value=True),
        TimestampObject(kind="timestamp", value=datetime(2030, 1, 1, tzinfo=UTC)),
    )
    KnowledgeAdministration(
        env.database, LocalAdminAuthority(principal_id="admin")
    ).register_knowledge_schema(
        preset(schema(corpus).model_copy(
            update={
                "predicates": tuple(
                    PredicateDefinition(
                        name=f"typed:{v.kind}",
                        subject_types=("project",),
                        object_kind=v.kind,
                    )
                    for v in values
                )
            }
        )),
    )
    support, dep = source(env)
    changes = (
        entity(support),
        *(
            AddAssertion(
                kind="assertion",
                local_id=v.kind,
                subject=LocalEntity(kind="local", local_id="project"),
                predicate=f"typed:{v.kind}",
                object=v,
                interpretation="inferred",
                support=support,
            )
            for v in values
        ),
    )
    ids = mappings(env.service.write(request(env, changes, (dep,))))
    service = KnowledgeService(env.database, env.service.identity)
    for value in values:
        assert service.contribution(env.scope, ids[value.kind]).payload.object == value


def _process_write(path, request_json, queue):
    from pathlib import Path

    from kg.evidence import EvidenceDatabase, EvidenceService
    from kg.models.evidence import LocalIdentity

    service = EvidenceService(EvidenceDatabase(Path(path)), LocalIdentity(principal_id="principal"))
    queue.put(service.write(WriteRequest.model_validate_json(request_json)).model_dump_json())


@pytest.mark.process
def test_independent_processes_share_one_atomic_retry_key(env):
    import multiprocessing

    from kg.models.foundation import WriteOutcome

    support, dep = source(env)
    req = request(env, (entity(support),), (dep,))
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    workers = [
        context.Process(
            target=_process_write,
            args=(
                str(env.database.path),
                req.model_dump_json(),
                queue,
            ),
        )
        for _ in range(2)
    ]
    for process in workers:
        process.start()
    results = [WriteOutcome.model_validate_json(queue.get(timeout=30)) for _ in workers]
    for process in workers:
        process.join(timeout=30)
        assert process.exitcode == 0
    assert mappings(results[0]) == mappings(results[1])


@pytest.mark.service
@pytest.mark.parametrize("size", [20, 2_100_000])
def test_foreign_source_cannot_leak_through_scratch_preflight(env, size):
    from kg.models.evidence import CorpusRegistration

    foreign = env.admin.register(
        CorpusRegistration(
            corpus_id="foreign",
            namespaces=("markdown", "email"),
            policy=env.policy.model_copy(update={"corpus_id": "foreign"}),
        )
    )
    foreign_scope = env.scope.model_copy(
        update={
            "corpus_id": "foreign",
            "access": env.scope.access.model_copy(
                update={"policy_version": foreign.policy_version}
            ),
        }
    )
    doc = receipt(env.service.write(put(foreign_scope, text="x" * size)))
    ref = (
        env.service.anchors(
            foreign_scope,
            doc.document_id,
            doc.processing.state_version,
        )
        .entries[0]
        .reference.model_copy(update={"corpus_id": "work"})
    )
    support = SourceSupport(kind="source", evidence=(ref,))
    dep = DocumentDependency(
        source_namespace="markdown",
        document_id=doc.document_id,
        revision_id=doc.revision_id,
        state_version=doc.processing.state_version,
    )
    explained = env.service.write_explained(request(env, (entity(support),), (dep,)))
    assert explained.outcome.error.code == "not_found"
    assert explained.report.state != "collected"
    from kg.knowledge._store import Store

    with reader(env) as (_, _, context):
        store = Store(context.connection, env.scope, context.meter.private_budget, context=context)
        try:
            with pytest.raises(EvidenceServiceError, match="not_found"):
                store.proof(ref, dep.state_version)
        finally:
            store.close()


@pytest.mark.service
def test_missing_anchor_and_stale_dependency_have_canonical_failures(env):
    support, dep = source(env)
    missing = SourceSupport(
        kind="source", evidence=(support.evidence[0].model_copy(update={"anchor_id": "missing"}),)
    )
    assert env.service.write(request(env, (entity(missing),), (dep,))).error.code == "not_found"
    stale = dep.model_copy(update={"state_version": "missing"})
    assert (
        env.service.write(request(env, (entity(support),), (stale,))).error.code == "state_conflict"
    )


@pytest.mark.service
def test_actual_scratch_failure_is_failed_and_report_irreversibly_has_no_data(env):
    doc = receipt(env.service.write(put(env.scope, text="x" * 2_100_000)))
    ref = env.service.anchors(env.scope, doc.document_id, doc.processing.state_version).entries[0]
    support = SourceSupport(kind="source", evidence=(ref.reference,))
    dep = DocumentDependency(
        source_namespace="markdown",
        document_id=doc.document_id,
        revision_id=doc.revision_id,
        state_version=doc.processing.state_version,
    )
    explained = env.service.write_explained(request(env, (entity(support),), (dep,)))
    assert explained.outcome.status == "failed"
    assert explained.outcome.error.code == "budget_exceeded"
    assert explained.report.state == "redacted"
    assert not hasattr(explained.report, "events")
    assert (
        env.service.diagnostics.report(
            env.scope,
            explained.outcome.error.diagnostic_id,
        ).state
        == "redacted"
    )
    assert (
        env.service.diagnostics.for_request(
            env.scope,
            explained.outcome.request_id,
        ).entries
        == ()
    )


@pytest.mark.service
def test_last_receipt_failure_rolls_back_every_knowledge_row(env, monkeypatch):
    import sqlite3

    import kg.knowledge._write as writes

    support, dep = source(env)
    req = request(env, (entity(support),), (dep,))
    original = writes.save

    def fail_after_receipt(*args):
        original(*args)
        raise sqlite3.OperationalError("injected-after-receipt")

    monkeypatch.setattr(writes, "save", fail_after_receipt)
    assert env.service.write(req).error.code == "internal_error"
    with env.database.connection() as connection:
        for table in (
            "entity",
            "contribution",
            "knowledge_write_response",
            "knowledge_write_provenance",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    monkeypatch.setattr(writes, "save", original)
    mappings(env.service.write(req))


@pytest.mark.service
def test_uncertain_commit_replays_same_key_without_duplicate_entities(env, monkeypatch):
    import sqlite3

    from kg.evidence._sql import AccountedConnection

    support, dep = source(env)
    req = request(env, (entity(support),), (dep,))
    original = AccountedConnection.commit

    def commit_then_fail(connection):
        original(connection)
        raise sqlite3.OperationalError("injected-after-commit")

    monkeypatch.setattr(AccountedConnection, "commit", commit_then_fail)
    failed = env.service.write_explained(req)
    assert failed.outcome.error.code == "internal_error"
    assert any(
        event.event.kind == "execution.commit" and event.event.observation == "unknown"
        for event in failed.report.events
    )
    monkeypatch.setattr(AccountedConnection, "commit", original)
    recovered = env.service.write(req)
    mappings(recovered)
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM entity").fetchone()[0] == 1


@pytest.mark.service
def test_write_keeps_validated_quote_scratch_through_post_state_and_receipt(env, monkeypatch):
    import kg.knowledge._write as writes

    doc = receipt(env.service.write(put(env.scope, text="q" * 100_000)))
    ref = env.service.anchors(env.scope, doc.document_id, doc.processing.state_version).entries[0]
    support = SourceSupport(kind="source", evidence=(ref.reference,))
    dep = DocumentDependency(
        source_namespace="markdown",
        document_id=doc.document_id,
        revision_id=doc.revision_id,
        state_version=doc.processing.state_version,
    )
    original = writes.save
    budgets = []

    def check_hold(context, *args):
        budget = context.connection._budget
        budgets.append(budget)
        assert budget._root._scratch >= 400_000
        return original(context, *args)

    monkeypatch.setattr(writes, "save", check_hold)
    mappings(env.service.write(request(env, (entity(support),), (dep,))))
    assert budgets[0]._root._scratch == 0
