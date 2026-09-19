from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import yaml

from kg.aliases import alias_pattern
from kg.config import read_source, relative_source_path, select_sources
from kg.db import Database
from kg.ids import anchor_id, digest, document_id, record_id, revision_id
from kg.ingest.explain import explain_document
from kg.lexical import refresh_lexical_index
from kg.markdown import MarkdownParseError, parse_markdown
from kg.models.contracts import IngestDocumentReport, IngestReport, IngestResult, IngestWithWarnings
from kg.models.manifest import CorpusManifest
from kg.record_state import (
    bind_record,
    resolve_record_state,
    validate_state_fields,
    without_state_fields,
)

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
        source_path = relative_source_path(manifest, path)
        source = read_source(manifest, path)
        content = source.content
        text = content.decode("utf-8")
        content_hash = digest(content)
        stable_document_id, moved = self._resolve_document_id(
            connection,
            manifest.corpus_id,
            source_path,
            content_hash,
            selected_source_paths,
        )
        stable_revision_id = revision_id(stable_document_id, content)
        parsed = parse_markdown(text, path.stem)
        now = _now()

        existing_document = connection.execute(
            """
            SELECT current_revision_id, is_active, source_path
            FROM source_document
            WHERE document_id = ?
            """,
            (stable_document_id,),
        ).fetchone()
        existing_revision = connection.execute(
            """
            SELECT indexed_parser_version, indexed_config_hash
            FROM source_revision
            WHERE revision_id = ?
            """,
            (stable_revision_id,),
        ).fetchone()
        previous_revision_id = (
            existing_document["current_revision_id"] if existing_document else None
        )
        if previous_revision_id and not connection.execute(
            "SELECT 1 FROM revision_activation WHERE document_id = ? LIMIT 1",
            (stable_document_id,),
        ).fetchone():
            self._record_activation(
                connection, stable_document_id, previous_revision_id, None, now
            )
            LOGGER.warning(
                "Started activation history for existing document %s; earlier transitions "
                "are unknown. Specify both revisions when comparing older states.",
                source_path,
            )

        index_is_current = (
            existing_revision
            and existing_revision["indexed_parser_version"] == PARSER_VERSION
            and existing_revision["indexed_config_hash"] == index_config_hash
        )
        report = IngestDocumentReport(
            source_path=source_path,
            outcome="changed" if existing_document else "added",
            reasons=[],
            document_id=stable_document_id,
            source_revision_id=stable_revision_id,
            previous_revision_id=previous_revision_id,
            previous_source_path=existing_document["source_path"] if moved else None,
            revision_state=(
                "new" if not existing_revision
                else "unchanged" if previous_revision_id == stable_revision_id
                else "reused"
            ),
            active=True,
        )
        if not existing_document:
            report.reasons.append("new_document")
        if not existing_revision:
            report.reasons.append("new_revision")
        elif previous_revision_id != stable_revision_id:
            report.reasons.append("restored_revision")
        if moved:
            report.reasons.append("moved_source")
        if existing_document and not existing_document["is_active"]:
            report.reasons.append("reactivated_source")
        if existing_revision:
            if existing_revision["indexed_parser_version"] != PARSER_VERSION:
                report.reasons.append("parser_changed")
            if existing_revision["indexed_config_hash"] != index_config_hash:
                report.reasons.append("index_configuration_changed")
        if index_is_current:
            state_changed = (
                existing_document
                and (
                    existing_document["current_revision_id"] != stable_revision_id
                    or existing_document["is_active"] != 1
                    or moved
                )
            )
            if state_changed:
                connection.execute(
                    """
                    UPDATE source_document
                    SET source_path = ?, title = ?, current_revision_id = ?,
                        is_active = 1, updated_at = ?
                    WHERE document_id = ?
                    """,
                    (
                        source_path,
                        parsed.title,
                        stable_revision_id,
                        now,
                        stable_document_id,
                    ),
                )
                if previous_revision_id != stable_revision_id:
                    self._record_activation(
                        connection, stable_document_id, stable_revision_id,
                        previous_revision_id, now,
                    )
            report.outcome = "changed" if state_changed else "unchanged"
            if not report.reasons:
                report.reasons.append("already_indexed")
            return report

        if existing_document:
            connection.execute(
                """
                UPDATE source_document
                SET source_path = ?, title = ?, current_revision_id = ?,
                    is_active = 1, updated_at = ?
                WHERE document_id = ?
                """,
                (
                    source_path,
                    parsed.title,
                    stable_revision_id,
                    now,
                    stable_document_id,
                ),
            )
        else:
            connection.execute(
                """
                INSERT INTO source_document (
                    document_id, corpus_id, source_path, title, current_revision_id,
                    is_active, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    stable_document_id,
                    manifest.corpus_id,
                    source_path,
                    parsed.title,
                    stable_revision_id,
                    now,
                    now,
                ),
            )
        if existing_revision:
            self._clear_derived_records(connection, stable_revision_id)
        else:
            connection.execute(
                """
                INSERT INTO source_revision (
                    revision_id, document_id, content_hash, observed_mtime, ingested_at,
                    frontmatter_json, indexed_parser_version, indexed_config_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stable_revision_id,
                    stable_document_id,
                    content_hash,
                    source.observed_mtime,
                    now,
                    json.dumps(parsed.frontmatter, sort_keys=True, default=str),
                    PARSER_VERSION,
                    index_config_hash,
                ),
            )
        if previous_revision_id != stable_revision_id:
            self._record_activation(
                connection, stable_document_id, stable_revision_id, previous_revision_id, now
            )

        aliases = self._aliases_for_corpus(connection, manifest.corpus_id)
        event_time = self._event_time(manifest, parsed.frontmatter)
        for anchor in parsed.anchors:
            validate_state_fields(anchor)
            stable_anchor_id = anchor_id(
                stable_revision_id,
                anchor.structural_path,
                anchor.start_offset,
                anchor.end_offset,
            )
            connection.execute(
                """
                INSERT OR IGNORE INTO source_anchor (
                    anchor_id, revision_id, structural_path, heading_path_json, anchor_kind,
                    start_offset, end_offset, quote, quote_hash
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stable_anchor_id,
                    stable_revision_id,
                    anchor.structural_path,
                    json.dumps(anchor.heading_path),
                    anchor.kind,
                    anchor.start_offset,
                    anchor.end_offset,
                    anchor.quote,
                    digest(anchor.quote),
                ),
            )
            mentioned_entities = self._persist_mentions(
                connection,
                stable_anchor_id,
                anchor.semantic_quote,
                anchor.quote,
                anchor.start_offset,
                aliases,
            )
            indexed_aliases = {
                alias["alias"]
                for alias in aliases
                if alias["entity_key"] in mentioned_entities
            }
            passage_id = record_id(stable_anchor_id, "passage", anchor.quote)
            heading_text = " / ".join(anchor.heading_path)
            connection.execute(
                """
                INSERT INTO passage (
                    passage_id, anchor_id, document_id, title, heading_text, passage_text,
                    event_time
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    passage_id,
                    stable_anchor_id,
                    stable_document_id,
                    parsed.title,
                    heading_text,
                    anchor.quote,
                    event_time,
                ),
            )
            connection.execute(
                """
                INSERT INTO passage_fts (
                    passage_id, title, heading_text, passage_text, aliases
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    passage_id,
                    parsed.title,
                    heading_text,
                    anchor.quote,
                    " ".join(sorted(indexed_aliases)),
                ),
            )
            if anchor.task:
                action_id = record_id(stable_anchor_id, "action", anchor.task.text)
                connection.execute(
                    """
                    INSERT INTO action_item (
                        record_id, anchor_id, text, status, owner, due_date
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        action_id,
                        stable_anchor_id,
                        anchor.task.text,
                        anchor.task.status,
                        anchor.task.owner,
                        anchor.task.due_date,
                    ),
                )
                bind_record(connection, anchor, stable_anchor_id, action_id, "action")
            if anchor.record_type in {"decision", "blocker", "conflict"}:
                record_text = _record_text(without_state_fields(anchor))
                explicit_id = record_id(stable_anchor_id, anchor.record_type, record_text)
                connection.execute(
                    f"""
                    INSERT INTO {anchor.record_type} (
                        record_id, anchor_id, text, event_time
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        explicit_id,
                        stable_anchor_id,
                        record_text,
                        event_time,
                    ),
                )
                if anchor.record_type == "decision":
                    bind_record(connection, anchor, stable_anchor_id, explicit_id, "decision")
            linked_entities = {
                alias["entity_key"]
                for link in anchor.wikilinks
                for alias in aliases
                if alias["normalized_alias"] == link.casefold()
            }
            for source_entity_key in sorted(mentioned_entities - linked_entities):
                for target_entity_key in sorted(linked_entities):
                    relationship_key = record_id(
                        stable_anchor_id,
                        "wikilink",
                        f"{source_entity_key}:{target_entity_key}",
                    )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO relationship (
                            relationship_id, source_entity_key, target_entity_key,
                            relationship_type, anchor_id
                        ) VALUES (?, ?, ?, 'wikilink', ?)
                        """,
                        (
                            relationship_key,
                            source_entity_key,
                            target_entity_key,
                            stable_anchor_id,
                        ),
                    )

        connection.execute(
            """
            UPDATE source_revision
            SET indexed_parser_version = ?, indexed_config_hash = ?
            WHERE revision_id = ?
            """,
            (PARSER_VERSION, index_config_hash, stable_revision_id),
        )
        report.records_rebuilt = True
        return report

    @staticmethod
    def _record_activation(
        connection: sqlite3.Connection,
        stable_document_id: str,
        selected_revision_id: str,
        previous_revision_id: str | None,
        activated_at: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO revision_activation (
                document_id, revision_id, previous_revision_id, activated_at
            ) VALUES (?, ?, ?, ?)
            """,
            (stable_document_id, selected_revision_id, previous_revision_id, activated_at),
        )

    def _clear_derived_records(
        self,
        connection: sqlite3.Connection,
        revision_id: str,
    ) -> None:
        anchor_query = "SELECT anchor_id FROM source_anchor WHERE revision_id = ?"
        passage_query = f"SELECT passage_id FROM passage WHERE anchor_id IN ({anchor_query})"
        connection.execute(
            f"DELETE FROM passage_fts WHERE passage_id IN ({passage_query})",
            (revision_id,),
        )
        for table in (
            "record_binding",
            "relationship",
            "mention",
            "action_item",
            "decision",
            "blocker",
            "conflict",
            "passage",
        ):
            connection.execute(
                f"DELETE FROM {table} WHERE anchor_id IN ({anchor_query})",
                (revision_id,),
            )

    def _resolve_document_id(
        self,
        connection: sqlite3.Connection,
        corpus_id: str,
        source_path: str,
        content_hash: str,
        selected_source_paths: set[str],
    ) -> tuple[str, bool]:
        existing = connection.execute(
            """
            SELECT document_id, is_active
            FROM source_document
            WHERE corpus_id = ? AND source_path = ?
            ORDER BY is_active DESC, updated_at DESC
            """,
            (corpus_id, source_path),
        ).fetchone()
        if existing and existing["is_active"]:
            return existing["document_id"], False

        parameters: list[object] = [corpus_id, content_hash, source_path]
        excluded_clause = ""
        if selected_source_paths:
            placeholders = ", ".join("?" for _ in selected_source_paths)
            excluded_clause = f"AND sd.source_path NOT IN ({placeholders})"
            parameters.extend(sorted(selected_source_paths))
        candidates = connection.execute(
            f"""
            SELECT sd.document_id, sd.is_active
            FROM source_document sd
            JOIN source_revision sr
              ON sr.revision_id = sd.current_revision_id
            WHERE sd.corpus_id = ?
              AND sr.content_hash = ?
              AND sd.source_path != ?
              {excluded_clause}
            """,
            parameters,
        ).fetchall()
        active_candidates = [candidate for candidate in candidates if candidate["is_active"]]
        if len(active_candidates) == 1:
            return active_candidates[0]["document_id"], True
        if len(candidates) == 1:
            return candidates[0]["document_id"], True
        if existing:
            return existing["document_id"], False
        candidate_id = document_id(corpus_id, source_path)
        generation = 0
        while connection.execute(
            "SELECT 1 FROM source_document WHERE document_id = ?", (candidate_id,)
        ).fetchone():
            generation += 1
            candidate_id = document_id(corpus_id, source_path, generation=generation)
        return candidate_id, False

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

    def _aliases_for_corpus(
        self,
        connection: sqlite3.Connection,
        corpus_id: str,
    ) -> list[sqlite3.Row]:
        return connection.execute(
            """
            SELECT e.entity_key, e.entity_id, ea.alias, ea.normalized_alias
            FROM entity_alias ea
            JOIN entity e ON e.entity_key = ea.entity_key
            WHERE e.corpus_id = ?
              AND e.is_active = 1
            ORDER BY length(ea.alias) DESC
            """,
            (corpus_id,),
        ).fetchall()

    def _persist_mentions(
        self,
        connection: sqlite3.Connection,
        stable_anchor_id: str,
        searchable_quote: str,
        source_quote: str,
        source_start_offset: int,
        aliases: list[sqlite3.Row],
    ) -> set[str]:
        mentioned: set[str] = set()
        occupied: set[tuple[int, int]] = set()
        for alias in aliases:
            pattern = alias_pattern(alias["alias"])
            for match in pattern.finditer(searchable_quote):
                span = (match.start(), match.end())
                if span in occupied:
                    continue
                occupied.add(span)
                mentioned.add(alias["entity_key"])
                absolute_start = source_start_offset + match.start()
                absolute_end = source_start_offset + match.end()
                connection.execute(
                    """
                    INSERT OR IGNORE INTO mention (
                        mention_id, entity_key, anchor_id, matched_text,
                        start_offset, end_offset
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record_id(
                            stable_anchor_id,
                            "mention",
                            f"{alias['entity_key']}:{absolute_start}:{absolute_end}",
                        ),
                        alias["entity_key"],
                        stable_anchor_id,
                        source_quote[match.start() : match.end()],
                        absolute_start,
                        absolute_end,
                    ),
                )
        return mentioned

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

    @staticmethod
    def _event_time(
        manifest: CorpusManifest,
        frontmatter: dict[str, object],
    ) -> str | None:
        field_name = manifest.metadata_fields.get("event_time")
        if not field_name:
            return None
        value = frontmatter.get(field_name)
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        return str(value) if value is not None else None


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _source_error(exc: Exception) -> str:
    if isinstance(exc, MarkdownParseError) and isinstance(exc.__cause__, yaml.MarkedYAMLError):
        mark = exc.__cause__.problem_mark
        location = f" at frontmatter line {mark.line + 1}, column {mark.column + 1}" if mark else ""
        return f"Invalid YAML frontmatter{location}; check source syntax."
    return str(exc)


def _record_text(quote: str) -> str:
    lines = re.split(r"\r\n|\r|\n", quote)
    if not lines:
        return ""
    marker = re.match(r"^([ \t]*(?:[-*+]|[0-9]{1,9}[.)])[ \t]+)", lines[0])
    if marker:
        lines[0] = lines[0][marker.end() :]
        continuation_indent = len(marker.group(1).expandtabs(4))
        for index in range(1, len(lines)):
            expanded = lines[index].expandtabs(4)
            if expanded[:continuation_indent].isspace():
                lines[index] = expanded[continuation_indent:]
    lines[0] = re.sub(r"^\[[ xX]\][ \t]+", "", lines[0])
    return "\n".join(lines).strip()
