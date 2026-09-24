import time
from contextlib import contextmanager

import pytest
from support.query_knowledge import plan, produce, setup, write
from support.withdrawal import withdrawal

from kg._execution_budget import Deadline, LocalExecutionMeter, PrivateBudget, PrivateResourceStop
from kg.evidence import EvidenceServiceError
from kg.evidence._read_context import observe, read_context
from kg.knowledge._reader import KnowledgeReader
from kg.knowledge._selection import DecisionSelectionItem
from kg.knowledge._store import RevalidationCache, Store
from kg.models.foundation import CreateEntity
from kg.query import QueryService


@contextmanager
def context(env, *, scope=None):
    scope = scope or env.scope
    budget = PrivateBudget(Deadline(time.monotonic() + 30))
    meter = LocalExecutionMeter(budget, max_operations=1, max_items=1000)
    with (
        observe(env.database, env.service.identity, scope, budget.deadline, budget) as observer,
        read_context(
            env.database,
            env.service.identity,
            scope,
            observer.session_id,
            budget.deadline,
            meter.begin_step("inspect"),
        ) as current,
    ):
        yield current, budget


def membership(env, count=2):
    subject, _ = produce(env, count)
    with QueryService(env.database, env.service.identity) as service:
        result = service.execute(plan(env, subject))
        assert result.result.error is None, result
        retained = service._support.sets[result.result.result_set_id]
        return tuple(DecisionSelectionItem.model_validate_json(p) for p in retained.members)


def test_withdrawal_checked_before_warm_exact_witness_cache(tmp_path):
    env = setup(tmp_path / "withdrawn.db")
    first, second = membership(env)
    assert env.service.write(withdrawal(env, second.record.record_id)).status == "applied"
    with context(env) as (current, budget):
        reader = KnowledgeReader(current, env.service.identity)
        try:
            reader.revalidate_member(first)
            cached = budget._scratch
            with pytest.raises(EvidenceServiceError, match="state_changed"):
                reader.revalidate_member(second)
            assert budget._scratch == cached
        finally:
            reader.close()


@pytest.mark.parametrize("altered", ["sequence", "basis", "support", "schema"])
def test_cached_revalidation_rechecks_complete_supplied_bundle(tmp_path, altered):
    env = setup(tmp_path / "tampered.db")
    first, second = membership(env)
    with context(env) as (current, budget):
        reader = KnowledgeReader(current, env.service.identity)
        try:
            reader.revalidate_member(first)
            cached = budget._scratch
            reader.revalidate_member(second)
            assert budget._scratch == cached > 0
            dependencies = second.dependencies
            witness = dependencies.subject_witness
            if altered == "sequence":
                witness = witness.model_copy(
                    update={
                        "contribution_sequence": witness.contribution_sequence + 1,
                    }
                )
            elif altered == "basis":
                proof = witness.basis.evidence[0].model_copy(update={"namespace_token": "changed"})
                witness = witness.model_copy(
                    update={
                        "basis": witness.basis.model_copy(
                            update={
                                "evidence": (proof,),
                            }
                        )
                    }
                )
            elif altered == "support":
                proof = dependencies.assertion_support[0].model_copy(
                    update={
                        "metadata_snapshot_id": "changed",
                    }
                )
                dependencies = dependencies.model_copy(update={"assertion_support": (proof,)})
            dependencies = dependencies.model_copy(update={"subject_witness": witness})
            bad = second.model_copy(update={"dependencies": dependencies})
            if altered == "schema":
                bad = bad.model_copy(update={"schema_version": "wrong"})
            with pytest.raises(EvidenceServiceError) as error:
                reader.revalidate_member(bad)
            assert error.value.failure.code == "state_changed"
            reader.revalidate_member(second)
        finally:
            reader.close()
        assert budget._scratch == 0
        assert not reader._revalidation.proofs and not reader._revalidation.witnesses
        reader.close()


def test_cache_has_no_cross_context_or_scope_authority(tmp_path):
    env = setup(tmp_path / "contexts.db")
    member = membership(env, 1)[0]
    with context(env) as (first, first_budget):
        reader = KnowledgeReader(first, env.service.identity)
        reader.revalidate_member(member)
        cache = reader._revalidation
        narrowed = env.scope.model_copy(
            update={
                "access": env.scope.access.model_copy(
                    update={
                        "namespaces": ("markdown",),
                    }
                )
            }
        )
        with context(env, scope=narrowed) as (second, second_budget):
            with pytest.raises(EvidenceServiceError) as error:
                Store(second.connection, second.scope, second_budget, context=second, cache=cache)
            assert error.value.failure.code == "invalid_request"
            other = KnowledgeReader(second, env.service.identity)
            other.revalidate_member(member)
            assert other._revalidation is not cache
            other.close()
            assert second_budget._scratch == 0
    assert first_budget._scratch == 0
    with pytest.raises(EvidenceServiceError):
        reader.revalidate_member(member)
    reader.close()


@pytest.mark.parametrize("resource", ["visits", "vm", "scratch"])
def test_warm_cache_cannot_bypass_remaining_root_budget(tmp_path, resource):
    env = setup(tmp_path / "limits.db")
    first, second = membership(env)
    with context(env) as (current, budget):
        reader = KnowledgeReader(current, env.service.identity)
        held = None
        try:
            reader.revalidate_member(first)
            if resource == "visits":
                budget.reserve_visits(100_000 - budget._visits)
            elif resource == "vm":
                budget.reserve_vm(10_000_000 - budget._vm)
            else:
                held = budget.reserve_scratch((128 << 20) - budget._scratch, "general")
            with pytest.raises(PrivateResourceStop):
                reader.revalidate_member(second)
        finally:
            reader.close()
            if held is not None:
                held.release()
        assert budget._scratch == 0


def test_snapshot_cache_capacity_and_transient_scratch_are_bounded(tmp_path):
    env = setup(tmp_path / "bounded.db")
    ids = []
    for start in range(0, 201, 100):
        ids.extend(
            write(
                env,
                tuple(
                    CreateEntity(
                        kind="entity",
                        local_id=f"e{i}",
                        name=f"Project {i}",
                        support=env.support,
                    )
                    for i in range(start, min(start + 100, 201))
                ),
            ).values()
        )
    with context(env) as (current, budget):
        cache = RevalidationCache(current)
        sizes = []
        try:
            for entity_id in ids:
                store = Store(
                    current.connection, current.scope, budget, context=current, cache=cache
                )
                try:
                    store.require_entity(entity_id)
                finally:
                    store.close()
                sizes.append(budget._scratch)
            assert len(cache.bases) == 200
            assert len(cache.proofs) == 1
            assert sizes[-1] == sizes[-2]
            assert 0 < budget._scratch < 64 << 20
        finally:
            cache.close()
        assert budget._scratch == 0
