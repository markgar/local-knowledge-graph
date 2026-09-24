import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from kg.config import load_manifest
from kg.db import Database
from kg.ingest import IngestService
from kg.models.contracts import ActionResult
from kg.retrieval import RetrievalService


def _manifest(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir(parents=True)
    (vault / "atlas.md").write_text(
        "# Atlas\n\n- [ ] Confirm release.\n\nRelease evidence.\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "corpus.yml"
    manifest.write_text(
        "\n".join(
            [
                "corpus_id: test",
                "display_name: Test corpus",
                "vault_root: vault",
                "database: index.sqlite3",
                "include:",
                '  - "**/*.md"',
            ]
        ),
        encoding="utf-8",
    )
    return manifest


@pytest.mark.service
def test_ingestion_is_idempotent_and_preserves_revisions(tmp_path: Path) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    service = IngestService(Database(manifest.database))

    first = service.ingest(manifest)
    historical_action_id = RetrievalService(
        Database(manifest.database),
        manifest.corpus_id,
    ).actions("Atlas")[0].record_id
    with Database(manifest.database).connection() as connection:
        document_before = tuple(
            connection.execute(
                """
                SELECT title, current_revision_id, is_active, created_at, updated_at
                FROM source_document
                """
            ).fetchone()
        )
    second = service.ingest(manifest)
    with Database(manifest.database).connection() as connection:
        document_after = tuple(
            connection.execute(
                """
                SELECT title, current_revision_id, is_active, created_at, updated_at
                FROM source_document
                """
            ).fetchone()
        )
    (manifest.vault_root / "atlas.md").write_text(
        "# Atlas\n\n- [x] Confirm release.\n\nUpdated evidence.\n",
        encoding="utf-8",
    )
    third = service.ingest(manifest)

    assert (first.added, first.changed, first.unchanged) == (1, 0, 0)
    assert (second.added, second.changed, second.unchanged) == (0, 0, 1)
    assert document_after == document_before
    assert (third.added, third.changed, third.unchanged) == (0, 1, 0)
    with Database(manifest.database).connection() as connection:
        count = connection.execute("SELECT count(*) FROM source_revision").fetchone()[0]
    assert count == 2
    actions = RetrievalService(database=Database(manifest.database), corpus_id="test").actions(
        "Atlas"
    )
    assert [action.status for action in actions] == ["completed"]
    historical = RetrievalService(
        Database(manifest.database),
        manifest.corpus_id,
    ).evidence(historical_action_id)
    assert historical.status == "open"


@pytest.mark.service
def test_retrieval_returns_cited_actions_and_search_results(tmp_path: Path) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    retrieval = RetrievalService(database, manifest.corpus_id)

    actions = retrieval.actions("Atlas", "open")
    results = retrieval.search("evidence")

    assert actions[0].quote == "- [ ] Confirm release."
    assert actions[0].source_path == "atlas.md"
    assert results[0].quote == "Release evidence."
    assert results[0].anchor_id
    assert retrieval.search("evidence", source_path="missing.md") == []
    assert retrieval.actions("Atlas", source_path="missing.md") == []


@pytest.mark.service
def test_removed_source_is_not_returned_as_current_evidence(tmp_path: Path) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    (manifest.vault_root / "atlas.md").unlink()

    result = IngestService(database).ingest(manifest)
    retrieval = RetrievalService(database, manifest.corpus_id)

    assert result.missing == 1
    assert retrieval.actions("Atlas") == []
    assert retrieval.search("evidence") == []


@pytest.mark.service
def test_moved_source_preserves_document_and_revision_identity(tmp_path: Path) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    with database.connection() as connection:
        before = connection.execute(
            """
            SELECT document_id, current_revision_id
            FROM source_document
            """
        ).fetchone()

    (manifest.vault_root / "atlas.md").rename(manifest.vault_root / "renamed.md")
    result = IngestService(database).ingest(manifest)

    with database.connection() as connection:
        after = connection.execute(
            """
            SELECT document_id, current_revision_id, source_path
            FROM source_document
            """
        ).fetchone()
        document_count = connection.execute(
            "SELECT count(*) FROM source_document"
        ).fetchone()[0]
    assert result.changed == 1
    assert document_count == 1
    assert after["document_id"] == before["document_id"]
    assert after["current_revision_id"] == before["current_revision_id"]
    assert after["source_path"] == "renamed.md"


@pytest.mark.service
def test_move_onto_inactive_historical_path_preserves_active_lineage(
    tmp_path: Path,
) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    database = Database(manifest.database)
    service = IngestService(database)
    service.ingest(manifest)
    (manifest.vault_root / "atlas.md").unlink()
    (manifest.vault_root / "current.md").write_text(
        "# Current\n\nCurrent lineage.\n",
        encoding="utf-8",
    )
    service.ingest(manifest)
    with database.connection() as connection:
        current_id = connection.execute(
            "SELECT document_id FROM source_document WHERE source_path = 'current.md'"
        ).fetchone()["document_id"]

    (manifest.vault_root / "current.md").rename(manifest.vault_root / "atlas.md")
    result = service.ingest(manifest)

    with database.connection() as connection:
        rows = connection.execute(
            """
            SELECT document_id, is_active
            FROM source_document
            WHERE source_path = 'atlas.md'
            ORDER BY is_active DESC
            """
        ).fetchall()
    assert result.changed == 1
    assert [(row["document_id"], row["is_active"]) for row in rows] == [
        (current_id, 1),
        (rows[1]["document_id"], 0),
    ]
    assert rows[1]["document_id"] != current_id


@pytest.mark.service
def test_shared_database_keeps_corpora_isolated(tmp_path: Path) -> None:
    first_manifest_path = _manifest(tmp_path / "first")
    first_manifest = load_manifest(first_manifest_path)
    shared_database = tmp_path / "shared.sqlite3"
    first_manifest.database = shared_database

    second_root = tmp_path / "second"
    second_manifest_path = _manifest(second_root)
    second_manifest_path.write_text(
        second_manifest_path.read_text(encoding="utf-8")
        .replace("corpus_id: test", "corpus_id: other")
        .replace("database: index.sqlite3", f"database: {shared_database}"),
        encoding="utf-8",
    )
    (second_root / "vault" / "atlas.md").write_text(
        "# Other\n\n- [ ] Other task.\n",
        encoding="utf-8",
    )
    second_manifest = load_manifest(second_manifest_path)

    IngestService(Database(shared_database)).ingest(first_manifest)
    IngestService(Database(shared_database)).ingest(second_manifest)

    first_actions = RetrievalService(Database(shared_database), "test").actions()
    second_actions = RetrievalService(Database(shared_database), "other").actions()
    assert [action.summary for action in first_actions] == ["Confirm release."]
    assert [action.summary for action in second_actions] == ["Other task."]


@pytest.mark.service
def test_search_treats_punctuation_as_user_text(tmp_path: Path) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)

    assert RetrievalService(database, manifest.corpus_id).search("evidence!")


@pytest.mark.service
def test_natural_search_matches_any_safely_quoted_term(tmp_path: Path) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    retrieval = RetrievalService(database, manifest.corpus_id)

    assert retrieval.search("unrelated evidence", query_mode="strict") == []
    assert retrieval.search("unrelated evidence", query_mode="natural")


@pytest.mark.service
def test_invalid_source_does_not_rollback_valid_sources(tmp_path: Path) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    (manifest.vault_root / "broken.md").write_text(
        "---\ntitle: [invalid\n---\n\nBody\n",
        encoding="utf-8",
    )
    database = Database(manifest.database)

    result = IngestService(database).ingest(manifest)

    assert result.added == 1
    assert result.failed == 1
    assert RetrievalService(database, manifest.corpus_id).search("evidence")


@pytest.mark.service
def test_failed_update_deactivates_stale_current_evidence(tmp_path: Path) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    (manifest.vault_root / "atlas.md").write_bytes(b"\xff")

    result = IngestService(database).ingest(manifest)

    assert result.failed == 1
    assert RetrievalService(database, manifest.corpus_id).search("evidence") == []


@pytest.mark.service
def test_manifest_alias_change_reindexes_unchanged_revision(tmp_path: Path) -> None:
    manifest_path = _manifest(tmp_path)
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8")
        + "\nseed_entities:\n"
        + "  - entity_id: atlas\n"
        + "    name: Atlas\n"
        + "    entity_type: project\n",
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)
    database = Database(manifest.database)
    first = IngestService(database).ingest(manifest)
    revision = RetrievalService(database, manifest.corpus_id).search("evidence")[
        0
    ].source_revision_id

    manifest.seed_entities[0].aliases.append("Release evidence")
    second = IngestService(database).ingest(manifest)
    result = RetrievalService(database, manifest.corpus_id).search(
        "evidence",
        subject="Release evidence",
    )

    assert first.added == 1
    assert second.changed == 1
    assert result[0].source_revision_id == revision


@pytest.mark.service
def test_seed_entities_can_be_removed_without_deleting_historical_identity(
    tmp_path: Path,
) -> None:
    manifest_path = _manifest(tmp_path)
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8")
        + "\nseed_entities:\n"
        + "  - entity_id: atlas\n"
        + "    name: Atlas\n"
        + "    entity_type: project\n",
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)
    database = Database(manifest.database)
    service = IngestService(database)
    service.ingest(manifest)
    manifest.seed_entities = []

    result = service.ingest(manifest)

    with database.connection() as connection:
        entity = connection.execute(
            """
            SELECT entity_id, canonical_name, is_active
            FROM entity WHERE corpus_id = 'test'
            """
        ).fetchone()
        revisions = connection.execute("SELECT count(*) FROM source_revision").fetchone()[0]
        anchors = connection.execute("SELECT count(*) FROM source_anchor").fetchone()[0]
    assert result.changed == 1
    assert tuple(entity) == ("atlas", "Atlas", 0)
    assert revisions == 1
    assert anchors > 0


@pytest.mark.service
def test_seed_entity_can_be_replaced_by_new_id_with_same_identity(
    tmp_path: Path,
) -> None:
    manifest_path = _manifest(tmp_path)
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8")
        + "\nseed_entities:\n"
        + "  - entity_id: old-atlas\n"
        + "    name: Atlas\n"
        + "    entity_type: project\n",
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)
    database = Database(manifest.database)
    service = IngestService(database)
    service.ingest(manifest)
    manifest.seed_entities[0].entity_id = "new-atlas"

    result = service.ingest(manifest)

    with database.connection() as connection:
        entities = connection.execute(
            """
            SELECT entity_id, canonical_name, is_active
            FROM entity WHERE corpus_id = 'test'
            ORDER BY entity_id
            """
        ).fetchall()
    assert result.changed == 1
    assert [tuple(entity) for entity in entities] == [
        ("new-atlas", "Atlas", 1),
        ("old-atlas", "Atlas", 0),
    ]


@pytest.mark.service
def test_ingestion_migrates_legacy_uniqueness_constraints(tmp_path: Path) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    with sqlite3.connect(manifest.database) as connection:
        connection.executescript(
            """
            CREATE TABLE source_document (
                document_id TEXT PRIMARY KEY,
                corpus_id TEXT NOT NULL,
                source_path TEXT NOT NULL,
                title TEXT NOT NULL,
                current_revision_id TEXT,
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (corpus_id, source_path)
            );
            CREATE TABLE entity (
                entity_key TEXT PRIMARY KEY,
                corpus_id TEXT NOT NULL,
                entity_id TEXT NOT NULL,
                entity_type TEXT NOT NULL,
                canonical_name TEXT NOT NULL,
                UNIQUE (corpus_id, entity_id),
                UNIQUE (corpus_id, entity_type, canonical_name)
            );
            """
        )

    result = IngestService(Database(manifest.database)).ingest(manifest)

    assert result.added == 1
    with Database(manifest.database).connection() as connection:
        entity_columns = {
            row["name"] for row in connection.execute("PRAGMA table_info(entity)")
        }
        indexes = {
            row["name"]
            for row in connection.execute(
                """
                SELECT name FROM sqlite_master
                WHERE type = 'index' AND name IN (
                    'source_document_active_path_idx',
                    'entity_active_name_idx'
                )
                """
            )
        }
    assert "is_active" in entity_columns
    assert indexes == {"source_document_active_path_idx", "entity_active_name_idx"}


@pytest.mark.service
def test_status_resolves_explicit_wikilink_relationships(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "atlas.md").write_text(
        "---\ntitle: Project Atlas\ndate: 2026-09-17\n---\n\n"
        "# Project Atlas\n\nAtlas depends on [[Compass]].\n",
        encoding="utf-8",
    )
    manifest_path = tmp_path / "corpus.yml"
    manifest_path.write_text(
        """
corpus_id: graph
display_name: Graph corpus
vault_root: vault
database: index.sqlite3
include:
  - "*.md"
seed_entities:
  - entity_id: atlas
    name: Project Atlas
    entity_type: project
    aliases: [Atlas]
  - entity_id: compass
    name: Compass
    entity_type: product
    aliases: []
metadata_fields:
  event_time: date
""".strip(),
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)

    retrieval = RetrievalService(database, manifest.corpus_id)
    result = retrieval.status("Atlas")

    assert [item.related_entity_ids for item in result.connected_entities] == [
        ["atlas", "compass"]
    ]
    assert result.recent_material
    assert {item.event_time for item in result.recent_material} == {"2026-09-17"}
    assert retrieval.connections("Atlas", datetime(9999, 1, 1, tzinfo=UTC)) == []

    with database.transaction() as connection:
        connection.execute(
            "UPDATE passage SET event_time = '2026-09-17T23:30:00-05:00'"
        )

    assert retrieval.connections(
        "Atlas",
        datetime(2026, 9, 18, 3, 0, tzinfo=UTC),
    )


@pytest.mark.service
def test_status_uses_one_read_snapshot_during_concurrent_update(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    (manifest.vault_root / "atlas.md").write_text(
        "# Atlas\n\n## Actions\n\n- [ ] Ship it.\n",
        encoding="utf-8",
    )
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    retrieval = RetrievalService(database, manifest.corpus_id)
    original_actions = retrieval._actions
    action_queries = 0

    def actions(
        connection: sqlite3.Connection,
        *,
        subject: str | None = None,
        status: str | None = None,
        since: datetime | None = None,
        source_path: str | None = None,
    ) -> list[ActionResult]:
        nonlocal action_queries
        results = original_actions(
            connection,
            subject=subject,
            status=status,
            since=since,
            source_path=source_path,
        )
        action_queries += 1
        if action_queries == 1:
            with database.transaction() as writer:
                writer.execute(
                    "UPDATE action_item SET status = 'completed' WHERE status = 'open'"
                )
        return results

    monkeypatch.setattr(retrieval, "_actions", actions)

    result = retrieval.status("Atlas")

    assert [action.summary for action in result.open_actions] == ["Ship it."]
    assert result.completed_actions == []
    assert [action.summary for action in retrieval.actions(status="completed")] == [
        "Ship it."
    ]


@pytest.mark.service
def test_search_rejects_non_positive_limit(tmp_path: Path) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)

    with pytest.raises(ValueError, match="limit must be at least 1"):
        RetrievalService(database, manifest.corpus_id).search("evidence", limit=0)


@pytest.mark.service
def test_ingestion_persists_structured_tasks_and_explicit_records(tmp_path: Path) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    (manifest.vault_root / "atlas.md").write_text(
        "# Atlas\n\n## Actions\n\n"
        "- [ ] Ship it. [owner:: Avery] [due:: 2026-10-01]\n\n"
        "## Decisions\n\n- Use SQLite.\n\n"
        "## Blockers\n\n- Waiting for approval.\n\n"
        "## Conflicts\n\n- Two sources disagree.\n",
        encoding="utf-8",
    )
    database = Database(manifest.database)

    IngestService(database).ingest(manifest)

    with database.connection() as connection:
        action = connection.execute(
            "SELECT text, owner, due_date FROM action_item"
        ).fetchone()
        counts = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("decision", "blocker", "conflict")
        }
    assert tuple(action) == ("Ship it.", "Avery", "2026-10-01")
    assert counts == {"decision": 1, "blocker": 1, "conflict": 1}

    status = RetrievalService(database, manifest.corpus_id).status("Atlas")
    assert [item.summary for item in status.decisions] == ["Use SQLite."]
    assert [item.summary for item in status.blockers] == ["Waiting for approval."]
    assert [item.summary for item in status.conflicts] == ["Two sources disagree."]


@pytest.mark.service
def test_ingestion_preserves_multiline_explicit_record_text(tmp_path: Path) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    (manifest.vault_root / "atlas.md").write_text(
        "# Atlas\n\n## Decisions\n\n"
        "- Use SQLite for local storage.\n"
        "  Keep provenance in normalized tables.\n\n"
        "## Blockers\n\n"
        "Approval is pending.\n"
        "The review board meets Friday.\n\n"
        "## Conflicts\n\n"
        "- Source A says launch.\n"
        "  Source B says wait.\n",
        encoding="utf-8",
    )
    database = Database(manifest.database)

    IngestService(database).ingest(manifest)
    status = RetrievalService(database, manifest.corpus_id).status("Atlas")

    assert [item.summary for item in status.decisions] == [
        "Use SQLite for local storage.\nKeep provenance in normalized tables."
    ]
    assert [item.summary for item in status.blockers] == [
        "Approval is pending.\nThe review board meets Friday."
    ]
    assert [item.summary for item in status.conflicts] == [
        "Source A says launch.\nSource B says wait."
    ]


@pytest.mark.service
def test_code_and_literal_wikilinks_do_not_create_graph_evidence(
    tmp_path: Path,
) -> None:
    manifest_path = _manifest(tmp_path)
    manifest_path.write_text(
        manifest_path.read_text(encoding="utf-8")
        + "\nseed_entities:\n"
        + "  - entity_id: atlas\n"
        + "    name: Atlas\n"
        + "    entity_type: project\n"
        + "  - entity_id: target\n"
        + "    name: Target\n"
        + "    entity_type: project\n"
        + "  - entity_id: inline\n"
        + "    name: Inline\n"
        + "    entity_type: project\n"
        + "  - entity_id: literal\n"
        + "    name: Literal\n"
        + "    entity_type: project\n",
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)
    (manifest.vault_root / "atlas.md").write_text(
        "# Atlas\n\nAtlas links to [[Target]], not `[[Inline]]` or \\[[Literal]].\n",
        encoding="utf-8",
    )
    database = Database(manifest.database)

    IngestService(database).ingest(manifest)

    with database.connection() as connection:
        mentioned = {
            row["entity_id"]
            for row in connection.execute(
                """
                SELECT DISTINCT e.entity_id
                FROM mention m JOIN entity e ON e.entity_key = m.entity_key
                """
            )
        }
        relationships = connection.execute(
            "SELECT count(*) FROM relationship"
        ).fetchone()[0]
    assert mentioned == {"atlas", "target"}
    assert relationships == 1


@pytest.mark.service
def test_subject_scope_traverses_two_hops_across_documents(tmp_path: Path) -> None:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "atlas.md").write_text(
        "# Atlas\n\nAtlas links to [[Compass]].\n",
        encoding="utf-8",
    )
    (vault / "compass.md").write_text(
        "# Compass\n\nCompass links to [[Beacon]].\n",
        encoding="utf-8",
    )
    (vault / "beacon.md").write_text(
        "# Beacon\n\n## Actions\n\n- [ ] Validate the prototype.\n",
        encoding="utf-8",
    )
    manifest_path = tmp_path / "corpus.yml"
    manifest_path.write_text(
        """
corpus_id: graph
display_name: Graph corpus
vault_root: vault
database: index.sqlite3
include:
  - "*.md"
seed_entities:
  - entity_id: atlas
    name: Atlas
    entity_type: project
  - entity_id: compass
    name: Compass
    entity_type: product
  - entity_id: beacon
    name: Beacon
    entity_type: topic
""".strip(),
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)

    retrieval = RetrievalService(database, manifest.corpus_id)
    status = retrieval.status("Atlas")

    assert [item.related_entity_ids for item in status.connected_entities] == [
        ["atlas", "compass"],
        ["compass", "beacon"],
    ]
    assert [action.summary for action in status.open_actions] == [
        "Validate the prototype."
    ]
    assert retrieval.search("prototype", subject="Atlas")
