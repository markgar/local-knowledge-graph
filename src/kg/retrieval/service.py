from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime

from kg.db import Database
from kg.models.contracts import ActionResult, EvidenceResult, SearchResult, StatusResult


class RecordNotFoundError(LookupError):
    pass


class SearchQueryError(ValueError):
    pass


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
    ) -> list[SearchResult]:
        expression = _fts_expression(query)
        if subject:
            expression = f"({expression}) AND ({_fts_expression(subject)})"
        clauses = [
            "passage_fts MATCH ?",
            "sd.corpus_id = ?",
            "sd.is_active = 1",
            "sr.revision_id = sd.current_revision_id",
        ]
        parameters: list[object] = [expression, self.corpus_id]
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
                related_entity_ids=[],
                rank=row["rank"],
            )
            for row in rows
        ]

    def actions(
        self,
        subject: str | None = None,
        status: str | None = None,
    ) -> list[ActionResult]:
        clauses = [
            "sd.corpus_id = ?",
            "sd.is_active = 1",
            "sr.revision_id = sd.current_revision_id",
        ]
        parameters: list[str] = [self.corpus_id]
        if status:
            clauses.append("ai.status = ?")
            parameters.append(status)
        if subject:
            clauses.append(
                """
                (
                    lower(ai.text) LIKE ?
                    OR lower(sd.title) LIKE ?
                    OR lower(sa.heading_path_json) LIKE ?
                )
                """
            )
            pattern = f"%{subject.casefold()}%"
            parameters.extend([pattern, pattern, pattern])
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
        return [self._action_from_row(row) for row in rows]

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
                return self._action_from_row(action)

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
        return EvidenceResult(
            record_id=passage["record_id"],
            record_type="passage",
            title=passage["title"],
            summary=None,
            status=None,
            event_time=passage["event_time"],
            source_path=passage["source_path"],
            source_revision_id=passage["revision_id"],
            anchor_id=passage["anchor_id"],
            heading_path=json.loads(passage["heading_path_json"]),
            quote=passage["passage_text"],
            related_entity_ids=[],
        )

    def status(self, subject: str, since: datetime | None = None) -> StatusResult:
        open_actions = self.actions(subject=subject, status="open")
        completed_actions = self.actions(subject=subject, status="completed")
        recent_material: list[EvidenceResult] = list(
            self.search(subject, subject=None, limit=10, since=since)
        )
        connected_entities = self._connected_entities(subject)
        gaps = [] if recent_material or open_actions or completed_actions else [
            f"No deterministic evidence found for {subject}"
        ]
        return StatusResult(
            subject=subject,
            recent_material=recent_material,
            open_actions=open_actions,
            completed_actions=completed_actions,
            connected_entities=connected_entities,
            evidence_gaps=gaps,
        )

    def _connected_entities(self, subject: str) -> list[str]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                WITH subject_entities AS (
                    SELECT DISTINCT e.entity_key
                    FROM entity e
                    JOIN entity_alias ea ON ea.entity_key = e.entity_key
                    WHERE e.corpus_id = ? AND ea.normalized_alias = ?
                ),
                connected AS (
                    SELECT r.target_entity_key AS entity_key
                    FROM relationship r
                    JOIN source_anchor sa ON sa.anchor_id = r.anchor_id
                    JOIN source_revision sr ON sr.revision_id = sa.revision_id
                    JOIN source_document sd ON sd.document_id = sr.document_id
                    WHERE r.source_entity_key IN subject_entities
                      AND sd.is_active = 1
                      AND sr.revision_id = sd.current_revision_id
                    UNION
                    SELECT r.source_entity_key AS entity_key
                    FROM relationship r
                    JOIN source_anchor sa ON sa.anchor_id = r.anchor_id
                    JOIN source_revision sr ON sr.revision_id = sa.revision_id
                    JOIN source_document sd ON sd.document_id = sr.document_id
                    WHERE r.target_entity_key IN subject_entities
                      AND sd.is_active = 1
                      AND sr.revision_id = sd.current_revision_id
                )
                SELECT e.entity_id
                FROM connected c
                JOIN entity e ON e.entity_key = c.entity_key
                WHERE e.corpus_id = ?
                ORDER BY e.entity_id
                """,
                (self.corpus_id, subject.casefold(), self.corpus_id),
            ).fetchall()
        return [row["entity_id"] for row in rows]

    @staticmethod
    def _action_from_row(row: sqlite3.Row) -> ActionResult:
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
            related_entity_ids=[],
            owner=row["owner"],
            due_date=row["due_date"],
        )


def _fts_expression(value: str) -> str:
    tokens = re.findall(r"\w+", value, flags=re.UNICODE)
    if not tokens:
        raise SearchQueryError("Search text must contain at least one letter or number")
    return " AND ".join(f'"{token}"' for token in tokens)
