from __future__ import annotations

import json
import logging
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path

import yaml

from kg.config import relative_source_path, select_sources
from kg.db import Database
from kg.ids import digest
from kg.ingest._intake import prepare_document
from kg.ingest._writer import DocumentWriter
from kg.ingest.explain import explain_document
from kg.lexical import refresh_lexical_index
from kg.markdown import MarkdownParseError
from kg.models.contracts import IngestDocumentReport, IngestReport, IngestResult, IngestWithWarnings
from kg.models.manifest import CorpusManifest
from kg.record_state import resolve_record_state

PARSER_VERSION = "6"
SCHEMA_VERSION = 4
LOGGER = logging.getLogger(__name__)


class IngestService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def ingest(
        self,
        manifest: CorpusManifest,
        *,
        explain: bool = False,
        include_quotes: bool = False,
        detail_limit: int = 50,
    ) -> IngestResult:
        if include_quotes and not explain:
            raise ValueError("include_quotes requires explain=True")
        if not 1 <= detail_limit <= 200:
            raise ValueError("detail_limit must be between 1 and 200")
        LOGGER.info(
            "Starting ingestion for corpus %s into %s",
            manifest.corpus_id,
            self.database.path,
        )
        self.database.initialize()
        self._migrate_schema()
        selection = select_sources(manifest)
        result: IngestResult = IngestResult(
            corpus_id=manifest.corpus_id,
            run_id=str(uuid.uuid4()),
            missing=len(selection.missing),
            errors=[f"No Markdown files matched: {pattern}" for pattern in selection.missing],
        )
        if explain:
            result = IngestReport(
                **result.model_dump(),
                configured_entities=sorted(
                    manifest.seed_entities, key=lambda entity: entity.entity_id
                ),
                unmatched_patterns=selection.missing,
                include_quotes=include_quotes,
                detail_limit=detail_limit,
            )
        index_config_hash = self._index_config_hash(manifest)
        started_at = _now()
        selected_source_paths = {
            relative_source_path(manifest, path)
            for path in selection.paths
        }

        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO ingest_run (
                    run_id, corpus_id, parser_version, schema_version, started_at,
                    status, counts_json
                ) VALUES (?, ?, ?, ?, ?, 'running', '{}')
                """,
                (
                    result.run_id,
                    manifest.corpus_id,
                    PARSER_VERSION,
                    SCHEMA_VERSION,
                    started_at,
                ),
            )
            self._sync_seed_entities(connection, manifest)

            for path in selection.paths:
                connection.execute("SAVEPOINT ingest_source")
                try:
                    document = self._ingest_file(
                        connection,
                        manifest,
                        path,
                        selected_source_paths,
                        index_config_hash,
                    )
                except (OSError, UnicodeError, ValueError, sqlite3.Error) as exc:
                    connection.execute("ROLLBACK TO SAVEPOINT ingest_source")
                    connection.execute("RELEASE SAVEPOINT ingest_source")
                    result.failed += 1
                    message = _source_error(exc)
                    result.errors.append(f"{path}: {message}")
                    LOGGER.warning("Failed to ingest %s: %s", path, message)
                    source_path = relative_source_path(manifest, path)
                    self._deactivate_failed_source(
                        connection,
                        manifest.corpus_id,
                        source_path,
                    )
                    if isinstance(result, IngestReport):
                        retained = connection.execute(
                            """
                            SELECT document_id, current_revision_id FROM source_document
                            WHERE corpus_id = ? AND source_path = ?
                            ORDER BY updated_at DESC, document_id LIMIT 1
                            """,
                            (manifest.corpus_id, source_path),
                        ).fetchone()
                        result.documents.append(IngestDocumentReport(
                            source_path=source_path, outcome="failed",
                            reasons=["source_failed"], error=message,
                            document_id=retained["document_id"] if retained else None,
                            source_revision_id=(
                                retained["current_revision_id"] if retained else None
                            ),
                        ))
                else:
                    connection.execute("RELEASE SAVEPOINT ingest_source")
                    if document.outcome == "added":
                        result.added += 1
                    elif document.outcome == "changed":
                        result.changed += 1
                    else:
                        result.unchanged += 1
                    if isinstance(result, IngestReport):
                        explain_document(
                            connection, document, include_quotes=include_quotes,
                            detail_limit=detail_limit,
                        )
                        result.documents.append(document)

            deactivated = self._deactivate_unselected(
                connection,
                manifest.corpus_id,
                selected_source_paths,
                collect=isinstance(result, IngestReport),
            )
            if isinstance(result, IngestReport):
                result.documents.extend(
                    IngestDocumentReport(
                        source_path=row["source_path"], outcome="deactivated",
                        reasons=["source_no_longer_selected"],
                        document_id=row["document_id"],
                        source_revision_id=row["current_revision_id"],
                        previous_revision_id=row["current_revision_id"],
                        revision_state="unchanged",
                    )
                    for row in deactivated
                )
                result.documents.sort(key=lambda document: (document.source_path, document.outcome))
            refresh_lexical_index(connection, manifest.corpus_id)
            state = resolve_record_state(
                connection, manifest.corpus_id, include_quotes=include_quotes
            )
            if isinstance(result, IngestReport):
                result.record_state = state.report(detail_limit)
            elif state.warnings:
                result = IngestWithWarnings(**result.model_dump(), state_warnings=state.warnings)
            for warning in state.warnings:
                LOGGER.warning("Record state: %s", warning)
            status = "completed" if result.failed == 0 else "completed_with_errors"
            connection.execute(
                """
                UPDATE ingest_run
                SET completed_at = ?, status = ?, counts_json = ?
                WHERE run_id = ?
                """,
                (
                    _now(),
                    status,
                    result.model_dump_json(
                        include={"added", "changed", "unchanged", "missing", "failed"}
                    ),
                    result.run_id,
                ),
            )
        LOGGER.info(
            "Completed ingestion for %s: added=%d changed=%d unchanged=%d failed=%d",
            manifest.corpus_id,
            result.added,
            result.changed,
            result.unchanged,
            result.failed,
        )
        return result

    def _ingest_file(
        self,
        connection: sqlite3.Connection,
        manifest: CorpusManifest,
        path: Path,
        selected_source_paths: set[str],
        index_config_hash: str,
    ) -> IngestDocumentReport:
        document = prepare_document(manifest, path)
        return DocumentWriter(PARSER_VERSION).write(
            connection, manifest.corpus_id, document, selected_source_paths, index_config_hash,
            clock=_now,
        )

    def _sync_seed_entities(
        self,
        connection: sqlite3.Connection,
        manifest: CorpusManifest,
    ) -> None:
        connection.execute(
            "UPDATE entity SET is_active = 0 WHERE corpus_id = ?",
            (manifest.corpus_id,),
        )
        connection.execute(
            """
            DELETE FROM entity_alias
            WHERE entity_key IN (
                SELECT entity_key FROM entity WHERE corpus_id = ?
            )
            """,
            (manifest.corpus_id,),
        )
        for entity in manifest.seed_entities:
            entity_key = digest(manifest.corpus_id, "entity", entity.entity_id)
            connection.execute(
                """
                INSERT INTO entity (
                    entity_key, corpus_id, entity_id, entity_type, canonical_name,
                    is_active
                ) VALUES (?, ?, ?, ?, ?, 1)
                ON CONFLICT(entity_key) DO UPDATE SET
                    entity_type = excluded.entity_type,
                    canonical_name = excluded.canonical_name,
                    is_active = 1
                """,
                (
                    entity_key,
                    manifest.corpus_id,
                    entity.entity_id,
                    entity.entity_type,
                    entity.name,
                ),
            )
            aliases = {entity.name, *entity.aliases}
            for alias in aliases:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO entity_alias (entity_key, alias, normalized_alias)
                    VALUES (?, ?, ?)
                    """,
                    (entity_key, alias, alias.casefold()),
                )

    def _migrate_schema(self) -> None:
        with self.database.connection() as connection:
            definitions = {
                row["name"]: row["sql"] or ""
                for row in connection.execute(
                    """
                    SELECT name, sql
                    FROM sqlite_master
                    WHERE type = 'table' AND name IN ('source_document', 'entity')
                    """
                )
            }
            source_is_legacy = "UNIQUE (corpus_id, source_path)" in definitions.get(
                "source_document", ""
            )
            entity_definition = definitions.get("entity", "")
            entity_is_legacy = (
                "is_active" not in entity_definition
                or "UNIQUE (corpus_id, entity_type, canonical_name)"
                in entity_definition
            )
            connection.execute("PRAGMA foreign_keys = OFF")
            try:
                connection.execute("BEGIN IMMEDIATE")
                if source_is_legacy:
                    connection.execute(
                        """
                        CREATE TABLE source_document_new (
                            document_id TEXT PRIMARY KEY,
                            corpus_id TEXT NOT NULL,
                            source_path TEXT NOT NULL,
                            title TEXT NOT NULL,
                            current_revision_id TEXT,
                            is_active INTEGER NOT NULL DEFAULT 1
                                CHECK (is_active IN (0, 1)),
                            created_at TEXT NOT NULL,
                            updated_at TEXT NOT NULL
                        )
                        """
                    )
                    connection.execute(
                        """
                        INSERT INTO source_document_new
                        SELECT document_id, corpus_id, source_path, title,
                               current_revision_id, is_active, created_at, updated_at
                        FROM source_document
                        """
                    )
                    connection.execute("DROP TABLE source_document")
                    connection.execute(
                        "ALTER TABLE source_document_new RENAME TO source_document"
                    )
                if entity_is_legacy:
                    connection.execute(
                        """
                        CREATE TABLE entity_new (
                            entity_key TEXT PRIMARY KEY,
                            corpus_id TEXT NOT NULL,
                            entity_id TEXT NOT NULL,
                            entity_type TEXT NOT NULL,
                            canonical_name TEXT NOT NULL,
                            is_active INTEGER NOT NULL DEFAULT 1
                                CHECK (is_active IN (0, 1)),
                            UNIQUE (corpus_id, entity_id)
                        )
                        """
                    )
                    active_expression = "is_active" if "is_active" in entity_definition else "1"
                    connection.execute(
                        f"""
                        INSERT INTO entity_new
                        SELECT entity_key, corpus_id, entity_id, entity_type,
                               canonical_name, {active_expression}
                        FROM entity
                        """
                    )
                    connection.execute("DROP TABLE entity")
                    connection.execute("ALTER TABLE entity_new RENAME TO entity")
                connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS source_document_active_path_idx
                    ON source_document(corpus_id, source_path)
                    WHERE is_active = 1
                    """
                )
                connection.execute(
                    """
                    CREATE UNIQUE INDEX IF NOT EXISTS entity_active_name_idx
                    ON entity(corpus_id, entity_type, canonical_name)
                    WHERE is_active = 1
                    """
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.execute("PRAGMA foreign_keys = ON")
            violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                raise sqlite3.IntegrityError(
                    f"schema migration left foreign key violations: {violations}"
                )

    def _deactivate_unselected(
        self,
        connection: sqlite3.Connection,
        corpus_id: str,
        selected_source_paths: set[str],
        *,
        collect: bool = False,
    ) -> list[sqlite3.Row]:
        condition = "corpus_id = ? AND is_active = 1"
        parameters = (corpus_id, *sorted(selected_source_paths))
        if selected_source_paths:
            placeholders = ", ".join("?" for _ in selected_source_paths)
            condition += f" AND source_path NOT IN ({placeholders})"
        deactivated = (
            connection.execute(
                f"""
                SELECT document_id, source_path, current_revision_id
                FROM source_document WHERE {condition}
                """,
                parameters,
            ).fetchall()
            if collect else []
        )
        connection.execute(
            f"""
            UPDATE source_document SET is_active = 0, updated_at = ?
            WHERE {condition}
            """,
            (_now(), *parameters),
        )
        return deactivated

    def _deactivate_failed_source(
        self,
        connection: sqlite3.Connection,
        corpus_id: str,
        source_path: str,
    ) -> None:
        connection.execute(
            """
            UPDATE source_document
            SET is_active = 0, updated_at = ?
            WHERE corpus_id = ? AND source_path = ?
            """,
            (_now(), corpus_id, source_path),
        )

    @staticmethod
    def _index_config_hash(manifest: CorpusManifest) -> str:
        index_config = {
            "seed_entities": [
                entity.model_dump(mode="json")
                for entity in manifest.seed_entities
            ],
            "metadata_fields": manifest.metadata_fields,
        }
        return digest(json.dumps(index_config, sort_keys=True))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _source_error(exc: Exception) -> str:
    if isinstance(exc, MarkdownParseError) and isinstance(exc.__cause__, yaml.MarkedYAMLError):
        mark = exc.__cause__.problem_mark
        location = f" at frontmatter line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        return f"Invalid YAML frontmatter{location}; check source syntax."
    return str(exc)
