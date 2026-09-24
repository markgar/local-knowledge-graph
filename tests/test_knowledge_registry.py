"""Trusted described-preset bootstrap is immutable and uses canonical revision storage."""

import json
import multiprocessing
import sqlite3
import subprocess
import sys
from pathlib import Path
from time import monotonic

import pytest
from support.evidence import environment
from support.knowledge import preset

from kg._execution_budget import Deadline, PrivateBudget
from kg.evidence import EvidenceDatabase, EvidenceServiceError
from kg.evidence._sql import AccountedConnection
from kg.evidence._transactions import writing
from kg.knowledge import KnowledgeAdministration
from kg.knowledge import _schema_operations as operations
from kg.knowledge._registry import definition_hash, definition_json
from kg.models.evidence import CorpusRegistration, LocalAdminAuthority, LocalIdentity, LocalPolicy
from kg.models.schema import (
    EntityTypeDefinition,
    SchemaDefinition,
    SchemaPresetRegistration,
    SchemaPresetRequest,
)

ADMIN = LocalAdminAuthority(principal_id="admin")


def stored(database):
    with database.connection() as conn:
        return tuple(
            tuple(row)
            for row in conn.execute(
                "SELECT * FROM knowledge_schema_revision ORDER BY corpus_id,sequence",
            )
        )


def test_registration_reopen_order_independence_and_no_fact_mutations(tmp_path):
    env = environment(tmp_path / "registry.db")
    admin = KnowledgeAdministration(env.database, ADMIN)
    value = preset()
    first = admin.register_knowledge_schema(value)
    assert first.status == "applied" and first.sequence == 1
    before = stored(env.database)
    definition = SchemaDefinition.model_validate_json(before[0][4])
    assert definition_hash("work", definition) == before[0][5]
    reordered = value.model_copy(
        update={
            "definition": value.definition.model_copy(
                update={
                    "entity_types": value.definition.entity_types[::-1],
                    "identifier_schemes": value.definition.identifier_schemes[::-1],
                    "predicates": value.definition.predicates[::-1],
                }
            )
        }
    )
    reopened = KnowledgeAdministration(EvidenceDatabase(env.database.path), ADMIN)
    second = reopened.register_knowledge_schema(reordered)
    assert second.status == "unchanged" and second.revision == first.revision
    assert stored(env.database) == before
    with env.database.connection() as conn:
        for table in (
            "entity",
            "contribution",
            "write_key",
            "receipt_clock",
            "seed_set",
            "document",
            "state_intent",
            "knowledge_schema_receipt",
        ):
            assert conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 4
    assert env.service.diagnostics.recent(env.scope).entries == ()


@pytest.mark.parametrize("field", ["preset_name", "preset_rationale", "definition"])
def test_preset_cannot_replace_a_registered_definition(tmp_path, field):
    env = environment(tmp_path / "registry.db")
    admin = KnowledgeAdministration(env.database, ADMIN)
    value = preset()
    admin.register_knowledge_schema(value)
    before = stored(env.database)
    replacement = (
        "Changed"
        if field != "definition"
        else value.definition.model_copy(
            update={
                "entity_types": (
                    *value.definition.entity_types,
                    EntityTypeDefinition(
                        name="team",
                        description="An explicitly identified team.",
                    ),
                ),
            }
        )
    )
    with pytest.raises(EvidenceServiceError, match="state_conflict"):
        admin.register_knowledge_schema(value.model_copy(update={field: replacement}))
    assert stored(env.database) == before


def test_corpus_isolation_and_provisioned_authority(tmp_path):
    env = environment(tmp_path / "registry.db")
    value = preset()
    admin = KnowledgeAdministration(env.database, ADMIN)
    admin.register_knowledge_schema(value)
    with pytest.raises(EvidenceServiceError, match="not_found"):
        admin.register_knowledge_schema(value.model_copy(update={"corpus_id": "absent"}))
    env.admin.register(
        CorpusRegistration(
            corpus_id="other",
            namespaces=("markdown",),
            policy=LocalPolicy(corpus_id="other"),
        )
    )
    assert admin.register_knowledge_schema(
        value.model_copy(update={"corpus_id": "other"})
    ).status == ("applied")
    for invalid in (env.service.identity, env.scope, value.model_dump()):
        with pytest.raises(EvidenceServiceError, match="invalid_request"):
            KnowledgeAdministration(env.database, invalid)
    with pytest.raises(EvidenceServiceError, match="state_conflict"):
        KnowledgeAdministration(
            env.database,
            LocalAdminAuthority(principal_id="different-admin"),
        ).register_knowledge_schema(value)


@pytest.mark.parametrize("forgery", ["top", "nested", "extra", "python-type", "description"])
def test_constructed_copied_and_nested_models_revalidated(tmp_path, forgery):
    env = environment(tmp_path / "registry.db")
    value = preset()
    if forgery == "top":
        value = SchemaPresetRequest.model_construct(corpus_id="work")
    elif forgery == "extra":
        value = value.model_copy(update={"unknown": True})
    elif forgery == "python-type":
        value = value.model_copy(
            update={
                "definition": value.definition.model_copy(
                    update={
                        "entity_types": list(value.definition.entity_types),
                    }
                )
            }
        )
    elif forgery == "description":
        value = value.model_copy(
            update={
                "definition": value.definition.model_copy(
                    update={
                        "entity_types": (
                            value.definition.entity_types[0].model_copy(update={"description": ""}),
                        ),
                    }
                )
            }
        )
    else:
        value = value.model_copy(
            update={
                "definition": value.definition.model_copy(
                    update={
                        "predicates": (
                            value.definition.predicates[0].model_copy(update={"object_types": ()}),
                        ),
                    }
                )
            }
        )
    with pytest.raises(EvidenceServiceError, match="invalid_request"):
        KnowledgeAdministration(env.database, ADMIN).register_knowledge_schema(value)
    assert stored(env.database) == ()


def test_owner_rollback_and_lifetime(tmp_path):
    env = environment(tmp_path / "registry.db")
    budget = PrivateBudget(Deadline(monotonic() + 30))
    with (
        pytest.raises(RuntimeError),
        writing(
            env.database,
            LocalIdentity(principal_id="admin"),
            budget=budget,
        ) as context,
    ):
        operations.preset(context, preset(), budget)
        raise RuntimeError("after insert")
    assert context.commit_outcome == "confirmed_rolled_back"
    assert stored(env.database) == ()
    with pytest.raises(EvidenceServiceError, match="invalid_request"):
        operations.preset(context, preset(), budget)


def test_insert_failure_rolls_back_and_logs_safe_error(tmp_path, monkeypatch, caplog):
    env = environment(tmp_path / "registry.db")
    execute = AccountedConnection.execute

    def fail(conn, sql, parameters=()):
        result = execute(conn, sql, parameters)
        if sql.startswith("INSERT INTO knowledge_schema_head"):
            raise sqlite3.OperationalError("private detail")
        return result

    with monkeypatch.context() as patch:
        patch.setattr(AccountedConnection, "execute", fail)
        with pytest.raises(EvidenceServiceError, match="internal_error") as error:
            KnowledgeAdministration(env.database, ADMIN).register_knowledge_schema(preset())
    assert "private detail" not in str(error.value)
    assert "class=OperationalError" in caplog.text
    assert stored(env.database) == ()


@pytest.mark.parametrize("corruption", ["json", "hash", "noncanonical"])
def test_corrupt_stored_definition_is_not_repaired(tmp_path, corruption):
    env = environment(tmp_path / "registry.db")
    admin = KnowledgeAdministration(env.database, ADMIN)
    admin.register_knowledge_schema(preset())
    with env.database.transaction() as conn:
        if corruption == "hash":
            conn.execute("UPDATE knowledge_schema_revision SET definition_hash='bad'")
        elif corruption == "json":
            conn.execute("UPDATE knowledge_schema_revision SET definition_json='{}'")
        else:
            row = conn.execute("SELECT definition_json FROM knowledge_schema_revision").fetchone()
            definition = SchemaDefinition.model_validate_json(row[0])
            conn.execute(
                "UPDATE knowledge_schema_revision SET definition_json=?",
                (json.dumps(json.loads(definition_json(definition)), indent=2),),
            )
    before = stored(env.database)
    with pytest.raises(EvidenceServiceError, match="internal_error"):
        admin.register_knowledge_schema(preset())
    assert stored(env.database) == before


def _register_worker(path, barrier, queue, name):
    admin = KnowledgeAdministration(EvidenceDatabase(Path(path)), ADMIN)
    value = preset().model_copy(update={"preset_name": name})
    barrier.wait()
    try:
        result = admin.register_knowledge_schema(value)
        queue.put((result.status, result.revision.revision_id))
    except EvidenceServiceError as error:
        queue.put((error.failure.code, None))


@pytest.mark.parametrize("same", [True, False])
def test_multiprocess_registration_serializes(tmp_path, same):
    env = environment(tmp_path / "registry.db")
    ctx = multiprocessing.get_context("spawn")
    barrier, queue = ctx.Barrier(2), ctx.Queue()
    processes = [
        ctx.Process(
            target=_register_worker,
            args=(str(env.database.path), barrier, queue, "one" if same or i == 0 else "two"),
        )
        for i in range(2)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(30)
        assert process.exitcode == 0
    outcomes = [queue.get(timeout=5) for _ in processes]
    assert sorted(o[0] for o in outcomes) == (
        ["applied", "unchanged"] if same else ["applied", "state_conflict"]
    )
    if same:
        assert outcomes[0][1] == outcomes[1][1]
    assert len(stored(env.database)) == 1


def test_example_reopens_exact_genesis(tmp_path):
    command = [
        sys.executable,
        str(Path(__file__).parents[1] / "examples/knowledge_schema.py"),
        "--database",
        str(tmp_path / "example.sqlite"),
    ]
    values = [
        SchemaPresetRegistration.model_validate_json(subprocess.check_output(command))
        for _ in range(2)
    ]
    assert [v.status for v in values] == ["applied", "unchanged"]
    assert values[0].revision == values[1].revision
