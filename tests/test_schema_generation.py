"""Selected schema context and bootstrap use real evidence, without model execution."""

import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier

import pytest
from pydantic import ValidationError
from support.evidence import environment, put, receipt

from kg.evidence import EvidenceDatabase, EvidenceServiceError
from kg.knowledge import KnowledgeAdministration, KnowledgeService
from kg.knowledge._registry import proposal_digest, proposal_json
from kg.models.evidence import EvidenceView, LocalAdminAuthority, LocalIdentity
from kg.models.schema import (
    SchemaApplyRequest,
    SchemaApproval,
    SchemaProposal,
    SchemaSample,
)

# Captured using the models at 627caed5622e7cacbc7dfcd6021e33ac86fec634, not the new serializer.
BASELINE = (
    '{"add_entity_types":[{"definition":{"description":"Collected specimen.","name":"specimen"},'
    '"review":{"candidates":[],"defer_assessment":"No automatic facts.","example_ids":["one"],'
    '"extension_rationale":"Source identifies specimens.","no_existing_candidate_reason":'
    '"Unconfigured.","reuse_assessment":"No terms."}}],"add_identifier_schemes":[],'
    '"add_predicates":[],"attribution":{"configuration_id":null,"model_id":null,"owner_id":"owner",'
    '"producer":"test","producer_version":"1","writer_id":"writer"},"base_revision":null,'
    '"corpus_id":"work","examples":[{"example_id":"one","explanation":"Exact specimen example.",'
    '"reference":{"anchor_id":"anchor","corpus_id":"work","document_id":"doc","passage_id":null,'
    '"revision_id":"revision","source_namespace":"markdown"},"state_version":"state"}],'
    '"interface_version":"schema-proposal/1","rationale":"Initial vocabulary.",'
    '"unresolved_concepts":[],"widen_predicates":[]}'
)
BASELINE_DIGEST = "a10bba9669ebece77cc59f46786bf39b78902c6c670c8d249b19eb310c25b53b"


@pytest.fixture
def selected(tmp_path):
    env = environment(tmp_path / "sample.sqlite")
    views = []
    for namespace, text in (
        ("markdown", "Specimen A is a fern.\r\nCafé specimen labels remain exact."),
        ("email", "Only the northern collection was sampled."),
    ):
        source = receipt(env.service.write(put(env.scope, namespace=namespace, text=text)))
        views.append(
            env.service.anchors(
                env.scope,
                source.document_id,
                source.processing.state_version,
            ).entries[0]
        )
    sample = SchemaSample.model_validate_json(
        json.dumps(
            {
                "support": [
                    {
                        "reference": v.reference.model_dump(mode="json"),
                        "state_version": v.state_version,
                    }
                    for v in views
                ],
                "intended_use": "Describe specimens.",
                "selection_rationale": "Operator selected these two excerpts.",
            }
        )
    )
    value = json.loads(BASELINE)
    value["examples"][0].update(sample.support[0].model_dump(mode="json"))
    value["initial_generation"] = {
        "sample": sample.model_dump(mode="json"),
        "coverage_status": "limited",
        "coverage_limitations": ["Northern collection only; selected excerpts, not whole corpus."],
        "synonym_decisions": [
            {
                "surface_forms": ["sample", "specimen"],
                "term": {"kind": "entity_type", "name": "specimen"},
                "rationale": "Use specimen for collected material, not a whole collection.",
            }
        ],
    }
    return env, sample, SchemaProposal.model_validate_json(json.dumps(value)), views


def reader(env):
    return KnowledgeService(env.database, env.service.identity)


def admin(env):
    return KnowledgeAdministration(env.database, LocalAdminAuthority(principal_id="principal"))


def apply_request(env, proposal, key="initial"):
    return SchemaApplyRequest(
        request_id="request",
        retry_key=key,
        scope=env.scope,
        proposal=proposal,
        approved_proposal_digest=proposal_digest(proposal),
        approval=SchemaApproval(
            human_reviewed=True, rationale="Test-controlled explicit approval."
        ),
    )


def dump(env):
    with env.database.connection() as connection:
        return tuple(connection.iterdump())


@pytest.mark.service
def test_exact_brief_validation_apply_and_protected_dependencies(selected):
    env, sample, value, views = selected
    before = dump(env)
    brief = reader(env).schema_generation_context(env.scope, sample)
    exact_views = tuple(
        EvidenceView.model_validate(v.model_dump(exclude={"ordinal"})) for v in views
    )
    assert brief.evidence == exact_views
    assert brief.status == "awaiting_agent" and brief.coverage == "selected_excerpts_only"
    serialized = brief.model_dump(mode="json")["evidence"][0]
    assert {"quote", "start", "end", "quote_hash", "metadata", "citation"} <= serialized.keys()
    validated = reader(env).validate_schema(env.scope, value)
    assert validated.initial_generation == value.initial_generation
    assert len(value.dependencies()) == 2
    assert dump(env) == before
    request = apply_request(env, value)
    saved = admin(env).apply_schema(request)
    assert saved.receipt is not None
    revision = saved.receipt.revision.revision_id
    assert reader(env).schema_change(env.scope, revision).proposal == value
    for view in exact_views:
        assert env.service.citation(env.scope, view.citation) == view
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM entity").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM contribution").fetchone()[0] == 0
    # Reopen-style fresh service, stale sample-only source, committed historical replay.
    receipt(
        env.service.write(
            put(
                env.scope,
                namespace="email",
                text="Updated coverage.",
                state=views[1].state_version,
            )
        )
    )
    assert admin(env).apply_schema(request).receipt == saved.receipt
    assert reader(env).schema_change(env.scope, revision).proposal == value
    changed = value.model_dump(mode="json")
    changed["initial_generation"]["coverage_limitations"] = ["Revised assessment."]
    conflicting = apply_request(env, SchemaProposal.model_validate_json(json.dumps(changed)))
    assert admin(env).apply_schema(conflicting).error.code == "retry_conflict"
    # Removing access only to sample-only email must protect history AND committed replay.
    policy = env.policy.model_copy(
        update={
            "grants": tuple(g for g in env.policy.grants if g.namespace != "email"),
        }
    )
    renewed = env.admin.replace_policy(policy, env.scope.access.policy_version)
    env.scope = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(
                update={
                    "namespaces": ("markdown",),
                    "policy_version": renewed.policy_version,
                }
            )
        }
    )
    with pytest.raises(EvidenceServiceError, match="not_found|forbidden"):
        reader(env).schema_change(env.scope, revision)
    assert admin(env).apply_schema(request.model_copy(update={"scope": env.scope})).receipt is None


@pytest.mark.service
@pytest.mark.parametrize("mutation", ["stale", "scope", "cross-corpus", "missing"])
def test_sample_only_source_and_scope_fail_closed(selected, mutation):
    env, sample, value, views = selected
    if mutation == "stale":
        receipt(
            env.service.write(
                put(
                    env.scope,
                    namespace="email",
                    text="Changed.",
                    state=views[1].state_version,
                )
            )
        )
    elif mutation == "scope":
        env.scope = env.scope.model_copy(
            update={
                "access": env.scope.access.model_copy(update={"namespaces": ("markdown",)}),
            }
        )
    else:
        data = sample.model_dump(mode="json")
        for capture in data["support"]:
            capture["reference"]["corpus_id" if mutation == "cross-corpus" else "anchor_id"] = (
                "nope"
            )
        sample = SchemaSample.model_validate_json(json.dumps(data))
    before = dump(env)
    with pytest.raises(EvidenceServiceError):
        reader(env).schema_generation_context(env.scope, sample)
    if mutation in {"stale", "scope"}:
        with pytest.raises(EvidenceServiceError):
            reader(env).validate_schema(env.scope, value)
        assert admin(env).apply_schema(apply_request(env, value)).receipt is None
    assert dump(env) == before


@pytest.mark.service
@pytest.mark.parametrize(
    "mutation",
    [
        "empty",
        "duplicate",
        "conflicting-state",
        "out-of-sample",
        "missing-limitations",
        "extra",
        "oversize",
        "duplicate-term",
        "bad-scalar",
    ],
)
def test_invalid_shapes_rejected_at_service_boundary(selected, mutation):
    env, sample, value, _ = selected
    data = value.model_dump(mode="json")
    support = data["initial_generation"]["sample"]["support"]
    if mutation == "empty":
        support.clear()
    elif mutation == "duplicate":
        support.append(support[0])
    elif mutation == "conflicting-state":
        added = json.loads(json.dumps(support[0]))
        added["reference"]["anchor_id"] = "another"
        added["state_version"] = "changed"
        support.append(added)
    elif mutation == "out-of-sample":
        data["examples"][0]["state_version"] = "not-selected"
    elif mutation == "missing-limitations":
        data["initial_generation"]["coverage_limitations"] = []
    elif mutation == "extra":
        data["initial_generation"]["sample"]["extract_automatically"] = True
    elif mutation == "oversize":
        data["initial_generation"]["sample"]["intended_use"] = "x" * 4097
    elif mutation == "duplicate-term":
        data["add_entity_types"].append(data["add_entity_types"][0])
    else:
        data["add_predicates"] = [
            {
                "definition": {
                    "name": "shape",
                    "description": "Shape.",
                    "subject_types": ["specimen"],
                    "object_kind": "json",
                },
                "review": data["add_entity_types"][0]["review"],
            }
        ]
    before = dump(env)
    with pytest.raises(ValidationError):
        SchemaProposal.model_validate_json(json.dumps(data))
    forged = sample.model_copy(update={"support": ()})
    with pytest.raises(EvidenceServiceError, match="invalid_request"):
        reader(env).schema_generation_context(env.scope, forged)
    assert dump(env) == before


@pytest.mark.service
@pytest.mark.parametrize("mutation", ["insufficient", "synonym", "endpoint"])
def test_invalid_semantics_never_apply(selected, mutation):
    env, _, value, _ = selected
    data = value.model_dump(mode="json")
    if mutation == "insufficient":
        data["initial_generation"]["coverage_status"] = "insufficient"
    elif mutation == "synonym":
        data["initial_generation"]["synonym_decisions"][0]["term"]["name"] = "unknown"
    else:
        data["add_predicates"] = [
            {
                "definition": {
                    "name": "color",
                    "description": "Color.",
                    "subject_types": ["unknown"],
                    "object_kind": "string",
                },
                "review": data["add_entity_types"][0]["review"],
            }
        ]
    value = SchemaProposal.model_validate_json(json.dumps(data))
    before = dump(env)
    with pytest.raises(EvidenceServiceError, match="invalid_request"):
        reader(env).validate_schema(env.scope, value)
    assert admin(env).apply_schema(apply_request(env, value)).receipt is None
    assert dump(env) == before


@pytest.mark.service
def test_whitespace_and_whole_revision_hydration_bound(selected, monkeypatch):
    from kg._execution_budget import PrivateBudget

    env, sample, _, _ = selected
    doc = receipt(env.service.write(put(env.scope, external="blank", text=" \n\t")))
    anchor = env.service.anchors(env.scope, doc.document_id, doc.processing.state_version).entries[
        0
    ]
    blank = sample.model_dump(mode="json")
    blank["support"] = [
        {
            "reference": anchor.reference.model_dump(mode="json"),
            "state_version": anchor.state_version,
        }
    ]
    with pytest.raises(EvidenceServiceError, match="invalid_request"):
        reader(env).schema_generation_context(
            env.scope,
            SchemaSample.model_validate_json(json.dumps(blank)),
        )
    original = PrivateBudget.reserve_scratch
    charges = []

    def cap(self, size, unit):
        charges.append((size, unit))
        if unit == "text":
            # Exercise the unchanged real 8 MiB reservation rejection, without huge fixtures.
            return original(self, (8 << 20) + 1, unit)
        return original(self, size, unit)

    monkeypatch.setattr(PrivateBudget, "reserve_scratch", cap)
    with pytest.raises(EvidenceServiceError, match="budget_exceeded"):
        reader(env).schema_generation_context(env.scope, sample)
    assert any(unit == "text" for _, unit in charges)


@pytest.mark.service
def test_accumulated_output_reservations_remain_owned(selected, monkeypatch):
    from kg._execution_budget import PrivateBudget, PrivateResourceStop
    from kg.knowledge import _schema_operations

    env, sample, _, _ = selected
    original = PrivateBudget.reserve_scratch
    hydrate = _schema_operations.evidence_view
    contexts, held = [], []

    def retain(*args, **kwargs):
        context = kwargs["context"]
        before = len(context._scratch)
        result = hydrate(*args, **kwargs)
        contexts.append(context)
        # These are the four quotation-hydration reservations, not Store.proof's cache.
        held.append(tuple(context._scratch[before:]))
        assert len(held[-1]) == 4
        assert all(not reservation._released for group in held for reservation in group)
        return result

    def exhaust(self, size, unit):
        if len(held) == 2 and unit == "general":
            assert all(not reservation._released for group in held for reservation in group)
            assert self._root._scratch >= sum(r._size for group in held for r in group)
            raise PrivateResourceStop()
        return original(self, size, unit)

    monkeypatch.setattr(_schema_operations, "evidence_view", retain)
    monkeypatch.setattr(PrivateBudget, "reserve_scratch", exhaust)
    with pytest.raises(EvidenceServiceError, match="budget_exceeded"):
        reader(env).schema_generation_context(env.scope, sample)
    assert len(held) == 2
    assert all(r._released for group in held for r in group)
    assert all(r._budget._root._scratch == 0 for group in held for r in group)
    assert all(not context._active for context in contexts)


@pytest.mark.service
@pytest.mark.parametrize("mutation", ["source", "schema", "policy"])
def test_generation_release_fence_withholds_context(selected, monkeypatch, mutation):
    from kg.knowledge import service as module

    env, sample, value, views = selected
    fence = module.release_fence

    @contextmanager
    def change(*args, **kwargs):
        if mutation == "source":
            receipt(
                env.service.write(
                    put(
                        env.scope,
                        namespace="email",
                        state=views[1].state_version,
                        text="New.",
                    )
                )
            )
        elif mutation == "schema":
            assert admin(env).apply_schema(apply_request(env, value)).receipt
        else:
            env.admin.replace_policy(
                env.policy.model_copy(update={"grants": ()}),
                env.scope.access.policy_version,
            )
        with fence(*args, **kwargs) as guard:
            yield guard

    monkeypatch.setattr(module, "release_fence", change)
    with pytest.raises(EvidenceServiceError, match="state_changed|forbidden"):
        reader(env).schema_generation_context(env.scope, sample)


@pytest.mark.process
def test_competing_initial_apply_and_retry_have_one_genesis(selected):
    env, _, value, _ = selected
    barrier = Barrier(2)

    def run(key):
        barrier.wait()
        return admin(env).apply_schema(apply_request(env, value, key))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(run, ["one", "two"]))
    assert sorted(result.status for result in results) == ["applied", "conflict"]
    for result, key in zip(results, ["one", "two"], strict=True):
        if result.receipt:
            assert admin(env).apply_schema(apply_request(env, value, key)).receipt == result.receipt
    with env.database.connection() as connection:
        assert (
            connection.execute("SELECT count(*) FROM knowledge_schema_revision").fetchone()[0] == 1
        )


@pytest.mark.service
def test_literal_baseline_serialization_digest_and_historical_replay(selected, tmp_path):
    from kg.knowledge._selection import CapturedEvidence

    env, _, _, _ = selected
    baseline = SchemaProposal.model_validate_json(BASELINE)
    assert proposal_json(baseline) == BASELINE
    assert proposal_digest(baseline) == BASELINE_DIGEST
    assert proposal_json(baseline.model_copy(update={"initial_generation": None})) == BASELINE
    nested = apply_request(env, baseline).model_dump(mode="json")["proposal"]
    assert "initial_generation" not in nested
    assert nested["base_revision"] is None and nested["attribution"]["model_id"] is None
    assert set(CapturedEvidence.model_fields) == {
        "reference",
        "dependency",
        "metadata_snapshot_id",
        "namespace_token",
    }
    fixture = json.loads(
        (Path(__file__).parent / "fixtures/schema-bootstrap-baseline.json").read_text()
    )
    assert fixture["baseline"] == "627caed5622e7cacbc7dfcd6021e33ac86fec634"
    database = EvidenceDatabase(tmp_path / "old-store.sqlite")
    database.initialize()
    with database.connection() as connection:
        connection.execute("BEGIN")
        connection.execute("PRAGMA defer_foreign_keys=ON")
        for statement in fixture["inserts"]:
            connection.execute(statement)
        connection.commit()
        before = tuple(connection.iterdump())
    # No current apply created these rows: they came from actual baseline service execution.
    reopened = EvidenceDatabase(database.path)
    reopened.initialize()
    request = SchemaApplyRequest.model_validate_json(json.dumps(fixture["request"]))
    administrator = KnowledgeAdministration(
        reopened,
        LocalAdminAuthority(principal_id="principal"),
    )
    replayed = administrator.apply_schema(request)
    assert replayed.receipt.model_dump(mode="json") == fixture["receipt"]
    old_change = KnowledgeService(reopened, LocalIdentity(principal_id="principal")).schema_change(
        request.scope,
        replayed.receipt.revision.revision_id,
    )
    assert old_change.proposal == request.proposal
    with reopened.connection() as connection:
        assert tuple(connection.iterdump()) == before


@pytest.mark.service
def test_nested_forgery_policy_aba_and_canonical_order(selected):
    env, sample, value, _ = selected
    forged = value.model_copy(
        update={
            "initial_generation": value.initial_generation.model_copy(
                update={
                    "sample": sample.model_copy(update={"support": ()}),
                }
            ),
        }
    )
    with pytest.raises(EvidenceServiceError, match="invalid_request"):
        reader(env).validate_schema(env.scope, forged)
    with pytest.raises(EvidenceServiceError, match="invalid_request"):
        admin(env).apply_schema(apply_request(env, value).model_copy(update={"proposal": forged}))
    data = value.model_dump(mode="json")
    generation = data["initial_generation"]
    generation["coverage_limitations"].append("No winter collections.")
    original = SchemaProposal.model_validate_json(json.dumps(data))
    generation["sample"]["support"].reverse()
    generation["coverage_limitations"].reverse()
    generation["synonym_decisions"][0]["surface_forms"].reverse()
    reordered = SchemaProposal.model_validate_json(json.dumps(data))
    assert proposal_digest(reordered) == proposal_digest(original)
    generation["coverage_limitations"][0] = "Changed interpretation."
    assert proposal_digest(SchemaProposal.model_validate_json(json.dumps(data))) != proposal_digest(
        original,
    )
    # Revoke/restore a sample-only namespace; renewed outer permission cannot repair old captures.
    restricted = env.policy.model_copy(
        update={
            "grants": tuple(g for g in env.policy.grants if g.namespace != "email"),
        }
    )
    revoked = env.admin.replace_policy(restricted, env.scope.access.policy_version)
    restored = env.admin.replace_policy(env.policy, revoked.policy_version)
    env.scope = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(
                update={"policy_version": restored.policy_version}
            ),
        }
    )
    with pytest.raises(EvidenceServiceError, match="state_conflict"):
        reader(env).validate_schema(env.scope, value)
    assert admin(env).apply_schema(apply_request(env, value)).receipt is None
