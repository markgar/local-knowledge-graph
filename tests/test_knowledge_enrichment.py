import time
from contextlib import contextmanager
from datetime import UTC, datetime
from threading import Event
from uuid import uuid4

import pytest
from support.classification import fixture_changes, typed_entity
from support.evidence import environment, put, receipt
from support.knowledge import preset, revision, schema
from support.private_knowledge import write as private_write

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
from kg.knowledge._selection import EligibleEOF, EntitySelector
from kg.knowledge._write_models import (
    AddAlias,
    AddAssertion,
    AddEntitySupport,
    AddIdentifier,
    ChangeSet,
    ChangeSetReceipt,
    CreateEntity,
    EntityObject,
    LocalEntity,
)
from kg.models.evidence import KnowledgeWriterBinding, LocalAdminAuthority, PolicyGrant
from kg.models.foundation import (
    Attribution,
    DocumentDependency,
    SeedSupport,
    SourceSupport,
    StoredEntity,
    StringObject,
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
    doc = receipt(
        private_write(env.service, put(env.scope, external=external, namespace=namespace))
    )
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
    return WriteRequest.model_construct(
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
            expected_schema_revision=revision(env),
            operation="enrich",
            changes=fixture_changes(env, changes),
            dependencies=tuple(deps),
        ),
    )


def mappings(result):
    assert result.error is None, result.error
    assert isinstance(result.receipt, ChangeSetReceipt)
    return {m.local_id: m.stored_id for m in result.receipt.mappings}


def entity(support, local="project", name="Project"):
    return typed_entity(
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
    result = private_write(env.service, req)
    ids = mappings(result)
    assert tuple(m.local_id for m in result.receipt.mappings) == (
        "decision",
        "alias",
        "id",
        "project",
        "project:classification",
        "project:selection",
    )
    service = KnowledgeService(env.database, env.service.identity)
    assert service.entity(env.scope, ids["project"]).name == "Project"
    assert service.contribution(env.scope, ids["decision"]).payload.object.value == "Ship"
    assert len(service.entities(env.scope, name="P").entries) == 1
    assert len(service.entities(env.scope, scheme="ticket", identifier="P-1").entries) == 1
    assert len(service.contributions(env.scope, ids["project"]).entries) == 5
    assert private_write(env.service, req).receipt == result.receipt
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
    create = CreateEntity(kind="entity", local_id="project", name="Project", support=support)
    first = request(env, (create,))
    ids = mappings(private_write(env.service, first))
    repeated = private_write(env.service, first.model_copy(update={"retry_key": "other"}))
    assert repeated.status == "unchanged"
    assert mappings(repeated) == ids
    service = KnowledgeService(env.database, env.service.identity)
    assert service.entity(env.scope, ids["project"]).is_current
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM contribution").fetchone()[0] == 1
        assert connection.execute("SELECT generation FROM seed_set").fetchone()[0] == 1
    conflict = request(env, (create.model_copy(update={"name": "Changed"}),))
    assert private_write(env.service, conflict).error.code == "state_conflict"


@pytest.mark.service
def test_failed_last_change_rolls_back_entities_seeds_and_key(env):
    support, dep = source(env)
    bad = decision(support, LocalEntity(kind="local", local_id="project")).model_copy(
        update={"interpretation": "inferred"},
    )
    req = request(env, (entity(support), bad), (dep,))
    assert private_write(env.service, req).error.code == "invalid_request"
    with env.database.connection() as connection:
        for table in ("entity", "contribution", "knowledge_write_response"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    corrected = req.model_copy(
        update={
            "payload": req.payload.model_copy(
                update={
                    "changes": fixture_changes(
                        env,
                        (
                            entity(support),
                            bad.model_copy(update={"interpretation": "explicit"}),
                        ),
                    ),
                }
            )
        }
    )
    mappings(private_write(env.service, corrected))


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
    result = private_write(env.service, req)
    ids = mappings(result)
    receipt(private_write(env.service, put(env.scope, state=dep.state_version, title="changed")))
    service = KnowledgeService(env.database, env.service.identity)
    with pytest.raises(EvidenceServiceError, match="not_found"):
        service.contribution(env.scope, ids["decision"])
    assert not service.contribution(env.scope, ids["decision"], mode="history").is_current
    assert private_write(env.service, req).receipt == result.receipt
    assert (
        private_write(env.service, req.model_copy(update={"retry_key": "fresh"})).error.code
        == "state_conflict"
    )


@pytest.mark.service
def test_bulk_seed_witness_and_exact_revalidation_do_not_charge_nested_evidence(env):
    seed = SeedSupport(
        kind="seed",
        source_namespace="markdown",
        seed_set_id="bulk",
        seed_key="project",
    )
    ids = mappings(private_write(env.service, request(env, (entity(seed),))))
    evidence, dependency = source(env)
    target = StoredEntity(kind="stored", entity_id=ids["project"])
    decision_ids = mappings(
        private_write(
            env.service,
            request(env, (decision(evidence, target),), (dependency,)),
        )
    )
    operation = _graph_build_operation(
        deadline=Deadline(time.monotonic() + 300),
        cancel=Event(),
    )
    budget = operation.budget
    with (
        observe(env.database, env.service.identity, env.scope, budget.deadline, budget) as observer,
        read_context(
            env.database,
            env.service.identity,
            env.scope,
            observer.session_id,
            budget.deadline,
            operation.meter,
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
    classification_support, classification_dep = source(env, "stable-classification")
    typed = entity(support)
    typed = (typed[0], typed[1].model_copy(update={"support": classification_support}), typed[2])
    ids = mappings(
        private_write(
            env.service,
            request(
                env,
                (
                    typed,
                    decision(support, LocalEntity(kind="local", local_id="project")),
                ),
                (dep, classification_dep),
            ),
        )
    )
    receipt(private_write(env.service, put(env.scope, state=dep.state_version, title="updated")))
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
                support=fresh,
            ),
        ),
        (fresh_dep,),
    )
    new_ids = mappings(private_write(env.service, new))
    with reader(env) as (adapter, _, _):
        page = adapter.select_decisions(ids["project"]).read()
        assert [item.record.record_id for item in page.items] == [new_ids["new"]]
        assert page.items[0].dependencies.subject_witness.contribution_id == new_ids["attestation"]


@pytest.mark.service
def test_full_conjunction_and_hidden_endpoint_prevent_alias_activation(env):
    support, dep = source(env)
    other, other_dep = source(env, "foreign", "email")
    ids = mappings(
        private_write(
            env.service,
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
            ),
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
    assert private_write(env.service, forged).error.code == "not_found"


@pytest.mark.service
def test_entity_object_typed_endpoints_and_complete_owned_manifest(env):
    support, dep = source(env)
    person = typed_entity(
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
    ids = mappings(
        private_write(env.service, request(env, (relation, person, entity(support)), (dep,)))
    )
    service = KnowledgeService(env.database, env.service.identity)
    assert len(service.contribution(env.scope, ids["edge"]).witnesses) == 2
    assert ids["edge"] in {
        c.contribution_id for c in service.contributions(env.scope, ids["project"]).entries
    }
    assert ids["edge"] in {
        c.contribution_id for c in service.contributions(env.scope, ids["person"]).entries
    }


@pytest.mark.service
def test_sequence_not_lexicographic_witness_and_no_reselect_on_revalidation(env, monkeypatch):
    import kg.knowledge._write as write_module

    support, dep = source(env)
    ids = mappings(private_write(env.service, request(env, (entity(support),), (dep,))))
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
        private_write(
            env.service,
            request(
                env,
                (
                    AddEntitySupport(
                        kind="entity_support",
                        local_id="extra",
                        entity=StoredEntity(kind="stored", entity_id=ids["project"]),
                        name="Project",
                        support=support,
                    ),
                ),
                (dep,),
            ),
        )
    )
    assert new["extra"] < first.contribution_id
    assert service.entity(env.scope, ids["project"]).witness == first


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
    assert private_write(env.service, req).error.code == "forbidden"
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


@pytest.mark.acceptance
def test_seed_maximum_active_slots_and_generation_per_unit(env):
    def seed(n):
        return SeedSupport(
            kind="seed",
            source_namespace="markdown",
            seed_set_id="bounded",
            seed_key=f"key-{n}",
        )

    first = request(
        env,
        tuple(
            CreateEntity(
                kind="entity",
                local_id=f"p{n}",
                name="Project",
                support=seed(n),
            )
            for n in range(100)
        ),
    )
    assert len(mappings(private_write(env.service, first))) == 100
    rejected = private_write(
        env.service,
        request(
            env,
            (
                CreateEntity(
                    kind="entity",
                    local_id="extra",
                    name="Project",
                    support=seed(100),
                ),
            ),
        ),
    )
    assert rejected.error.code == "invalid_request"
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM entity").fetchone()[0] == 100
        assert connection.execute("SELECT generation FROM seed_set").fetchone()[0] == 1


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
        preset(
            schema(corpus).model_copy(
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
            )
        ),
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
    ids = mappings(private_write(env.service, request(env, changes, (dep,))))
    service = KnowledgeService(env.database, env.service.identity)
    for value in values:
        assert service.contribution(env.scope, ids[value.kind]).payload.object == value


def _process_write(path, request_json, queue):
    from pathlib import Path

    from kg.evidence import EvidenceDatabase, EvidenceService
    from kg.models.evidence import LocalIdentity

    service = EvidenceService(EvidenceDatabase(Path(path)), LocalIdentity(principal_id="principal"))
    queue.put(service.write(WriteRequest.model_validate_json(request_json)).model_dump_json())


@pytest.mark.service
def test_missing_anchor_and_stale_dependency_have_canonical_failures(env):
    support, dep = source(env)
    missing = SourceSupport(
        kind="source", evidence=(support.evidence[0].model_copy(update={"anchor_id": "missing"}),)
    )
    assert (
        private_write(env.service, request(env, (entity(missing),), (dep,))).error.code
        == "not_found"
    )
    stale = dep.model_copy(update={"state_version": "missing"})
    assert (
        private_write(env.service, request(env, (entity(support),), (stale,))).error.code
        == "state_conflict"
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
    assert private_write(env.service, req).error.code == "internal_error"
    with env.database.connection() as connection:
        for table in (
            "entity",
            "contribution",
            "knowledge_write_response",
            "knowledge_write_provenance",
        ):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
    monkeypatch.setattr(writes, "save", original)
    mappings(private_write(env.service, req))


@pytest.mark.service
def test_write_keeps_validated_quote_scratch_through_post_state_and_receipt(env, monkeypatch):
    import kg.knowledge._write as writes

    doc = receipt(private_write(env.service, put(env.scope, text="q" * 100_000)))
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
    mappings(private_write(env.service, request(env, (entity(support),), (dep,))))
    assert budgets[0]._root._scratch == 0
