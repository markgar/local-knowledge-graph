import math
import sqlite3
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from support.evidence import environment, receipt
from support.indexing import ControlledProvider, process, request, service
from support.knowledge import preset, revision, schema

from kg._execution_budget import Deadline, PrivateBudget, PrivateResourceStop
from kg.evidence._sql import AccountedConnection
from kg.evidence._support import TransactionEvidence
from kg.evidence._transactions import writing
from kg.evidence._values import now, timestamp
from kg.evidence.errors import EvidenceServiceError
from kg.indexing import _storage, _vectors
from kg.indexing._configuration import configuration_id
from kg.indexing._process import Process
from kg.knowledge import KnowledgeAdministration, KnowledgeService
from kg.models.evidence import KnowledgeWriterBinding, LocalAdminAuthority, PolicyGrant
from kg.models.execution import ExecutionReport, ExplainOptions
from kg.models.foundation import (
    ChangeSet,
    ChangeSetReceipt,
    CreateEntity,
    DocumentDependency,
    ExpectedState,
    RemoveDocument,
    SourceSupport,
    SuppliedAnchor,
)
from kg.models.indexing import DEFAULT_CONFIGURATION, IndexConfiguration, ProcessResult
from kg.retrieval.dense import DenseIndexError, EmbeddingProfile


@pytest.mark.parametrize("text", ["", " ", "A\r\nCafe\u0301 \U0001f680\0" * 150])
def test_real_standalone_process_unchanged_and_rebuild(tmp_path, text):
    env = environment(tmp_path / "index.db")
    value = request(env, text=text)
    saved = receipt(env.service.write(value))
    index, provider, loads = service(env)
    assert index.status(env.scope, saved.document_id).status == "pending"
    assert not loads
    result = process(index, env, value, saved)
    assert result.outcome == "ready", result
    assert result.produced_vectors == (len(text) + 1023) // 1024
    current = index.status(env.scope, saved.document_id)
    assert current.status == "ready" and current.provider_compatibility == "unverified"
    assert len(loads) == 1
    evidence = env.service.document(env.scope, saved.document_id)
    assert evidence.processing.indexing == "ready"
    assert evidence.processing.enrichment == "pending"
    assert not index.pending(env.scope).entries
    original_calls = len(provider.calls)
    again = process(index, env, value, saved)
    assert again.outcome == "unchanged" and again.projection_id == result.projection_id
    assert len(loads) == 2 and len(provider.calls) == original_calls
    rebuilt = process(index, env, value, saved, mode="rebuild")
    assert rebuilt.outcome == "ready" and rebuilt.projection_id != result.projection_id
    assert rebuilt.passage_set_id == result.passage_set_id
    assert rebuilt.produced_vectors == result.produced_vectors
    if rebuilt.cleanup_pending:
        while index.cleanup(env.scope, value.attribution, saved.document_id).has_more:
            pass
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM document_projection").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM index_staging_member").fetchone()[0] == 0


@pytest.mark.parametrize("contextual", [False, True])
def test_metadata_change_reuses_only_identical_representation(tmp_path, contextual):
    env = environment(tmp_path / "metadata.db")
    config = IndexConfiguration(
        contextual=contextual,
        representation="generic-title-quote/1" if contextual else "exact-quote/1",
    )
    first = request(env, text="A\r\nB", title="T")
    saved = receipt(env.service.write(first))
    index, provider, _ = service(env)
    initial = process(index, env, first, saved, configuration=config)
    page = env.service.passages(env.scope, saved.document_id, saved.processing.state_version)
    citation = page.entries[0].citation
    if contextual:
        assert provider.calls == [("T\n\nA\r\nB",)]
        assert env.service.document(env.scope, saved.document_id).processing.indexing == "pending"
    second = request(env, text="A\r\nB", title="New", state=saved.processing.state_version)
    updated = receipt(env.service.write(second))
    assert updated.revision_id == saved.revision_id
    assert index.status(env.scope, saved.document_id, config).status == "pending"
    result = process(index, env, second, updated, configuration=config)
    assert result.outcome == "ready"
    assert result.reused_vectors == (0 if contextual else 1)
    assert result.produced_vectors == (1 if contextual else 0)
    assert result.projection_id != initial.projection_id
    assert result.passage_set_id == initial.passage_set_id
    assert env.service.citation(env.scope, citation).metadata.metadata.title == "T"


@pytest.mark.parametrize("profile", list(EmbeddingProfile))
@pytest.mark.parametrize("policy", ["codepoint-window/1", "supplied-anchors/1"])
def test_both_policies_and_profiles_are_real_projections(tmp_path, profile, policy):
    env = environment(tmp_path / "profiles.db")
    text = "abcde"
    anchors = (
        SuppliedAnchor(local_id="b", start=1, end=4, quote="bcd"),
        SuppliedAnchor(local_id="a", start=0, end=5, quote=text),
    )
    value = request(env, text=text, policy=policy, anchors=anchors)
    saved = receipt(env.service.write(value))
    index, provider, _ = service(env, ControlledProvider(profile))
    result = process(
        index, env, value, saved, configuration=IndexConfiguration(embedding_profile=profile.value)
    )
    assert result.outcome == "ready"
    assert result.produced_vectors == (1 if policy == "codepoint-window/1" else 2)
    assert sum(len(batch) for batch in provider.calls) == result.produced_vectors
    with env.database.connection() as connection:
        rows = connection.execute("SELECT * FROM projection_member ORDER BY ordinal").fetchall()
        for row in rows:
            assert row["dimensions"] == provider.dimensions
            vector = struct.unpack(f"<{provider.dimensions}f", row["vector"])
            assert math.hypot(*vector) == pytest.approx(1.0, abs=1e-6)
    assert ProcessResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("policy", ["unknown/1", "supplied-anchors/1"])
def test_unsupported_policy_records_failure_without_provider_or_false_readiness(tmp_path, policy):
    env = environment(tmp_path / "policy.db")
    value = request(env, policy=policy)
    saved = receipt(env.service.write(value))
    index, _, loads = service(env)
    result = process(index, env, value, saved)
    assert result.outcome == "failed" and result.reason == "unsupported_policy"
    assert not loads
    status = index.status(env.scope, saved.document_id)
    assert status.status == "failed" and status.passages == "not_processed"
    assert env.service.document(env.scope, saved.document_id).processing.indexing == "failed"
    assert index.pending(env.scope).entries == (status,)


@pytest.mark.parametrize("bad", ["count", "dimension", "nan", "infinity", "zero"])
def test_invalid_vectors_never_publish_and_keep_real_passages(tmp_path, bad):
    env = environment(tmp_path / "invalid.db")
    value = request(env)
    saved = receipt(env.service.write(value))
    index, provider, _ = service(env)

    def encode(texts, **kw):
        if bad == "count":
            return []
        if bad == "dimension":
            return [[1.0]]
        first = {"nan": float("nan"), "infinity": float("inf"), "zero": 0.0}[bad]
        return [[first] + [0.0] * (provider.dimensions - 1)]

    provider.encode_documents = encode
    result = process(index, env, value, saved)
    assert result.outcome == "failed" and result.reason == "invalid_provider_output"
    assert index.status(env.scope, saved.document_id).status == "failed"
    assert (
        env.service.passages(
            env.scope,
            saved.document_id,
            saved.processing.state_version,
        ).status
        == "complete"
    )
    with env.database.connection() as connection:
        assert (
            connection.execute("SELECT count(*) FROM active_document_projection").fetchone()[0] == 0
        )


def test_unavailable_runtime_changed_and_failed_rebuild_preserve_good_projection(tmp_path):
    env = environment(tmp_path / "runtime.db")
    value = request(env)
    saved = receipt(env.service.write(value))
    index, _, _ = service(env)
    original = process(index, env, value, saved)

    def unavailable(profile):
        raise DenseIndexError("sensitive provider path MUST NOT LEAK")

    index._provider_factory = unavailable
    failed = process(index, env, value, saved, mode="rebuild")
    assert failed.reason == "provider_unavailable"
    status = index.status(env.scope, saved.document_id)
    assert status.status == "ready" and status.projection_id == original.projection_id
    assert status.latest_attempt.status == "failed"
    assert "sensitive" not in status.model_dump_json()
    assert env.service.document(env.scope, saved.document_id).processing.indexing == "ready"
    changed, provider, _ = service(env, ControlledProvider(runtime="other-runtime"))
    assert changed.status(env.scope, saved.document_id).provider_compatibility == "unverified"
    result = process(changed, env, value, saved)
    assert result.outcome == "ready" and result.produced_vectors == 1
    assert provider.calls


@pytest.mark.parametrize(
    "attribute,value",
    [
        ("revision", "wrong"),
        ("name", "wrong"),
        ("dimensions", 42),
        ("query_encoding", "wrong"),
        ("document_encoding", "wrong"),
        ("normalization", "none"),
        ("pipeline_version", "made-up"),
    ],
)
def test_exact_provider_identity_is_mandatory(tmp_path, attribute, value):
    env = environment(tmp_path / "identity.db")
    req = request(env, text="")
    saved = receipt(env.service.write(req))
    index, provider, _ = service(env)
    setattr(provider, attribute, value)
    result = process(index, env, req, saved)
    assert result.reason == "invalid_provider_output" and not provider.calls
    assert index.status(env.scope, saved.document_id).status == "failed"


@pytest.mark.parametrize("late_failure", [False, True])
def test_two_file_backed_workers_newer_attempt_fences_late_result(tmp_path, late_failure):
    env = environment(tmp_path / "race.db")
    value = request(env, text="x" * 9000)
    saved = receipt(env.service.write(value))
    first, provider, _ = service(env)
    second, _, _ = service(env)
    entered, resume = Event(), Event()

    def blocked():
        entered.set()
        assert resume.wait(20)
        if late_failure:
            raise DenseIndexError("late failure")

    provider.before_encode = blocked
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(process, first, env, value, saved)
        try:
            assert entered.wait(20)
            winner = process(second, env, value, saved)
            assert winner.outcome == "ready"
        finally:
            resume.set()
        loser = future.result(timeout=20)
    assert loser.outcome == "stale" and loser.reason == "superseded_attempt"
    assert second.status(env.scope, saved.document_id).projection_id == winner.projection_id
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM index_staging_member").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM document_projection").fetchone()[0] == 1
        assert (
            connection.execute(
                "SELECT status FROM index_attempt WHERE attempt_id=?",
                (loser.attempt_id,),
            ).fetchone()[0]
            == "superseded"
        )


@pytest.mark.parametrize("change", ["text", "title", "policy"])
def test_edit_while_encoding_rejects_stale_output(tmp_path, change):
    env = environment(tmp_path / "state.db")
    value = request(env, text="one")
    saved = receipt(env.service.write(value))
    index, provider, _ = service(env)
    changed = []

    def edit():
        updated = request(
            env,
            text="two" if change == "text" else "one",
            title="new" if change == "title" else "Title",
            policy="supplied-anchors/1" if change == "policy" else "codepoint-window/1",
            anchors=(SuppliedAnchor(local_id="whole", start=0, end=3, quote="one"),)
            if change == "policy"
            else (),
            state=saved.processing.state_version,
        )
        changed.append((updated, receipt(env.service.write(updated))))
        provider.before_encode = None

    provider.before_encode = edit
    result = process(index, env, value, saved)
    assert result.outcome == "stale" and result.reason == "state_changed"
    assert index.status(env.scope, saved.document_id).status == "pending"
    updated, new_saved = changed[0]
    assert process(index, env, updated, new_saved).outcome == "ready"
    assert (
        env.service.passages(
            env.scope,
            saved.document_id,
            saved.processing.state_version,
        ).status
        == "complete"
    )


def test_crash_after_passages_and_partial_staging_restarts_not_resumes(tmp_path, monkeypatch):
    env = environment(tmp_path / "crash.db")
    value = request(env, text="x" * 16000)
    saved = receipt(env.service.write(value))
    index, provider, _ = service(env)
    original = provider.encode_documents
    count = 0

    def crash(texts, **kw):
        nonlocal count
        count += 1
        if count == 2:
            raise RuntimeError("simulated abrupt host failure")
        return original(texts, **kw)

    monkeypatch.setattr(provider, "encode_documents", crash)
    with pytest.raises(RuntimeError):
        process(index, env, value, saved)
    assert index.status(env.scope, saved.document_id).status == "pending"
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM index_staging_member").fetchone()[0] > 0
        assert connection.execute("SELECT count(*) FROM document_projection").fetchone()[0] == 0
    restarted, fresh, _ = service(env)
    recovered = process(restarted, env, value, saved)
    assert recovered.outcome == "ready" and recovered.produced_vectors == 16
    assert sum(len(batch) for batch in fresh.calls) == 16
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM index_staging_member").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM state_passage_set").fetchone()[0] == 1


def test_staging_transaction_failure_rolls_back_whole_batch(tmp_path, monkeypatch):
    env = environment(tmp_path / "rollback.db")
    value = request(env, text="x" * 3000)
    saved = receipt(env.service.write(value))
    index, _, _ = service(env)
    original = AccountedConnection.execute
    inserts = 0

    def fail_second(connection, sql, parameters=()):
        nonlocal inserts
        if sql.startswith("INSERT INTO index_staging_member"):
            inserts += 1
            if inserts == 2:
                raise sqlite3.OperationalError("simulated storage failure")
        return original(connection, sql, parameters)

    monkeypatch.setattr(AccountedConnection, "execute", fail_second)
    result = process(index, env, value, saved)
    assert result.outcome == "failed" and result.reason == "storage_failure"
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM index_staging_member").fetchone()[0] == 0
        assert (
            connection.execute("SELECT count(*) FROM active_document_projection").fetchone()[0] == 0
        )


def test_publication_failure_preserves_old_projection_and_canonical_evidence(tmp_path, monkeypatch):
    env = environment(tmp_path / "publication.db")
    value = request(env)
    saved = receipt(env.service.write(value))
    index, _, _ = service(env)
    previous = process(index, env, value, saved)
    original = Process.finish

    def fail(self, context, outcome):
        original(self, context, outcome)
        raise sqlite3.OperationalError("before owner commit")

    monkeypatch.setattr(Process, "finish", fail)
    result = process(index, env, value, saved, mode="rebuild")
    assert result.reason == "storage_failure"
    assert index.status(env.scope, saved.document_id).projection_id == previous.projection_id
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM document_projection").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM passage_set").fetchone()[0] == 1
    cleaned = index.cleanup(env.scope, value.attribution, saved.document_id, limit=1)
    assert cleaned.removed == 1 and not cleaned.has_more


def test_policy_fence_and_effective_default_never_destroy_history(tmp_path):
    env = environment(tmp_path / "access.db")
    policy = env.policy.model_copy(
        update={
            "grants": (
                *env.policy.grants,
                *(
                    PolicyGrant(principal_id="principal", namespace=ns, grant="write_knowledge")
                    for ns in env.scope.access.namespaces
                ),
            )
        }
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
    saved = receipt(env.service.write(value))
    index, _, _ = service(env)
    process(index, env, value, saved)
    passage = env.service.passages(
        env.scope,
        saved.document_id,
        saved.processing.state_version,
    ).entries[0]
    with env.database.transaction() as connection:
        connection.execute(
            "UPDATE source_namespace SET policy_token='rotated' "
            "WHERE corpus_id=? AND namespace='markdown'",
            (env.scope.corpus_id,),
        )
    assert index.status(env.scope, saved.document_id).reason == "state_changed"
    view = env.service.document(env.scope, saved.document_id)
    assert view.processing.indexing == "pending" and view.indexing_reason == "state_changed"
    assert env.service.citation(env.scope, passage.citation).quote == passage.quote
    dependency = DocumentDependency(
        source_namespace=passage.reference.source_namespace,
        document_id=saved.document_id,
        revision_id=saved.revision_id,
        state_version=saved.processing.state_version,
    )
    with (
        writing(env.database, env.service.identity) as context,
        pytest.raises(EvidenceServiceError, match="state_conflict"),
    ):
        TransactionEvidence(context).validate_current(
            env.scope,
            (dependency,),
            (passage.reference,),
        )
    assert process(index, env, value, saved).outcome == "stale"


def test_normal_policy_rotation_invalidates_state_without_rewriting_old_citation(tmp_path):
    env = environment(tmp_path / "rotation.db")
    value = request(env)
    saved = receipt(env.service.write(value))
    index, _, _ = service(env)
    process(index, env, value, saved)
    page = env.service.passages(env.scope, saved.document_id, saved.processing.state_version)
    changed = env.admin.replace_policy(
        env.policy.model_copy(
            update={
                "grants": (
                    *env.policy.grants,
                    PolicyGrant(
                        principal_id="another",
                        namespace="markdown",
                        grant="read",
                    ),
                ),
            }
        ),
        env.scope.access.policy_version,
    )
    env.scope = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(
                update={
                    "policy_version": changed.policy_version,
                }
            )
        }
    )
    current = env.service.document(env.scope, saved.document_id)
    assert current.state_version != saved.processing.state_version
    assert current.processing.indexing == "pending"
    assert index.status(env.scope, saved.document_id).status == "pending"
    assert env.service.citation(env.scope, page.entries[0].citation).quote == page.entries[0].quote


def test_reports_observe_once_and_never_contain_source_or_provider_exception(tmp_path):
    env = environment(tmp_path / "reports.db")
    value = request(env, text="SECRET_SOURCE_CANARY")
    saved = receipt(env.service.write(value))
    index, provider, _ = service(env)
    result = index.process_explained(
        env.scope,
        value.attribution,
        saved.document_id,
        saved.processing.state_version,
        options=ExplainOptions(detail="detailed"),
    )
    assert result.outcome.outcome == "ready" and len(provider.calls) == 1
    assert isinstance(result.report, ExecutionReport)
    assert "SECRET_SOURCE_CANARY" not in result.report.model_dump_json()
    phases = [
        event.event.phase for event in result.report.events if event.event.kind == "indexing.phase"
    ]
    assert phases == ["admission", "passage", "vector", "publication"]
    index.diagnostics.recent(env.scope)
    assert len(provider.calls) == 1
    again = index.process_explained(
        env.scope,
        value.attribution,
        saved.document_id,
        saved.processing.state_version,
    )
    assert again.outcome.outcome == "unchanged" and len(provider.calls) == 1


@pytest.mark.parametrize("vector", [[0.0, 0.0], [float("nan"), 1.0], [float("inf"), 1.0]])
def test_canonical_vector_rejects_invalid_values(vector):
    with pytest.raises(_vectors.InvalidVector):
        _vectors.encode(vector, 2)


def test_canonical_vector_is_normalized_float32():
    encoded = _vectors.encode([3.0, 4.0], 2)
    assert struct.unpack("<2f", encoded) == pytest.approx((0.6, 0.8))
    _vectors.validate(encoded, 2)


def test_unchanged_projection_does_not_fake_ready_after_empty_member_table(tmp_path):
    env = environment(tmp_path / "missing.db")
    value = request(env)
    saved = receipt(env.service.write(value))
    index, _, _ = service(env)
    process(index, env, value, saved)
    with env.database.transaction() as connection:
        connection.execute("DELETE FROM projection_member")
    with pytest.raises(EvidenceServiceError, match="internal_error"):
        index.status(env.scope, saved.document_id)
    with pytest.raises(EvidenceServiceError, match="internal_error"):
        env.service.document(env.scope, saved.document_id)
    repaired = process(index, env, value, saved, mode="rebuild")
    assert repaired.outcome == "ready" and repaired.produced_vectors == 1


def test_cleanup_preserves_live_staging_but_drains_provably_stale_attempt(tmp_path):
    env = environment(tmp_path / "stale-cleanup.db")
    value = request(env, text="x" * 16000)
    saved = receipt(env.service.write(value))
    index, provider, _ = service(env)
    original = provider.encode_documents
    calls = 0

    def crash(texts, **kw):
        nonlocal calls
        calls += 1
        if calls > 1:
            raise RuntimeError("host died")
        return original(texts, **kw)

    provider.encode_documents = crash
    with pytest.raises(RuntimeError):
        process(index, env, value, saved)
    assert index.cleanup(env.scope, value.attribution, saved.document_id).removed == 0
    updated = request(env, text="x" * 16000, title="new", state=saved.processing.state_version)
    receipt(env.service.write(updated))
    removed = 0
    while True:
        cleanup = index.cleanup(env.scope, value.attribution, saved.document_id, limit=1)
        assert cleanup.removed <= 1
        removed += cleanup.removed
        if not cleanup.has_more:
            break
    assert removed > 0
    with env.database.connection() as connection:
        assert connection.execute("SELECT status FROM index_attempt").fetchone()[0] == "stale"
        assert connection.execute("SELECT count(*) FROM passage").fetchone()[0] == 16


def test_terminal_summary_pruning_preserves_monotonic_fence(tmp_path):
    env = environment(tmp_path / "pruning.db")
    value = request(env)
    saved = receipt(env.service.write(value))
    with writing(env.database, env.service.identity) as context:
        for _ in range(105):
            attempt = _storage.admit(
                context.connection,
                env.service.identity,
                env.scope,
                value.attribution,
                saved.document_id,
                saved.processing.state_version,
                DEFAULT_CONFIGURATION,
            )
            context.connection.execute(
                "UPDATE index_attempt SET status='failed',completed_at=? WHERE attempt_id=?",
                (timestamp(now()), attempt.attempt_id),
            )
    index, _, _ = service(env)
    cleaned = index.cleanup(env.scope, value.attribution, saved.document_id, limit=3)
    assert cleaned.removed == 3 and cleaned.has_more
    assert index.cleanup(env.scope, value.attribution, saved.document_id, limit=3).removed == 2
    result = process(index, env, value, saved)
    assert result.outcome == "ready"
    with env.database.connection() as connection:
        assert connection.execute("SELECT fence FROM index_work_slot").fetchone()[0] == 106
        assert connection.execute("SELECT count(*) FROM index_attempt").fetchone()[0] == 100


def test_bounded_pending_scoped_cursor_and_model_free_inspection(tmp_path):
    env = environment(tmp_path / "pending.db")
    index, _, loads = service(env)
    documents = {}
    for namespace, external in [
        ("markdown", "one"),
        ("markdown", "two"),
        ("markdown", "three"),
        ("email", "one"),
    ]:
        value = request(env, namespace=namespace, external=external)
        saved = receipt(env.service.write(value))
        documents[saved.document_id] = (value, saved)
    ready_id = min(documents)
    value, saved = documents[ready_id]
    process(index, env, value, saved)
    loads.clear()
    after = None
    seen = []
    while True:
        page = index.pending(env.scope, after_document_id=after, limit=1)
        seen.extend(entry.document_id for entry in page.entries)
        if not page.has_more:
            break
        assert page.next_after_document_id != after
        after = page.next_after_document_id
    assert sorted(seen) == sorted(set(documents) - {ready_id})
    assert not loads
    scoped = env.scope.model_copy(
        update={
            "access": env.scope.access.model_copy(
                update={
                    "namespaces": ("markdown",),
                }
            )
        }
    )
    assert all(
        documents[entry.document_id][0].payload.document.source_namespace == "markdown"
        for entry in index.pending(scoped).entries
    )
    denied_id = next(
        key
        for key, (val, _) in documents.items()
        if val.payload.document.source_namespace == "email"
    )
    with pytest.raises(EvidenceServiceError, match="not_found"):
        index.status(scoped, denied_id)


def test_remove_restore_and_readiness_remain_separate_from_historical_citation(tmp_path):
    env = environment(tmp_path / "restore.db")
    value = request(env)
    saved = receipt(env.service.write(value))
    index, _, _ = service(env)
    result = process(index, env, value, saved)
    passage = env.service.passages(
        env.scope,
        saved.document_id,
        saved.processing.state_version,
    ).entries[0]
    remove = value.model_copy(
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
    removed = receipt(env.service.write(remove))
    status = index.status(env.scope, saved.document_id)
    assert status.status == "inactive" and not index.pending(env.scope).entries
    assert env.service.document(env.scope, saved.document_id).processing.indexing == "pending"
    assert env.service.citation(env.scope, passage.citation).quote == passage.quote
    restored = request(env, state=removed.processing.state_version)
    restored_saved = receipt(env.service.write(restored))
    again = process(index, env, restored, restored_saved)
    assert again.outcome == "ready" and again.projection_id != result.projection_id
    assert again.passage_set_id == result.passage_set_id


def test_vector_rebuild_keeps_actual_same_transaction_support_valid(tmp_path):
    env = environment(tmp_path / "support.db")
    policy = env.policy.model_copy(
        update={
            "grants": (
                *env.policy.grants,
                *(
                    PolicyGrant(principal_id="principal", namespace=ns, grant="write_knowledge")
                    for ns in env.scope.access.namespaces
                ),
            )
        }
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
    saved = receipt(env.service.write(value))
    index, _, _ = service(env)
    initial = process(index, env, value, saved)
    reference = (
        env.service.passages(
            env.scope,
            saved.document_id,
            saved.processing.state_version,
        )
        .entries[0]
        .reference
    )
    dependency = DocumentDependency(
        source_namespace=reference.source_namespace,
        document_id=saved.document_id,
        revision_id=saved.revision_id,
        state_version=saved.processing.state_version,
    )
    with writing(env.database, env.service.identity) as context:
        support = TransactionEvidence(context).validate_current(
            env.scope,
            (dependency,),
            (reference,),
        )
    rebuilt = process(index, env, value, saved, mode="rebuild")
    assert rebuilt.projection_id != initial.projection_id
    with writing(env.database, env.service.identity) as context:
        assert (
            TransactionEvidence(context).validate_current(
                env.scope,
                (dependency,),
                (reference,),
            )
            == support
        )


@pytest.mark.parametrize("index_first", [False, True])
def test_vector_rebuild_preserves_committed_k1_anchor_support(tmp_path, monkeypatch, index_first):
    env = environment(tmp_path / "committed-support.db")
    policy = env.policy.model_copy(
        update={
            "grants": env.policy.grants
            + tuple(
                PolicyGrant(principal_id="principal", namespace=ns, grant="write_knowledge")
                for ns in env.scope.access.namespaces
            ),
            "knowledge_bindings": tuple(
                KnowledgeWriterBinding(
                    namespace=ns, principal_id="principal", owner_id="owner", writer_id="writer"
                )
                for ns in env.scope.access.namespaces
            ),
        }
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
    KnowledgeAdministration(
        env.database, LocalAdminAuthority(principal_id="admin")
    ).register_knowledge_schema(preset(schema()))
    value = request(
        env,
        text="supported source",
        anchors=(SuppliedAnchor(local_id="proof", start=0, end=9, quote="supported"),),
    )
    saved = receipt(env.service.write(value))
    anchor = env.service.anchors(
        env.scope, saved.document_id, saved.processing.state_version
    ).entries[0]
    index, provider, _ = service(env)
    budgets = []
    budget_factory = index._budget

    def budget():
        root = budget_factory()
        budgets.append(root)
        return root

    monkeypatch.setattr(index, "_budget", budget)
    if index_first:
        assert process(index, env, value, saved).outcome == "ready"
    before_enrich = env.service.document(env.scope, saved.document_id).processing
    enrichment = value.model_copy(
        update={
            "request_id": "enrich",
            "retry_key": "enrich",
            "payload": ChangeSet(expected_schema_revision=revision(env),
                operation="enrich",
                dependencies=(
                    DocumentDependency(
                        source_namespace=anchor.reference.source_namespace,
                        document_id=saved.document_id,
                        revision_id=saved.revision_id,
                        state_version=saved.processing.state_version,
                    ),
                ),
                changes=(
                    CreateEntity(
                        kind="entity",
                        local_id="project",
                        name="Supported project",
                        support=SourceSupport(kind="source", evidence=(anchor.reference,)),
                    ),
                ),
            ),
        }
    )
    outcome = env.service.write(enrichment)
    assert outcome.error is None
    assert isinstance(outcome.receipt, ChangeSetReceipt)
    entity_id = outcome.receipt.mappings[0].stored_id
    knowledge = KnowledgeService(env.database, env.service.identity)
    entity = knowledge.entity(env.scope, entity_id)
    contribution = knowledge.contribution(env.scope, entity.witness.contribution_id)
    assert entity.is_current and contribution.is_current and contribution.evidence
    assert env.service.document(env.scope, saved.document_id).processing == before_enrich
    initial = process(index, env, value, saved)
    assert initial.outcome == ("unchanged" if index_first else "ready")
    ready = env.service.document(env.scope, saved.document_id).processing
    assert ready.indexing == "ready" and ready.enrichment == "pending"
    alternate = process(
        index,
        env,
        value,
        saved,
        configuration=IndexConfiguration(contextual=True, representation="generic-title-quote/1"),
    )
    assert alternate.outcome == "ready"
    assert env.service.document(env.scope, saved.document_id).processing == ready

    def during_inference():
        assert knowledge.contribution(env.scope, contribution.contribution_id) == contribution

    provider.before_encode = during_inference
    rebuilt = process(index, env, value, saved, mode="rebuild")
    assert rebuilt.outcome == "ready" and not rebuilt.cleanup_pending
    assert rebuilt.projection_id != initial.projection_id
    assert rebuilt.passage_set_id == initial.passage_set_id
    assert knowledge.entity(env.scope, entity_id) == entity
    assert knowledge.contribution(env.scope, contribution.contribution_id) == contribution
    assert env.service.citation(env.scope, anchor.citation).quote == anchor.quote
    assert env.service.document(env.scope, saved.document_id).processing == ready
    assert env.service.write(enrichment).receipt == outcome.receipt
    assert len(budgets) == 3 + int(index_first)
    assert all(
        root._root is root
        and 0 < root._vm <= 10_000_000
        and 0 < root._visits <= 100_000
        and root._scratch == 0
        for root in budgets
    )


def test_expired_root_deadline_discards_late_provider_output(tmp_path, monkeypatch):
    env = environment(tmp_path / "late.db")
    value = request(env)
    saved = receipt(env.service.write(value))
    index, provider, _ = service(env)
    budget = index._budget()
    monkeypatch.setattr(index, "_budget", lambda: budget)
    provider.before_encode = lambda: setattr(budget, "deadline", Deadline(0))
    with pytest.raises(EvidenceServiceError, match="budget_exceeded"):
        process(index, env, value, saved)
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM projection_member").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM index_staging_member").fetchone()[0] == 0
    assert budget._scratch == 0


def test_oversized_single_representation_is_not_silently_truncated_for_budget(tmp_path):
    env = environment(tmp_path / "oversized.db")
    text = "x" * 9000
    value = request(
        env,
        text=text,
        policy="supplied-anchors/1",
        anchors=(SuppliedAnchor(local_id="large", start=0, end=len(text), quote=text),),
    )
    saved = receipt(env.service.write(value))
    index, provider, _ = service(env)
    with pytest.raises(EvidenceServiceError, match="budget_exceeded"):
        process(index, env, value, saved)
    assert not provider.calls
    assert (
        env.service.passages(
            env.scope,
            saved.document_id,
            saved.processing.state_version,
        )
        .entries[0]
        .quote
        == text
    )


def test_no_claim_participant_or_search_capability_is_faked(tmp_path):
    env = environment(tmp_path / "unsupported.db")
    index, _, _ = service(env)
    assert index.capabilities().unsupported == ("search", "coordinated_process")
    with pytest.raises(EvidenceServiceError, match="unsupported"):
        index._process_coordinated(object())
    assert not hasattr(index, "search")


def test_configuration_hash_binds_representation_and_profile():
    baseline = configuration_id(DEFAULT_CONFIGURATION)
    assert baseline != configuration_id(
        IndexConfiguration(
            contextual=True,
            representation="generic-title-quote/1",
        )
    )
    assert baseline != configuration_id(
        IndexConfiguration(embedding_profile="qwen3-embedding-0.6b")
    )
    assert baseline == configuration_id(IndexConfiguration())


def test_commit_then_lost_response_is_observable_and_retry_verifies_unchanged(
    tmp_path, monkeypatch
):
    env = environment(tmp_path / "lost-response.db")
    value = request(env)
    saved = receipt(env.service.write(value))
    index, _, _ = service(env)
    original_execute = AccountedConnection.execute
    original_commit = AccountedConnection.commit

    def execute(connection, sql, parameters=()):
        result = original_execute(connection, sql, parameters)
        if sql.startswith("INSERT INTO active_document_projection"):
            connection._simulate_lost_response = True
        return result

    def commit(connection):
        original_commit(connection)
        if getattr(connection, "_simulate_lost_response", False):
            raise sqlite3.OperationalError("commit succeeded; response lost")

    with monkeypatch.context() as patch:
        patch.setattr(AccountedConnection, "execute", execute)
        patch.setattr(AccountedConnection, "commit", commit)
        with pytest.raises(EvidenceServiceError, match="internal_error"):
            process(index, env, value, saved)
    assert index.status(env.scope, saved.document_id).status == "ready"
    assert process(index, env, value, saved).outcome == "unchanged"


def test_status_releases_no_snapshot_after_concurrent_commit(tmp_path, monkeypatch):
    from kg.indexing import _inspection

    env = environment(tmp_path / "observer.db")
    value = request(env)
    saved = receipt(env.service.write(value))
    index, _, _ = service(env)
    process(index, env, value, saved)
    original = _inspection.status

    def changed(*args, **kw):
        result = original(*args, **kw)
        receipt(
            env.service.write(request(env, title="changed", state=saved.processing.state_version))
        )
        return result

    monkeypatch.setattr(_inspection, "status", changed)
    with pytest.raises(EvidenceServiceError, match="state_changed"):
        index.status(env.scope, saved.document_id)


def test_diagnostic_allocation_failure_cannot_change_projection(tmp_path, monkeypatch):
    from kg.indexing import _process

    env = environment(tmp_path / "diagnostic-fault.db")
    value = request(env)
    saved = receipt(env.service.write(value))
    index, provider, _ = service(env)

    def exhausted(*args, **kwargs):
        raise MemoryError("diagnostic-only allocation")

    monkeypatch.setattr(_process, "IndexPhase", exhausted)
    result = process(index, env, value, saved)
    assert result.outcome == "ready" and len(provider.calls) == 1
    assert index.status(env.scope, saved.document_id).status == "ready"


def test_context_metadata_reserves_before_fetch_and_holds_through_use(tmp_path):
    from kg.indexing._inspection import title
    from kg.models.foundation import MetadataEntry

    env = environment(tmp_path / "metadata-budget.db")
    value = request(env)
    value = value.model_copy(
        update={
            "payload": value.payload.model_copy(
                update={
                    "metadata": value.payload.metadata.model_copy(
                        update={
                            "attributes": tuple(
                                MetadataEntry(key=f"key{i}", value="\U0001f680" * 4096)
                                for i in range(100)
                            )
                        }
                    ),
                }
            )
        }
    )
    saved = receipt(env.service.write(value))
    budget = PrivateBudget(Deadline(time.monotonic() + 30))
    statements = []
    with env.database.connection() as connection:
        connection.set_trace_callback(statements.append)
        with (
            budget.reserve_scratch(128 << 20, "general"),
            pytest.raises(PrivateResourceStop),
            title(connection, saved.document_id, saved.processing.state_version, budget),
        ):
            pytest.fail("Metadata read bypassed the remaining scratch limit")
        assert not any(sql.startswith("SELECT m.metadata_json") for sql in statements)
        with title(connection, saved.document_id, saved.processing.state_version, budget) as result:
            assert result == "Title" and budget._scratch > 1_600_000
        assert budget._scratch == 0


def test_explicit_cleanup_retires_inactive_pointer_within_row_limit(tmp_path):
    env = environment(tmp_path / "inactive-cleanup.db")
    value = request(env)
    saved = receipt(env.service.write(value))
    index, _, _ = service(env)
    result = process(index, env, value, saved)
    passage = env.service.passages(
        env.scope,
        saved.document_id,
        saved.processing.state_version,
    ).entries[0]
    removed = value.model_copy(
        update={
            "retry_key": "cleanup-remove",
            "payload": RemoveDocument(
                operation="remove_document",
                document=value.payload.document,
                precondition=ExpectedState(
                    kind="match", state_version=saved.processing.state_version
                ),
            ),
        }
    )
    receipt(env.service.write(removed))
    count = 0
    while True:
        cleanup = index.cleanup(env.scope, value.attribution, saved.document_id, limit=1)
        assert cleanup.removed <= 1
        count += cleanup.removed
        if not cleanup.has_more:
            break
    assert count == 3  # Pointer, one vector/lexical member, projection manifest.
    with env.database.connection() as connection:
        for table in ("active_document_projection", "document_projection", "projection_member"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT passage_set_id FROM state_passage_set WHERE state_version=?",
                (saved.processing.state_version,),
            ).fetchone()[0]
            == result.passage_set_id
        )
    assert env.service.citation(env.scope, passage.citation).quote == passage.quote


def test_postcommit_budget_stop_is_explicit_pending_then_reclaimed(tmp_path, monkeypatch):
    env = environment(tmp_path / "cleanup-pending.db")
    value = request(env)
    saved = receipt(env.service.write(value))
    index, _, _ = service(env)
    previous = process(index, env, value, saved)
    original = Process.cleanup_after_commit

    def expire(self):
        self.budget.deadline = Deadline(0)
        return original(self)

    monkeypatch.setattr(Process, "cleanup_after_commit", expire)
    result = process(index, env, value, saved, mode="rebuild")
    assert result.outcome == "ready" and result.cleanup_pending
    assert result.projection_id != previous.projection_id
    assert index.status(env.scope, saved.document_id).projection_id == result.projection_id
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM document_projection").fetchone()[0] == 2
    cleanup = index.cleanup(env.scope, value.attribution, saved.document_id, limit=1)
    assert cleanup.removed == 1 and cleanup.has_more
    cleanup = index.cleanup(env.scope, value.attribution, saved.document_id, limit=1)
    assert cleanup.removed == 1 and not cleanup.has_more
    assert index.status(env.scope, saved.document_id).projection_id == result.projection_id


def test_constructed_configuration_is_revalidated_before_admission(tmp_path):
    env = environment(tmp_path / "forged.db")
    value = request(env)
    saved = receipt(env.service.write(value))
    index, _, loads = service(env)
    for configuration in (
        IndexConfiguration.model_construct(embedding_profile="unregistered"),
        IndexConfiguration.model_construct(contextual=True, representation="exact-quote/1"),
    ):
        with pytest.raises(EvidenceServiceError, match="invalid_request"):
            process(index, env, value, saved, configuration=configuration)
    assert not loads
    with env.database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM index_attempt").fetchone()[0] == 0
