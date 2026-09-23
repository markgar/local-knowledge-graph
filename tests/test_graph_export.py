from contextlib import closing, contextmanager
from threading import Event
from time import monotonic
from uuid import uuid4

import pytest
from support.graph import fixture

from kg._execution_budget import Deadline, _graph_build_operation
from kg.evidence._graph_observer import open_graph_source
from kg.evidence.errors import EvidenceServiceError
from kg.knowledge import _graph_export as export
from kg.knowledge._graph_export import GraphEntity, GraphExportError, graph_export
from kg.models.foundation import (
    AddEntitySupport,
    ExpectedState,
    ExternalDocument,
    RemoveDocument,
    SeedSupport,
    SourceSupport,
    StoredEntity,
    WriteRequest,
)


def operation():
    return _graph_build_operation(deadline=Deadline(monotonic() + 299), cancel=Event())


@contextmanager
def cursor(env, op, **kwargs):
    with closing(open_graph_source(
        env.database, env.identity, env.scope, op.budget.deadline, op.budget,
    )) as source, source.operation(
        source.binding, env.identity, env.scope, op.budget.deadline, op.budget,
    ) as current, current.read_context(op.meter) as context, closing(
        graph_export(context, **kwargs),
    ) as stream:
        yield stream


def test_complete_mapping_proof_identity_and_exact_semantic_charge(tmp_path):
    env = fixture(tmp_path / "source.sqlite", decisions=230)
    op = operation()
    seen, entities, sizes = {}, {}, []
    with cursor(env, op) as stream:
        while True:
            with closing(stream.read()) as page:
                sizes.append(len(page.items))
                for item in page.items:
                    if isinstance(item, GraphEntity):
                        entities[item.entity_id] = item
                    else:
                        assert item.assertion_id not in seen
                        seen[item.assertion_id] = item
                        expected = env.expected[item.assertion_id]
                        assert item.subject_witness == env.witnesses[expected["subject"]]
                        assert (
                            tuple(p.captured.reference for p in item.support) == expected["support"]
                        )
                        if expected["object"]:
                            assert item.object_witness == env.witnesses[expected["object"]]
                if page.eof:
                    break
        assert stream.read().eof
    assert set(seen) == set(env.expected)
    assert set(entities) == set(env.witnesses)
    assert sizes == [200, 47]
    assert op.snapshot().semantic_items_reserved == 247
    assert op.snapshot().scratch_live_bytes == 0


def test_byte_boundary_pending_is_neither_lost_nor_recharged(tmp_path, monkeypatch):
    env = fixture(tmp_path / "source.sqlite")
    op = operation()
    monkeypatch.setattr(export, "PAGE_BYTES", 16000)
    ids, pages = [], 0
    with cursor(env, op) as stream:
        while True:
            with closing(stream.read()) as page:
                pages += 1
                ids.extend(getattr(i, "entity_id", getattr(i, "assertion_id", None))
                           for i in page.items)
                if page.eof:
                    break
    assert pages > 1
    assert len(ids) == len(set(ids)) == 29
    assert op.snapshot().semantic_items_reserved == 29
    assert op.snapshot().scratch_live_bytes == 0


def test_page_lease_close_and_coverage_mismatch(tmp_path):
    env = fixture(tmp_path / "source.sqlite")
    op = operation()
    with cursor(env, op) as stream:
        coverage = stream.coverage
        page = stream.read(limit=1)
        with pytest.raises(EvidenceServiceError, match="invalid_request"):
            stream.read()
        page.close()
        stream.read(limit=1).close()
    with pytest.raises(GraphExportError, match="unsupported_mapping"), cursor(
        env, operation(),
        expected_coverage=coverage.model_copy(update={"schema_version": "other/1"}),
    ):
        pass
    assert op.snapshot().scratch_live_bytes == 0


def test_same_snapshot_canonical_cursor_decision_differential(tmp_path):
    from kg.knowledge._reader import KnowledgeReader
    from kg.knowledge._selection import EligibleEOF

    env = fixture(tmp_path / "source.sqlite", decisions=210)
    op = operation()
    with cursor(env, op) as stream:
        exported = {}
        while True:
            with closing(stream.read()) as page:
                for item in page.items:
                    if not isinstance(item, GraphEntity) and item.decision_member is not None:
                        exported[item.assertion_id] = item.decision_member
                if page.eof:
                    break
        canonical = {}
        adapter = KnowledgeReader(stream.context, env.identity)
        try:
            for project in env.projects:
                with closing(adapter.select_decisions(project)) as decisions:
                    while True:
                        page = decisions.read()
                        for item in page.items:
                            canonical[item.record.record_id] = item
                        if page.terminal is not None:
                            assert isinstance(page.terminal, EligibleEOF)
                            break
        finally:
            adapter.close()
        assert exported == canonical
        assert len(exported) == 210
    assert op.snapshot().scratch_live_bytes == 0


@pytest.mark.parametrize("rejected,seed", [(2000, False), (4000, True)])
def test_real_rejected_activation_trials_have_bounded_live_scratch(tmp_path, rejected, seed):
    env = fixture(tmp_path / "source.sqlite", decisions=0)
    # A new entity initially has only support in the namespace later excluded.
    from kg.models.foundation import CreateEntity
    support = SourceSupport(kind="source", evidence=(env.references[2],))
    identifier = env.write((CreateEntity(
        kind="entity", local_id="denied", name="Alternatives", entity_type="project",
        support=support,
    ),))["denied"]
    for start in range(0, rejected, 50):
        env.write(tuple(AddEntitySupport(
            kind="entity_support", local_id=f"alternative-{i}",
            entity=StoredEntity(kind="stored", entity_id=identifier),
            name="Alternatives", entity_type="project",
            support=SourceSupport(kind="source", evidence=(env.references[1 + i % 2],)),
        ) for i in range(start, min(start + 50, rejected))))
    selected = env.write((AddEntitySupport(
        kind="entity_support", local_id="selected",
        entity=StoredEntity(kind="stored", entity_id=identifier),
        name="Alternatives", entity_type="project",
        support=(SeedSupport(kind="seed", source_namespace="notes",
                             seed_set_id="alternatives", seed_key="selected") if seed else
                 SourceSupport(kind="source", evidence=(env.references[0],))),
    ),))["selected"]
    removed = env.evidence.write(WriteRequest(
        contract_version="foundation/1", request_id=str(uuid4()), retry_key=str(uuid4()),
        scope=env.scope, attribution=env.attribution,
        payload=RemoveDocument(
            operation="remove_document",
            document=ExternalDocument(source_namespace="notes", synchronization_scope="local",
                                      external_id="note-2"),
            precondition=ExpectedState(kind="match", state_version=env.dependencies[
                env.references[2].document_id].state_version),
        ),
    ))
    assert removed.error is None
    env.scope = env.scope.model_copy(update={
        "access": env.scope.access.model_copy(update={"namespaces": ("notes",)}),
    })
    op = operation()
    with cursor(env, op) as stream:
        found = None
        while True:
            with closing(stream.read(limit=1)) as page:
                for item in page.items:
                    if isinstance(item, GraphEntity) and item.entity_id == identifier:
                        found = item
                assert len(stream.context._scratch) < 30
                if page.eof:
                    break
    assert found.witness.contribution_id == selected
    assert op.snapshot().scratch_peak_bytes < 2 << 20
    assert op.snapshot().scratch_live_bytes == 0
