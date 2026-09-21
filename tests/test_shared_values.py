from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError
from support.evidence import environment, put, receipt

from kg.evidence import EvidenceServiceError
from kg.evidence.administration import _read_policy
from kg.models.evidence import CorpusRegistration, KnowledgeWriterBinding, LocalPolicy
from kg.models.foundation import AddEntitySupport, ChangeSet, SeedSupport, WriteRequest

FIXTURE = Path(__file__).resolve().parents[1] / "corpora/foundation/entity-support.json"


def test_attestation_fixture_schema_and_round_trip() -> None:
    value = WriteRequest.model_validate_json(FIXTURE.read_text())
    assert WriteRequest.model_validate_json(value.model_dump_json()) == value
    assert isinstance(value.payload.changes[0], AddEntitySupport)
    assert {"AddEntitySupport", "SeedSupport"} <= WriteRequest.model_json_schema()["$defs"].keys()
    assert set(SeedSupport.model_json_schema()["required"]) == {
        "kind", "source_namespace", "seed_set_id", "seed_key",
    }


@pytest.mark.parametrize(
    "mutation", ["local", "outside", "missing-namespace", "missing-set", "extra"],
)
def test_attestation_rejects_invalid_seed_and_entity_scope(mutation) -> None:
    value = json.loads(FIXTURE.read_text())
    change = value["payload"]["changes"][0]
    if mutation == "local":
        change["entity"] = {"kind": "local", "local_id": "attestation"}
    elif mutation == "outside":
        change["support"]["source_namespace"] = "outside"
    elif mutation == "missing-namespace":
        del change["support"]["source_namespace"]
    elif mutation == "missing-set":
        del change["support"]["seed_set_id"]
    else:
        change["support"]["unknown"] = "no"
    with pytest.raises(ValidationError):
        WriteRequest.model_validate_json(json.dumps(value))


def test_source_attestation_obeys_existing_dependency_and_occurrence_limits() -> None:
    path = FIXTURE.with_name("enrichment.json")
    request = json.loads(path.read_text())
    first = request["payload"]["changes"][0]
    first.update(kind="entity_support", entity={"kind": "stored", "entity_id": "existing"})
    request["payload"]["changes"] = [first]
    references = first["support"]["evidence"]
    request["payload"]["dependencies"] = [
        dep for dep in request["payload"]["dependencies"]
        if any(ref["document_id"] == dep["document_id"] for ref in references)
    ]
    valid = WriteRequest.model_validate_json(json.dumps(request))
    assert isinstance(valid.payload, ChangeSet)
    request["payload"]["dependencies"] = []
    with pytest.raises(ValidationError, match="matching document dependency"):
        WriteRequest.model_validate_json(json.dumps(request))


def test_knowledge_binding_policy_roundtrip_and_namespace_rotation(tmp_path) -> None:
    env = environment(tmp_path / "policy.db")
    markdown = receipt(env.service.write(put(env.scope)))
    email = receipt(env.service.write(put(env.scope, namespace="email")))
    binding = KnowledgeWriterBinding(
        namespace="markdown", principal_id="principal", owner_id="owner", writer_id="knowledge",
    )
    policy = env.policy.model_copy(update={"knowledge_bindings": (binding,)})
    changed = env.admin.replace_policy(policy, env.scope.access.policy_version)
    assert changed.affected_namespaces == ("markdown",)
    assert changed.changed_documents == 1
    with env.database.connection() as connection:
        assert set(_read_policy(connection, "work").knowledge_bindings) == {binding}
        assert connection.execute(
            "SELECT current_state FROM document WHERE document_id=?", (email.document_id,),
        ).fetchone()[0] == email.processing.state_version
        assert connection.execute(
            "SELECT current_state FROM document WHERE document_id=?", (markdown.document_id,),
        ).fetchone()[0] != markdown.processing.state_version
    identical = env.admin.replace_policy(policy, changed.policy_version)
    assert identical.status == "unchanged"
    assert identical.changed_documents == 0
    removed = env.admin.replace_policy(env.policy, changed.policy_version)
    assert removed.affected_namespaces == ("markdown",) and removed.changed_documents == 1
    with env.database.connection() as connection:
        assert _read_policy(connection, "work").knowledge_bindings == ()


def test_binding_registration_equality_order_duplicates_and_unknown_namespace(tmp_path) -> None:
    env = environment(tmp_path / "register.db")
    bindings = tuple(
        KnowledgeWriterBinding(
            namespace="markdown", principal_id=f"p{i}", owner_id="o", writer_id="w",
        )
        for i in range(2)
    )
    policy = LocalPolicy(corpus_id="other", knowledge_bindings=bindings)
    registration = CorpusRegistration(corpus_id="other", namespaces=("markdown",), policy=policy)
    first = env.admin.register(registration)
    assert env.admin.register(registration.model_copy(update={
        "policy": policy.model_copy(update={"knowledge_bindings": bindings[::-1]}),
    })).policy_version == first.policy_version
    with pytest.raises(ValidationError):
        LocalPolicy(corpus_id="other", knowledge_bindings=(bindings[0], bindings[0]))
    with pytest.raises(ValidationError):
        CorpusRegistration(corpus_id="other", namespaces=("email",), policy=policy)
    with pytest.raises(EvidenceServiceError) as error:
        env.admin.replace_policy(policy.model_copy(update={
            "knowledge_bindings": (bindings[0].model_copy(update={"namespace": "unknown"}),),
        }), first.policy_version)
    assert error.value.failure.code == "invalid_request"
