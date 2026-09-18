from __future__ import annotations

import json
import re
import sqlite3
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from kg.config import relative_source_path, select_sources
from kg.db import Database
from kg.ids import anchor_id, digest, document_id, record_id, revision_id
from kg.markdown import parse_markdown
from kg.models.contracts import IngestResult
from kg.models.manifest import CorpusManifest

PARSER_VERSION = "1"
SCHEMA_VERSION = 1


class IngestService:
    def __init__(self, database: Database) -> None:
        self.database = database

    def ingest(self, manifest: CorpusManifest) -> IngestResult:
        self.database.migrate()
        selection = select_sources(manifest)
        result = IngestResult(
            corpus_id=manifest.corpus_id,
            run_id=str(uuid.uuid4()),
            missing=len(selection.missing),
            errors=[f"No Markdown files matched: {pattern}" for pattern in selection.missing],
        )
        started_at = _now()
        selected_document_ids = {
            document_id(
                manifest.corpus_id,
                relative_source_path(manifest, path),
            )
            for path in selection.paths
        }

        with self.database.transaction() as connection:
            connection.execute(
                """
                INSERT INTO ingest_run (
                    run_id, corpus_id, parser_version, schema_version, started_at, status, counts_json
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
                    outcome = self._ingest_file(connection, manifest, path)
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

            self._deactivate_unselected(
                connection,
                manifest.corpus_id,
                selected_document_ids,
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
        return result

    def _ingest_file(
        self,
        connection: sqlite3.Connection,
        manifest: CorpusManifest,
        path: Path,
    ) -> Literal["added", "changed", "unchanged"]:
        source_path = relative_source_path(manifest, path)
        stable_document_id = document_id(manifest.corpus_id, source_path)
        content = path.read_bytes()
        text = content.decode("utf-8")
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
            "SELECT 1 FROM source_revision WHERE revision_id = ?",
            (stable_revision_id,),
        ).fetchone()

        if existing_revision:
            if (
                existing_document
                and (
                    existing_document["current_revision_id"] != stable_revision_id
                    or existing_document["is_active"] != 1
                )
            ):
                connection.execute(
                    """
                    UPDATE source_document
                    SET current_revision_id = ?, is_active = 1, updated_at = ?
                    WHERE document_id = ?
                    """,
                    (stable_revision_id, now, stable_document_id),
                )
            return "unchanged"

        if existing_document:
            connection.execute(
                """
                UPDATE source_document
                SET title = ?, current_revision_id = ?, is_active = 1, updated_at = ?
                WHERE document_id = ?
                """,
                (parsed.title, stable_revision_id, now, stable_document_id),
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
        connection.execute(
            """
            INSERT INTO source_revision (
                revision_id, document_id, content_hash, observed_mtime, ingested_at,
                frontmatter_json
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                stable_revision_id,
                stable_document_id,
                digest(content),
                datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
                now,
                json.dumps(parsed.frontmatter, sort_keys=True, default=str),
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
                INSERT INTO source_anchor (
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
            linked_entities = {
                alias["entity_key"]
                for link in anchor.wikilinks
                for alias in aliases
                if alias["normalized_alias"] == link.casefold()
            }
            for source_entity_key in mentioned_entities - linked_entities:
                for target_entity_key in linked_entities:
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

        return "changed" if existing_document else "added"

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
        selected_document_ids: set[str],
    ) -> None:
        if selected_document_ids:
            placeholders = ", ".join("?" for _ in selected_document_ids)
            connection.execute(
                f"""
                UPDATE source_document
                SET is_active = 0, updated_at = ?
                WHERE corpus_id = ?
                  AND is_active = 1
                  AND document_id NOT IN ({placeholders})
                """,
                (_now(), corpus_id, *sorted(selected_document_ids)),
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
    return datetime.now(timezone.utc).isoformat()
