from __future__ import annotations

import json
import logging
import re
import sqlite3
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal

from kg.config import relative_source_path, select_sources
from kg.db import Database
from kg.ids import anchor_id, digest, document_id, record_id, revision_id
from kg.markdown import parse_markdown
from kg.models.contracts import IngestResult
from kg.models.manifest import CorpusManifest

PARSER_VERSION = "2"
SCHEMA_VERSION = 1
LOGGER = logging.getLogger(__name__)


class IngestService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def ingest(self, manifest: CorpusManifest) -> IngestResult:
        LOGGER.info(
            "Starting ingestion for corpus %s into %s",
            manifest.corpus_id,
            self.database.path,
        )
        self.database.initialize()
        selection = select_sources(manifest)
        result = IngestResult(
            corpus_id=manifest.corpus_id,
            run_id=str(uuid.uuid4()),
            missing=len(selection.missing),
            errors=[f"No Markdown files matched: {pattern}" for pattern in selection.missing],
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
                    outcome = self._ingest_file(
                        connection,
                        manifest,
                        path,
                        selected_source_paths,
                        index_config_hash,
                    )
                    connection.execute("RELEASE SAVEPOINT ingest_source")
                    if outcome == "added":
                        result.added += 1
                    elif outcome == "changed":
                        result.changed += 1
                    else:
                        result.unchanged += 1
                except (OSError, UnicodeError, ValueError, sqlite3.Error) as exc:
                    connection.execute("ROLLBACK TO SAVEPOINT ingest_source")
                    connection.execute("RELEASE SAVEPOINT ingest_source")
                    result.failed += 1
                    result.errors.append(f"{path}: {exc}")
                    LOGGER.warning("Failed to ingest %s: %s", path, exc)
                    self._deactivate_failed_source(
                        connection,
                        manifest.corpus_id,
                        relative_source_path(manifest, path),
                    )

            self._deactivate_unselected(
                connection,
                manifest.corpus_id,
                selected_source_paths,
            )
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
                    result.model_dump_json(exclude={"run_id", "corpus_id", "errors"}),
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
    ) -> Literal["added", "changed", "unchanged"]:
        source_path = relative_source_path(manifest, path)
        source_size = path.stat().st_size
        if source_size > manifest.max_source_bytes:
            raise ValueError(
                f"source exceeds max_source_bytes "
                f"({source_size} > {manifest.max_source_bytes})"
            )
        content = path.read_bytes()
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
            SELECT current_revision_id, is_active
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

        index_is_current = (
            existing_revision
            and existing_revision["indexed_parser_version"] == PARSER_VERSION
            and existing_revision["indexed_config_hash"] == index_config_hash
        )
        if index_is_current:
            if (
                existing_document
                and (
                    existing_document["current_revision_id"] != stable_revision_id
                    or existing_document["is_active"] != 1
                    or moved
                )
            ):
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
            return "changed" if moved else "unchanged"

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
                    datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat(),
                    now,
                    json.dumps(parsed.frontmatter, sort_keys=True, default=str),
                    PARSER_VERSION,
                    index_config_hash,
                ),
            )

        aliases = self._aliases_for_corpus(connection, manifest.corpus_id)
        event_time = self._event_time(manifest, parsed.frontmatter)
        for anchor in parsed.anchors:
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
                connection.execute(
                    """
                    INSERT INTO action_item (
                        record_id, anchor_id, text, status, owner, due_date
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record_id(stable_anchor_id, "action", anchor.task.text),
                        stable_anchor_id,
                        anchor.task.text,
                        anchor.task.status,
                        anchor.task.owner,
                        anchor.task.due_date,
                    ),
                )
            if anchor.record_type in {"decision", "blocker", "conflict"}:
                record_text = _record_text(anchor.quote)
                connection.execute(
                    f"""
                    INSERT INTO {anchor.record_type} (
                        record_id, anchor_id, text, event_time
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        record_id(
                            stable_anchor_id,
                            anchor.record_type,
                            record_text,
                        ),
                        stable_anchor_id,
                        record_text,
                        event_time,
                    ),
                )
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
        return "changed" if existing_document else "added"

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
            SELECT document_id
            FROM source_document
            WHERE corpus_id = ? AND source_path = ?
            """,
            (corpus_id, source_path),
        ).fetchone()
        if existing:
            return existing["document_id"], False

        parameters: list[object] = [corpus_id, content_hash, source_path]
        excluded_clause = ""
        if selected_source_paths:
            placeholders = ", ".join("?" for _ in selected_source_paths)
            excluded_clause = f"AND sd.source_path NOT IN ({placeholders})"
            parameters.extend(sorted(selected_source_paths))
        candidates = connection.execute(
            f"""
            SELECT sd.document_id
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
        if len(candidates) == 1:
            return candidates[0]["document_id"], True
        return document_id(corpus_id, source_path), False

    def _sync_seed_entities(
        self,
        connection: sqlite3.Connection,
        manifest: CorpusManifest,
    ) -> None:
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
                    entity_key, corpus_id, entity_id, entity_type, canonical_name
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(entity_key) DO UPDATE SET
                    entity_type = excluded.entity_type,
                    canonical_name = excluded.canonical_name
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
            ORDER BY length(ea.alias) DESC
            """,
            (corpus_id,),
        ).fetchall()

    def _persist_mentions(
        self,
        connection: sqlite3.Connection,
        stable_anchor_id: str,
        quote: str,
        source_start_offset: int,
        aliases: list[sqlite3.Row],
    ) -> set[str]:
        mentioned: set[str] = set()
        occupied: set[tuple[int, int]] = set()
        for alias in aliases:
            pattern = re.compile(
                rf"(?<!\w){re.escape(alias['alias'])}(?!\w)",
                re.IGNORECASE,
            )
            for match in pattern.finditer(quote):
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
                        match.group(0),
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
    ) -> None:
        if selected_source_paths:
            placeholders = ", ".join("?" for _ in selected_source_paths)
            connection.execute(
                f"""
                UPDATE source_document
                SET is_active = 0, updated_at = ?
                WHERE corpus_id = ?
                  AND is_active = 1
                  AND source_path NOT IN ({placeholders})
                """,
                (_now(), corpus_id, *sorted(selected_source_paths)),
            )
        else:
            connection.execute(
                """
                UPDATE source_document
                SET is_active = 0, updated_at = ?
                WHERE corpus_id = ? AND is_active = 1
                """,
                (_now(), corpus_id),
            )

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


def _record_text(quote: str) -> str:
    first_line = quote.splitlines()[0]
    value = re.sub(r"^[ \t]*[-*+][ \t]+", "", first_line)
    return re.sub(r"^\[[ xX]\][ \t]+", "", value).strip()
