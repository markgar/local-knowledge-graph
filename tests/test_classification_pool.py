"""Portable ownership/accounting tests, not native capacity acceptance."""

import json
from contextlib import ExitStack, closing
from time import monotonic
from types import SimpleNamespace

import pytest
from support.graph import fixture
from test_graph_export import cursor, operation

from kg._execution_budget import Deadline, LocalExecutionMeter, PrivateBudget, PrivateResourceStop
from kg.graph._decisions import _ClassificationPool
from kg.graph._native import NativeError
from kg.graph.session import _size
from kg.knowledge._graph_export import GraphAssertion


@pytest.fixture
def assertions(tmp_path):
    env = fixture(tmp_path / "source.sqlite", decisions=4)
    found = []
    with cursor(env, operation()) as stream:
        while True:
            with closing(stream.read()) as page:
                found.extend(item for item in page.items if isinstance(item, GraphAssertion))
                if page.eof:
                    break
    return found


def context():
    budget = PrivateBudget(Deadline(monotonic() + 30))
    meter = LocalExecutionMeter(budget, max_operations=1, max_items=10000).begin_step("pool")
    return SimpleNamespace(meter=meter), budget


def test_actual_shared_identity_same_complete_json_and_overlapping_custody(assertions):
    decisions = [a for a in assertions if a.object_entity_id is None]
    first, second = next(
        (a, b)
        for a in decisions
        for b in decisions
        if a.assertion_id != b.assertion_id and a.subject_id == b.subject_id
    )
    assert first.classification_witnesses[0] is not second.classification_witnesses[0]
    ctx, budget = context()
    with ExitStack() as custody:
        pool = _ClassificationPool(ctx, custody)
        shared, owned, refs = pool.share_validated(first)
        admitted = budget._scratch
        again, _, _ = pool.share_validated(second)
        assert admitted >= 4096 and budget._scratch == admitted
        assert shared.classification_witnesses[0] is again.classification_witnesses[0]
        assert (
            shared.decision_member.dependencies.subject_classification
            is (shared.classification_witnesses[0])
        )
        assert shared.model_dump_json() == first.model_dump_json()
        assert again.model_dump_json() == second.model_dump_json()
        assert "classification_witnesses" not in json.loads(owned)
        assert json.loads(owned)["subject_witness"] == first.subject_witness.model_dump(mode="json")
        assert refs == 64 * (
            len(shared.classification_witnesses) + len(shared.classification_evidence) + 1
        )
        final = budget.reserve_scratch(_size(shared) + 256, "general")
        assert budget._scratch == admitted + _size(shared) + 256
    assert budget._scratch == _size(shared) + 256
    final.release()
    assert budget._scratch == 0

    relations = [a for a in assertions if a.object_entity_id is not None]
    first, second = next(
        (a, b)
        for a in relations
        for b in relations
        if a.assertion_id != b.assertion_id and a.object_entity_id == b.object_entity_id
    )
    with ExitStack() as custody:
        pool = _ClassificationPool(ctx, custody)
        one, _, _ = pool.share_validated(first)
        two, _, _ = pool.share_validated(second)
        assert one.classification_evidence
        assert all(
            a is b
            for a, b in zip(
                one.classification_evidence,
                two.classification_evidence,
                strict=True,
            )
        )
    assert budget._scratch == 0


def test_pool_full_fallback_keeps_original_objects_and_charge_and_failure_cleanup(assertions):
    base = next(a for a in assertions if a.object_entity_id is not None)
    ctx, budget = context()
    with ExitStack() as custody:
        pool = _ClassificationPool(ctx, custody)
        for index in range(100):
            captures = tuple(
                c.model_copy(
                    update={
                        "selection_id": f"synthetic-selection-{index}-{side}",
                    }
                )
                for side, c in enumerate(base.classification_witnesses)
            )
            value = base.model_copy(update={"classification_witnesses": captures})
            pool.share_validated(value)
        assert len(pool.bundles) == 200
        before = budget._scratch
        returned, owned, references = pool.share_validated(base)
        assert returned is base and owned is None and references == 0
        assert budget._scratch == before
    assert budget._scratch == 0
    with pytest.raises(PrivateResourceStop), ExitStack() as custody:
        custody.enter_context(budget.reserve_scratch((64 << 20) - 2048, "general"))
        pool = _ClassificationPool(ctx, custody)
        pool.share_validated(base)
    assert not pool.bundles and budget._scratch == 0


def test_pool_collision_rejects_instead_of_replacing_shared_proof(assertions):
    base = next(a for a in assertions if a.object_entity_id is not None)
    ctx, budget = context()
    with pytest.raises(NativeError, match="invalid_projection"), ExitStack() as custody:
        pool = _ClassificationPool(ctx, custody)
        pool.share_validated(base)
        changed = base.model_copy(
            update={
                "classification_witnesses": (
                    base.classification_witnesses[0].model_copy(
                        update={"claim_id": "different-claim"}
                    ),
                    *base.classification_witnesses[1:],
                )
            }
        )
        pool.share_validated(changed)
    assert budget._scratch == 0
