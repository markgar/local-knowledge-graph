"""Real E1 -> E3 -> K1 passage acceptance, without vector/model prerequisites."""

import multiprocessing
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Event
from uuid import uuid4

import pytest
from support.classification import fixture_changes, typed_entity
from support.evidence import environment, receipt
from support.indexing import process
from support.indexing import request as document_request
from support.indexing import service as index_service
from support.knowledge import preset, revision, schema
from support.withdrawal import withdrawal

from kg._execution_budget import (
    Deadline,
    LocalExecutionMeter,
    PrivateBudget,
    PrivateResourceStop,
    _graph_build_operation,
)
from kg.evidence import EvidenceDatabase, EvidenceService, EvidenceServiceError
from kg.evidence._read_context import observe, read_context, release_fence
from kg.evidence._support import TransactionEvidence
from kg.evidence._transactions import writing
from kg.indexing._passages import produce
from kg.knowledge import KnowledgeAdministration, KnowledgeService
from kg.knowledge._reader import KnowledgeReader
from kg.knowledge._selection import EligibleEOF, EntitySelector
from kg.knowledge._store import Store
from kg.models.evidence import (
    CorpusRegistration,
    KnowledgeWriterBinding,
    LocalIdentity,
    PolicyGrant,
)
from kg.models.foundation import (
    AddAlias,
    AddAssertion,
    AddEntitySupport,
    AddIdentifier,
    AddMention,
    Attribution,
    ChangeSet,
    ChangeSetReceipt,
    DocumentDependency,
    EntityObject,
    ExpectedState,
    LocalEntity,
    RemoveDocument,
    SourceSupport,
    StoredEntity,
    StringObject,
    SuppliedAnchor,
    WriteBatch,
    WriteOutcome,
    WriteRequest,
)

TEXT = "Sam and Sam own Atlas.\r\nCafe\u0301 \U0001f680\0"


@pytest.fixture
def env(tmp_path):
    env = environment(tmp_path / "knowledge-passages.db")
    env.policy = env.policy.model_copy(
        update={
            "grants": env.policy.grants
            + tuple(
                PolicyGrant(principal_id="principal", namespace=ns, grant="write_knowledge")
                for ns in ("markdown", "email")
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
    version = env.admin.replace_policy(env.policy, env.scope.access.policy_version).policy_version
    env.scope = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(
                update={
                    "policy_version": version,
                    "grants": ("read", "write_documents", "write_knowledge"),
                }
            ),
        }
    )
    KnowledgeAdministration(env.database, env.admin.authority).register_knowledge_schema(
        preset(schema()),
    )
    return env


def passage(env, external="a", namespace="markdown", policy="codepoint-window/1", **kw):
    value = document_request(
        env,
        external=external,
        namespace=namespace,
        policy=policy,
        text=kw.pop("text", TEXT),
        anchors=kw.pop(
            "anchors",
            (
                SuppliedAnchor(
                    local_id="whole",
                    start=0,
                    end=len(TEXT),
                    quote=TEXT,
                ),
            )
            if policy == "supplied-anchors/1"
            else (),
        ),
        **kw,
    )
    saved = receipt(env.service.write(value))
    produce(
        env.database,
        env.service.identity,
        env.scope,
        value.attribution,
        saved.document_id,
        saved.processing.state_version,
    )
    page = env.service.passages(env.scope, saved.document_id, saved.processing.state_version)
    return value, saved, page


def support(saved, page):
    return SourceSupport(kind="source", evidence=tuple(p.reference for p in page.entries)), (
        DocumentDependency(
            source_namespace=page.entries[0].reference.source_namespace,
            document_id=saved.document_id,
            revision_id=saved.revision_id,
            state_version=saved.processing.state_version,
        )
    )


def request(env, changes, dependencies, retry=None):
    return WriteRequest(
        contract_version="foundation/1",
        request_id=str(uuid4()),
        retry_key=retry or str(uuid4()),
        scope=env.scope,
        attribution=Attribution(
            owner_id="owner",
            writer_id="writer",
            producer="passage-tests",
            producer_version="1",
        ),
        payload=ChangeSet(expected_schema_revision=revision(env),
            operation="enrich", changes=fixture_changes(env, changes),
            dependencies=tuple(dependencies),
        ),
    )


def mappings(outcome):
    assert outcome.error is None, outcome.error
    assert isinstance(outcome.receipt, ChangeSetReceipt)
    return {entry.local_id: entry.stored_id for entry in outcome.receipt.mappings}


def entity(evidence, local="project", name="Atlas", kind="project"):
    return typed_entity(
        kind="entity",
        local_id=local,
        name=name,
        entity_type=kind,
        support=evidence,
    )


def mention(evidence, target, local="mention"):
    return AddMention(kind="mention", local_id=local, entity=target, support=evidence)


def decision(evidence, target, local="decision"):
    return AddAssertion(
        kind="assertion",
        local_id=local,
        subject=target,
        predicate="work:decision",
        object=StringObject(kind="string", value="Ship Atlas"),
        interpretation="explicit",
        support=evidence,
    )


def inventory(env):
    with env.database.connection() as connection:
        return {
            table: tuple(tuple(r) for r in connection.execute(f"SELECT * FROM {table}"))
            for table in (
                "entity",
                "contribution",
                "entity_support",
                "alias",
                "identifier",
                "mention",
                "assertion",
                "contribution_evidence",
                "write_key",
                "knowledge_write_response",
                "knowledge_write_provenance",
            )
        }


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
        adapter = KnowledgeReader(context, env.service.identity)
        try:
            yield adapter, context, meter, observer
        finally:
            adapter.close()
    assert budget._scratch == 0


def variants(evidence, target):
    return (
        entity(evidence),
        AddEntitySupport(
            kind="entity_support",
            local_id="support",
            entity=target,
            name="Atlas",
            support=evidence,
        ),
        AddAlias(kind="alias", local_id="alias", entity=target, alias="Launch", support=evidence),
        AddIdentifier(
            kind="identifier",
            local_id="identifier",
            entity=target,
            scheme="ticket",
            value="same",
            support=evidence,
        ),
        mention(evidence, target),
        decision(evidence, target),
    )


@pytest.mark.parametrize("policy", ["codepoint-window/1", "supplied-anchors/1"])
def test_all_passage_variants_exact_provenance_and_capabilities_before_vectors(env, policy):
    value, saved, page = passage(env, policy=policy)
    evidence, dep = support(saved, page)
    target_id = mappings(env.service.write(request(env, (entity(evidence),), (dep,))))["project"]
    target = StoredEntity(kind="stored", entity_id=target_id)
    req = request(env, variants(evidence, target), (dep,))
    outcome = env.service.write_explained(req)
    ids = mappings(outcome.outcome)
    assert outcome.report.state == "collected"
    service = KnowledgeService(env.database, env.service.identity)
    caps = service.capabilities(env.scope)
    assert caps.change_kinds == (
        "entity",
        "entity_support",
        "alias",
        "identifier",
        "mention",
        "assertion",
    )
    assert caps.support == "anchors_passages_and_seed_add"
    assert caps.unsupported == ("replace_seed_set", "retraction", "traversal")
    for change in req.payload.changes:
        if change.kind == "entity":
            view = service.entity(env.scope, ids[change.local_id])
            contribution_id = view.witness.contribution_id
        else:
            contribution_id = ids[change.local_id]
        view = service.contribution(env.scope, contribution_id)
        assert view.is_current and view.payload.support == evidence
        assert view.evidence[0].dependency == dep
        assert (
            view.evidence[0].metadata_snapshot_id == page.entries[0].citation.metadata_snapshot_id
        )
        exact = env.service.evidence(env.scope, view.evidence[0].reference)
        assert exact.quote == TEXT and env.service.citation(env.scope, exact.citation) == exact
        with env.database.connection() as connection:
            row = connection.execute(
                "SELECT passage_set_id,passage_id FROM contribution_evidence "
                "WHERE contribution_id=?",
                (contribution_id,),
            ).fetchone()
            assert tuple(row) == (page.passage_set_id, page.entries[0].reference.passage_id)
    mentions = service.contributions(env.scope, target_id, kind="mention", limit=1)
    assert [v.contribution_id for v in mentions.entries] == [ids["mention"]]
    assert not mentions.has_more
    assert ids["mention"] in {
        v.contribution_id for v in service.contributions(env.scope, target_id).entries
    }
    doc = env.service.document(env.scope, saved.document_id)
    assert doc.processing.indexing == doc.processing.enrichment == "pending"
    assert env.service.write(req).receipt == outcome.outcome.receipt
    index, _, _ = index_service(env)
    assert process(index, env, value, saved).outcome == "ready"
    assert process(index, env, value, saved, mode="rebuild").outcome == "ready"
    assert service.contribution(env.scope, ids["mention"]).is_current
    assert env.service.write(req).receipt == outcome.outcome.receipt
    assert env.service.citation(env.scope, page.entries[0].citation).quote == TEXT


def test_a09_a17_same_names_conjunction_alternatives_and_no_inferred_edges(env):
    value, a, a_page = passage(env)
    _, b, b_page = passage(env, "b", "email")
    sa, da = support(a, a_page)
    sb, db = support(b, b_page)
    both = SourceSupport(kind="source", evidence=sa.evidence + sb.evidence)
    sam = LocalEntity(kind="local", local_id="sam")
    other = LocalEntity(kind="local", local_id="other")
    req = request(
        env,
        (
            mention(both, sam, "sam-mention"),
            mention(both, other, "other-mention"),
            entity(both, "sam", "Sam", "person"),
            entity(sb, "other", "Sam", "person"),
            entity(sb, "atlas"),
            AddAlias(kind="alias", local_id="alias", entity=sam, alias="S", support=both),
            *(
                AddIdentifier(
                    kind="identifier",
                    local_id=f"id-{i}",
                    entity=who,
                    scheme="email",
                    value="sam@example.test",
                    support=sb,
                )
                for i, who in enumerate((sam, other))
            ),
        ),
        (da, db),
    )
    first = env.service.write(req)
    ids = mappings(first)
    assert ids["sam"] != ids["other"]
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM assertion").fetchone()[0] == 0
    with reader(env) as (adapter, _, _, _):
        cursor = adapter.resolve_entity(EntitySelector(name="Sam"))
        assert {i.entity_id for i in cursor.read().items} == {ids["sam"], ids["other"]}
        cursor.close()
    target = StoredEntity(kind="stored", entity_id=ids["sam"])
    alternatives = request(
        env,
        (
            AddEntitySupport(
                kind="entity_support",
                local_id="independent",
                entity=target,
                name="Sam",
                support=sb,
            ),
            *(
                AddAssertion(
                    kind="assertion",
                    local_id=interpretation,
                    subject=who,
                    predicate="work:owns",
                    object=EntityObject(
                        kind="entity", entity=StoredEntity(kind="stored", entity_id=ids["atlas"])
                    ),
                    interpretation=interpretation,
                    support=evidence,
                )
                for interpretation, who, evidence in (
                    ("explicit", target, both),
                    ("inferred", StoredEntity(kind="stored", entity_id=ids["other"]), sb),
                )
            ),
        ),
        (da, db),
    )
    extra = mappings(env.service.write(alternatives))
    service = KnowledgeService(env.database, env.service.identity)
    assert all(
        service.contribution(env.scope, extra[k]).is_current for k in ("explicit", "inferred")
    )
    removed = value.model_copy(
        update={
            "retry_key": str(uuid4()),
            "payload": RemoveDocument(
                operation="remove_document",
                document=value.payload.document,
                precondition=ExpectedState(kind="match", state_version=da.state_version),
            ),
        }
    )
    receipt(env.service.write(removed))
    assert service.entity(env.scope, ids["sam"]).witness.contribution_id == extra["independent"]
    for cid in (ids["sam-mention"], ids["other-mention"], ids["alias"], extra["explicit"]):
        with pytest.raises(EvidenceServiceError, match="not_found"):
            service.contribution(env.scope, cid)
        historical = service.contribution(env.scope, cid, mode="history")
        assert not historical.is_current and len(historical.evidence) == 2
    assert service.contribution(env.scope, extra["inferred"]).is_current
    assert env.service.write(req).receipt == first.receipt
    narrow = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(update={"namespaces": ("email",)}),
        }
    )
    with pytest.raises(EvidenceServiceError, match="not_found"):
        service.contribution(narrow, ids["sam-mention"], mode="history")
    with pytest.raises(EvidenceServiceError, match="invalid_request"):
        env.service.write(req.model_copy(update={"scope": narrow}))


@pytest.mark.parametrize("source_number", [0, 1])
@pytest.mark.parametrize("mutation", ["text", "metadata", "passage_policy", "boundaries", "remove"])
def test_each_source_staleness_atomically_rejects_all_variants(env, source_number, mutation):
    sources = [
        passage(env, "a", policy="supplied-anchors/1"),
        passage(env, "b", "email", policy="supplied-anchors/1"),
    ]
    pairs = [support(saved, page) for _, saved, page in sources]
    evidence = SourceSupport(
        kind="source", evidence=tuple(ref for supported, _ in pairs for ref in supported.evidence)
    )
    deps = tuple(dep for _, dep in pairs)
    target_id = mappings(env.service.write(request(env, (entity(evidence),), deps)))["project"]
    req = request(env, variants(evidence, StoredEntity(kind="stored", entity_id=target_id)), deps)
    value, saved, _ = sources[source_number]
    if mutation == "remove":
        changed = value.model_copy(
            update={
                "retry_key": str(uuid4()),
                "payload": RemoveDocument(
                    operation="remove_document",
                    document=value.payload.document,
                    precondition=ExpectedState(
                        kind="match", state_version=saved.processing.state_version
                    ),
                ),
            }
        )
    else:
        content = value.payload.content
        if mutation == "text":
            content = content.model_copy(update={"text": "Other", "anchors": ()})
        elif mutation == "passage_policy":
            content = content.model_copy(update={"passage_policy": "codepoint-window/1"})
        elif mutation == "boundaries":
            content = content.model_copy(
                update={
                    "anchors": (SuppliedAnchor(local_id="short", start=0, end=3, quote=TEXT[:3]),)
                }
            )
        changed = value.model_copy(
            update={
                "retry_key": str(uuid4()),
                "payload": value.payload.model_copy(
                    update={
                        "precondition": ExpectedState(
                            kind="match", state_version=saved.processing.state_version
                        ),
                        "content": content,
                        "metadata": value.payload.metadata.model_copy(update={"title": "New"})
                        if mutation == "metadata"
                        else value.payload.metadata,
                    }
                ),
            }
        )
    receipt(env.service.write(changed))
    before = inventory(env)
    result = env.service.write(req)
    assert result.error.code == "state_conflict" and result.receipt is None
    assert inventory(env) == before


@pytest.mark.parametrize("transition", ["text", "metadata", "policy", "remove"])
@pytest.mark.parametrize("withdraw", [False, True])
def test_roundtrip_never_resurrects_support_and_fresh_support_is_explicit(
    env, transition, withdraw,
):
    original, a, page = passage(env)
    evidence, dep = support(a, page)
    target = LocalEntity(kind="local", local_id="project")
    req = request(
        env, (entity(evidence), mention(evidence, target), decision(evidence, target)), (dep,)
    )
    outcome = env.service.write(req)
    ids = mappings(outcome)
    if transition == "remove":
        payload = RemoveDocument(
            operation="remove_document",
            document=original.payload.document,
            precondition=ExpectedState(kind="match", state_version=dep.state_version),
        )
    else:
        updates = {}
        if transition == "text":
            updates["content"] = original.payload.content.model_copy(update={"text": "B"})
        elif transition == "metadata":
            updates["metadata"] = original.payload.metadata.model_copy(update={"title": "B"})
        else:
            updates["content"] = original.payload.content.model_copy(
                update={
                    "passage_policy": "supplied-anchors/1",
                    "anchors": (
                        SuppliedAnchor(local_id="whole", start=0, end=len(TEXT), quote=TEXT),
                    ),
                }
            )
        payload = original.payload.model_copy(
            update={
                "precondition": ExpectedState(kind="match", state_version=dep.state_version),
                **updates,
            }
        )
    changed = receipt(
        env.service.write(
            original.model_copy(
                update={
                    "retry_key": str(uuid4()),
                    "payload": payload,
                }
            )
        )
    )
    if withdraw:
        assert env.service.write(withdrawal(env, ids["decision"])).status == "applied"
    _, restored, restored_page = passage(env, state=changed.processing.state_version)
    assert restored.processing.state_version != dep.state_version
    service = KnowledgeService(env.database, env.service.identity)
    with pytest.raises(EvidenceServiceError, match="not_found"):
        service.entity(env.scope, ids["project"])
    assert not service.contribution(env.scope, ids["mention"], mode="history").is_current
    assert env.service.write(req).receipt == outcome.receipt
    new_support, new_dep = support(restored, restored_page)
    stored = StoredEntity(kind="stored", entity_id=ids["project"])
    renewed = request(
        env,
        (
            mention(new_support, stored),
            AddEntitySupport(
                kind="entity_support",
                local_id="support",
                entity=stored,
                name="Atlas",
                support=new_support,
            ),
        ),
        (new_dep,),
    )
    new_ids = mappings(env.service.write(renewed))
    assert service.contribution(env.scope, new_ids["mention"]).is_current
    assert not service.contribution(env.scope, ids["mention"], mode="history").is_current
    assert env.service.citation(env.scope, page.entries[0].citation).quote == TEXT
    historical = service.contribution(env.scope, ids["decision"], mode="history")
    assert not historical.is_current
    assert (historical.withdrawal is not None) == withdraw


@pytest.mark.parametrize("revoked", ["read", "write_knowledge", "binding"])
@pytest.mark.parametrize("replay", [False, True])
def test_withdrawal_two_namespace_authority_and_report_redaction(env, revoked, replay):
    _, a, page = passage(env)
    _, b, other = passage(env, "b", "email")
    sa, da = support(a, page)
    sb, db = support(b, other)
    both = SourceSupport(kind="source", evidence=sa.evidence + sb.evidence)
    creation = request(
        env, (entity(sa), decision(both, LocalEntity(kind="local", local_id="project"))),
        (da, db),
    )
    target = mappings(env.service.write(creation))["decision"]
    req = withdrawal(env, target)
    explained = env.service.write_explained(req) if replay else None
    if explained:
        assert explained.outcome.status == "applied"
        assert TEXT not in explained.report.model_dump_json()
    narrowed = env.scope.model_copy(update={
        "access": env.scope.access.model_copy(update={"namespaces": ("markdown",)}),
    })
    with env.database.connection() as c:
        keys = c.execute("SELECT count(*) FROM write_key").fetchone()[0]
    narrow_result = env.service.write(req.model_copy(update={"scope": narrowed}))
    assert narrow_result.error.code == "not_found"
    with env.database.connection() as c:
        assert c.execute("SELECT count(*) FROM write_key").fetchone()[0] == keys
        assert c.execute("SELECT count(*) FROM assertion_withdrawal").fetchone()[0] == int(replay)
    if revoked == "binding":
        policy = env.policy.model_copy(update={
            "knowledge_bindings": tuple(
                b for b in env.policy.knowledge_bindings if b.namespace != "email"
            ),
        })
    else:
        policy = env.policy.model_copy(update={
            "grants": tuple(
                g for g in env.policy.grants
                if not (g.namespace == "email" and g.grant == revoked)
            ),
        })
    version = env.admin.replace_policy(policy, env.scope.access.policy_version).policy_version
    denied = env.scope.model_copy(update={
        "access": env.scope.access.model_copy(update={"policy_version": version}),
    })
    result = env.service.write(req.model_copy(update={"scope": denied}))
    assert result.error.code == "forbidden"
    assert target not in result.model_dump_json()
    if explained:
        assert not env.service.diagnostics.for_request(denied, req.request_id).entries
    with env.database.connection() as c:
        assert c.execute("SELECT count(*) FROM assertion_withdrawal").fetchone()[0] == int(replay)


def test_withdrawal_requires_historical_endpoint_even_outside_assertion_support(env):
    _, a, page = passage(env)
    _, b, other = passage(env, "b", "email")
    sa, da = support(a, page)
    sb, db = support(b, other)
    target = mappings(env.service.write(request(
        env, (entity(sb), decision(sa, LocalEntity(kind="local", local_id="project"))), (da, db),
    )))["decision"]
    narrowed = env.scope.model_copy(update={
        "access": env.scope.access.model_copy(update={"namespaces": ("markdown",)}),
    })
    assert env.service.write(withdrawal(env, target, scope=narrowed)).error.code == "not_found"
    assert env.service.write(withdrawal(env, target)).status == "applied"
    assert env.service.write(withdrawal(env, target, scope=narrowed)).error.code == "not_found"


@pytest.mark.parametrize(
    "field", ["passage_id", "anchor_id", "revision_id", "document_id", "namespace"]
)
def test_forged_chain_rejected_before_any_knowledge_mutation(env, field):
    _, a, page = passage(env)
    _, b, other = passage(env, "b", "email")
    evidence, dep = support(a, page)
    foreign = other.entries[0].reference
    key = "source_namespace" if field == "namespace" else field
    bad_ref = evidence.evidence[0].model_copy(update={key: getattr(foreign, key)})
    bad_support = SourceSupport(kind="source", evidence=(bad_ref,))
    bad_dep = dep.model_copy(
        update={
            "source_namespace": bad_ref.source_namespace,
            "document_id": bad_ref.document_id,
            "revision_id": bad_ref.revision_id,
        }
    )
    req = request(
        env,
        (
            entity(bad_support),
            mention(bad_support, LocalEntity(kind="local", local_id="project")),
        ),
        (bad_dep,),
    )
    before = inventory(env)
    failed = env.service.write(req)
    assert failed.error.code in {"not_found", "state_conflict"} and failed.receipt is None
    assert inventory(env) == before


def test_same_revision_passage_from_wrong_policy_membership_is_not_accepted(env):
    _, first, first_page = passage(env, policy="supplied-anchors/1")
    _, second, second_page = passage(
        env,
        state=first.processing.state_version,
        policy="codepoint-window/1",
    )
    assert first.revision_id == second.revision_id
    old, _ = support(first, first_page)
    current, dep = support(second, second_page)
    bad = SourceSupport(kind="source", evidence=(old.evidence[0],))
    req = request(
        env,
        (
            entity(current),
            mention(bad, LocalEntity(kind="local", local_id="project")),
        ),
        (dep,),
    )
    before = inventory(env)
    assert env.service.write(req).error.code == "not_found"
    assert inventory(env) == before


def test_cross_corpus_passage_forgery_and_scope_revocation_fail_closed(env):
    registered = env.admin.register(
        CorpusRegistration(
            corpus_id="foreign",
            namespaces=("markdown", "email"),
            policy=env.policy.model_copy(update={"corpus_id": "foreign"}),
        )
    )
    original_scope = env.scope
    env.scope = env.scope.model_copy(
        update={
            "corpus_id": "foreign",
            "access": env.scope.access.model_copy(
                update={
                    "policy_version": registered.policy_version,
                }
            ),
        }
    )
    _, saved, page = passage(env)
    evidence, dep = support(saved, page)
    env.scope = original_scope
    forged = SourceSupport(
        kind="source",
        evidence=(evidence.evidence[0].model_copy(update={"corpus_id": original_scope.corpus_id}),),
    )
    before = inventory(env)
    result = env.service.write_explained(request(env, (entity(forged),), (dep,)))
    assert result.outcome.error.code == "not_found" and result.report.state != "collected"
    assert inventory(env) == before
    _, saved, page = passage(env)
    evidence, dep = support(saved, page)
    req = request(env, (entity(evidence),), (dep,))
    old = env.policy
    revoked = old.model_copy(
        update={
            "grants": tuple(
                g for g in old.grants if not (g.namespace == "markdown" and g.grant == "read")
            )
        }
    )
    version = env.admin.replace_policy(revoked, env.scope.access.policy_version).policy_version
    assert env.service.write(req).error.code == "forbidden"
    version = env.admin.replace_policy(old, version).policy_version
    env.scope = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(update={"policy_version": version}),
        }
    )
    assert (
        env.service.write(req.model_copy(update={"scope": env.scope})).error.code
        == "state_conflict"
    )


def test_real_transaction_validation_lifetime_and_exact_quotes(env):
    _, saved, page = passage(env)
    evidence, dep = support(saved, page)
    with writing(env.database, env.service.identity) as context:
        validator = TransactionEvidence(context)
        validated = validator.validate_current(env.scope, (dep,), evidence.evidence)
        assert validated[0].quote == TEXT
        assert validated[0].metadata_snapshot_id == page.entries[0].citation.metadata_snapshot_id
    with pytest.raises(EvidenceServiceError, match="invalid_request"):
        validator.validate_current(env.scope, (dep,), evidence.evidence)


def test_late_receipt_failure_rolls_back_every_variant_and_batch_units_are_independent(
    env, monkeypatch
):
    import kg.knowledge._write as writes

    _, saved, page = passage(env)
    evidence, dep = support(saved, page)
    target_id = mappings(env.service.write(request(env, (entity(evidence),), (dep,))))["project"]
    req = request(env, variants(evidence, StoredEntity(kind="stored", entity_id=target_id)), (dep,))
    original = writes.save
    before = inventory(env)
    budgets = []

    def fail(context, *args):
        original(context, *args)
        budgets.append(context.connection._budget)
        assert context.connection._budget._root._scratch > len(TEXT.encode()) * 4
        raise sqlite3.OperationalError("after-receipt")

    monkeypatch.setattr(writes, "save", fail)
    assert env.service.write(req).error.code == "internal_error"
    assert inventory(env) == before and budgets[0]._root._scratch == 0
    monkeypatch.setattr(writes, "save", original)
    stale = req.model_copy(
        update={
            "retry_key": str(uuid4()),
            "request_id": str(uuid4()),
            "payload": req.payload.model_copy(
                update={
                    "dependencies": (dep.model_copy(update={"state_version": "absent"}),),
                }
            ),
        }
    )
    another = req.model_copy(update={"retry_key": str(uuid4()), "request_id": str(uuid4())})
    result = env.service.write_batch(
        WriteBatch(
            contract_version="foundation/1",
            batch_id="passages",
            items=(req, stale, another),
        )
    )
    assert [r.status for r in result.outcomes] == ["applied", "conflict", "applied"]
    assert mappings(result.outcomes[0]) != mappings(result.outcomes[2])


def _process_write(path, request_json, queue):
    from pathlib import Path

    service = EvidenceService(EvidenceDatabase(Path(path)), LocalIdentity(principal_id="principal"))
    queue.put(service.write(WriteRequest.model_validate_json(request_json)).model_dump_json())


def test_two_process_retry_convergence_reopen_expiry_and_uncertain_commit(env, monkeypatch):
    from kg.evidence._sql import AccountedConnection

    _, saved, page = passage(env)
    evidence, dep = support(saved, page)
    req = request(
        env,
        (
            entity(evidence),
            mention(evidence, LocalEntity(kind="local", local_id="project")),
        ),
        (dep,),
    )
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    workers = [
        context.Process(
            target=_process_write,
            args=(str(env.database.path), req.model_dump_json(), queue),
        )
        for _ in range(2)
    ]
    for worker in workers:
        worker.start()
    outcomes = [WriteOutcome.model_validate_json(queue.get(timeout=30)) for _ in workers]
    for worker in workers:
        worker.join(timeout=30)
        assert worker.exitcode == 0
    queue.close()
    assert mappings(outcomes[0]) == mappings(outcomes[1])
    restarted = EvidenceService(env.database, env.service.identity)
    assert restarted.write(req).receipt == outcomes[0].receipt
    original = AccountedConnection.commit

    def commit_then_fail(connection):
        original(connection)
        raise sqlite3.OperationalError("lost-commit-result")

    new_req = req.model_copy(update={"retry_key": str(uuid4())})
    monkeypatch.setattr(AccountedConnection, "commit", commit_then_fail)
    assert restarted.write(new_req).error.code == "internal_error"
    monkeypatch.setattr(AccountedConnection, "commit", original)
    mappings(restarted.write(new_req))
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM mention").fetchone()[0] == 2
    at = datetime(2030, 1, 1, tzinfo=UTC)
    restarted._clock = lambda: at
    timed = req.model_copy(update={"retry_key": "expiry"})
    mappings(restarted.write(timed))
    changed = timed.model_copy(
        update={
            "payload": timed.payload.model_copy(
                update={"changes": (entity(evidence, name="Changed"),)}
            ),
        }
    )
    assert restarted.write(changed).error.code == "retry_conflict"
    restarted._clock = lambda: at + timedelta(days=30)
    assert restarted.write(changed).error.code == "retry_expired"
    restarted._clock = lambda: at
    assert restarted.write(timed).error.code == "retry_expired"


def test_owner_transaction_serializes_source_edit_after_passage_validation(env, monkeypatch):
    import kg.knowledge._write as writes

    original, saved, page = passage(env)
    evidence, dep = support(saved, page)
    req = request(
        env,
        (
            entity(evidence),
            mention(evidence, LocalEntity(kind="local", local_id="project")),
        ),
        (dep,),
    )
    entered, release, attempting = Event(), Event(), Event()
    save = writes.save

    def paused(*args):
        entered.set()
        assert release.wait(10)
        return save(*args)

    def edit():
        attempting.set()
        return env.service.write(
            original.model_copy(
                update={
                    "retry_key": str(uuid4()),
                    "payload": original.payload.model_copy(
                        update={
                            "precondition": ExpectedState(
                                kind="match", state_version=dep.state_version
                            ),
                            "metadata": original.payload.metadata.model_copy(
                                update={"title": "changed"}
                            ),
                        }
                    ),
                }
            )
        )

    monkeypatch.setattr(writes, "save", paused)
    with ThreadPoolExecutor(2) as pool:
        pending = pool.submit(env.service.write, req)
        assert entered.wait(10)
        edited = pool.submit(edit)
        assert attempting.wait(10) and not edited.done()
        release.set()
        outcome = pending.result(timeout=15)
        ids = mappings(outcome)
        receipt(edited.result(timeout=15))
    service = KnowledgeService(env.database, env.service.identity)
    assert not service.contribution(env.scope, ids["mention"], mode="history").is_current
    assert env.service.write(req).receipt == outcome.receipt


def test_passage_snapshot_cache_preserves_complete_bundle_bounds_and_lifetime(env):
    value, a, page = passage(env)
    _, b, other = passage(env, "b", "email")
    sa, da = support(a, page)
    sb, db = support(b, other)
    both = SourceSupport(kind="source", evidence=sa.evidence + sb.evidence)
    target = LocalEntity(kind="local", local_id="project")
    ids = mappings(
        env.service.write(
            request(
                env,
                (
                    entity(both),
                    decision(both, target),
                ),
                (da, db),
            )
        )
    )
    with reader(env) as (adapter, context, meter, observer):
        cursor = adapter.select_decisions(ids["project"])
        member = cursor.read().items[0]
        cursor.close()
        adapter.revalidate_member(member)
        baseline = meter.public_accounting().items_consumed
        adapter.revalidate_member(member)
        assert meter.public_accounting().items_consumed == baseline == 1
        cache = adapter._revalidation
        assert len(cache.proofs) == 2 and context.meter.private_budget._scratch > 0
        complete = member.dependencies.assertion_support
        for changed in (
            complete[:1],
            complete[::-1],
            (
                complete[0].model_copy(update={"metadata_snapshot_id": "forged"}),
                complete[1],
            ),
        ):
            altered = member.model_copy(
                update={
                    "record": member.record.model_copy(
                        update={
                            "support": SourceSupport(
                                kind="source", evidence=tuple(p.reference for p in changed)
                            ),
                        }
                    ),
                    "dependencies": member.dependencies.model_copy(
                        update={"assertion_support": changed}
                    ),
                }
            )
            with pytest.raises(EvidenceServiceError, match="state_changed"):
                adapter.revalidate_member(altered)
        witness = member.dependencies.subject_witness
        forged = member.model_copy(
            update={
                "dependencies": member.dependencies.model_copy(
                    update={
                        "subject_witness": witness.model_copy(
                            update={
                                "contribution_sequence": witness.contribution_sequence + 1,
                            }
                        ),
                    }
                ),
            }
        )
        with pytest.raises(EvidenceServiceError, match="state_changed"):
            adapter.revalidate_member(forged)
        with (
            reader(env) as (_, other_context, _, _),
            pytest.raises(EvidenceServiceError, match="invalid_request"),
        ):
            Store(
                other_context.connection,
                env.scope,
                other_context.meter.private_budget,
                context=other_context,
                cache=cache,
            )
        receipt(
            env.service.write(
                value.model_copy(
                    update={
                        "retry_key": str(uuid4()),
                        "payload": value.payload.model_copy(
                            update={
                                "precondition": ExpectedState(
                                    kind="match", state_version=da.state_version
                                ),
                                "metadata": value.payload.metadata.model_copy(
                                    update={"title": "changed"}
                                ),
                            }
                        ),
                    }
                )
            )
        )
        adapter.revalidate_member(member)  # Snapshot-local proof only, not release authority.
        with (
            pytest.raises(EvidenceServiceError, match="state_changed"),
            release_fence(observer, env.service.identity, env.scope, observer.budget.deadline),
        ):
            pass
        root = context.meter.private_budget
        root._visits = root._max_visits
        with pytest.raises(PrivateResourceStop):
            adapter.revalidate_member(member)
    assert cache.closed and not cache.proofs and not cache.witnesses and not cache.reservations
    with pytest.raises(EvidenceServiceError, match="invalid_request"):
        adapter.revalidate_member(member)
    with (
        reader(env) as (fresh, _, _, _),
        pytest.raises(EvidenceServiceError, match="not_found"),
    ):
        fresh.revalidate_member(member)


def test_mentions_cannot_activate_stale_or_inaccessible_endpoints(env):
    original, a, page = passage(env)
    sa, da = support(a, page)
    ids = mappings(env.service.write(request(env, (entity(sa),), (da,))))
    target = StoredEntity(kind="stored", entity_id=ids["project"])
    _, b, other = passage(env, "b", "email")
    sb, db = support(b, other)
    fresh = request(env, (mention(sb, target),), (db,))
    narrow = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(update={"namespaces": ("email",)}),
        }
    )
    before = inventory(env)
    assert env.service.write(fresh.model_copy(update={"scope": narrow})).error.code == "not_found"
    assert inventory(env) == before
    receipt(
        env.service.write(
            original.model_copy(
                update={
                    "retry_key": str(uuid4()),
                    "payload": original.payload.model_copy(
                        update={
                            "precondition": ExpectedState(
                                kind="match", state_version=da.state_version
                            ),
                            "metadata": original.payload.metadata.model_copy(
                                update={"title": "changed"}
                            ),
                        }
                    ),
                }
            )
        )
    )
    before = inventory(env)
    assert env.service.write(fresh).error.code == "state_conflict"
    assert inventory(env) == before
    service = KnowledgeService(env.database, env.service.identity)
    with pytest.raises(EvidenceServiceError, match="not_found"):
        service.entity(env.scope, target.entity_id)


def test_revoked_support_history_replay_and_namespace_policy_roundtrip(env):
    _, a, page = passage(env)
    _, b, other = passage(env, "b", "email")
    sa, da = support(a, page)
    sb, db = support(b, other)
    both = SourceSupport(kind="source", evidence=sa.evidence + sb.evidence)
    req = request(
        env,
        (
            entity(sb),
            mention(both, LocalEntity(kind="local", local_id="project")),
        ),
        (da, db),
    )
    outcome = env.service.write(req)
    ids = mappings(outcome)
    service = KnowledgeService(env.database, env.service.identity)
    revoked = env.policy.model_copy(
        update={
            "grants": tuple(
                g
                for g in env.policy.grants
                if not (g.namespace == "markdown" and g.grant == "read")
            )
        }
    )
    version = env.admin.replace_policy(revoked, env.scope.access.policy_version).policy_version
    denied = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(update={"policy_version": version}),
        }
    )
    with pytest.raises(EvidenceServiceError, match="forbidden"):
        service.contribution(denied, ids["mention"], mode="history")
    assert env.service.write(req.model_copy(update={"scope": denied})).error.code == "forbidden"
    version = env.admin.replace_policy(env.policy, version).policy_version
    env.scope = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(update={"policy_version": version}),
        }
    )
    assert service.entity(env.scope, ids["project"]).is_current
    assert not service.contribution(env.scope, ids["mention"], mode="history").is_current
    assert env.service.write(req.model_copy(update={"scope": env.scope})).receipt == outcome.receipt
    with pytest.raises(EvidenceServiceError, match="not_found"):
        service.contribution(env.scope, ids["mention"])


def test_mixed_anchor_passage_conjunction_and_mention_page_boundaries(env):
    _, saved, page = passage(env, policy="supplied-anchors/1")
    passage_support, dep = support(saved, page)
    anchor = (
        env.service.anchors(
            env.scope,
            saved.document_id,
            saved.processing.state_version,
        )
        .entries[0]
        .reference
    )
    mixed = SourceSupport(kind="source", evidence=(anchor,) + passage_support.evidence)
    ids = mappings(env.service.write(request(env, (entity(mixed),), (dep,))))
    target = StoredEntity(kind="stored", entity_id=ids["project"])
    changes = tuple(mention(passage_support, target, f"m{i}") for i in range(3))
    committed = mappings(env.service.write(request(env, changes, (dep,))))
    service = KnowledgeService(env.database, env.service.identity)
    assert len(service.entity(env.scope, target.entity_id).witness.basis.evidence) == 2
    first = service.contributions(env.scope, target.entity_id, kind="mention", limit=2)
    assert first.has_more and len(first.entries) == 2
    second = service.contributions(
        env.scope,
        target.entity_id,
        kind="mention",
        limit=2,
        after_sequence=first.next_after_sequence,
    )
    assert not second.has_more
    assert [v.contribution_id for v in (*first.entries, *second.entries)] == list(
        committed.values()
    )


def test_passage_selection_inherits_local_page_and_global_scratch_limits(env):
    _, saved, page = passage(env)
    evidence, dep = support(saved, page)
    ids = mappings(env.service.write(request(env, (entity(evidence),), (dep,))))
    target = StoredEntity(kind="stored", entity_id=ids["project"])
    mappings(
        env.service.write(
            request(
                env,
                (
                    decision(evidence, target, "a"),
                    decision(evidence, target, "b"),
                ),
                (dep,),
            )
        )
    )
    with reader(env) as (adapter, context, meter, _):
        cursor = adapter.select_decisions(target.entity_id)
        assert len(cursor.read(limit=1).items) == 1
        assert cursor.budget._root is context.meter.private_budget
        assert cursor.budget._max_visits == 10_000
        cursor.budget.reserve_visits(10_000 - cursor.budget._visits)
        stopped = cursor.read(limit=1)
        assert stopped.items == () and stopped.terminal.kind == "private_resource_stop"
        assert meter.public_accounting().items_consumed == 1
        cursor.close()
    with reader(env) as (adapter, context, meter, _):
        cursor = adapter.select_decisions(target.entity_id)
        member = cursor.read().items[0]
        cursor.close()
        before = meter.public_accounting().items_consumed
        with (
            context.meter.private_budget.reserve_scratch(64 << 20, "general"),
            pytest.raises(PrivateResourceStop),
        ):
            adapter.revalidate_member(member)
        assert meter.public_accounting().items_consumed == before


def test_bulk_and_interactive_select_identical_mixed_support_and_stale_exclusion(env):
    _, saved_a, page_a = passage(env, policy="supplied-anchors/1")
    value_b, saved_b, page_b = passage(env, external="second", namespace="email")
    sa, da = support(saved_a, page_a)
    sb, db = support(saved_b, page_b)
    anchor = env.service.anchors(
        env.scope, saved_a.document_id, saved_a.processing.state_version,
    ).entries[0].reference
    mixed = SourceSupport(kind="source", evidence=(anchor,) + sa.evidence + sb.evidence)
    ids = mappings(env.service.write(request(env, (entity(sa),), (da,))))
    target = StoredEntity(kind="stored", entity_id=ids["project"])
    records = mappings(env.service.write(request(
        env, (decision(mixed, target, "mixed"), decision(sa, target, "independent")), (da, db),
    )))

    def select(bulk):
        operation = _graph_build_operation(
            deadline=Deadline(time.monotonic() + 300), cancel=Event(),
        ) if bulk else None
        budget = operation.budget if operation else PrivateBudget(Deadline(time.monotonic() + 30))
        meter = LocalExecutionMeter(budget, max_operations=1, max_items=100)
        step = operation.meter if operation else meter.begin_step("read")
        with (
            observe(
                env.database, env.service.identity, env.scope, budget.deadline, budget,
            ) as observer,
            read_context(
                env.database, env.service.identity, env.scope, observer.session_id,
                budget.deadline, step,
            ) as context,
        ):
            adapter = KnowledgeReader(context, env.service.identity)
            cursor = adapter.select_decisions(ids["project"])
            try:
                page = cursor.read()
                assert isinstance(page.terminal, EligibleEOF)
                for member in page.items:
                    adapter.revalidate_member(member)
                count = (
                    operation.snapshot().semantic_items_reserved
                    if operation else meter.public_accounting().items_consumed
                )
                assert count == len(page.items)
                return page.items
            finally:
                cursor.close()
                adapter.close()

    ordinary = select(False)
    assert ordinary == select(True)
    assert {item.record.record_id for item in ordinary} == set(records.values())
    combined = next(item for item in ordinary if item.record.record_id == records["mixed"])
    assert combined.record.support == mixed
    assert tuple(p.reference for p in combined.dependencies.assertion_support) == mixed.evidence
    removed = env.service.write(value_b.model_copy(update={
        "retry_key": str(uuid4()),
        "payload": RemoveDocument(
            operation="remove_document", document=value_b.payload.document,
            precondition=ExpectedState(kind="match", state_version=db.state_version),
        ),
    }))
    assert removed.error is None
    remaining = select(False)
    assert remaining == select(True)
    assert [item.record.record_id for item in remaining] == [records["independent"]]
