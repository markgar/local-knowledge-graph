from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from support.evidence import environment, put, receipt

from kg._sqlite import EVIDENCE_APPLICATION_ID
from kg.evidence import EvidenceDatabase, EvidenceServiceError
from kg.evidence import database as database_module
from kg.evidence._format import expected_manifest, schema_sql

TABLES = set(
    [
        "store_format",
        "schema_object_manifest",
        "corpus",
        "source_namespace",
        "writer_binding",
        "policy_grant",
        "document",
        "revision",
        "anchor_set",
        "document_state",
        "metadata_snapshot",
        "anchor",
        "anchor_set_member",
        "processing_state",
        "write_key",
        "write_response",
        "write_provenance",
        "receipt_clock",
        "knowledge_schema_revision",
        "knowledge_schema_head",
        "knowledge_schema_change",
        "knowledge_schema_receipt",
        "knowledge_writer_binding",
        "entity",
        "entity_origin",
        "classification",
        "classification_head",
        "classification_selection",
        "classification_selection_targets",
        "classification_withdrawal",
        "assertion_classification",
        "contribution",
        "entity_support",
        "alias",
        "identifier",
        "mention",
        "assertion",
        "assertion_withdrawal",
        "contribution_evidence",
        "seed_set",
        "seed_slot",
        "contribution_seed",
        "seed_membership_event",
        "knowledge_write_response",
        "knowledge_write_provenance",
        "passage_policy_definition",
        "passage_set",
        "passage",
        "passage_set_member",
        "state_passage_set",
        "index_configuration",
        "index_work_slot",
        "index_attempt",
        "index_staging_member",
        "document_projection",
        "projection_member",
        "active_document_projection",
        "processing_guard",
        "processing_plan",
        "processing_worker_binding",
        "processing_job",
        "processing_dependency",
        "processing_checkpoint",
        "processing_batch",
        "processing_unit",
        "state_intent",
        "processing_scan",
        "processing_intent_ack",
        "sync_scope",
        "sync_run",
        "sync_page",
        "sync_identity",
        "sync_seen",
        "sync_removal_provenance",
        "processing_key",
        "processing_response",
    ]
)
INDEXES = set(
    [
        "anchor_revision_idx",
        "state_document_idx",
        "policy_principal_idx",
        "knowledge_binding_principal_idx",
        "entity_name_idx",
        "contribution_owner_idx",
        "entity_support_entity_idx",
        "alias_name_idx",
        "alias_entity_idx",
        "identifier_value_idx",
        "identifier_entity_idx",
        "mention_entity_idx",
        "assertion_subject_idx",
        "assertion_predicate_idx",
        "assertion_object_idx",
        "classification_entity",
        "contribution_evidence_state_idx",
        "contribution_seed_slot_idx",
        "seed_event_slot_idx",
        "passage_anchor_idx",
        "projection_state_idx",
        "projection_reuse_idx",
        "processing_claim_idx",
        "processing_status_idx",
        "processing_dependency_state_idx",
        "processing_lease_idx",
        "intent_corpus_sequence_idx",
        "sync_seen_document_idx",
        "write_key_expiry_idx",
        "processing_key_expiry_idx",
    ]
)


def _image(path: Path) -> tuple[bytes, tuple[object, ...]]:
    with sqlite3.connect(path) as connection:
        logical = (
            connection.execute("PRAGMA application_id").fetchone()[0],
            connection.execute("PRAGMA user_version").fetchone()[0],
            connection.execute("PRAGMA journal_mode").fetchone()[0],
            tuple(connection.iterdump()),
        )
    return path.read_bytes(), logical


@pytest.mark.service
def test_complete_inventory_and_relational_programs(tmp_path: Path) -> None:
    assert expected_manifest().signature == (
        "abba0723ec31fb9ce3c32c9c1bdb62d8f5a98de1ec72693a6d67ce1073efe5a9"
    )
    database = EvidenceDatabase(tmp_path / "complete.db")
    database.initialize()
    with database.connection() as connection:
        catalog = connection.execute(
            "SELECT type,name FROM sqlite_schema WHERE name NOT GLOB 'sqlite_*'"
        ).fetchall()
        assert {row[1] for row in catalog if row[0] == "table"} == TABLES
        assert {row[1] for row in catalog if row[0] == "index"} == INDEXES
        assert len(TABLES) == 76 and len(INDEXES) == 30
        assert {row[0] for row in catalog} == {"table", "index"}
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 5
        assert tuple(connection.execute("SELECT * FROM store_format").fetchone()) == (
            1,
            "evidence-store/5",
            "canonical-sqlite-manifest/1",
            expected_manifest().signature,
        )
        count = connection.execute("SELECT count(*) FROM schema_object_manifest").fetchone()[0]
        assert count == 106
        for table in sorted(TABLES):
            # Compilation catches missing parent keys even when every future-service table is empty.
            connection.execute(f'EXPLAIN DELETE FROM "{table}"').fetchall()
            for column in connection.execute("SELECT * FROM pragma_table_info(?)", (table,)):
                if column["pk"] and column["type"] != "INTEGER":
                    assert column["notnull"], (table, column["name"])


@pytest.mark.service
@pytest.mark.parametrize(
    "alteration",
    [
        "DROP INDEX assertion_subject_idx",
        "DROP TABLE processing_checkpoint",
        "ALTER TABLE entity ADD COLUMN unexpected TEXT",
        "CREATE TABLE unrelated(value TEXT)",
        "CREATE VIEW unexpected AS SELECT 1",
        "CREATE TRIGGER unexpected AFTER INSERT ON entity BEGIN SELECT 1; END",
        "UPDATE store_format SET schema_signature='copied-but-not-valid'",
        "DELETE FROM store_format",
        "DELETE FROM schema_object_manifest WHERE name='projection_member'",
        "UPDATE schema_object_manifest SET definition_hash='forged' WHERE name='entity'",
        "PRAGMA user_version=1",
        "PRAGMA user_version=3",
        "PRAGMA application_id=0",
    ],
)
def test_altered_format_rejected_without_mutation(tmp_path: Path, alteration: str) -> None:
    database = EvidenceDatabase(tmp_path / "altered.db")
    database.initialize()
    with sqlite3.connect(database.path) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute(alteration)
    before = _image(database.path)
    for admission in (database.initialize, database.connection, database.transaction):
        with pytest.raises(EvidenceServiceError) as error:
            if admission == database.initialize:
                admission()
            else:
                with admission():
                    pytest.fail("A partial or altered format was admitted")
        assert error.value.failure.code == "unsupported"
        assert _image(database.path) == before


@pytest.mark.service
@pytest.mark.parametrize(
    "replacement",
    [
        "CREATE TABLE processing_guard(corpus_id TEXT PRIMARY KEY,epoch INTEGER,blocked INTEGER)",
        "CREATE TABLE processing_guard(corpus_id TEXT NOT NULL PRIMARY KEY REFERENCES corpus,"
        "epoch INTEGER NOT NULL CHECK(epoch>=0),blocked INTEGER NOT NULL CHECK(blocked IN(0,1)))",
    ],
)
def test_copied_manifest_cannot_hide_constraint_changes(tmp_path: Path, replacement: str) -> None:
    database = EvidenceDatabase(tmp_path / "changed-check.db")
    database.initialize()
    with sqlite3.connect(database.path) as connection:
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("DROP TABLE processing_guard")
        connection.execute(replacement)
    before = _image(database.path)
    with pytest.raises(EvidenceServiceError, match="unsupported"):
        database.initialize()
    assert _image(database.path) == before


@pytest.mark.service
@pytest.mark.parametrize("version", [1, 2, 3, 4])
def test_old_or_spoofed_nonempty_format_is_not_repaired(tmp_path: Path, version: int) -> None:
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as connection:
        connection.execute(f"PRAGMA application_id={EVIDENCE_APPLICATION_ID}")
        connection.execute(f"PRAGMA user_version={version}")
        connection.execute("CREATE TABLE store_format(singleton INTEGER PRIMARY KEY,format TEXT)")
        connection.execute("INSERT INTO store_format VALUES(1,?)", (f"evidence-store/{version}",))
        connection.execute("CREATE TABLE retained(content BLOB)")
        connection.execute("INSERT INTO retained VALUES (?)", (b"A\r\n\0retained",))
    before = _image(path)
    with pytest.raises(EvidenceServiceError) as error:
        EvidenceDatabase(path).initialize()
    assert error.value.failure.code == "unsupported"
    assert _image(path) == before


@pytest.mark.service
@pytest.mark.parametrize("stage", ["ddl", "manifest"])
def test_failed_full_initialization_rolls_back_headers_and_objects(
    tmp_path: Path,
    monkeypatch,
    stage: str,
) -> None:
    path = tmp_path / "empty.db"
    with sqlite3.connect(path):
        pass
    if stage == "ddl":
        monkeypatch.setattr(database_module, "schema_sql", lambda: schema_sql() + "INVALID SQL;")
    else:
        original = database_module.install_manifest

        def fail_after_manifest(connection):
            original(connection)
            raise sqlite3.OperationalError("injected manifest failure")

        monkeypatch.setattr(database_module, "install_manifest", fail_after_manifest)
    with pytest.raises(EvidenceServiceError) as error:
        EvidenceDatabase(path).initialize()
    assert error.value.failure.code == "internal_error"
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA application_id").fetchone()[0] == 0
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute("SELECT * FROM sqlite_schema").fetchall() == []
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def _insert(connection: sqlite3.Connection, table: str, **values: object) -> None:
    columns = ",".join(values)
    markers = ",".join("?" for _ in values)
    connection.execute(f"INSERT INTO {table}({columns}) VALUES ({markers})", tuple(values.values()))


@pytest.fixture
def relational_store(tmp_path: Path):
    """Real evidence with explicitly schema-only future-package rows, not service acceptance."""
    env = environment(tmp_path / "relations.db")
    other = environment(env.database.path, corpus="other")
    saved = receipt(env.service.write(put(env.scope)))
    foreign = receipt(other.service.write(put(other.scope)))
    with env.database.transaction() as connection:
        _insert(
            connection,
            "knowledge_schema_revision",
            corpus_id="work",
            revision_id="s",
            sequence=1,
            definition_json="{}",
            definition_hash="h",
            origin="trusted_preset",
            committed_at="now",
        )
        _insert(
            connection,
            "entity",
            entity_id="e",
            corpus_id="work",
            name="Entity",
            creation_sequence=1,
        )
        _insert(
            connection,
            "entity",
            entity_id="foreign-e",
            corpus_id="other",
            name="Entity",
            creation_sequence=1,
        )
        key = connection.execute("SELECT key_id FROM write_key WHERE corpus_id='work'").fetchone()[
            0
        ]
        for i, kind in enumerate(
            ("entity_support", "alias", "identifier", "mention", "assertion"), 1
        ):
            _insert(
                connection,
                "contribution",
                contribution_id=kind,
                corpus_id="work",
                sequence=i,
                kind=kind,
                key_id=key,
                local_id=kind,
                owner_id="owner",
                writer_id="writer",
                producer="test",
                producer_version="1",
                schema_version="s",
                committed_at="now",
                support_kind="source",
            )
    return env, saved, foreign


@pytest.mark.service
@pytest.mark.parametrize(
    "kind,extra",
    [
        ("entity_support", {"attested_name": "Entity", "attested_type": "person"}),
        ("alias", {"alias": "Alias"}),
        ("identifier", {"scheme": "test", "value": "id"}),
        ("mention", {}),
    ],
)
def test_typed_details_reject_wrong_kind_and_cross_corpus(relational_store, kind, extra) -> None:
    env, _, _ = relational_store
    with env.database.connection() as connection:
        for contribution, entity in ((kind, "foreign-e"), ("assertion", "e")):
            with pytest.raises(sqlite3.IntegrityError):
                _insert(
                    connection,
                    kind,
                    corpus_id="work",
                    contribution_id=contribution,
                    kind=kind,
                    entity_id=entity,
                    **extra,
                )
        _insert(
            connection,
            kind,
            corpus_id="work",
            contribution_id=kind,
            kind=kind,
            entity_id="e",
            **extra,
        )
        connection.rollback()


@pytest.mark.service
@pytest.mark.parametrize(
    "object_kind,column,value",
    [
        ("entity", "object_entity_id", "e"),
        ("string", "object_string", "statement"),
        ("integer", "object_integer", str(2**100)),
        ("boolean", "object_boolean", 1),
        ("timestamp", "object_timestamp", "2026-09-21T00:00:00.000000+00:00"),
    ],
)
def test_assertion_exactly_one_discriminated_object(
    relational_store,
    object_kind,
    column,
    value,
) -> None:
    env, _, _ = relational_store
    base = dict(
        corpus_id="work",
        contribution_id="assertion",
        kind="assertion",
        subject_id="e",
        predicate="decision",
        interpretation="explicit",
        object_kind=object_kind,
    )
    with env.database.connection() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            _insert(connection, "assertion", **base)
        extra = "object_string" if column != "object_string" else "object_integer"
        with pytest.raises(sqlite3.IntegrityError):
            _insert(connection, "assertion", **base, **{column: value, extra: "2"})
        _insert(connection, "assertion", **base, **{column: value})
        assert connection.execute(f"SELECT {column} FROM assertion").fetchone()[0] == value
        connection.rollback()


@pytest.mark.service
def test_evidence_scope_and_passage_nullity(relational_store) -> None:
    env, saved, foreign = relational_store
    ref = env.service.anchors(env.scope, saved.document_id, saved.processing.state_version).entries[
        0
    ]
    base = dict(
        corpus_id="work",
        contribution_id="assertion",
        ordinal=1,
        support_kind="source",
        namespace="markdown",
        document_id=saved.document_id,
        revision_id=saved.revision_id,
        anchor_id=ref.reference.anchor_id,
        state_version=saved.processing.state_version,
        metadata_snapshot_id=saved.metadata_snapshot_id,
    )
    with env.database.connection() as connection:
        for changes in (
            {"document_id": foreign.document_id},
            {"namespace": "email"},
            {"metadata_snapshot_id": foreign.metadata_snapshot_id},
            {"passage_id": "missing"},
            {"passage_set_id": "missing"},
            {"passage_id": "missing", "passage_set_id": "missing"},
            {"support_kind": "seed"},
            {"ordinal": 201},
        ):
            with pytest.raises(sqlite3.IntegrityError):
                _insert(connection, "contribution_evidence", **(base | changes))
        _insert(connection, "contribution_evidence", **base)
        connection.rollback()


@pytest.mark.service
def test_schema_presence_does_not_enable_services(tmp_path: Path) -> None:
    env = environment(tmp_path / "capabilities.db")
    capabilities = env.service.capabilities()
    assert capabilities.interface_version == "evidence/2"
    assert "passages" in capabilities.operations
    assert "enrich" in capabilities.operations
    assert {"indexing", "search", "synchronization", "purge"} <= set(
        capabilities.unsupported
    )
    assert not set(capabilities.operations) & {
        "process",
        "search",
        "execute",
        "claim",
        "begin_snapshot",
    }
    saved = receipt(env.service.write(put(env.scope)))
    view = env.service.document(env.scope, saved.document_id)
    assert view.indexing_reason == view.enrichment_reason == "processor_not_available"
    assert "processing_reason" not in view.model_dump()
    with env.database.connection() as connection:
        assert tuple(connection.execute("SELECT * FROM processing_guard").fetchone()) == (
            "work",
            0,
            0,
        )
        for table in ("processing_job", "contribution", "passage", "document_projection"):
            assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0


@pytest.mark.service
@pytest.mark.parametrize(
    "table,column",
    [
        ("revision", "sequence"),
        ("revision", "byte_length"),
        ("document_state", "sequence"),
        ("anchor", "ordinal"),
        ("anchor", "start_offset"),
        ("anchor", "end_offset"),
        ("anchor_set_member", "ordinal"),
    ],
)
def test_evidence_integer_fields_reject_fractional_values(tmp_path: Path, table, column) -> None:
    env = environment(tmp_path / "integers.db")
    receipt(env.service.write(put(env.scope)))
    with env.database.connection() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(f"UPDATE {table} SET {column}=1.5")
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.fixture
def passage_store(relational_store):
    env, saved, foreign = relational_store
    ref = (
        env.service.anchors(env.scope, saved.document_id, saved.processing.state_version)
        .entries[0]
        .reference
    )
    scope = dict(
        corpus_id="work",
        namespace="markdown",
        document_id=saved.document_id,
        revision_id=saved.revision_id,
    )
    with env.database.transaction() as connection:
        _insert(
            connection,
            "passage_policy_definition",
            policy_token="policy",
            definition_hash="definition",
            definition_json="{}",
        )
        _insert(
            connection,
            "passage_set",
            **scope,
            passage_set_id="set",
            policy_token="policy",
            definition_hash="definition",
            manifest_hash="manifest",
            member_count=1,
            origin_state=saved.processing.state_version,
        )
        _insert(
            connection,
            "passage",
            **scope,
            passage_set_id="set",
            passage_id="passage",
            anchor_id=ref.anchor_id,
            ordinal=1,
            origin_state=saved.processing.state_version,
        )
        _insert(
            connection, "passage_set_member", passage_set_id="set", passage_id="passage", ordinal=1
        )
        _insert(
            connection,
            "state_passage_set",
            **scope,
            passage_set_id="set",
            state_version=saved.processing.state_version,
        )
        _insert(
            connection,
            "index_configuration",
            configuration_id="config",
            descriptor_json="{}",
            descriptor_hash="config-hash",
        )
        _insert(
            connection,
            "index_work_slot",
            corpus_id="work",
            namespace="markdown",
            document_id=saved.document_id,
            configuration_id="config",
            fence=1,
        )
        _insert(
            connection,
            "index_attempt",
            **scope,
            attempt_id="attempt",
            state_version=saved.processing.state_version,
            configuration_id="config",
            fence=1,
            status="staging",
            created_at="now",
        )
        _insert(
            connection,
            "document_projection",
            **scope,
            projection_id="projection",
            state_version=saved.processing.state_version,
            passage_set_id="set",
            configuration_id="config",
            execution_identity_json="{}",
            execution_identity_hash="identity",
            attempt_id="attempt",
            lexical_count=1,
            vector_count=1,
            manifest_hash="manifest",
            created_at="now",
        )
    return env, saved, foreign, ref, scope


@pytest.mark.service
def test_passage_evidence_requires_actual_state_membership_and_anchor(passage_store) -> None:
    env, saved, _, ref, scope = passage_store
    newer = receipt(
        env.service.write(
            put(env.scope, state=saved.processing.state_version, title="New metadata")
        )
    )
    base = dict(
        **scope,
        contribution_id="assertion",
        ordinal=1,
        support_kind="source",
        anchor_id=ref.anchor_id,
        state_version=saved.processing.state_version,
        metadata_snapshot_id=saved.metadata_snapshot_id,
        passage_set_id="set",
        passage_id="passage",
    )
    with env.database.connection() as connection:
        with pytest.raises(sqlite3.IntegrityError):
            _insert(
                connection,
                "contribution_evidence",
                **(
                    base
                    | {
                        "state_version": newer.processing.state_version,
                        "metadata_snapshot_id": newer.metadata_snapshot_id,
                    }
                ),
            )
        anchor = dict(
            connection.execute(
                "SELECT * FROM anchor WHERE anchor_id=?", (ref.anchor_id,)
            ).fetchone()
        )
        _insert(
            connection,
            "anchor",
            **(
                anchor
                | {
                    "anchor_id": "different-anchor",
                    "origin_key": "different-origin",
                    "ordinal": 2,
                }
            ),
        )
        with pytest.raises(sqlite3.IntegrityError):
            _insert(
                connection,
                "contribution_evidence",
                **(
                    base
                    | {
                        "anchor_id": "different-anchor",
                    }
                ),
            )
        _insert(connection, "contribution_evidence", **base)
        connection.rollback()


@pytest.mark.service
@pytest.mark.parametrize(
    "table,owner",
    [
        ("index_staging_member", {"attempt_id": "attempt", "configuration_id": "config"}),
        ("projection_member", {"projection_id": "projection"}),
    ],
)
def test_vectors_and_projection_scope_constraints(passage_store, table, owner) -> None:
    env, _, foreign, _, scope = passage_store
    base = dict(
        **scope,
        **owner,
        passage_set_id="set",
        passage_id="passage",
        ordinal=1,
        lexical_input="exact",
        representation_hash="representation",
        vector=b"\0\0\0\0",
        dimensions=1,
        vector_hash="vector",
    )
    with env.database.connection() as connection:
        for changes in (
            {"vector": "text"},
            {"vector": b""},
            {"dimensions": 2},
            {"dimensions": 1.5},
            {"dimensions": 0},
            {"namespace": "email"},
            {"document_id": foreign.document_id},
        ):
            with pytest.raises(sqlite3.IntegrityError):
                _insert(connection, table, **(base | changes))
        _insert(connection, table, **base)
        connection.rollback()


@pytest.mark.service
def test_attempt_terminal_and_identity_nullity(passage_store) -> None:
    env, _, _, _, _ = passage_store
    with env.database.connection() as connection:
        for assignment in (
            "status='published'",
            "completed_at='now'",
            "execution_identity_hash='alone'",
            "reason='invented'",
            "fence=0",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(f"UPDATE index_attempt SET {assignment}")
        connection.execute(
            "UPDATE index_attempt SET status='published',completed_at='now',"
            "execution_identity_hash='identity',execution_identity_json='{}'"
        )
        connection.rollback()


@pytest.mark.service
def test_worker_claim_target_and_settlement_constraints(relational_store) -> None:
    env, saved, foreign = relational_store
    with env.database.connection() as connection:
        _insert(
            connection,
            "processing_plan",
            corpus_id="work",
            plan_id="plan",
            plan_version="1",
            kind="enrichment",
            producer="test",
            producer_version="1",
            definition_hash="plan",
            capability_json="{}",
            enabled=1,
        )
        _insert(
            connection,
            "processing_batch",
            corpus_id="work",
            batch_id="batch",
            principal_id="principal",
            owner_id="owner",
            writer_id="writer",
            envelope_digest="batch",
            item_count=1,
            created_at="now",
        )
        key = dict(connection.execute("SELECT * FROM write_key WHERE corpus_id='work'").fetchone())
        _insert(
            connection,
            "processing_unit",
            unit_id="unit",
            corpus_id="work",
            batch_id="batch",
            ordinal=0,
            operation=key["operation"],
            writer_id="writer",
            retry_key_hash=key["key_hash"],
            payload_digest=key["digest"],
            status="pending",
            attempt_fence=0,
            transient=0,
        )
        base = dict(
            job_id="job",
            corpus_id="work",
            namespace="markdown",
            owner_id="owner",
            writer_id="writer",
            principal_id="principal",
            plan_id="plan",
            plan_version="1",
            target_kind="document",
            document_id=saved.document_id,
            target_state=saved.processing.state_version,
            target_digest="target",
            logical_key="logical",
            creation_sequence=1,
            status="queued",
            status_version=1,
            checkpoint_sequence=0,
            lifetime_attempts=0,
            retry_episode=1,
            episode_attempts=0,
            next_due_at="now",
            claim_fence=0,
            guard_epoch=0,
            created_at="now",
            updated_at="now",
        )
        for changes in (
            {"status": "running"},
            {"worker_id": "worker"},
            {"status": "succeeded"},
            {"failure_code": "internal_error"},
            {"target_kind": "unit"},
            {"target_state": foreign.processing.state_version},
            {"batch_id": "batch", "unit_id": "unit"},
            {"episode_attempts": 11},
            {
                "status": "succeeded",
                "result_kind": "projection",
                "result_id": "projection",
                "result_outcome": "applied",
            },
        ):
            with pytest.raises(sqlite3.IntegrityError):
                _insert(connection, "processing_job", **(base | changes))
        _insert(
            connection,
            "processing_job",
            **(
                base
                | {
                    "target_kind": "unit",
                    "document_id": None,
                    "target_state": None,
                    "batch_id": "batch",
                    "unit_id": "unit",
                }
            ),
        )
        for assignment in (
            "status='settled'",
            f"canonical_key_id='{key['key_id']}'",
            "status='settled',failure_code='internal_error'",
            f"status='settled',canonical_key_id='{key['key_id']}',writer_id='other-writer'",
        ):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(f"UPDATE processing_unit SET {assignment}")
        connection.execute(
            "UPDATE processing_unit SET status='settled',canonical_key_id=?", (key["key_id"],)
        )
        connection.rollback()
