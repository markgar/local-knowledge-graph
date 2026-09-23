"""Persist prepared evidence on the ingestion facade's transaction and savepoint."""

from __future__ import annotations

import json
import logging
import sqlite3
from collections.abc import Callable

from kg.aliases import alias_pattern
from kg.ids import anchor_id, digest, document_id, record_id, revision_id
from kg.ingest._prepared import PreparedDocument
from kg.models.contracts import IngestDocumentReport
from kg.record_state import bind_record, validate_state_fields

LOGGER = logging.getLogger(__name__)


class DocumentWriter:
    def __init__(self, parser_version: str) -> None:
        self.parser_version = parser_version

    def write(
        self,
        connection: sqlite3.Connection,
        corpus_id: str,
        document: PreparedDocument,
        selected_source_paths: set[str],
        index_config_hash: str,
        *,
        clock: Callable[[], str],
    ) -> IngestDocumentReport:
        if not connection.in_transaction:
            raise ValueError("Prepared document writes require an active ingestion transaction")
        source_path = document.source_path
        content = document.content
        content_hash = digest(content)
        stable_document_id, moved = self._resolve_document_id(
            connection,
            corpus_id,
            source_path,
            content_hash,
            selected_source_paths,
        )
        stable_revision_id = revision_id(stable_document_id, content)
        now = clock()

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
            and existing_revision["indexed_parser_version"] == self.parser_version
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
            if existing_revision["indexed_parser_version"] != self.parser_version:
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
                        document.title,
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
                    document.title,
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
                    corpus_id,
                    source_path,
                    document.title,
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
                    document.observed_mtime,
                    now,
                    json.dumps(document.metadata, sort_keys=True, default=str),
                    self.parser_version,
                    index_config_hash,
                ),
            )
        if previous_revision_id != stable_revision_id:
            self._record_activation(
                connection, stable_document_id, stable_revision_id, previous_revision_id, now
            )

        aliases = self._aliases_for_corpus(connection, corpus_id)
        event_time = document.event_time
        for anchor in document.anchors:
            validate_state_fields(
                anchor.metadata, is_action=anchor.task is not None, record_type=anchor.record_type,
            )
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
                anchor.mention_text,
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
                    document.title,
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
                    document.title,
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
                bind_record(connection, anchor.metadata, stable_anchor_id, action_id, "action")
            if anchor.record_type in {"decision", "blocker", "conflict"}:
                explicit_id = record_id(stable_anchor_id, anchor.record_type, anchor.record_text)
                connection.execute(
                    f"""
                    INSERT INTO {anchor.record_type} (
                        record_id, anchor_id, text, event_time
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (
                        explicit_id,
                        stable_anchor_id,
                        anchor.record_text,
                        event_time,
                    ),
                )
                if anchor.record_type == "decision":
                    bind_record(
                        connection, anchor.metadata, stable_anchor_id, explicit_id, "decision",
                    )
            linked_entities = {
                alias["entity_key"]
                for link in anchor.linked_aliases
                for alias in aliases
                if alias["normalized_alias"] == link.casefold()
            }
            for source_entity_key in sorted(mentioned_entities - linked_entities):
                for target_entity_key in sorted(linked_entities):
                    relationship_key = record_id(
                        stable_anchor_id,
                        anchor.relationship_type,
                        f"{source_entity_key}:{target_entity_key}",
                    )
                    connection.execute(
                        """
                        INSERT OR IGNORE INTO relationship (
                            relationship_id, source_entity_key, target_entity_key,
                            relationship_type, anchor_id
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            relationship_key,
                            source_entity_key,
                            target_entity_key,
                            anchor.relationship_type,
                            stable_anchor_id,
                        ),
                    )

        connection.execute(
            """
            UPDATE source_revision
            SET indexed_parser_version = ?, indexed_config_hash = ?
            WHERE revision_id = ?
            """,
            (self.parser_version, index_config_hash, stable_revision_id),
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
