from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from kg.db import Database
from kg.models.contracts import ActionResult, EvidenceResult, SearchResult, StatusResult


class RecordNotFoundError(LookupError):
    pass


class SearchQueryError(ValueError):
    pass


@dataclass(frozen=True)
class SubjectScope:
    entity_keys: tuple[str, ...]
    entity_ids: tuple[str, ...]


class RetrievalService:
    def __init__(self, database: Database, corpus_id: str) -> None:
        self.database = database
        self.corpus_id = corpus_id
        self.database.migrate()

    def search(
        self,
        query: str,
        subject: str | None = None,
        limit: int = 20,
        since: datetime | None = None,
        source_path: str | None = None,
    ) -> list[SearchResult]:
        expression = _fts_expression(query)
        clauses = [
            "passage_fts MATCH ?",
            "sd.corpus_id = ?",
            "sd.is_active = 1",
            "sr.revision_id = sd.current_revision_id",
        ]
        parameters: list[object] = [expression, self.corpus_id]
        scope = self._subject_scope(subject) if subject else None
        if subject:
            subject_clause, subject_parameters = self._subject_clause(
                subject,
                scope,
                anchor_expression="sa.anchor_id",
            )
            clauses.append(subject_clause)
            parameters.extend(subject_parameters)
        if since:
            clauses.append(
                """
                COALESCE(
                    datetime(p.event_time),
                    datetime(sr.observed_mtime),
                    datetime(sr.ingested_at)
                ) >= datetime(?)
                """
            )
            parameters.append(since.isoformat())
        if source_path:
            clauses.append("sd.source_path = ?")
            parameters.append(source_path)
        parameters.append(limit)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    p.passage_id AS record_id,
                    p.title,
                    p.passage_text,
                    p.event_time,
                    sd.source_path,
                    sr.revision_id,
                    sa.anchor_id,
                    sa.heading_path_json,
                    bm25(passage_fts) AS rank
                FROM passage_fts
                JOIN passage p ON p.passage_id = passage_fts.passage_id
                JOIN source_anchor sa ON sa.anchor_id = p.anchor_id
                JOIN source_revision sr ON sr.revision_id = sa.revision_id
                JOIN source_document sd ON sd.document_id = sr.document_id
                WHERE {" AND ".join(clauses)}
                ORDER BY rank
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        related = self._related_entities(row["anchor_id"] for row in rows)
        return [
            SearchResult(
                record_id=row["record_id"],
                record_type="passage",
                title=row["title"],
                summary=None,
                status=None,
                event_time=row["event_time"],
                source_path=row["source_path"],
                source_revision_id=row["revision_id"],
                anchor_id=row["anchor_id"],
                heading_path=json.loads(row["heading_path_json"]),
                quote=row["passage_text"],
                related_entity_ids=related.get(row["anchor_id"], []),
                rank=row["rank"],
            )
            for row in rows
        ]

    def actions(
        self,
        subject: str | None = None,
        status: str | None = None,
        since: datetime | None = None,
        source_path: str | None = None,
    ) -> list[ActionResult]:
        clauses = [
            "sd.corpus_id = ?",
            "sd.is_active = 1",
            "sr.revision_id = sd.current_revision_id",
        ]
        parameters: list[object] = [self.corpus_id]
        if status:
            clauses.append("ai.status = ?")
            parameters.append(status)
        if subject:
            subject_clause, subject_parameters = self._subject_clause(
                subject,
                self._subject_scope(subject),
                anchor_expression="sa.anchor_id",
                extra_expressions=("ai.text",),
            )
            clauses.append(subject_clause)
            parameters.extend(subject_parameters)
        if since:
            clauses.append(
                """
                COALESCE(
                    datetime(p.event_time),
                    datetime(sr.observed_mtime),
                    datetime(sr.ingested_at)
                ) >= datetime(?)
                """
            )
            parameters.append(since.isoformat())
        if source_path:
            clauses.append("sd.source_path = ?")
            parameters.append(source_path)
        query = f"""
            SELECT
                ai.record_id,
                ai.text,
                ai.status,
                ai.owner,
                ai.due_date,
                p.event_time,
                sd.title,
                sd.source_path,
                sr.revision_id,
                sa.anchor_id,
                sa.heading_path_json,
                sa.quote
            FROM action_item ai
            JOIN source_anchor sa ON sa.anchor_id = ai.anchor_id
            JOIN passage p ON p.anchor_id = sa.anchor_id
            JOIN source_revision sr ON sr.revision_id = sa.revision_id
            JOIN source_document sd ON sd.document_id = sr.document_id
            WHERE {" AND ".join(clauses)}
            ORDER BY sd.source_path, sa.start_offset
        """
        with self.database.connection() as connection:
            rows = connection.execute(query, parameters).fetchall()
        related = self._related_entities(row["anchor_id"] for row in rows)
        return [
            self._action_from_row(row, related.get(row["anchor_id"], []))
            for row in rows
        ]

    def decisions(
        self,
        subject: str | None = None,
        since: datetime | None = None,
    ) -> list[EvidenceResult]:
        return self._explicit_records("decision", subject, since)

    def blockers(
        self,
        subject: str | None = None,
        since: datetime | None = None,
    ) -> list[EvidenceResult]:
        return self._explicit_records("blocker", subject, since)

    def conflicts(
        self,
        subject: str | None = None,
        since: datetime | None = None,
    ) -> list[EvidenceResult]:
        return self._explicit_records("conflict", subject, since)

    def connections(
        self,
        subject: str,
        since: datetime | None = None,
    ) -> list[EvidenceResult]:
        scope = self._subject_scope(subject)
        if not scope.entity_keys:
            return []
        placeholders = ", ".join("?" for _ in scope.entity_keys)
        filters = []
        parameters: list[object] = [
            *scope.entity_keys,
            *scope.entity_keys,
            self.corpus_id,
        ]
        if since:
            filters.append(
                "COALESCE(p.event_time, sr.observed_mtime, sr.ingested_at) >= ?"
            )
            parameters.append(since.isoformat())
        filter_sql = "".join(f" AND {condition}" for condition in filters)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    r.relationship_id AS record_id,
                    r.relationship_type,
                    source.entity_id AS source_entity_id,
                    target.entity_id AS target_entity_id,
                    p.event_time,
                    sd.title,
                    sd.source_path,
                    sr.revision_id,
                    sa.anchor_id,
                    sa.heading_path_json,
                    sa.quote
                FROM relationship r
                JOIN entity source ON source.entity_key = r.source_entity_key
                JOIN entity target ON target.entity_key = r.target_entity_key
                JOIN source_anchor sa ON sa.anchor_id = r.anchor_id
                JOIN passage p ON p.anchor_id = sa.anchor_id
                JOIN source_revision sr ON sr.revision_id = sa.revision_id
                JOIN source_document sd ON sd.document_id = sr.document_id
                WHERE r.source_entity_key IN ({placeholders})
                  AND r.target_entity_key IN ({placeholders})
                  AND sd.corpus_id = ?
                  AND sd.is_active = 1
                  AND sr.revision_id = sd.current_revision_id
                  {filter_sql}
                ORDER BY
                    sd.source_path,
                    sa.start_offset,
                    source.entity_id,
                    target.entity_id,
                    r.relationship_type,
                    r.relationship_id
                """,
                parameters,
            ).fetchall()
        return [
            EvidenceResult(
                record_id=row["record_id"],
                record_type="relationship",
                title=row["title"],
                summary=(
                    f"{row['source_entity_id']} {row['relationship_type']} "
                    f"{row['target_entity_id']}"
                ),
                status=None,
                event_time=row["event_time"],
                source_path=row["source_path"],
                source_revision_id=row["revision_id"],
                anchor_id=row["anchor_id"],
                heading_path=json.loads(row["heading_path_json"]),
                quote=row["quote"],
                related_entity_ids=[
                    row["source_entity_id"],
                    row["target_entity_id"],
                ],
            )
            for row in rows
        ]

    def evidence(self, selected_record_id: str) -> EvidenceResult:
        with self.database.connection() as connection:
            action = connection.execute(
                """
                SELECT
                    ai.record_id,
                    ai.text,
                    ai.status,
                    ai.owner,
                    ai.due_date,
                    p.event_time,
                    sd.title,
                    sd.source_path,
                    sr.revision_id,
                    sa.anchor_id,
                    sa.heading_path_json,
                    sa.quote
                FROM action_item ai
                JOIN source_anchor sa ON sa.anchor_id = ai.anchor_id
                JOIN passage p ON p.anchor_id = sa.anchor_id
                JOIN source_revision sr ON sr.revision_id = sa.revision_id
                JOIN source_document sd ON sd.document_id = sr.document_id
                WHERE ai.record_id = ? AND sd.corpus_id = ?
                """,
                (selected_record_id, self.corpus_id),
            ).fetchone()
            if action:
                related = self._related_entities([action["anchor_id"]])
                return self._action_from_row(
                    action,
                    related.get(action["anchor_id"], []),
                )

            for table in ("decision", "blocker", "conflict"):
                explicit = connection.execute(
                    f"""
                    SELECT
                        r.record_id,
                        r.text,
                        r.event_time,
                        sd.title,
                        sd.source_path,
                        sr.revision_id,
                        sa.anchor_id,
                        sa.heading_path_json,
                        sa.quote
                    FROM {table} r
                    JOIN source_anchor sa ON sa.anchor_id = r.anchor_id
                    JOIN source_revision sr ON sr.revision_id = sa.revision_id
                    JOIN source_document sd ON sd.document_id = sr.document_id
                    WHERE r.record_id = ? AND sd.corpus_id = ?
                    """,
                    (selected_record_id, self.corpus_id),
                ).fetchone()
                if explicit:
                    related = self._related_entities([explicit["anchor_id"]])
                    return self._evidence_from_row(
                        explicit,
                        table,
                        related.get(explicit["anchor_id"], []),
                    )

            relationship = connection.execute(
                """
                SELECT
                    r.relationship_id AS record_id,
                    r.relationship_type,
                    source.entity_id AS source_entity_id,
                    target.entity_id AS target_entity_id,
                    p.event_time,
                    sd.title,
                    sd.source_path,
                    sr.revision_id,
                    sa.anchor_id,
                    sa.heading_path_json,
                    sa.quote
                FROM relationship r
                JOIN entity source ON source.entity_key = r.source_entity_key
                JOIN entity target ON target.entity_key = r.target_entity_key
                JOIN source_anchor sa ON sa.anchor_id = r.anchor_id
                JOIN passage p ON p.anchor_id = sa.anchor_id
                JOIN source_revision sr ON sr.revision_id = sa.revision_id
                JOIN source_document sd ON sd.document_id = sr.document_id
                WHERE r.relationship_id = ? AND sd.corpus_id = ?
                """,
                (selected_record_id, self.corpus_id),
            ).fetchone()
            if relationship:
                return EvidenceResult(
                    record_id=relationship["record_id"],
                    record_type="relationship",
                    title=relationship["title"],
                    summary=(
                        f"{relationship['source_entity_id']} "
                        f"{relationship['relationship_type']} "
                        f"{relationship['target_entity_id']}"
                    ),
                    status=None,
                    event_time=relationship["event_time"],
                    source_path=relationship["source_path"],
                    source_revision_id=relationship["revision_id"],
                    anchor_id=relationship["anchor_id"],
                    heading_path=json.loads(relationship["heading_path_json"]),
                    quote=relationship["quote"],
                    related_entity_ids=[
                        relationship["source_entity_id"],
                        relationship["target_entity_id"],
                    ],
                )

            passage = connection.execute(
                """
                SELECT
                    p.passage_id AS record_id,
                    p.title,
                    p.passage_text,
                    p.event_time,
                    sd.source_path,
                    sr.revision_id,
                    sa.anchor_id,
                    sa.heading_path_json
                FROM passage p
                JOIN source_anchor sa ON sa.anchor_id = p.anchor_id
                JOIN source_revision sr ON sr.revision_id = sa.revision_id
                JOIN source_document sd ON sd.document_id = sr.document_id
                WHERE p.passage_id = ? AND sd.corpus_id = ?
                """,
                (selected_record_id, self.corpus_id),
            ).fetchone()
        if not passage:
            raise RecordNotFoundError(f"No record found for {selected_record_id}")
        related = self._related_entities([passage["anchor_id"]])
        return self._evidence_from_row(
            passage,
            "passage",
            related.get(passage["anchor_id"], []),
        )

    def status(self, subject: str, since: datetime | None = None) -> StatusResult:
        open_actions = self.actions(subject=subject, status="open", since=since)
        completed_actions = self.actions(subject=subject, status="completed", since=since)
        decisions = self.decisions(subject, since)
        blockers = self.blockers(subject, since)
        conflicts = self.conflicts(subject, since)
        recent_material = self._subject_material(subject, since)
        connected_entities = self.connections(subject, since)
        has_evidence = any(
            (
                recent_material,
                decisions,
                open_actions,
                completed_actions,
                blockers,
                conflicts,
                connected_entities,
            )
        )
        gaps = [] if has_evidence else [f"No deterministic evidence found for {subject}"]
        return StatusResult(
            subject=subject,
            recent_material=recent_material,
            decisions=decisions,
            open_actions=open_actions,
            completed_actions=completed_actions,
            blockers=blockers,
            connected_entities=connected_entities,
            evidence_gaps=gaps,
            conflicts=conflicts,
        )

    def _subject_scope(self, subject: str) -> SubjectScope:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                WITH RECURSIVE
                subject_entities(entity_key, entity_id, depth) AS (
                    SELECT DISTINCT e.entity_key, e.entity_id, 0
                    FROM entity e
                    JOIN entity_alias ea ON ea.entity_key = e.entity_key
                    WHERE e.corpus_id = ? AND ea.normalized_alias = ?
                ),
                walk(entity_key, entity_id, depth) AS (
                    SELECT entity_key, entity_id, depth FROM subject_entities
                    UNION
                    SELECT
                        CASE
                            WHEN r.source_entity_key = w.entity_key
                            THEN r.target_entity_key
                            ELSE r.source_entity_key
                        END,
                        neighbor.entity_id,
                        w.depth + 1
                    FROM walk w
                    JOIN relationship r
                      ON r.source_entity_key = w.entity_key
                      OR r.target_entity_key = w.entity_key
                    JOIN source_anchor sa ON sa.anchor_id = r.anchor_id
                    JOIN source_revision sr ON sr.revision_id = sa.revision_id
                    JOIN source_document sd ON sd.document_id = sr.document_id
                    JOIN entity neighbor
                      ON neighbor.entity_key = CASE
                          WHEN r.source_entity_key = w.entity_key
                          THEN r.target_entity_key
                          ELSE r.source_entity_key
                      END
                    WHERE w.depth < 2
                      AND sd.corpus_id = ?
                      AND sd.is_active = 1
                      AND sr.revision_id = sd.current_revision_id
                )
                SELECT entity_key, entity_id, min(depth) AS depth
                FROM walk
                GROUP BY entity_key, entity_id
                ORDER BY depth, entity_id
                """,
                (self.corpus_id, subject.casefold(), self.corpus_id),
            ).fetchall()
        return SubjectScope(
            entity_keys=tuple(row["entity_key"] for row in rows),
            entity_ids=tuple(row["entity_id"] for row in rows),
        )

    def _subject_material(
        self,
        subject: str,
        since: datetime | None,
        limit: int = 20,
    ) -> list[EvidenceResult]:
        clauses = [
            "sd.corpus_id = ?",
            "sd.is_active = 1",
            "sr.revision_id = sd.current_revision_id",
        ]
        parameters: list[object] = [self.corpus_id]
        subject_clause, subject_parameters = self._subject_clause(
            subject,
            self._subject_scope(subject),
            anchor_expression="sa.anchor_id",
        )
        clauses.append(subject_clause)
        parameters.extend(subject_parameters)
        if since:
            clauses.append(
                """
                COALESCE(
                    datetime(p.event_time),
                    datetime(sr.observed_mtime),
                    datetime(sr.ingested_at)
                ) >= datetime(?)
                """
            )
            parameters.append(since.isoformat())
        parameters.append(limit)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    p.passage_id AS record_id,
                    p.title,
                    p.passage_text,
                    p.event_time,
                    sd.source_path,
                    sr.revision_id,
                    sa.anchor_id,
                    sa.heading_path_json,
                    sa.start_offset
                FROM passage p
                JOIN source_anchor sa ON sa.anchor_id = p.anchor_id
                JOIN source_revision sr ON sr.revision_id = sa.revision_id
                JOIN source_document sd ON sd.document_id = sr.document_id
                WHERE {" AND ".join(clauses)}
                ORDER BY
                    COALESCE(p.event_time, sr.observed_mtime, sr.ingested_at) DESC,
                    sd.source_path,
                    sa.start_offset
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        related = self._related_entities(row["anchor_id"] for row in rows)
        return [
            self._evidence_from_row(
                row,
                "passage",
                related.get(row["anchor_id"], []),
            )
            for row in rows
        ]

    def _explicit_records(
        self,
        table: str,
        subject: str | None,
        since: datetime | None,
    ) -> list[EvidenceResult]:
        if table not in {"decision", "blocker", "conflict"}:
            raise ValueError(f"Unsupported explicit record table: {table}")
        clauses = [
            "sd.corpus_id = ?",
            "sd.is_active = 1",
            "sr.revision_id = sd.current_revision_id",
        ]
        parameters: list[object] = [self.corpus_id]
        if subject:
            subject_clause, subject_parameters = self._subject_clause(
                subject,
                self._subject_scope(subject),
                anchor_expression="sa.anchor_id",
                extra_expressions=("r.text",),
            )
            clauses.append(subject_clause)
            parameters.extend(subject_parameters)
        if since:
            clauses.append(
                """
                COALESCE(
                    datetime(r.event_time),
                    datetime(sr.observed_mtime),
                    datetime(sr.ingested_at)
                ) >= datetime(?)
                """
            )
            parameters.append(since.isoformat())
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT
                    r.record_id,
                    r.text,
                    r.event_time,
                    sd.title,
                    sd.source_path,
                    sr.revision_id,
                    sa.anchor_id,
                    sa.heading_path_json,
                    sa.quote
                FROM {table} r
                JOIN source_anchor sa ON sa.anchor_id = r.anchor_id
                JOIN source_revision sr ON sr.revision_id = sa.revision_id
                JOIN source_document sd ON sd.document_id = sr.document_id
                WHERE {" AND ".join(clauses)}
                ORDER BY sd.source_path, sa.start_offset
                """,
                parameters,
            ).fetchall()
        related = self._related_entities(row["anchor_id"] for row in rows)
        return [
            self._evidence_from_row(
                row,
                table,
                related.get(row["anchor_id"], []),
            )
            for row in rows
        ]

    def _subject_clause(
        self,
        subject: str,
        scope: SubjectScope | None,
        *,
        anchor_expression: str,
        extra_expressions: tuple[str, ...] = (),
    ) -> tuple[str, list[object]]:
        pattern = f"%{subject.casefold()}%"
        text_expressions = ("sd.title", "sa.heading_path_json", *extra_expressions)
        clauses = [f"lower({expression}) LIKE ?" for expression in text_expressions]
        parameters: list[object] = [pattern] * len(text_expressions)
        if scope and scope.entity_keys:
            placeholders = ", ".join("?" for _ in scope.entity_keys)
            clauses.append(
                f"""
                EXISTS (
                    SELECT 1 FROM mention m
                    WHERE m.anchor_id = {anchor_expression}
                      AND m.entity_key IN ({placeholders})
                )
                """
            )
            parameters.extend(scope.entity_keys)
            clauses.append(
                f"""
                EXISTS (
                    SELECT 1
                    FROM mention document_mention
                    JOIN source_anchor document_anchor
                      ON document_anchor.anchor_id = document_mention.anchor_id
                    WHERE document_anchor.revision_id = sr.revision_id
                      AND document_mention.entity_key IN ({placeholders})
                )
                """
            )
            parameters.extend(scope.entity_keys)
        return f"({' OR '.join(clauses)})", parameters

    def _related_entities(
        self,
        anchor_ids: Iterable[str],
    ) -> dict[str, list[str]]:
        unique_anchor_ids = sorted(set(anchor_ids))
        if not unique_anchor_ids:
            return {}
        placeholders = ", ".join("?" for _ in unique_anchor_ids)
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT DISTINCT m.anchor_id, e.entity_id
                FROM mention m
                JOIN entity e ON e.entity_key = m.entity_key
                WHERE m.anchor_id IN ({placeholders})
                  AND e.corpus_id = ?
                ORDER BY m.anchor_id, e.entity_id
                """,
                (*unique_anchor_ids, self.corpus_id),
            ).fetchall()
        result: dict[str, list[str]] = {}
        for row in rows:
            result.setdefault(row["anchor_id"], []).append(row["entity_id"])
        return result

    @staticmethod
    def _action_from_row(
        row: sqlite3.Row,
        related_entity_ids: list[str],
    ) -> ActionResult:
        return ActionResult(
            record_id=row["record_id"],
            record_type="action",
            title=row["title"],
            summary=row["text"],
            status=row["status"],
            event_time=row["event_time"],
            source_path=row["source_path"],
            source_revision_id=row["revision_id"],
            anchor_id=row["anchor_id"],
            heading_path=json.loads(row["heading_path_json"]),
            quote=row["quote"],
            related_entity_ids=related_entity_ids,
            owner=row["owner"],
            due_date=row["due_date"],
        )

    @staticmethod
    def _evidence_from_row(
        row: sqlite3.Row,
        record_type: str,
        related_entity_ids: list[str],
    ) -> EvidenceResult:
        columns = set(row.keys())
        passage_text = row["passage_text"] if "passage_text" in columns else None
        summary = row["text"] if "text" in columns else None
        quote = row["quote"] if "quote" in columns else passage_text
        if not isinstance(quote, str):
            raise ValueError("Evidence row is missing its source quote")
        return EvidenceResult(
            record_id=row["record_id"],
            record_type=record_type,
            title=row["title"],
            summary=summary,
            status=None,
            event_time=row["event_time"],
            source_path=row["source_path"],
            source_revision_id=row["revision_id"],
            anchor_id=row["anchor_id"],
            heading_path=json.loads(row["heading_path_json"]),
            quote=quote,
            related_entity_ids=related_entity_ids,
        )


def _fts_expression(value: str) -> str:
    tokens = re.findall(r"\w+", value, flags=re.UNICODE)
    if not tokens:
        raise SearchQueryError("Search text must contain at least one letter or number")
    return " AND ".join(f'"{token}"' for token in tokens)
