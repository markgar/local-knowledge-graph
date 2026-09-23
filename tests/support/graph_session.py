"""Real canonical observer with an explicitly fake native producer for portable tests."""

from contextlib import closing
from pathlib import Path
from tempfile import mkdtemp
from threading import get_ident

from kg.evidence._graph_observer import open_graph_source
from kg.graph import session as module
from kg.graph._build import (
    GraphBuildManifest,
    GraphBuildResources,
    GraphCleanupResidue,
    StagedGraph,
)
from kg.graph._native import NativeGraphReadHandle, NativeRows
from kg.graph.session import LocalGraphSession
from kg.knowledge._graph_export import graph_export
from kg.models.foundation import (
    CreateOnly,
    ExternalDocument,
    PutDocument,
    SourceMetadata,
    SuppliedContent,
    WriteRequest,
)
from support.graph import fixture


class Result:
    def __init__(self):
        self.remaining = True

    def has_next(self):
        return self.remaining

    def get_next(self):
        self.remaining = False
        return [42]

    def close(self):
        pass


class Native(NativeGraphReadHandle):
    def __init__(self):
        super().__init__()
        self._usable = True
        self.calls = []

    def execute(self, template, parameters, *, budget, cancel, timeout_milliseconds=None):
        self.calls.append((budget, cancel, timeout_milliseconds, get_ident()))
        rows = NativeRows(self, Result(), budget, cancel)
        self._rows.append(rows)
        return rows


def setup(tmp_path, monkeypatch):
    env = fixture(tmp_path / "source.sqlite", decisions=0)
    builds = []

    def builder(database, identity, scope, *, staging_parent, operation, expected_coverage=None):
        source = open_graph_source(database, identity, scope, operation.budget.deadline,
                                   operation.budget)
        directory = Path(mkdtemp(dir=staging_parent))
        residue = GraphCleanupResidue()
        residue.observer, residue.directory = source, directory
        native = residue.native_reader = Native()
        with source.operation(source.binding, identity, scope, operation.budget.deadline,
                              operation.budget) as current:
            with (
                current.read_context(operation.meter) as canonical,
                closing(graph_export(canonical, expected_coverage=expected_coverage)) as cursor,
            ):
                coverage = cursor.coverage
            with current.release_fence():
                pass
        manifest = GraphBuildManifest(
            coverage=coverage, identity=identity, scope=scope, build_id="fake",
            entity_count=0, relationship_count=0, decision_count=0,
            assertion_support_occurrences=0, entity_source_support_occurrences=0,
            seed_witness_count=0, unique_evidence_count=0, content_sha256="0" * 64,
        )
        resources = GraphBuildResources(
            source.binding, manifest, directory, native, source, operation.snapshot(), [residue],
        )
        stage = StagedGraph(resources, operation)
        builds.append((stage, operation, get_ident()))
        return stage

    monkeypatch.setattr(module, "build_graph", builder)
    session = LocalGraphSession(
        env.database, env.identity, env.scope, graph_directory=tmp_path / "derived",
    )
    return env, session, builds


def count(context):
    rows = context.native.execute("RETURN 42", {})
    value = rows.read()[0][0]
    rows.close()
    return context.retain(value)


def write_request(env, identifier="new"):
    return WriteRequest(
        contract_version="foundation/1", request_id=identifier, retry_key=identifier,
        scope=env.scope, attribution=env.attribution,
        payload=PutDocument(
            operation="put_document", precondition=CreateOnly(kind="create"),
            document=ExternalDocument(
                source_namespace="notes", external_id=identifier, synchronization_scope="local",
            ),
            content=SuppliedContent(
                text="A controlled graph-session write.", passage_policy="supplied-anchors/1",
            ),
            metadata=SourceMetadata(title="Controlled write", location="example://controlled"),
        ),
    )
