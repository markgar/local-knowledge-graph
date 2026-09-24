from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from support.evidence import environment, put, receipt

from kg._execution_budget import (
    Deadline,
    LocalExecutionMeter,
    PrivateBudget,
    PrivateResourceStop,
    PublicBudgetStop,
)
from kg.evidence._read_context import observe, read_context, read_evidence, release_fence
from kg.evidence._reads import evidence_view
from kg.evidence._support import TransactionEvidence
from kg.evidence._transactions import writing
from kg.evidence.errors import EvidenceServiceError
from kg.indexing._passages import PassagePolicyError, prepare, produce, publish
from kg.models.evidence import LocalIdentity, LocalPolicy, PolicyGrant
from kg.models.execution import ExecutionReport, ExplainOptions
from kg.models.foundation import (
    DocumentDependency,
    ExpectedState,
    RemoveDocument,
    SuppliedAnchor,
    SuppliedContent,
)
from kg.models.indexing import PassagePage
from kg.models.indexing_events import IndexPhase


def request(
    env, *, text="A\r\nCafe\u0301 \U0001f680", policy="codepoint-window/1", anchors=(), **kwargs
):
    value = put(env.scope, text=text, **kwargs)
    return value.model_copy(
        update={
            "payload": value.payload.model_copy(
                update={
                    "content": SuppliedContent(text=text, passage_policy=policy, anchors=anchors),
                }
            )
        }
    )


def build(env, value):
    saved = receipt(env.service.write(value))
    produced = produce(
        env.database,
        env.service.identity,
        env.scope,
        value.attribution,
        saved.document_id,
        saved.processing.state_version,
    )
    page = env.service.passages(env.scope, saved.document_id, saved.processing.state_version)
    return saved, produced, page


def inventory(env):
    with env.database.connection() as connection:
        return {
            table: tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table}"))
            for table in (
                "anchor",
                "anchor_set_member",
                "passage_policy_definition",
                "passage_set",
                "passage",
                "passage_set_member",
                "state_passage_set",
                "processing_state",
            )
        }


@pytest.mark.service
@pytest.mark.parametrize("text", ["", " ", "a" * 1023 + "\r\nCafe\u0301 \U0001f680\0" + "b" * 1024])
def test_exact_windows_empty_sets_reuse_and_pending_index(tmp_path, text):
    env = environment(tmp_path / "passages.db")
    value = request(env, text=text)
    saved = receipt(env.service.write(value))
    before = env.service.passages(env.scope, saved.document_id, saved.processing.state_version)
    assert before.status == "not_processed" and before.passage_set_id is None
    result = produce(
        env.database,
        env.service.identity,
        env.scope,
        value.attribution,
        saved.document_id,
        saved.processing.state_version,
    )
    page = env.service.passages(env.scope, saved.document_id, saved.processing.state_version)
    assert page.status == "complete"
    assert page.passage_set_id == result.passage_set_id
    assert result.member_count == (len(text) + 1023) // 1024
    assert "".join(entry.quote for entry in page.entries) == text
    for ordinal, entry in enumerate(page.entries, 1):
        assert (entry.ordinal, entry.start, entry.end) == (
            ordinal,
            (ordinal - 1) * 1024,
            min(ordinal * 1024, len(text)),
        )
        assert entry.is_current_support and not entry.member_of_current_anchor_set
        assert env.service.citation(env.scope, entry.citation).quote == entry.quote
        anchor = entry.reference.model_copy(update={"passage_id": None})
        assert env.service.evidence(env.scope, anchor).is_current_support
    assert (
        env.service.anchors(
            env.scope,
            saved.document_id,
            saved.processing.state_version,
        ).entries
        == ()
    )
    revision = env.service.revision_anchors(env.scope, saved.document_id, saved.revision_id)
    assert [e.reference.anchor_id for e in revision.entries] == [
        e.reference.anchor_id for e in page.entries
    ]
    snapshot = inventory(env)
    again = produce(
        env.database,
        env.service.identity,
        env.scope,
        value.attribution,
        saved.document_id,
        saved.processing.state_version,
    )
    assert again.outcome == "reused" and inventory(env) == snapshot
    current = env.service.document(env.scope, saved.document_id)
    assert current.processing.indexing == current.processing.enrichment == "pending"
    assert {"search", "indexing"} <= set(env.service.capabilities().unsupported)
    assert not hasattr(env.service, "process") and not hasattr(env.service, "search")
    assert PassagePage.model_validate_json(page.model_dump_json()) == page


@pytest.mark.service
def test_supplied_overlap_order_reuse_and_generated_local_id_domain(tmp_path):
    env = environment(tmp_path / "supplied.db")
    text = "abcde"
    anchors = (
        SuppliedAnchor(local_id="z", start=1, end=4, quote="bcd"),
        SuppliedAnchor(local_id="e3-window-0", start=0, end=5, quote=text),
        SuppliedAnchor(local_id="a", start=1, end=4, quote="bcd"),
    )
    saved, _, page = build(
        env,
        request(
            env,
            text=text,
            policy="supplied-anchors/1",
            anchors=anchors,
        ),
    )
    assert [e.local_id for e in page.entries] == ["e3-window-0", "a", "z"]
    supplied = env.service.anchors(env.scope, saved.document_id, saved.processing.state_version)
    assert [e.reference.anchor_id for e in page.entries] == [
        e.reference.anchor_id for e in supplied.entries
    ]
    assert all(e.member_of_current_anchor_set for e in page.entries)
    new, _, generated = build(
        env,
        request(
            env,
            text=text,
            anchors=anchors,
            state=saved.processing.state_version,
        ),
    )
    assert new.revision_id == saved.revision_id
    assert generated.entries[0].local_id == page.entries[0].local_id
    assert generated.entries[0].reference.anchor_id != page.entries[0].reference.anchor_id
    assert env.service.citation(env.scope, page.entries[0].citation).quote == text


@pytest.mark.service
@pytest.mark.parametrize(
    "policy,text,detail",
    [
        ("future/1", "text", "unsupported_policy"),
        ("supplied-anchors/1", "text", "boundaries_required"),
    ],
)
def test_unsupported_policy_is_explicit_and_never_partial(tmp_path, policy, text, detail):
    env = environment(tmp_path / "unsupported.db")
    value = request(env, text=text, policy=policy)
    saved = receipt(env.service.write(value))
    before = inventory(env)
    with pytest.raises(PassagePolicyError) as error:
        produce(
            env.database,
            env.service.identity,
            env.scope,
            value.attribution,
            saved.document_id,
            saved.processing.state_version,
        )
    assert error.value.failure.code == "unsupported" and error.value.detail == detail
    assert inventory(env) == before


@pytest.mark.service
def test_empty_supplied_set_is_published(tmp_path):
    env = environment(tmp_path / "empty.db")
    _, result, page = build(env, request(env, text="", policy="supplied-anchors/1"))
    assert result.member_count == 0 and page.status == "complete"


@pytest.mark.service
def test_pagination_saved_metadata_and_policy_roundtrip(tmp_path):
    env = environment(tmp_path / "history.db")
    value = request(env, text="a" * 2049)
    saved, _, original = build(env, value)
    first = env.service.passages(
        env.scope,
        saved.document_id,
        saved.processing.state_version,
        limit=1,
    )
    second = env.service.passages(
        env.scope,
        saved.document_id,
        saved.processing.state_version,
        after_ordinal=first.next_after_ordinal,
        limit=2,
    )
    assert first.has_more and not second.has_more
    assert first.entries + second.entries == original.entries
    newer, result, page = build(
        env,
        request(
            env,
            text="a" * 2049,
            state=saved.processing.state_version,
            title="New title",
        ),
    )
    assert result.passage_set_id == original.passage_set_id
    assert page.entries[0].reference == original.entries[0].reference
    assert (
        env.service.evidence(env.scope, page.entries[0].reference).metadata.metadata.title
        == "Title"
    )
    assert (
        env.service.citation(env.scope, page.entries[0].citation).metadata.metadata.title
        == "New title"
    )
    changed, _, other = build(
        env,
        request(
            env,
            text="a" * 2049,
            state=newer.processing.state_version,
            policy="supplied-anchors/1",
            anchors=(SuppliedAnchor(local_id="all", start=0, end=2049, quote="a" * 2049),),
        ),
    )
    assert other.passage_set_id != original.passage_set_id
    assert not env.service.evidence(env.scope, original.entries[0].reference).is_current_support
    restored, _, returned = build(
        env,
        request(
            env,
            text="a" * 2049,
            state=changed.processing.state_version,
        ),
    )
    assert restored.processing.state_version != saved.processing.state_version
    assert returned.passage_set_id == original.passage_set_id
    assert env.service.citation(env.scope, original.entries[0].citation).quote == "a" * 1024


@pytest.mark.service
def test_prepare_publish_fences_changed_state_policy_and_authority(tmp_path):
    env = environment(tmp_path / "race.db")
    value = request(env)
    saved = receipt(env.service.write(value))
    prepared = prepare(
        env.database,
        env.service.identity,
        env.scope,
        value.attribution,
        saved.document_id,
        saved.processing.state_version,
    )
    receipt(env.service.write(request(env, state=saved.processing.state_version, title="Changed")))
    before = inventory(env)
    with (
        pytest.raises(EvidenceServiceError, match="state_conflict"),
        writing(env.database, env.service.identity) as context,
    ):
        publish(context, env.scope, value.attribution, prepared)
    assert inventory(env) == before
    with pytest.raises(EvidenceServiceError, match="forbidden"):
        prepare(
            env.database,
            LocalIdentity(principal_id="foreign"),
            env.scope,
            value.attribution,
            saved.document_id,
            saved.processing.state_version,
        )


@pytest.mark.service
def test_revocation_between_prepare_and_publication(tmp_path):
    env = environment(tmp_path / "revoke.db")
    value = request(env)
    saved = receipt(env.service.write(value))
    prepared = prepare(
        env.database,
        env.service.identity,
        env.scope,
        value.attribution,
        saved.document_id,
        saved.processing.state_version,
    )
    env.admin.replace_policy(
        env.policy.model_copy(update={"grants": ()}), env.scope.access.policy_version
    )
    before = inventory(env)
    with (
        pytest.raises(EvidenceServiceError, match="forbidden"),
        writing(env.database, env.service.identity) as context,
    ):
        publish(context, env.scope, value.attribution, prepared)
    assert inventory(env) == before


@pytest.mark.process
def test_atomic_rollback_and_concurrent_same_state_convergence(tmp_path):
    env = environment(tmp_path / "atomic.db")
    value = request(env, text="z" * 2049)
    saved = receipt(env.service.write(value))
    prepared = prepare(
        env.database,
        env.service.identity,
        env.scope,
        value.attribution,
        saved.document_id,
        saved.processing.state_version,
    )
    before = inventory(env)
    with (
        pytest.raises(RuntimeError, match="abort"),
        writing(env.database, env.service.identity) as context,
    ):
        publish(context, env.scope, value.attribution, prepared)
        assert (
            env.service.passages(
                env.scope, saved.document_id, saved.processing.state_version,
            ).status == "not_processed"
        )
        raise RuntimeError("abort")
    assert inventory(env) == before
    barrier = Barrier(2)

    def worker():
        barrier.wait(timeout=5)
        with writing(env.database, env.service.identity) as context:
            return publish(context, env.scope, value.attribution, prepared)

    with ThreadPoolExecutor(max_workers=2) as pool:
        left, right = list(pool.map(lambda _: worker(), range(2)))
    assert left.passage_set_id == right.passage_set_id
    assert {left.outcome, right.outcome} == {"produced", "reused"}
    assert (
        len(
            env.service.passages(
                env.scope,
                saved.document_id,
                saved.processing.state_version,
            ).entries
        )
        == 3
    )


@pytest.mark.service
@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE anchor SET quote='corrupt' WHERE origin_kind='generated'",
        "DELETE FROM passage_set_member",
        "UPDATE passage_set SET manifest_hash='corrupt'",
        "UPDATE passage_policy_definition SET definition_json='corrupt'",
    ],
)
def test_reuse_detects_corruption_never_repairs_or_overwrites(tmp_path, sql):
    env = environment(tmp_path / "corrupt.db")
    value = request(env)
    saved, _, _ = build(env, value)
    with env.database.transaction() as connection:
        connection.execute(sql)
    before = inventory(env)
    with pytest.raises(EvidenceServiceError, match="internal_error"):
        produce(
            env.database,
            env.service.identity,
            env.scope,
            value.attribution,
            saved.document_id,
            saved.processing.state_version,
        )
    assert inventory(env) == before


@pytest.mark.service
def test_exact_chain_scope_and_state_checks_across_adapters(tmp_path):
    env = environment(tmp_path / "chain.db")
    saved, _, page = build(env, request(env))
    foreign, _, other = build(env, request(env, external="other", namespace="email"))
    ref = page.entries[0].reference
    for replacements in (
        {"corpus_id": "foreign"},
        {"source_namespace": "email"},
        {"document_id": foreign.document_id},
        {"revision_id": foreign.revision_id},
        {"anchor_id": other.entries[0].reference.anchor_id},
        {"passage_id": other.entries[0].reference.passage_id},
    ):
        with pytest.raises(EvidenceServiceError, match="not_found"):
            env.service.evidence(env.scope, ref.model_copy(update=replacements))
    with pytest.raises(EvidenceServiceError, match="not_found"):
        env.service.evidence(env.scope, ref, state_version=foreign.processing.state_version)
    with pytest.raises(EvidenceServiceError, match="not_found"):
        env.service.citation(
            env.scope,
            page.entries[0].citation.model_copy(
                update={
                    "metadata_snapshot_id": foreign.metadata_snapshot_id,
                }
            ),
        )
    scoped = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(
                update={
                    "namespaces": ("email",),
                }
            )
        }
    )
    with pytest.raises(EvidenceServiceError, match="not_found"):
        env.service.passages(scoped, saved.document_id, saved.processing.state_version)


@pytest.mark.service
def test_passage_support_same_transaction_no_index_dependency_and_stale_state(tmp_path):
    env = environment(tmp_path / "support.db")
    policy = LocalPolicy(
        corpus_id="work",
        bindings=env.policy.bindings,
        grants=(
            *env.policy.grants,
            *(
                PolicyGrant(principal_id="principal", namespace=ns, grant="write_knowledge")
                for ns in ("markdown", "email")
            ),
        ),
    )
    version = env.admin.replace_policy(policy, env.scope.access.policy_version).policy_version
    env.scope = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(
                update={
                    "policy_version": version,
                    "grants": ("read", "write_documents", "write_knowledge"),
                }
            )
        }
    )
    value = request(env)
    saved, _, page = build(env, value)
    ref = page.entries[0].reference
    dependency = DocumentDependency(
        source_namespace=ref.source_namespace,
        document_id=ref.document_id,
        revision_id=ref.revision_id,
        state_version=saved.processing.state_version,
    )
    with writing(env.database, env.service.identity) as context:
        support = TransactionEvidence(context)
        assert (
            support.validate_current(env.scope, (dependency,), (ref,))[0].quote
            == value.payload.content.text
        )
        assert (
            context.connection.execute("SELECT count(*) FROM document_projection").fetchone()[0]
            == 0
        )
    with pytest.raises(EvidenceServiceError, match="invalid_request"):
        support.validate_current(env.scope, (dependency,), (ref,))
    next_state = receipt(
        env.service.write(
            request(
                env,
                state=saved.processing.state_version,
                title="New state",
            )
        )
    )
    with writing(env.database, env.service.identity) as context:
        with pytest.raises(EvidenceServiceError, match="state_conflict"):
            TransactionEvidence(context).validate_current(env.scope, (dependency,), (ref,))
        with pytest.raises(EvidenceServiceError, match="not_found"):
            TransactionEvidence(context).validate_current(
                env.scope,
                (
                    dependency.model_copy(
                        update={"state_version": next_state.processing.state_version}
                    ),
                ),
                (ref,),
            )


@pytest.mark.service
def test_snapshot_resolver_exact_charges_shared_budget_and_observer(tmp_path):
    env = environment(tmp_path / "budget.db")
    _, _, page = build(env, request(env))
    reference = page.entries[0].reference
    deadline = Deadline(time.monotonic() + 30)
    budget = PrivateBudget(deadline)
    meter = LocalExecutionMeter(budget, max_operations=1, max_items=5)
    with observe(env.database, env.service.identity, env.scope, deadline, budget) as observer:
        with read_context(
            env.database,
            env.service.identity,
            env.scope,
            observer.session_id,
            deadline,
            meter.begin_step("search"),
        ) as context:
            for stage in ("search_temp", "search_lexical", "search_vector", "search_rerank"):
                context.meter.reserve_public(stage)
            resolved = evidence_view(
                context.connection,
                context.scope,
                reference,
                context=context,
                stage="search_final_evidence",
            )
            assert resolved.quote == page.entries[0].quote
            assert meter.public_accounting().items_consumed == 5
            assert budget._scratch > 0
            with pytest.raises(PublicBudgetStop):
                read_evidence(context, reference)
            local = budget.limited(max_visits=1)
            with context.using_budget(local), pytest.raises(PrivateResourceStop):
                read_evidence(context, reference)
            assert context.meter.private_budget is budget
        assert budget._scratch == 0
        with release_fence(observer, env.service.identity, env.scope, deadline, budget=budget):
            pass
    direct_budget = PrivateBudget(deadline)
    direct = LocalExecutionMeter(direct_budget, max_operations=1, max_items=1)
    with observe(
        env.database, env.service.identity, env.scope, deadline, direct_budget
    ) as observer:
        with read_context(
            env.database,
            env.service.identity,
            env.scope,
            observer.session_id,
            deadline,
            direct.begin_step("evidence"),
        ) as context:
            assert read_evidence(context, reference).reference == reference
        assert direct.public_accounting().items_consumed == 1


@pytest.mark.service
def test_passage_reports_single_execution_quotes_opt_in_and_irreversible_redaction(
    tmp_path, monkeypatch
):
    env = environment(tmp_path / "reports.db")
    saved, _, page = build(env, request(env))
    from kg.evidence import _reads

    original = _reads.evidence_view
    calls = []

    def counting(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    monkeypatch.setattr(_reads, "evidence_view", counting)
    report = env.service.passages_explained(
        env.scope,
        saved.document_id,
        saved.processing.state_version,
    )
    assert report.outcome == page and len(calls) == 1
    assert isinstance(report.report, ExecutionReport)
    assert any(isinstance(e.event, IndexPhase) for e in report.report.events)
    assert page.entries[0].quote not in report.report.model_dump_json()
    detailed = env.service.passages_explained(
        env.scope,
        saved.document_id,
        saved.processing.state_version,
        ExplainOptions(detail="detailed", include_quotes=True),
    )
    assert isinstance(detailed.report, ExecutionReport)
    assert any(e.event.kind == "execution.quote" for e in detailed.report.events)
    with env.database.transaction() as connection:
        connection.execute("DELETE FROM passage_set_member")
    assert (
        env.service.diagnostics.report(env.scope, detailed.report.report_id).state == "unavailable"
    )
    with env.database.transaction() as connection:
        connection.execute(
            "INSERT INTO passage_set_member SELECT passage_set_id,passage_id,ordinal FROM passage"
        )
    assert env.service.diagnostics.report(env.scope, detailed.report.report_id).state == "redacted"


@pytest.mark.service
def test_remove_history_and_invalid_pagination(tmp_path):
    env = environment(tmp_path / "remove.db")
    value = request(env)
    saved, _, page = build(env, value)
    removed = receipt(
        env.service.write(
            value.model_copy(
                update={
                    "retry_key": "remove",
                    "payload": RemoveDocument(
                        operation="remove_document",
                        document=value.payload.document,
                        precondition=ExpectedState(
                            kind="match", state_version=saved.processing.state_version
                        ),
                    ),
                }
            )
        )
    )
    assert not env.service.citation(env.scope, page.entries[0].citation).is_current_support
    with pytest.raises(EvidenceServiceError, match="state_conflict"):
        produce(
            env.database,
            env.service.identity,
            env.scope,
            value.attribution,
            saved.document_id,
            removed.processing.state_version,
        )
    for after, limit in ((-1, 1), (False, 1), (0, 0), (0, 201), (0, True)):
        with pytest.raises(EvidenceServiceError, match="invalid_request"):
            env.service.passages(
                env.scope,
                saved.document_id,
                saved.processing.state_version,
                after_ordinal=after,
                limit=limit,
            )
