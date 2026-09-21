import hashlib
import json
import multiprocessing
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from support.evidence import environment
from support.knowledge import schema

from kg.evidence import EvidenceDatabase, EvidenceServiceError
from kg.evidence._sql import AccountedConnection
from kg.evidence._transactions import writing
from kg.knowledge import KnowledgeAdministration
from kg.knowledge._registry import register_schema
from kg.models.evidence import CorpusRegistration, LocalAdminAuthority, LocalIdentity, LocalPolicy
from kg.models.knowledge import KnowledgeSchema, KnowledgeSchemaRegistration, PredicateDefinition

ADMIN = LocalAdminAuthority(principal_id="admin")


def stored(database):
    with database.connection() as connection:
        return tuple(tuple(row) for row in connection.execute(
            "SELECT * FROM knowledge_schema ORDER BY corpus_id",
        ))


def test_actual_registration_reopen_order_independence_and_no_other_mutations(tmp_path):
    env = environment(tmp_path / "registry.db")
    admin = KnowledgeAdministration(env.database, ADMIN)
    value = schema()
    assert admin.register_knowledge_schema(value) == KnowledgeSchemaRegistration(
        corpus_id="work", schema_version="test/1", status="applied",
    )
    before = stored(env.database)
    definition, digest = before[0][2:]
    assert hashlib.sha256(definition.encode()).hexdigest() == digest
    assert KnowledgeSchema.model_validate_json(definition).corpus_id == "work"
    reordered = value.model_copy(update={
        "entity_types": value.entity_types[::-1],
        "identifier_schemes": value.identifier_schemes[::-1],
        "predicates": value.predicates[::-1],
    })
    reopened = KnowledgeAdministration(EvidenceDatabase(env.database.path), ADMIN)
    assert reopened.register_knowledge_schema(reordered).status == "unchanged"
    assert reopened.register_knowledge_schema(value).status == "unchanged"
    assert stored(env.database) == before
    with env.database.connection() as connection:
        for table in ("entity", "contribution", "write_key", "receipt_clock", "seed_set",
                      "document", "state_intent"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2
    assert env.service.diagnostics.recent(env.scope).entries == ()
    assert not hasattr(admin, "diagnostics")
    assert not hasattr(admin, "write")


def test_predicate_type_order_is_not_priority(tmp_path):
    env = environment(tmp_path / "order.db")
    admin = KnowledgeAdministration(env.database, ADMIN)
    value = schema().model_copy(update={"predicates": (
        schema().predicates[0].model_copy(update={
            "subject_types": ("person", "project"), "object_types": ("person", "project"),
        }),
    )})
    assert admin.register_knowledge_schema(value).status == "applied"
    reverse = value.model_copy(update={"predicates": (
        value.predicates[0].model_copy(update={
            "subject_types": ("project", "person"), "object_types": ("project", "person"),
        }),
    )})
    assert admin.register_knowledge_schema(reverse).status == "unchanged"


@pytest.mark.parametrize("change", ["version", "type", "scheme", "predicate", "projection"])
def test_immutable_definition_conflict_preserves_original(tmp_path, change):
    env = environment(tmp_path / "immutable.db")
    admin = KnowledgeAdministration(env.database, ADMIN)
    value = schema()
    admin.register_knowledge_schema(value)
    before = stored(env.database)
    fields = value.model_dump()
    if change == "version":
        fields["schema_version"] = "test/2"
    elif change == "type":
        fields["entity_types"] += ("team",)
    elif change == "scheme":
        fields["identifier_schemes"] = ("other",)
    elif change == "predicate":
        fields["predicates"][0]["subject_types"] = ("project",)
    else:
        fields["predicates"][1]["record_projection"] = None
    with pytest.raises(EvidenceServiceError) as error:
        admin.register_knowledge_schema(KnowledgeSchema.model_validate(fields))
    assert error.value.failure.code == "state_conflict"
    assert stored(env.database) == before


def test_corpus_isolation_missing_corpus_and_context_authority(tmp_path):
    env = environment(tmp_path / "scope.db")
    admin = KnowledgeAdministration(env.database, ADMIN)
    admin.register_knowledge_schema(schema())
    before = stored(env.database)
    with pytest.raises(EvidenceServiceError) as error:
        admin.register_knowledge_schema(schema(corpus="absent"))
    assert error.value.failure.code == "not_found"
    assert stored(env.database) == before
    env.admin.register(CorpusRegistration(
        corpus_id="other", namespaces=("markdown",), policy=LocalPolicy(corpus_id="other"),
    ))
    assert admin.register_knowledge_schema(schema(corpus="other", version="different")).status == (
        "applied"
    )
    with writing(env.database, LocalIdentity(principal_id="not-admin")) as context:
        with pytest.raises(EvidenceServiceError) as error:
            register_schema(context, ADMIN, schema())
        assert error.value.failure.code == "forbidden"
    with pytest.raises(EvidenceServiceError) as error:
        KnowledgeAdministration(env.database, env.service.identity)
    assert error.value.failure.code == "invalid_request"
    with pytest.raises(EvidenceServiceError):
        KnowledgeAdministration(env.database, env.scope)


@pytest.mark.parametrize("forgery", ["top", "nested", "descriptor", "extra", "python-type"])
def test_constructed_and_copied_models_revalidated_before_mutation(tmp_path, forgery):
    env = environment(tmp_path / "forged.db")
    admin = KnowledgeAdministration(env.database, ADMIN)
    value = schema()
    if forgery == "top":
        value = KnowledgeSchema.model_construct(
            corpus_id="work", schema_version="v1", entity_types=("Upper",),
        )
    elif forgery == "nested":
        value = value.model_copy(update={"predicates": (
            value.predicates[0].model_copy(update={"object_types": ()}),
        )})
    elif forgery == "descriptor":
        predicate = value.predicates[1]
        value = value.model_copy(update={"predicates": (
            predicate.model_copy(update={"record_projection":
                predicate.record_projection.model_copy(update={"encoding": "action"})}),
        )})
    elif forgery == "python-type":
        value = value.model_copy(update={"entity_types": ["person"]})
    else:
        value = value.model_copy(update={"unknown": True})
    with pytest.raises(EvidenceServiceError) as error:
        admin.register_knowledge_schema(value)
    assert error.value.failure.code == "invalid_request"
    assert stored(env.database) == ()


def test_authority_defensive_revalidation_and_typed_service_input(tmp_path):
    env = environment(tmp_path / "authority.db")
    with pytest.raises(EvidenceServiceError):
        KnowledgeAdministration(env.database, ADMIN.model_copy(update={"principal_id": ""}))
    admin = KnowledgeAdministration(env.database, ADMIN)
    with pytest.raises(EvidenceServiceError):
        admin.register_knowledge_schema(schema().model_dump())
    admin.authority = ADMIN.model_copy(update={"extra": "forged"})
    with pytest.raises(EvidenceServiceError):
        admin.register_knowledge_schema(schema())
    assert stored(env.database) == ()


def test_same_context_kernel_rollback_and_lifetime(tmp_path):
    env = environment(tmp_path / "transaction.db")
    with pytest.raises(RuntimeError, match="after insert"), writing(
        env.database, LocalIdentity(principal_id="admin"),
    ) as context:
        assert register_schema(context, ADMIN, schema()).status == "applied"
        assert register_schema(context, ADMIN, schema()).status == "unchanged"
        assert context.connection.in_transaction
        raise RuntimeError("after insert")
    assert context.commit_outcome == "confirmed_rolled_back"
    assert stored(env.database) == ()
    with pytest.raises(EvidenceServiceError) as error:
        register_schema(context, ADMIN, schema())
    assert error.value.failure.code == "invalid_request"
    assert KnowledgeAdministration(env.database, ADMIN).register_knowledge_schema(
        schema(),
    ).status == "applied"


def test_insert_failure_rolls_back_with_explicit_storage_error(tmp_path, monkeypatch, caplog):
    env = environment(tmp_path / "failure.db")
    original = AccountedConnection.execute

    def fail(connection, sql, parameters=()):
        if sql.startswith("INSERT INTO knowledge_schema"):
            original(connection, sql, parameters)
            raise sqlite3.OperationalError("private database error")
        return original(connection, sql, parameters)

    with monkeypatch.context() as patch:
        patch.setattr(AccountedConnection, "execute", fail)
        with pytest.raises(EvidenceServiceError) as error:
            KnowledgeAdministration(env.database, ADMIN).register_knowledge_schema(schema())
    assert error.value.failure.code == "internal_error"
    assert "private database error" not in str(error.value)
    assert "class=OperationalError" in caplog.text
    assert stored(env.database) == ()


@pytest.mark.parametrize("corruption", ["json", "hash", "version", "corpus", "noncanonical"])
def test_corrupt_stored_registry_is_not_unchanged_or_replaceable(tmp_path, corruption):
    env = environment(tmp_path / "corrupt.db")
    admin = KnowledgeAdministration(env.database, ADMIN)
    admin.register_knowledge_schema(schema())
    with env.database.transaction() as connection:
        if corruption == "hash":
            connection.execute("UPDATE knowledge_schema SET definition_hash='bad'")
        elif corruption == "version":
            connection.execute("UPDATE knowledge_schema SET schema_version='other'")
        else:
            definition = stored(env.database)[0][2]
            if corruption == "json":
                definition = "{broken"
            elif corruption == "corpus":
                definition = definition.replace('"corpus_id":"work"', '"corpus_id":"foreign"')
            else:
                definition = json.dumps(json.loads(definition), indent=2)
            connection.execute(
                "UPDATE knowledge_schema SET definition_json=?,definition_hash=?",
                (definition, hashlib.sha256(definition.encode()).hexdigest()),
            )
    before = stored(env.database)
    with pytest.raises(EvidenceServiceError) as error:
        admin.register_knowledge_schema(schema())
    assert error.value.failure.code == "internal_error"
    assert stored(env.database) == before


def _register_worker(path, version, barrier, results):
    admin = KnowledgeAdministration(EvidenceDatabase(Path(path)), ADMIN)
    barrier.wait(timeout=20)
    try:
        result = admin.register_knowledge_schema(schema(version=version))
        results.put(result.status)
    except EvidenceServiceError as error:
        results.put(error.failure.code)


@pytest.mark.parametrize("different", [False, True])
def test_two_process_registrations_converge_or_conflict(tmp_path, different):
    env = environment(tmp_path / "race.db")
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(3), ctx.Queue()
    versions = ("v1", "v2" if different else "v1")
    processes = [
        ctx.Process(target=_register_worker, args=(str(env.database.path), v, barrier, results))
        for v in versions
    ]
    for process in processes:
        process.start()
    barrier.wait(timeout=20)
    outcomes = [results.get(timeout=30) for _ in processes]
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    assert sorted(outcomes) == ["applied", "state_conflict" if different else "unchanged"]
    rows = stored(env.database)
    assert len(rows) == 1 and rows[0][1] in versions


def test_maximum_registry_and_type_sides_persist_without_truncation(tmp_path):
    env = environment(tmp_path / "limits.db")
    types = tuple(f"type{i}" for i in range(1000))
    value = KnowledgeSchema(
        corpus_id="work", schema_version="max/1", entity_types=types,
        identifier_schemes=tuple(f"scheme{i}" for i in range(1000)),
        predicates=tuple(
            PredicateDefinition(
                name=f"predicate{i}", subject_types=types[:100],
                object_kind="entity", object_types=types[-100:],
            )
            for i in range(1000)
        ),
    )
    admin = KnowledgeAdministration(env.database, ADMIN)
    assert admin.register_knowledge_schema(value).status == "applied"
    persisted = KnowledgeSchema.model_validate_json(stored(env.database)[0][2])
    assert len(persisted.entity_types) == len(persisted.identifier_schemes) == 1000
    assert len(persisted.predicates) == 1000
    assert all(len(p.subject_types) == len(p.object_types) == 100 for p in persisted.predicates)
    assert admin.register_knowledge_schema(value).status == "unchanged"


def test_executable_example_registers_then_reopens_unchanged(tmp_path):
    example = Path(__file__).resolve().parents[1] / "examples" / "knowledge_schema.py"
    path = tmp_path / "example.db"
    for status in ("applied", "unchanged"):
        output = subprocess.run(
            [sys.executable, str(example), "--database", str(path)],
            text=True, capture_output=True, check=True,
        )
        value = KnowledgeSchemaRegistration.model_validate_json(output.stdout)
        assert value.status == status and value.corpus_id == "registry-demo"
