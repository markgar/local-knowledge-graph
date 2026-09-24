"""Build a disposable graph from labelled synthetic canonical facts; no models."""

from __future__ import annotations

import argparse
import json
import platform
import resource
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from threading import Event
from time import monotonic
from uuid import uuid4

from classification_inputs import classified_entity

from kg._execution_budget import Deadline, _graph_build_operation
from kg.evidence import EvidenceAdministration, EvidenceDatabase, EvidenceService
from kg.evidence._read_context import read_evidence
from kg.graph._build import GraphBuildError, build_graph, retry_graph_cleanup
from kg.graph._native import evidence_id, proof_id
from kg.indexing._passages import produce
from kg.knowledge import KnowledgeAdministration, KnowledgeService
from kg.knowledge._graph_export import GraphAssertion, GraphEvidence
from kg.knowledge._selection import SeedWitness
from kg.models.evidence import (
    CorpusRegistration,
    KnowledgeWriterBinding,
    LocalAdminAuthority,
    LocalIdentity,
    LocalPolicy,
    PolicyGrant,
    WriterBinding,
)
from kg.models.foundation import (
    AccessContext,
    AddAssertion,
    Attribution,
    ChangeSet,
    ChangeSetReceipt,
    CreateOnly,
    DocumentDependency,
    DocumentReceipt,
    EntityObject,
    ExternalDocument,
    PutDocument,
    SchemaRevisionRef,
    Scope,
    SeedSupport,
    SelectClassification,
    SourceMetadata,
    SourceSupport,
    StoredEntity,
    StoredSelectionRef,
    StringObject,
    SuppliedAnchor,
    SuppliedContent,
    WriteRequest,
)
from kg.models.knowledge import RecordProjection
from kg.models.schema import (
    EntityTypeDefinition,
    SchemaDefinition,
    SchemaPredicateDefinition,
    SchemaPresetRequest,
)


@dataclass
class Fixture:
    database: EvidenceDatabase
    identity: LocalIdentity
    scope: Scope
    evidence: EvidenceService
    attribution: Attribution
    references: list
    dependencies: dict
    expected: dict
    witnesses: dict
    projects: list
    person: str
    schema_revision: SchemaRevisionRef
    selections: dict[str, str] = field(default_factory=dict)

    def write(self, changes):
        changes = tuple(item for value in changes for item in (
            value if isinstance(value, tuple) else (value,)
        ))
        prepared = []
        for change in changes:
            if isinstance(change, AddAssertion):
                fields = {}
                if change.subject_classification is None:
                    fields["subject_classification"] = StoredSelectionRef(
                        kind="stored", event_id=self.selections[change.subject.entity_id],
                    )
                if isinstance(change.object, EntityObject) and change.object_classification is None:
                    fields["object_classification"] = StoredSelectionRef(
                        kind="stored", event_id=self.selections[change.object.entity.entity_id],
                    )
                change = change.model_copy(update=fields)
            prepared.append(change)
        changes = tuple(prepared)
        refs = [
            ref for change in changes if not isinstance(change, SelectClassification)
            and isinstance(change.support, SourceSupport)
            for ref in change.support.evidence
        ]
        deps = {ref.document_id: self.dependencies[ref.document_id] for ref in refs}
        result = self.evidence.write(WriteRequest(
            contract_version="foundation/1", request_id=str(uuid4()), retry_key=str(uuid4()),
            scope=self.scope, attribution=self.attribution,
            payload=ChangeSet(
                expected_schema_revision=self.schema_revision,
                operation="enrich", changes=tuple(changes), dependencies=tuple(deps.values()),
            ),
        ))
        if not isinstance(result.receipt, ChangeSetReceipt):
            raise RuntimeError(result.model_dump_json())
        mapping = {item.local_id: item.stored_id for item in result.receipt.mappings}
        self.selections.update({
            item.entity_id: item.selection_id for item in result.receipt.entity_classifications
        })
        for change in changes:
            if isinstance(change, AddAssertion):
                self.expected[mapping[change.local_id]] = {
                    "subject": change.subject.entity_id,
                    "object": (change.object.entity.entity_id
                               if isinstance(change.object, EntityObject) else None),
                    "text": (
                        change.object.value if isinstance(change.object, StringObject) else None
                    ),
                    "support": change.support.evidence,
                }
        return mapping


def fixture(path: Path, *, decisions: int = 12, varied: bool = False) -> Fixture:
    database = EvidenceDatabase(path)
    database.initialize()
    identity = LocalIdentity(principal_id="local")
    authority = LocalAdminAuthority(principal_id="admin")
    namespaces = ("notes", "mail")
    grants = tuple(
        grant for ns in namespaces for grant in (
            PolicyGrant(principal_id="local", namespace=ns, grant="read"),
            PolicyGrant(principal_id="local", namespace=ns, grant="write_knowledge"),
            PolicyGrant(principal_id="local", namespace=ns, grant="seed"),
            PolicyGrant(principal_id="local", namespace=ns, grant="write_documents",
                        owner_id="owner", writer_id="example", synchronization_scope="local"),
        )
    )
    registered = EvidenceAdministration(database, authority).register(CorpusRegistration(
        corpus_id="graph-example", namespaces=namespaces,
        policy=LocalPolicy(
            corpus_id="graph-example", grants=grants,
            bindings=tuple(WriterBinding(namespace=ns, owner_id="owner", writer_id="example",
                                         synchronization_scope="local") for ns in namespaces),
            knowledge_bindings=tuple(KnowledgeWriterBinding(
                namespace=ns, principal_id="local", owner_id="owner", writer_id="example",
            ) for ns in namespaces),
        ),
    ))
    schema_registration = KnowledgeAdministration(database, authority).register_knowledge_schema(
        SchemaPresetRequest(
            corpus_id="graph-example", preset_name="graph-example/1",
            preset_rationale="Explicit synthetic graph acceptance vocabulary.",
            definition=SchemaDefinition(
                entity_types=(
                    EntityTypeDefinition(name="person", description="An individual person."),
                    EntityTypeDefinition(name="project", description="An identified project."),
                ),
                predicates=(
                    SchemaPredicateDefinition(
                        name="work:owns", description="The subject is accountable for the object.",
                        subject_types=("person",), object_kind="entity", object_types=("project",),
                    ),
                    SchemaPredicateDefinition(
                        name="work:decision", description="An explicit decision about the subject.",
                        subject_types=("project",), object_kind="string",
                        record_projection=RecordProjection(encoding="direct-subject-decision/1"),
                    ),
                ),
            ),
        ),
    )
    scope = Scope(corpus_id="graph-example", access=AccessContext(
        principal_id="local", policy_version=registered.policy_version,
        namespaces=namespaces, grants=("read", "write_documents", "write_knowledge", "seed"),
    ))
    evidence = EvidenceService(database, identity)
    attribution = Attribution(owner_id="owner", writer_id="example",
                              producer="synthetic-graph-example", producer_version="1")
    env = Fixture(
        database, identity, scope, evidence, attribution, [], {}, {}, {}, [], "",
        schema_registration.revision,
    )
    for i in range(1000 if varied else 12):
        ns = namespaces[i % 2]
        text = f"Synthetic note {i}: A\r\nCafe\u0301 \U0001f680. Record the reviewed decision."
        outcome = evidence.write(WriteRequest(
            contract_version="foundation/1", request_id=str(uuid4()), retry_key=f"note-{i}",
            scope=scope, attribution=attribution,
            payload=PutDocument(
                operation="put_document",
                document=ExternalDocument(source_namespace=ns, synchronization_scope="local",
                                          external_id=f"note-{i}"),
                precondition=CreateOnly(kind="create"),
                content=SuppliedContent(text=text, anchors=(SuppliedAnchor(
                    local_id="whole", start=0, end=len(text), quote=text,
                ),), passage_policy="supplied-anchors/1"),
                metadata=SourceMetadata(title=f"Synthetic note {i}", location=f"example://note/{i}"),
            ),
        ))
        if not isinstance(outcome.receipt, DocumentReceipt):
            raise RuntimeError(outcome.model_dump_json())
        doc = outcome.receipt
        env.dependencies[doc.document_id] = DocumentDependency(
            source_namespace=ns, document_id=doc.document_id, revision_id=doc.revision_id,
            state_version=doc.processing.state_version,
        )
        if i % 2:
            produce(database, identity, scope, attribution, doc.document_id,
                    doc.processing.state_version)
            ref = evidence.passages(scope, doc.document_id, doc.processing.state_version).entries[0]
        else:
            ref = evidence.anchors(scope, doc.document_id, doc.processing.state_version).entries[0]
        env.references.append(ref.reference)
    source = SourceSupport(kind="source", evidence=(env.references[0],))
    created = env.write(tuple(
        change for i in range(2) for change in classified_entity(
            local_id=f"person-{i}", name="Alice", entity_type="person", support=source,
        )
    ))
    people = {f"person-{i}": created[f"person-{i}"] for i in range(2)}
    env.person = people["person-0"]
    project_count = 100 if varied else 5
    for i in range(project_count):
        if i % 5 == 0:
            support = SeedSupport(kind="seed", source_namespace="notes",
                                  seed_set_id="projects", seed_key=f"project-{i}")
        else:
            index = (i * 2 + 1) if i % 5 in (1, 2) else i * 2
            support = SourceSupport(kind="source",
                                    evidence=(env.references[index % len(env.references)],))
        entity_id = env.write(classified_entity(
            local_id="project", name=f"Project {i}",
            entity_type="project", support=support,
        ))["project"]
        env.projects.append(entity_id)
        env.write(tuple(AddAssertion(
            kind="assertion", local_id=f"owns-{j}", predicate="work:owns",
            subject=StoredEntity(kind="stored", entity_id=env.person),
            object=EntityObject(
                kind="entity", entity=StoredEntity(kind="stored", entity_id=entity_id),
            ),
            interpretation="explicit",
            support=SourceSupport(kind="source", evidence=tuple(
                env.references[(i * 2 + k) % len(env.references)] for k in range(j + 1)
            )),
        ) for j in range(2)))
    batch = []
    for i in range(decisions):
        project = 0 if i < decisions // 2 else 1 + (i - decisions // 2) % (project_count - 1)
        refs = (env.references[i % len(env.references)],)
        if i % 5 == 0:
            refs += (env.references[(i + 1) % len(env.references)],)
        batch.append(AddAssertion(
            kind="assertion", local_id=f"decision-{i}", predicate="work:decision",
            subject=StoredEntity(kind="stored", entity_id=env.projects[project]),
            object=StringObject(kind="string", value="Independently submitted same-text decision"),
            interpretation="explicit", support=SourceSupport(kind="source", evidence=refs),
        ))
        if len(batch) == 20 or i == decisions - 1:
            env.write(batch)
            batch.clear()
    service = KnowledgeService(database, identity)
    for entity_id in (*people.values(), *env.projects):
        env.witnesses[entity_id] = service.entity(scope, entity_id).witness
    return env


def query(native, operation, sql):
    rows = native.execute(sql, {}, budget=operation.budget, cancel=operation.cancel)
    try:
        while page := rows.read():
            yield from page
    finally:
        rows.close()


def verify(stage, operation, env):
    # Expectations come from the real write receipts, not graph selection.
    seen = set()
    expected_edges = set()
    with stage.observer.operation(
        stage.binding, env.identity, env.scope, operation.budget.deadline, operation.budget,
    ) as current:
        with current.read_context(operation.meter) as context:
            for identifier, payload in query(stage.native, operation,
                    "MATCH (a:Assertion) RETURN a.assertion_id, a.dependency_json "
                    "ORDER BY a.assertion_id"):
                assert identifier not in seen
                seen.add(identifier)
                actual = GraphAssertion.model_validate_json(payload)
                expected = env.expected[identifier]
                assert actual.subject_id == expected["subject"]
                assert actual.object_entity_id == expected["object"]
                assert actual.decision_text == expected["text"]
                assert tuple(e.captured.reference for e in actual.support) == expected["support"]
                assert actual.subject_witness == env.witnesses[actual.subject_id]
                if actual.object_entity_id:
                    assert actual.object_witness == env.witnesses[actual.object_entity_id]
                for ordinal, proof in enumerate(actual.support, 1):
                    assert proof.captured.dependency == env.dependencies[
                        proof.captured.reference.document_id]
                    expected_edges.add((identifier, evidence_id(proof), ordinal))
            assert seen == set(env.expected)
            edges = list(query(stage.native, operation,
                "MATCH (a:Assertion)-[s:ASSERTION_SOURCE]->(e:Evidence) "
                "RETURN a.assertion_id,e.evidence_id,s.ordinal"))
            assert len(edges) == len(expected_edges) and set(edges) == expected_edges
            columns = (
                "evidence_id", "corpus_id", "namespace", "document_id", "revision_id",
                "anchor_id", "passage_id", "passage_set_id", "state_version",
                "metadata_snapshot_id", "namespace_token",
            )
            for row in query(stage.native, operation,
                    "MATCH (e:Evidence) RETURN " + ",".join(f"e.{name}" for name in columns)):
                data = dict(zip(columns, row, strict=True))
                # Hydrate each distinct reference through SQLite, not graph text.
                from kg.knowledge._selection import CapturedEvidence
                from kg.models.foundation import EvidenceRef
                ref = EvidenceRef(
                    corpus_id=data["corpus_id"], source_namespace=data["namespace"],
                    document_id=data["document_id"], revision_id=data["revision_id"],
                    anchor_id=data["anchor_id"], passage_id=data["passage_id"],
                )
                view = read_evidence(context, ref, state_version=data["state_version"])
                assert view.citation.metadata_snapshot_id == data["metadata_snapshot_id"]
                proof = GraphEvidence(captured=CapturedEvidence(
                    reference=ref, dependency=env.dependencies[ref.document_id],
                    metadata_snapshot_id=data["metadata_snapshot_id"],
                    namespace_token=data["namespace_token"],
                ), passage_set_id=data["passage_set_id"])
                assert data["evidence_id"] == evidence_id(proof)
            proofs = dict(query(stage.native, operation,
                "MATCH (e:Entity)-[:SELECTED]->(p:EntityProof) RETURN e.entity_id,p.witness_json"))
            assert set(proofs) == set(env.witnesses)
            for identifier, witness in env.witnesses.items():
                assert type(witness).model_validate_json(proofs[identifier]) == witness
            subject_proofs = list(query(stage.native, operation,
                "MATCH (a:Assertion)-[:SUBJECT_PROOF]->(p:EntityProof) "
                "RETURN a.assertion_id,p.proof_id"))
            assert len(subject_proofs) == len(env.expected)
            assert set(subject_proofs) == {
                (identifier, proof_id(env.witnesses[expected["subject"]]))
                for identifier, expected in env.expected.items()
            }
            object_proofs = list(query(stage.native, operation,
                "MATCH (a:Assertion)-[:OBJECT_PROOF]->(p:EntityProof) "
                "RETURN a.assertion_id,p.proof_id"))
            expected_objects = {
                (identifier, proof_id(env.witnesses[expected["object"]]))
                for identifier, expected in env.expected.items() if expected["object"] is not None
            }
            assert len(object_proofs) == len(expected_objects)
            assert set(object_proofs) == expected_objects
            expected_topology = {
                "SUBJECT": [(e["subject"], key, 0) for key, e in env.expected.items()],
                "OBJECT": [(key, e["object"], 0) for key, e in env.expected.items()
                           if e["object"] is not None],
                "SELECTED": [(key, proof_id(w), 0) for key, w in env.witnesses.items()],
            }
            endpoints = {
                "SUBJECT": ("Entity", "entity_id", "Assertion", "assertion_id"),
                "OBJECT": ("Assertion", "assertion_id", "Entity", "entity_id"),
                "SELECTED": ("Entity", "entity_id", "EntityProof", "proof_id"),
            }
            for edge, expected in expected_topology.items():
                source, source_key, target, target_key = endpoints[edge]
                actual = query(stage.native, operation,
                    f"MATCH (s:{source})-[r:{edge}]->(t:{target}) "
                    f"RETURN s.{source_key},t.{target_key},r.ordinal")
                assert Counter(actual) == Counter(expected)
            source_expected, seed_expected = [], []
            for witness in env.witnesses.values():
                if isinstance(witness.basis, SeedWitness):
                    seed = witness.basis
                    seed_expected.append((
                        proof_id(witness), proof_id(witness), seed.namespace, seed.owner_id,
                        seed.writer_id, seed.seed_set_id, seed.seed_key, seed.contribution_id,
                        seed.membership_event_id, seed.generation,
                    ))
                else:
                    for ordinal, captured in enumerate(witness.basis.evidence, 1):
                        source_expected.append((
                            proof_id(witness), captured.reference.document_id,
                            captured.reference.anchor_id, captured.reference.passage_id, ordinal,
                            captured.dependency.state_version, captured.metadata_snapshot_id,
                            captured.namespace_token,
                        ))
            assert Counter(query(stage.native, operation,
                "MATCH (p:EntityProof)-[r:ENTITY_SOURCE]->(e:Evidence) RETURN p.proof_id,"
                "e.document_id,e.anchor_id,e.passage_id,r.ordinal,e.state_version,"
                "e.metadata_snapshot_id,e.namespace_token")) == Counter(source_expected)
            assert Counter(query(stage.native, operation,
                "MATCH (p:EntityProof)-[:ENTITY_SEED]->(s:SeedProof) RETURN p.proof_id,s.proof_id,"
                "s.namespace,s.owner_id,s.writer_id,s.seed_set_id,s.seed_key,s.contribution_id,"
                "s.membership_event_id,s.generation")) == Counter(seed_expected)
        with current.release_fence():
            pass
    return len(seen)


def run(output: Path, *, varied: bool):
    output.mkdir(parents=True, exist_ok=False)
    env = fixture(output / "canonical.sqlite", decisions=10000 if varied else 12, varied=varied)
    operation = _graph_build_operation(deadline=Deadline(monotonic() + 299.99), cancel=Event())
    start = monotonic()
    stage = None
    try:
        stage = build_graph(env.database, env.identity, env.scope, staging_parent=output,
                            operation=operation)
        build_seconds = monotonic() - start
        verified = verify(stage, operation, env)
        result = {
            "status": "complete", "platform": platform.platform(),
            "python": platform.python_version(), "build_seconds": build_seconds,
            "verified_assertions": verified, "manifest": stage.manifest.model_dump(mode="json"),
            "accounting": asdict(operation.snapshot()),
            "graph_bytes": sum(p.stat().st_size for p in stage.directory.rglob("*") if p.is_file()),
            "native_buffer_pool_bytes": 256 << 20,
            "native_threads": 2,
            "process_peak_rss_bytes": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        }
    except GraphBuildError as error:
        residue = error.take_cleanup_residue()
        if residue is not None and not retry_graph_cleanup(residue).complete:
            print("Cleanup pending:", residue.owned_paths)
        result = {"status": "blocked", "failure": asdict(error.failure),
                  "accounting": asdict(error.accounting), "cleanup": asdict(error.cleanup)}
        (output / "result.json").write_text(json.dumps(result, indent=2, default=str) + "\n")
        raise
    finally:
        if stage is not None:
            residue = stage.close()
            if residue is not None:
                raise RuntimeError(f"Cleanup pending: {residue.owned_paths}")
    result["cleanup"] = "complete"
    (output / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", choices=("small", "varied-10000"), default="small")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run(args.output, varied=args.case == "varied-10000")
