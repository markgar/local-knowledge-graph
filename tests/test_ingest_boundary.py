from __future__ import annotations

import json
import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from kg.db import Database
from kg.ids import anchor_id, digest, document_id, record_id, revision_id
from kg.ingest import IngestService
from kg.ingest._intake import prepare_document
from kg.ingest._prepared import PreparedAnchor, PreparedDocument, PreparedTask
from kg.ingest._writer import DocumentWriter
from kg.ingest.service import PARSER_VERSION
from kg.models.manifest import CorpusManifest

NOW = "2026-09-20T12:00:00+00:00"
TABLES = (
    "source_document", "source_revision", "revision_activation", "source_anchor",
    "passage", "passage_fts", "action_item", "record_binding",
)


@pytest.fixture
def database(tmp_path: Path) -> Database:
    database = Database(tmp_path / "index.sqlite3")
    database.initialize()
    return database


@pytest.fixture
def prepared() -> PreparedDocument:
    quote = "- [ ] Review Atlas. [key:: review]"
    return PreparedDocument(
        source_path="note.md",
        content=quote.encode(),
        observed_mtime=NOW,
        title="Review",
        metadata={"date": "2026-09-19"},
        event_time="2026-09-19",
        anchors=(PreparedAnchor(
            structural_path="block/1",
            heading_path=(),
            kind="task",
            start_offset=0,
            end_offset=len(quote),
            quote=quote,
            mention_text=quote,
            metadata={"key": "review"},
            task=PreparedTask("Review Atlas.", "open", None, None),
            record_type=None,
            record_text="",
            linked_aliases=(),
            relationship_type="wikilink",
        ),),
    )


def _write(
    connection: sqlite3.Connection, document: PreparedDocument, config_hash: str = "config",
) -> None:
    DocumentWriter(PARSER_VERSION).write(
        connection, "test", document, {document.source_path}, config_hash, clock=lambda: NOW,
    )


def _snapshot(connection: sqlite3.Connection) -> dict[str, list[tuple[object, ...]]]:
    return {
        table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")]
        for table in TABLES
    }


def test_writer_uses_prepared_values_without_source_io(
    database: Database, prepared: PreparedDocument, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: object, **kwargs: object) -> None:
        pytest.fail("Persistence must not acquire or parse a source")

    monkeypatch.setattr("kg.ingest._intake.read_source", forbidden)
    monkeypatch.setattr("kg.ingest._intake.parse_markdown", forbidden)
    assert not (database.path.parent / prepared.source_path).exists()
    with database.transaction() as connection:
        _write(connection, prepared)
        assert connection.in_transaction
        assert connection.execute("SELECT count(*) FROM lexical_projection").fetchone()[0] == 0

    expected_document = document_id("test", prepared.source_path)
    expected_revision = revision_id(expected_document, prepared.content)
    anchor = prepared.anchors[0]
    expected_anchor = anchor_id(
        expected_revision, anchor.structural_path, anchor.start_offset, anchor.end_offset,
    )
    with database.connection() as connection:
        revision = connection.execute("SELECT * FROM source_revision").fetchone()
        assert revision["document_id"] == expected_document
        assert revision["revision_id"] == expected_revision
        assert json.loads(revision["frontmatter_json"]) == prepared.metadata
        stored = connection.execute("SELECT * FROM source_anchor").fetchone()
        assert stored["anchor_id"] == expected_anchor
        assert stored["quote"] == prepared.content.decode()
        assert stored["quote_hash"] == digest(anchor.quote)
        assert (stored["start_offset"], stored["end_offset"]) == (0, len(anchor.quote))
        passage = connection.execute("SELECT * FROM passage").fetchone()
        assert passage["passage_id"] == record_id(expected_anchor, "passage", anchor.quote)
        assert passage["title"] == prepared.title
        assert passage["event_time"] == prepared.event_time
        action = connection.execute("SELECT * FROM action_item").fetchone()
        assert action["record_id"] == record_id(expected_anchor, "action", "Review Atlas.")
        assert (action["text"], action["status"], action["owner"]) == (
            "Review Atlas.", "open", None,
        )
        binding = connection.execute("SELECT * FROM record_binding").fetchone()
        assert (binding["record_key"], binding["supersedes_key"]) == ("review", None)


def test_writer_requires_caller_transaction(
    database: Database, prepared: PreparedDocument,
) -> None:
    with database.connection() as connection:
        with pytest.raises(ValueError, match="active ingestion transaction"):
            _write(connection, prepared)
        assert all(not rows for rows in _snapshot(connection).values())


def test_writer_failure_rolls_back_only_callers_source_savepoint(
    database: Database, prepared: PreparedDocument,
) -> None:
    invalid = replace(prepared.anchors[0], structural_path="block/2", metadata={"key": "bad key"})
    rejected = replace(
        prepared, source_path="rejected.md", content=prepared.content + b"\n",
        anchors=(*prepared.anchors, invalid),
    )
    with database.transaction() as connection:
        _write(connection, prepared)
        before = _snapshot(connection)
        connection.execute("SAVEPOINT ingest_source")
        with pytest.raises(ValueError, match="Invalid key"):
            _write(connection, rejected)
        assert connection.in_transaction
        assert connection.execute("SELECT count(*) FROM source_document").fetchone()[0] == 2
        connection.execute("ROLLBACK TO SAVEPOINT ingest_source")
        connection.execute("RELEASE SAVEPOINT ingest_source")
        assert _snapshot(connection) == before
    with database.connection() as connection:
        assert _snapshot(connection) == before


def test_writer_success_is_still_owned_by_outer_transaction(
    database: Database, prepared: PreparedDocument,
) -> None:
    with pytest.raises(RuntimeError, match="abort run"), database.transaction() as connection:
        _write(connection, prepared)
        raise RuntimeError("abort run")
    with database.connection() as connection:
        assert all(not rows for rows in _snapshot(connection).values())


def test_current_revision_skips_validation_but_rebuild_does_not(
    database: Database, prepared: PreparedDocument,
) -> None:
    invalid = replace(prepared.anchors[0], metadata={"key": "bad key"})
    reinterpreted = replace(prepared, anchors=(invalid,))
    with database.transaction() as connection:
        _write(connection, prepared)
        before = _snapshot(connection)
        report = DocumentWriter(PARSER_VERSION).write(
            connection, "test", reinterpreted, {"note.md"}, "config", clock=lambda: NOW,
        )
        assert report.outcome == "unchanged"
        assert report.reasons == ["already_indexed"]
        assert not report.records_rebuilt
        assert _snapshot(connection) == before
        connection.execute("SAVEPOINT rebuild")
        with pytest.raises(ValueError, match="Invalid key"):
            _write(connection, reinterpreted, config_hash="changed-config")
        connection.execute("ROLLBACK TO SAVEPOINT rebuild")
        connection.execute("RELEASE SAVEPOINT rebuild")
        assert _snapshot(connection) == before


def test_intake_defers_state_validation_and_facade_retains_source_isolation(tmp_path: Path) -> None:
    source = tmp_path / "bad.md"
    source.write_text("- [ ] Review. [key:: bad key]\n")
    (tmp_path / "good.md").write_text("- [ ] Keep this valid task.\n")
    manifest = CorpusManifest(
        corpus_id="test", display_name="Test", vault_root=tmp_path,
        database=tmp_path / "index.sqlite3", include=["*.md"],
    )
    prepared = prepare_document(manifest, source)
    assert prepared.anchors[0].metadata["key"] == "bad key"
    assert not manifest.database.exists()
    result = IngestService(Database(manifest.database)).ingest(manifest)
    assert (result.added, result.failed) == (1, 1)
    assert "Invalid key" in result.errors[0]
    with Database(manifest.database).connection() as connection:
        assert [row[0] for row in connection.execute(
            "SELECT source_path FROM source_document"
        )] == ["good.md"]
        assert connection.execute("SELECT count(*) FROM action_item").fetchone()[0] == 1
        assert connection.execute("SELECT count(*) FROM lexical_projection").fetchone()[0] == 1
