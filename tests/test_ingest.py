from pathlib import Path

from kg.config import load_manifest
from kg.db import Database
from kg.ingest import IngestService
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


def test_search_treats_punctuation_as_user_text(tmp_path: Path) -> None:
    manifest = load_manifest(_manifest(tmp_path))
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)

    assert RetrievalService(database, manifest.corpus_id).search("evidence!")


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

    result = RetrievalService(database, manifest.corpus_id).status("Atlas")

    assert result.connected_entities == ["compass"]
    assert result.recent_material
    assert {item.event_time for item in result.recent_material} == {"2026-09-17"}


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

    assert status.connected_entities == ["compass", "beacon"]
    assert [action.summary for action in status.open_actions] == [
        "Validate the prototype."
    ]
    assert retrieval.search("prototype", subject="Atlas")
