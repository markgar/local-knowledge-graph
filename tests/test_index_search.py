import time
from contextlib import contextmanager

import pytest
from support.evidence import environment
from support.index_search import prepared

from kg._execution_budget import Deadline, LocalExecutionMeter, PrivateBudget, PublicBudgetStop
from kg.evidence._read_context import observe, read_context, release_fence
from kg.evidence.errors import EvidenceServiceError
from kg.models.execution import ExplainOptions


@pytest.mark.service
def test_actual_full_pipeline_exact_citation_and_report(tmp_path):
    import json
    from pathlib import Path

    case = json.loads(Path("corpora/acceptance/canonical-search.json").read_text())
    env = environment(tmp_path / "search.sqlite3")
    search, _, provider, reranker, _, saved = prepared(
        env, text=case["text"], policy=case["passage_policy"],
    )
    result = search.search_explained(
        env.scope, case["query"], options=ExplainOptions(detail="detailed", include_quotes=True),
    )
    assert result.outcome.items_consumed == case["expected_items"]
    assert len(result.outcome.hits) == 1
    hit = result.outcome.hits[0]
    assert (hit.evidence.start, hit.evidence.end) == (case["expected_start"], case["expected_end"])
    assert hit.evidence.quote == "A\r\nCafe\u0301 \U0001f680"
    assert hit.evidence.citation.state_version == saved.processing.state_version
    assert env.service.citation(env.scope, hit.evidence.citation) == hit.evidence
    assert len(reranker.calls) == 1
    assert provider.calls[-1] == ("query", "A")
    candidate = next(event.event for event in result.report.events
                     if event.event.kind == "indexing.candidate")
    assert candidate.lexical_rank == candidate.dense_rank == candidate.rerank_rank == 1
    assert candidate.final_rank == 1
    assert candidate.fused_score == 1 / 21 + 0.5 / 21
    assert candidate.rerank_score == hit.score
    assert search.diagnostics.recent(env.scope).entries


@pytest.mark.service
@pytest.mark.parametrize("allowance", [4, 5, 10])
def test_exact_same_snapshot_schedule_and_repeated_pool(tmp_path, allowance):
    env = environment(tmp_path / "search.sqlite3")
    search, _, _, _, _, _ = prepared(env, text="alpha")
    budget = PrivateBudget(Deadline(time.monotonic() + 30))
    execution = LocalExecutionMeter(budget, max_operations=1, max_items=allowance)
    meter = execution.begin_step("search")
    events = []
    reserve = meter.reserve_public

    def charged(stage, n=1):
        reserve(stage, n)
        events.extend([stage] * n)

    meter.reserve_public = charged
    capture = search._collector.begin_capture("search", env.scope, required="read")
    with (
        observe(env.database, search.identity, env.scope, budget.deadline, budget) as observer,
        read_context(env.database, search.identity, env.scope, observer.session_id,
                     budget.deadline, meter) as context,
    ):
        if allowance == 4:
            with pytest.raises(PublicBudgetStop):
                search._search_in_context(context, "alpha", capture=capture)
        else:
            selected = search._search_in_context(context, "alpha", capture=capture)
            selected.ranked.projection.check(context)
            if allowance == 10:
                search._search_in_context(context, "alpha", capture=capture)
            with release_fence(observer, search.identity, env.scope, budget.deadline):
                pass
    schedule = ["search_temp", "search_lexical", "search_vector",
                "search_rerank", "search_final_evidence"]
    assert events == (schedule[:4] if allowance == 4 else schedule * (allowance // 5))
    assert budget._scratch == 0


@pytest.mark.service
def test_missing_projection_is_not_empty_success(tmp_path):
    from support.evidence import receipt
    from support.indexing import request

    env = environment(tmp_path / "search.sqlite3")
    search, _, _, _, _, _ = prepared(env)
    receipt(env.service.write(request(env, external="unindexed")))
    with pytest.raises(EvidenceServiceError) as failure:
        search.search(env.scope, "no lexical match")
    assert failure.value.failure.code == "stale_index"


@pytest.mark.service
@pytest.mark.parametrize("text", [None, ""])
def test_empty_still_initializes_both_and_encodes_query(tmp_path, text):
    from support.index_search import Reranker, SearchProvider

    from kg.indexing import EvidenceSearchService

    env = environment(tmp_path / "empty.sqlite3")
    if text is None:
        search = EvidenceSearchService(env.database, env.service.identity)
        provider, reranker = SearchProvider(), Reranker()
    else:
        search, _, provider, reranker, _, _ = prepared(env, text=text)
    loads = []
    search._provider_factory = lambda profile: (loads.append("embedding") or provider)
    search._reranker_factory = lambda: (loads.append("reranker") or reranker)
    result = search.search(env.scope, "alpha")
    assert result.hits == ()
    assert result.items_consumed == 0
    assert sorted(loads) == ["embedding", "reranker"]
    assert provider.calls[-1] == ("query", "alpha")
    assert reranker.calls == []
    assert len(result.projection_ids) == (0 if text is None else 1)


@pytest.mark.service
@pytest.mark.parametrize("query", ["", " ", "___", "!!!", "\u0301", "x" * 4097])
def test_invalid_and_tokenless_queries_fail(tmp_path, query):
    env = environment(tmp_path / "invalid.sqlite3")
    search, _, _, _, _, _ = prepared(env)
    with pytest.raises(EvidenceServiceError) as failure:
        search.search(env.scope, query)
    assert failure.value.failure.code == "invalid_request"


@pytest.mark.service
@pytest.mark.parametrize("query", ['"alpha" OR nope*', "ALPHA beta", "alpha alpha", "no-match"])
def test_natural_tokens_not_raw_syntax_and_dense_only_still_reranks(tmp_path, query):
    env = environment(tmp_path / "tokens.sqlite3")
    search, _, _, reranker, _, _ = prepared(env, text="alpha beta")
    result = search.search_explained(env.scope, query, options=ExplainOptions(detail="detailed"))
    assert len(result.outcome.hits) == 1
    assert len(reranker.calls) == 1
    event = next(item.event for item in result.report.events
                 if item.event.kind == "indexing.candidate")
    assert event.lexical_member == (query != "no-match")
    if query == "no-match":
        assert event.lexical_score is event.lexical_rank is event.lexical_contribution is None
        assert result.outcome.items_consumed == 4


@pytest.mark.service
@pytest.mark.parametrize("fault", [
    "embedding_missing", "reranker_missing", "runtime", "profile", "reranker_pin",
    "zero_vector", "nan_vector", "dimension", "rerank_length", "rerank_nan", "rerank_bool",
])
def test_provider_faults_no_fallback_or_projection_mutation(tmp_path, fault):
    from kg.retrieval.dense import DenseIndexError
    from kg.retrieval.rerank import RerankerError

    env = environment(tmp_path / "fault.sqlite3")
    search, index, provider, reranker, _, saved = prepared(env, text="alpha")
    original = index.status(env.scope, saved.document_id)

    def unavailable(*args):
        raise DenseIndexError("PRIVATE_EXCEPTION_CANARY")

    def no_reranker():
        raise RerankerError("PRIVATE_EXCEPTION_CANARY")

    if fault == "embedding_missing":
        search._provider_factory = unavailable
    elif fault == "reranker_missing":
        search._reranker_factory = no_reranker
    elif fault == "runtime":
        provider.pipeline_version += "|changed"
    elif fault == "profile":
        provider.name = "wrong"
    elif fault == "reranker_pin":
        reranker.revision = "wrong"
    elif fault in ("zero_vector", "nan_vector", "dimension"):
        provider.encode_query = lambda _: (
            [0.0] * provider.dimensions if fault == "zero_vector" else
            [float("nan")] * provider.dimensions if fault == "nan_vector" else [1.0]
        )
    else:
        reranker.score = lambda *a, **k: (
            [] if fault == "rerank_length" else [float("nan")] if fault == "rerank_nan" else [True]
        )
    with pytest.raises(EvidenceServiceError) as failure:
        search.search(env.scope, "alpha")
    assert failure.value.failure.code in ("unsupported", "stale_index", "internal_error")
    assert "CANARY" not in str(failure.value)
    assert index.status(env.scope, saved.document_id) == original
    assert not search.diagnostics.recent(env.scope).entries
    for capture in search._collector.candidates():
        assert not capture.events and not capture.configuration_ids


@pytest.mark.service
def test_hidden_namespace_other_corpus_and_history_cannot_change_scores(tmp_path):
    from support.evidence import receipt
    from support.index_search import SearchProvider
    from support.indexing import process, request, service

    env = environment(tmp_path / "isolation.sqlite3")
    search, index, _, _, value, saved = prepared(env, text="alpha beta")
    narrow = env.scope.model_copy(update={
        "access": env.scope.access.model_copy(update={"namespaces": ("markdown",)}),
    })
    before = search.search_explained(narrow, "alpha", options=ExplainOptions(detail="detailed"))
    denied = request(env, namespace="email", external="doc", text="alpha " * 300)
    denied_saved = receipt(env.service.write(denied))
    assert process(index, env, denied, denied_saved).outcome == "ready"
    other = environment(env.database.path, corpus="other")
    other_index, _, _ = service(other, SearchProvider())
    other_value = request(other, text="alpha")
    other_saved = receipt(other.service.write(other_value))
    assert process(other_index, other, other_value, other_saved).outcome == "ready"
    historical = request(env, text="alpha " * 200, state=saved.processing.state_version)
    history_saved = receipt(env.service.write(historical))
    assert process(index, env, historical, history_saved).outcome == "ready"
    restored = request(env, text="alpha beta", state=history_saved.processing.state_version)
    restored_saved = receipt(env.service.write(restored))
    assert process(index, env, restored, restored_saved).outcome == "ready"
    after = search.search_explained(narrow, "alpha", options=ExplainOptions(detail="detailed"))

    def facts(result):
        return [event.event for event in result.report.events
                if event.event.kind == "indexing.candidate"]

    assert facts(before) == facts(after)
    assert before.outcome.items_consumed == after.outcome.items_consumed == 5
    assert before.outcome.hits[0].score == after.outcome.hits[0].score
    assert after.outcome.hits[0].evidence.citation.state_version == (
        restored_saved.processing.state_version
    )
    assert after.outcome.hits[0].evidence.reference.source_namespace == "markdown"
    assert len(after.outcome.projection_ids) == 1


@pytest.mark.service
@pytest.mark.parametrize("change", ["text", "metadata", "policy", "remove", "rebuild", "unrelated"])
@pytest.mark.parametrize("stage", ["embedding", "rerank"])
def test_concurrent_changes_never_release_ranked_data(tmp_path, change, stage):
    from support.evidence import receipt
    from support.indexing import process, request

    from kg.models.foundation import ExpectedState, RemoveDocument

    env = environment(tmp_path / "race.sqlite3")
    search, index, provider, reranker, value, saved = prepared(env, text="alpha")
    old_citation = env.service.passages(
        env.scope, saved.document_id, saved.processing.state_version,
    ).entries[0].citation
    invoked = []

    def mutate():
        if invoked:
            return
        invoked.append(True)
        if change == "rebuild":
            provider.before_encode = None
            assert process(index, env, value, saved, mode="rebuild").outcome == "ready"
            index.cleanup(env.scope, value.attribution, saved.document_id)
        elif change == "policy":
            policy = env.policy.model_copy(update={
                "grants": tuple(g for g in env.policy.grants if g.grant != "read"),
            })
            env.admin.replace_policy(policy, env.scope.access.policy_version)
        elif change == "remove":
            removed = value.model_copy(update={
                "retry_key": "remove",
                "payload": RemoveDocument(
                    operation="remove_document", document=value.payload.document,
                    precondition=ExpectedState(
                        kind="match", state_version=saved.processing.state_version,
                    ),
                ),
            })
            receipt(env.service.write(removed))
        else:
            updated = request(
                env, text="beta" if change == "text" else "alpha",
                title="New" if change == "metadata" else "Title",
                external="other" if change == "unrelated" else "doc",
                state=None if change == "unrelated" else saved.processing.state_version,
            )
            receipt(env.service.write(updated))
    if stage == "embedding":
        provider.before_encode = mutate
    else:
        reranker.before_score = mutate
    with pytest.raises(EvidenceServiceError) as failure:
        search.search(env.scope, "alpha")
    assert failure.value.failure.code in ("state_changed", "forbidden")
    assert invoked
    assert not search.diagnostics.recent(env.scope).entries
    assert all(not c.events and not c.configuration_ids for c in search._collector.candidates())
    if change != "policy":
        assert env.service.citation(env.scope, old_citation).quote == "alpha"


@pytest.mark.service
def test_observer_gap_commit_search_detects_even_complete_new_snapshot(tmp_path, monkeypatch):
    from support.evidence import receipt
    from support.indexing import request

    import kg.indexing.search as module

    env = environment(tmp_path / "gap.sqlite3")
    search, _, _, _, _, _ = prepared(env, text="alpha")
    original = module.read_context

    @contextmanager
    def gap(*args, **kwargs):
        receipt(env.service.write(request(env, namespace="email", external="outside")))
        with original(*args, **kwargs) as context:
            yield context

    monkeypatch.setattr(module, "read_context", gap)
    narrow = env.scope.model_copy(update={
        "access": env.scope.access.model_copy(update={"namespaces": ("markdown",)}),
    })
    with pytest.raises(EvidenceServiceError, match="state_changed"):
        search.search(narrow, "alpha")


@pytest.mark.service
@pytest.mark.parametrize("resource", ["visits", "vm", "scratch", "deadline", "provider"])
def test_inherited_private_limits_release_buffers_and_redact(tmp_path, monkeypatch, resource):
    env = environment(tmp_path / "limits.sqlite3")
    search, _, _, reranker, _, _ = prepared(env, text="alpha")
    budget = PrivateBudget(Deadline(time.monotonic() + 30))
    monkeypatch.setattr(search, "_budget", lambda: budget)
    held = []

    def exhaust():
        if resource == "visits":
            budget._visits = 100_000
        elif resource == "vm":
            budget._vm = 10_000_000
        elif resource == "scratch":
            held.append(budget.reserve_scratch((128 << 20) - budget._scratch, "general"))
        elif resource == "deadline":
            budget.deadline = Deadline(0)

    reranker.before_score = exhaust
    if resource == "scratch":
        import kg.indexing._search as pipeline
        original = pipeline.evidence_view

        def exhausted_hydration(*args, **kwargs):
            exhaust()
            return original(*args, **kwargs)

        monkeypatch.setattr(pipeline, "evidence_view", exhausted_hydration)
        reranker.before_score = None
    query = "q" * 4096 if resource == "provider" else "alpha"
    if resource == "provider":
        query = "\U0001f680" * 4095 + "a"
    with pytest.raises(EvidenceServiceError, match="budget_exceeded"):
        search.search(env.scope, query)
    for reservation in held:
        reservation.release()
    assert budget._scratch == 0
    for capture in search._collector.candidates():
        assert not capture.events and not capture.configuration_ids


@pytest.mark.service
def test_temp_full_is_resource_failure_and_next_search_is_clean(tmp_path, monkeypatch):
    import kg.evidence._read_context as read_module

    env = environment(tmp_path / "temp.sqlite3")
    search, _, _, _, _, _ = prepared(env, text="alpha " * 100)
    original = read_module._limit_temp

    def tiny(connection):
        original(connection)
        connection.execute("PRAGMA temp.max_page_count=1")

    with monkeypatch.context() as patch:
        patch.setattr(read_module, "_limit_temp", tiny)
        with pytest.raises(EvidenceServiceError, match="budget_exceeded"):
            search.search(env.scope, "alpha")
    assert search.search(env.scope, "alpha").hits
    with env.database.connection() as connection:
        assert not connection.execute(
            "SELECT name FROM sqlite_temp_master WHERE name LIKE 'e3_%'",
        ).fetchall()


@pytest.mark.acceptance
@pytest.mark.parametrize("offset", [9995, 9996])
def test_exact_ten_thousand_shared_boundary(tmp_path, offset):
    env = environment(tmp_path / "public.sqlite3")
    search, _, _, _, _, _ = prepared(env, text="alpha")
    budget = PrivateBudget(Deadline(time.monotonic() + 30))
    execution = LocalExecutionMeter(budget, max_operations=1, max_items=10_000)
    meter = execution.begin_step("search")
    for _ in range(offset):
        meter.reserve_public("evidence_reference")
    capture = search._collector.begin_capture("search", env.scope, required="read")
    with (
        observe(env.database, search.identity, env.scope, budget.deadline, budget) as observer,
        read_context(env.database, search.identity, env.scope, observer.session_id,
                     budget.deadline, meter) as context,
    ):
        if offset == 9995:
            assert search._search_in_context(context, "alpha", capture=capture).response.hits
        else:
            with pytest.raises(PublicBudgetStop):
                search._search_in_context(context, "alpha", capture=capture)
    assert execution.public_accounting().items_consumed == 10_000


@pytest.mark.service
def test_dedup_ties_pool_limits_and_actual_report_priority(tmp_path):
    from kg.models.foundation import SuppliedAnchor

    env = environment(tmp_path / "pools.sqlite3")
    text = "alpha"
    search, _, _, reranker, _, saved = prepared(
        env, text=text, policy="supplied-anchors/1",
        anchors=tuple(
            SuppliedAnchor(local_id=f"a{i}", start=0, end=5, quote=text) for i in range(60)
        ),
    )
    result = search.search_explained(
        env.scope, "alpha", limit=3, options=ExplainOptions(detail="detailed"),
    )
    ids = sorted(entry.reference.passage_id for entry in env.service.passages(
        env.scope, saved.document_id, saved.processing.state_version,
    ).entries)
    assert [hit.evidence.reference.passage_id for hit in result.outcome.hits] == ids[:3]
    assert len(reranker.calls) == 50
    assert result.outcome.items_consumed == 60 + 50 + 60 + 50 + 3
    assert result.outcome.lexical_truncated and result.outcome.dense_truncated
    assert not result.outcome.fusion_truncated
    candidates = [e.event for e in result.report.events if e.event.kind == "indexing.candidate"]
    assert [event.final_rank for event in candidates[:3]] == [1, 2, 3]
    assert candidates[3].exclusion == "below_return_limit"
    assert candidates[3].rerank_rank == 4


@pytest.mark.service
@pytest.mark.parametrize("profile", ["gte-modernbert", "qwen3-embedding-0.6b"])
@pytest.mark.parametrize("contextual", [False, True])
def test_supported_configurations_quote_only_lexical_and_exact_representation(
    tmp_path, profile, contextual,
):
    from support.evidence import receipt
    from support.index_search import Reranker, SearchProvider
    from support.indexing import process, request, service

    from kg.indexing import EvidenceSearchService
    from kg.models.indexing import IndexConfiguration
    from kg.retrieval.dense import EmbeddingProfile

    env = environment(tmp_path / "configuration.sqlite3")
    provider, reranker = SearchProvider(EmbeddingProfile(profile)), Reranker()
    index, _, _ = service(env, provider)
    configuration = IndexConfiguration(
        embedding_profile=profile, contextual=contextual,
        representation="generic-title-quote/1" if contextual else "exact-quote/1",
    )
    value = request(env, text="A\r\nB", title="UniqueTitle")
    saved = receipt(env.service.write(value))
    assert process(index, env, value, saved, configuration=configuration).outcome == "ready"
    search = EvidenceSearchService(env.database, env.service.identity)
    search._provider_factory = lambda _: provider
    search._reranker_factory = lambda: reranker
    result = search.search_explained(
        env.scope, "UniqueTitle", configuration, options=ExplainOptions(detail="detailed"),
    )
    assert reranker.calls[0][1] == (
        "UniqueTitle\n\nA\r\nB" if contextual else "A\r\nB",
    )
    assert result.outcome.hits[0].evidence.quote == "A\r\nB"
    candidate = next(e.event for e in result.report.events if e.event.kind == "indexing.candidate")
    assert candidate.lexical_member is False
    assert result.outcome.items_consumed == 4


@pytest.mark.service
def test_wrong_logical_configuration_and_stale_metadata_fail(tmp_path):
    from support.evidence import receipt
    from support.indexing import request

    from kg.models.indexing import IndexConfiguration

    env = environment(tmp_path / "stale.sqlite3")
    search, _, _, _, _, saved = prepared(env, text="alpha")
    with pytest.raises(EvidenceServiceError, match="stale_index"):
        search.search(env.scope, "alpha", IndexConfiguration(
            contextual=True, representation="generic-title-quote/1",
        ))
    receipt(env.service.write(request(
        env, text="alpha", title="new metadata", state=saved.processing.state_version,
    )))
    with pytest.raises(EvidenceServiceError, match="stale_index"):
        search.search(env.scope, "alpha")


@pytest.mark.service
@pytest.mark.parametrize(
    "failure", ["append", "phase_constructor", "candidate_constructor", "candidate_display"],
)
def test_capture_failure_never_changes_model_inputs_or_business_outcome(
    tmp_path, monkeypatch, failure,
):
    import kg.indexing._search as pipeline
    from kg.diagnostics._collector import Capture

    env = environment(tmp_path / "capture.sqlite3")
    search, _, _, reranker, _, _ = prepared(env, text="alpha")
    ordinary = search.search(env.scope, "alpha")
    first_inputs = list(reranker.calls)
    reranker.calls.clear()

    def capacity(*args, **kwargs):
        raise MemoryError()

    if failure == "append":
        monkeypatch.setattr(Capture, "append", capacity)
    elif failure == "candidate_display":
        monkeypatch.setattr(pipeline.Pipeline, "report_candidates", capacity)
    else:
        monkeypatch.setattr(
            pipeline, "IndexPhase" if failure == "phase_constructor" else "IndexCandidate",
            capacity,
        )
    explained = search.search_explained(
        env.scope, "alpha", options=ExplainOptions(detail="detailed", include_quotes=True),
    )
    assert explained.outcome.hits == ordinary.hits
    assert explained.outcome.items_consumed == ordinary.items_consumed
    assert reranker.calls == first_inputs
    assert explained.report.state == "unavailable"


@pytest.mark.service
def test_borrowed_child_report_stays_provisional_and_parent_redaction_is_terminal(tmp_path):
    from kg.models.execution import ExplainOptions

    env = environment(tmp_path / "child.sqlite3")
    search, _, _, _, _, _ = prepared(env, text="alpha")
    budget = PrivateBudget(Deadline(time.monotonic() + 30))
    execution = LocalExecutionMeter(budget, max_operations=1, max_items=5)
    parent = search._collector.begin_capture("execute", env.scope, required="read")
    with (
        observe(env.database, search.identity, env.scope, budget.deadline, budget) as observer,
        read_context(env.database, search.identity, env.scope, observer.session_id,
                     budget.deadline, execution.begin_step("child")) as context,
    ):
        child = search._begin_child_capture(
            context, parent.group, observer.retain(), step_id="child",
            options=ExplainOptions(detail="detailed"),
        )
        result = search._search_in_context(context, "alpha", capture=child)
        assert result.ranked.result.hits
        assert context.connection.in_transaction
        assert not context.connection.execute(
            "SELECT name FROM sqlite_temp_master WHERE name LIKE 'e3_%'",
        ).fetchall()
        child.finish("complete")
        assert search.diagnostics._publish(child).state == "unavailable"
        assert search.diagnostics.report(env.scope, child.report_id).state == "unavailable"
        parent.group.redact("state_changed")
        parent.finish("failed", reason="state_changed")
        with release_fence(observer, search.identity, env.scope, budget.deadline) as fence:
            parent.group.release(fence)
        assert parent.group.state == "redacted"
        assert not child.events and not child.configuration_ids
    with pytest.raises(EvidenceServiceError):
        result.ranked.projection.check(context)
    assert search.diagnostics.report(env.scope, child.report_id).state == "redacted"


@pytest.mark.service
def test_initial_authorization_precedes_models_and_denied_scope_not_silently_reduced(tmp_path):
    env = environment(tmp_path / "authorization.sqlite3")
    search, _, _, _, _, _ = prepared(env, text="alpha")
    policy = env.policy.model_copy(update={
        "grants": tuple(g for g in env.policy.grants if not (
            g.namespace == "email" and g.grant == "read"
        )),
    })
    registered = env.admin.replace_policy(policy, env.scope.access.policy_version)
    scope = env.scope.model_copy(update={
        "access": env.scope.access.model_copy(update={"policy_version": registered.policy_version}),
    })

    def unexpected(*args):
        pytest.fail("Provider loaded before authorization")

    search._provider_factory = search._reranker_factory = unexpected
    with pytest.raises(EvidenceServiceError, match="forbidden"):
        search.search(scope, "alpha")


@pytest.mark.service
def test_supplied_document_example_uses_actual_lifecycle_and_full_search(tmp_path, monkeypatch):
    from support.index_search import Reranker, SearchProvider
    from support.modules import module

    import kg.indexing.search as search_module
    import kg.indexing.service as index_module

    monkeypatch.setattr(index_module, "SentenceTransformerEmbeddingProvider", SearchProvider)
    monkeypatch.setattr(search_module, "SentenceTransformerEmbeddingProvider", SearchProvider)
    monkeypatch.setattr(search_module, "SentenceTransformerCrossEncoderProvider", Reranker)
    example = module("examples/canonical_search.py")
    result = example.run(tmp_path / "example.sqlite3", "alpha exact source", "alpha")
    assert result.hits[0].evidence.quote == "alpha exact source"
    assert result.items_consumed == 5


@pytest.mark.process
@pytest.mark.parametrize("phase", ["temp", "rerank"])
def test_terminated_child_leaves_no_lease_or_temp_and_preserves_projection(tmp_path, phase):
    import multiprocessing

    from support.index_search import blocked_search

    env = environment(tmp_path / "child-process.sqlite3")
    search, index, _, _, _, saved = prepared(env, text="alpha")
    original = index.status(env.scope, saved.document_id)
    spawn = multiprocessing.get_context("spawn")
    ready, resume = spawn.Event(), spawn.Event()
    child = spawn.Process(target=blocked_search, args=(env.database.path, ready, resume, phase))
    child.start()
    try:
        assert ready.wait(20), "Child never reached actual search stage"
        child.terminate()
        child.join(timeout=10)
        assert not child.is_alive()
    finally:
        if child.is_alive():
            child.terminate()
            child.join(timeout=10)
        child.close()
    assert index.status(env.scope, saved.document_id) == original
    assert search.search(env.scope, "alpha").items_consumed == 5
    with env.database.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        assert not connection.execute(
            "SELECT name FROM sqlite_temp_master WHERE name LIKE 'e3_%'",
        ).fetchall()
        connection.rollback()


@pytest.mark.service
def test_summary_detailed_and_quote_options_use_same_actual_inputs(tmp_path):
    env = environment(tmp_path / "reports.sqlite3")
    search, _, provider, reranker, _, _ = prepared(env, text="alpha PRIVATE_SOURCE_CANARY")
    outcomes, inputs = [], []
    for options in (
        ExplainOptions(), ExplainOptions(detail="detailed"),
        ExplainOptions(detail="detailed", include_quotes=True),
    ):
        provider.calls.clear()
        reranker.calls.clear()
        result = search.search_explained(env.scope, "alpha", options=options)
        outcomes.append(result.outcome.hits)
        inputs.append((list(provider.calls), list(reranker.calls)))
        wire = result.report.model_dump_json()
        assert ("PRIVATE_SOURCE_CANARY" in wire) == options.include_quotes
        assert ("indexing.candidate" in wire) == (options.detail == "detailed")
    assert outcomes[0] == outcomes[1] == outcomes[2]
    assert inputs[0] == inputs[1] == inputs[2]


@pytest.mark.service
def test_failed_later_attempt_keeps_valid_projection_searchable(tmp_path):
    from support.indexing import process

    from kg.retrieval.dense import DenseIndexError

    env = environment(tmp_path / "failed-attempt.sqlite3")
    search, index, _, _, value, saved = prepared(env, text="alpha")
    before = search.search(env.scope, "alpha").hits

    def unavailable(profile):
        raise DenseIndexError("provider unavailable")

    index._provider_factory = unavailable
    assert process(index, env, value, saved, mode="rebuild").outcome == "failed"
    assert search.search(env.scope, "alpha").hits == before


@pytest.mark.service
def test_stored_corruption_is_not_a_partial_search(tmp_path):
    env = environment(tmp_path / "corrupt.sqlite3")
    search, _, _, _, _, saved = prepared(env, text="alpha")
    with env.database.transaction() as connection:
        connection.execute(
            "UPDATE projection_member SET lexical_input='wrong' WHERE projection_id IN "
            "(SELECT projection_id FROM active_document_projection WHERE document_id=?)",
            (saved.document_id,),
        )
    with pytest.raises(EvidenceServiceError, match="internal_error"):
        search.search(env.scope, "alpha")
    assert not search.diagnostics.recent(env.scope).entries
