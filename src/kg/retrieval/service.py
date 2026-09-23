from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Literal, cast

from kg.db import Database
from kg.lexical import LEXICAL_INDEX_VERSION, corpus_fingerprint, lexical_table
from kg.models.contracts import (
    ActionResult,
    EvidenceResult,
    RecordStateReport,
    RevisionComparisonResult,
    RevisionRangeChange,
    RevisionResult,
    SearchResult,
    SourceContextResult,
    SourceRangeResult,
    StatusResult,
)
from kg.record_state import resolve_record_state


class RecordNotFoundError(LookupError):
    pass


class SearchQueryError(ValueError):
    pass


class RevisionComparisonError(ValueError):
    pass


@dataclass(frozen=True)
class SubjectScope:
    entity_keys: tuple[str, ...]
    entity_ids: tuple[str, ...]


@dataclass(frozen=True)
class PassageProjectionRecord:
    passage_id: str
    anchor_id: str
    revision_id: str
    document_id: str
    quote: str
    title: str
    heading_path: tuple[str, ...]


class RetrievalService:
    def __init__(self, database: Database, corpus_id: str) -> None:
        self.database = database
        self.corpus_id = corpus_id
        self.database.initialize()

    def search(
        self,
        query: str,
        subject: str | None = None,
        limit: int = 20,
        since: datetime | None = None,
        source_path: str | None = None,
        query_mode: Literal["strict", "natural"] = "strict",
    ) -> list[SearchResult]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        expression = _fts_expression(query, query_mode)
        table = lexical_table(self.corpus_id)
        clauses = [
            f"{table} MATCH ?",
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
        if source_path:
            clauses.append("sd.source_path = ?")
            parameters.append(source_path)
        with self.database.connection() as connection:
            connection.execute("BEGIN")
            projection = connection.execute(
                "SELECT source_fingerprint, version FROM lexical_projection WHERE corpus_id = ?",
                (self.corpus_id,),
            ).fetchone()
            if projection is None and not connection.execute(
                "SELECT 1 FROM source_document WHERE corpus_id = ? LIMIT 1",
                (self.corpus_id,),
            ).fetchone():
                return []
            if (
                projection is None
                or projection["version"] != LEXICAL_INDEX_VERSION
                or projection["source_fingerprint"] != self._index_fingerprint(connection)
            ):
                raise SearchQueryError(
                    "The corpus-scoped lexical index is missing or stale; run 'kg ingest' again."
                )
            if subject:
                subject_clause, subject_parameters = self._subject_clause(
                    subject,
                    self._subject_scope(subject, connection),
                    anchor_expression="sa.anchor_id",
                )
                clauses.append(subject_clause)
                parameters.extend(subject_parameters)
            parameters.append(limit)
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
                    bm25({table}) AS rank
                FROM {table}
                JOIN passage p ON p.passage_id = {table}.passage_id
                JOIN source_anchor sa ON sa.anchor_id = p.anchor_id
                JOIN source_revision sr ON sr.revision_id = sa.revision_id
                JOIN source_document sd ON sd.document_id = sr.document_id
                WHERE {" AND ".join(clauses)}
                ORDER BY rank, sd.source_path, sa.start_offset, p.passage_id
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

    def current_passages(self) -> list[PassageProjectionRecord]:
        with self.database.connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    p.passage_id,
                    p.document_id,
                    p.passage_text,
                    p.title,
                    sa.heading_path_json,
                    sr.revision_id,
                    sa.anchor_id
                FROM passage p
                JOIN source_anchor sa ON sa.anchor_id = p.anchor_id
                JOIN source_revision sr ON sr.revision_id = sa.revision_id
                JOIN source_document sd ON sd.document_id = sr.document_id
                WHERE sd.corpus_id = ?
                  AND sd.is_active = 1
                  AND sr.revision_id = sd.current_revision_id
                ORDER BY sd.source_path, sa.start_offset, p.passage_id
                """,
                (self.corpus_id,),
            ).fetchall()
        return [
            PassageProjectionRecord(
                passage_id=row["passage_id"],
                anchor_id=row["anchor_id"],
                revision_id=row["revision_id"],
                document_id=row["document_id"],
                quote=row["passage_text"],
                title=row["title"],
                heading_path=tuple(json.loads(row["heading_path_json"])),
            )
            for row in rows
        ]

    def source_range(self, anchor_id: str) -> SourceRangeResult:
        with self.database.connection() as connection:
            row = connection.execute(
                """
                SELECT
                    sd.document_id,
                    sd.source_path,
                    sd.current_revision_id,
                    sr.revision_id,
                    sa.anchor_id,
                    sa.structural_path,
                    sa.heading_path_json,
                    sa.anchor_kind,
                    sa.start_offset,
                    sa.end_offset,
                    sa.quote,
                    sa.quote_hash
                FROM source_anchor sa
                JOIN source_revision sr ON sr.revision_id = sa.revision_id
                JOIN source_document sd ON sd.document_id = sr.document_id
                WHERE sa.anchor_id = ? AND sd.corpus_id = ?
                """,
                (anchor_id, self.corpus_id),
            ).fetchone()
        if not row:
            raise RecordNotFoundError(f"No source range found for {anchor_id}")
        return self._source_range_from_row(row)

    def source_context(
        self,
        anchor_id: str,
        *,
        max_anchors: int = 50,
    ) -> SourceContextResult:
        if not 1 <= max_anchors <= 200:
            raise ValueError("max_anchors must be between 1 and 200")
        with self.database.connection() as connection:
            connection.execute("BEGIN")
            revision = connection.execute(
                """
                SELECT sa.revision_id
                FROM source_anchor sa
                JOIN source_revision sr ON sr.revision_id = sa.revision_id
                JOIN source_document sd ON sd.document_id = sr.document_id
                WHERE sa.anchor_id = ? AND sd.corpus_id = ?
                """,
                (anchor_id, self.corpus_id),
            ).fetchone()
            if not revision:
                raise RecordNotFoundError(f"No source range found for {anchor_id}")
            anchors = [
                self._source_range_from_row(row)
                for row in self._revision_anchor_rows(connection, revision["revision_id"])
            ]
        # Enclosing list anchors precede headings that start on the same line.
        anchors.sort(
            key=lambda anchor: (
                anchor.start_offset,
                -anchor.end_offset,
                anchor.anchor_kind == "heading",
                anchor.anchor_id,
            )
        )
        selected_index = next(
            index for index, anchor in enumerate(anchors) if anchor.anchor_id == anchor_id
        )
        selected = anchors[selected_index]
        heading_index = next(
            (
                index
                for index in range(selected_index, -1, -1)
                if anchors[index].anchor_kind == "heading"
                and anchors[index].heading_path == selected.heading_path
            ),
            None,
        )
        section_heading = anchors[heading_index] if heading_index is not None else None
        start = heading_index if heading_index is not None else 0
        depth = len(section_heading.heading_path) if section_heading else 0
        end = next(
            (
                index
                for index in range(start + (section_heading is not None), len(anchors))
                if anchors[index].anchor_kind == "heading"
                and (section_heading is None or len(anchors[index].heading_path) <= depth)
            ),
            len(anchors),
        )
        section = anchors[start:end]
        position = selected_index - start
        window_start = max(0, min(position - max_anchors // 2, len(section) - max_anchors))
        return SourceContextResult(
            selected=selected,
            section_heading=section_heading,
            anchors=section[window_start : window_start + max_anchors],
            total_anchors=len(section),
            truncated=len(section) > max_anchors,
        )

    def revisions(self, source_path: str) -> list[RevisionResult]:
        with self.database.connection() as connection:
            document = self._document_for_source(connection, source_path)
            rows = connection.execute(
                """
                SELECT
                    sd.document_id,
                    sd.source_path,
                    sd.current_revision_id,
                    sr.revision_id,
                    sr.content_hash,
                    sr.observed_mtime,
                    sr.ingested_at
                FROM source_revision sr
                JOIN source_document sd ON sd.document_id = sr.document_id
                WHERE sr.document_id = ?
                ORDER BY sr.ingested_at, sr.revision_id
                """,
                (document["document_id"],),
            ).fetchall()
        return [
            RevisionResult(
                document_id=row["document_id"],
                source_path=row["source_path"],
                source_revision_id=row["revision_id"],
                content_hash=row["content_hash"],
                observed_mtime=row["observed_mtime"],
                ingested_at=row["ingested_at"],
                is_current=row["revision_id"] == row["current_revision_id"],
            )
            for row in rows
        ]

    def compare_revisions(
        self,
        source_path: str,
        *,
        from_revision_id: str | None = None,
        to_revision_id: str | None = None,
    ) -> RevisionComparisonResult:
        with self.database.connection() as connection:
            connection.execute("BEGIN")
            document = self._document_for_source(connection, source_path)
            revisions = connection.execute(
                """
                SELECT revision_id, ingested_at
                FROM source_revision
                WHERE document_id = ?
                ORDER BY ingested_at, revision_id
                """,
                (document["document_id"],),
            ).fetchall()
            revision_ids = [row["revision_id"] for row in revisions]
            selected_to = to_revision_id or document["current_revision_id"]
            if selected_to not in revision_ids:
                raise RevisionComparisonError(
                    f"Revision {selected_to} does not belong to {source_path}"
                )
            if from_revision_id is None:
                activation = connection.execute(
                    """
                    SELECT previous_revision_id
                    FROM revision_activation
                    WHERE document_id = ? AND revision_id = ?
                    ORDER BY activation_id DESC
                    LIMIT 1
                    """,
                    (document["document_id"], selected_to),
                ).fetchone()
                if activation is None or activation["previous_revision_id"] is None:
                    raise RevisionComparisonError(
                        f"No recorded predecessor exists for {source_path}; "
                        "specify --from and --to for revisions without activation history."
                    )
                selected_from = activation["previous_revision_id"]
            else:
                selected_from = from_revision_id
            if selected_from not in revision_ids:
                raise RevisionComparisonError(
                    f"Revision {selected_from} does not belong to {source_path}"
                )
            if selected_from == selected_to:
                raise RevisionComparisonError("Revision comparison requires two revisions")

            before_rows = self._revision_anchor_rows(connection, selected_from)
            after_rows = self._revision_anchor_rows(connection, selected_to)

        before_by_path = {row["structural_path"]: row for row in before_rows}
        after_by_path = {row["structural_path"]: row for row in after_rows}
        common_paths = sorted(before_by_path.keys() & after_by_path.keys())
        modified = [
            RevisionRangeChange(
                structural_path=path,
                before=self._source_range_from_row(before_by_path[path]),
                after=self._source_range_from_row(after_by_path[path]),
            )
            for path in common_paths
            if before_by_path[path]["quote_hash"] != after_by_path[path]["quote_hash"]
        ]
        unchanged_count = sum(
            before_by_path[path]["quote_hash"] == after_by_path[path]["quote_hash"]
            for path in common_paths
        )
        removed = [
            self._source_range_from_row(before_by_path[path])
            for path in sorted(before_by_path.keys() - after_by_path.keys())
        ]
        added = [
            self._source_range_from_row(after_by_path[path])
            for path in sorted(after_by_path.keys() - before_by_path.keys())
        ]
        return RevisionComparisonResult(
            document_id=document["document_id"],
            source_path=source_path,
            from_revision_id=selected_from,
            to_revision_id=selected_to,
            added=added,
            removed=removed,
            modified=modified,
            unchanged_count=unchanged_count,
        )

    def index_fingerprint(
        self,
        connection: sqlite3.Connection | None = None,
    ) -> str:
        if connection is None:
            with self.database.connection() as managed_connection:
                return self._index_fingerprint(managed_connection)
        return self._index_fingerprint(connection)

    def _index_fingerprint(self, connection: sqlite3.Connection) -> str:
        return corpus_fingerprint(connection, self.corpus_id)

    def _document_for_source(
        self,
        connection: sqlite3.Connection,
        source_path: str,
    ) -> sqlite3.Row:
        rows = connection.execute(
            """
            SELECT document_id, source_path, current_revision_id, is_active
            FROM source_document
            WHERE corpus_id = ? AND source_path = ?
            ORDER BY is_active DESC, updated_at DESC, document_id
            """,
            (self.corpus_id, source_path),
        ).fetchall()
        if not rows:
            raise RecordNotFoundError(f"No source document found for {source_path}")
        active = [row for row in rows if row["is_active"]]
        candidates = active or rows
        if len(candidates) != 1:
            raise RevisionComparisonError(
                f"Source path is ambiguous in corpus {self.corpus_id}: {source_path}"
            )
        return cast(sqlite3.Row, candidates[0])

    def _revision_anchor_rows(
        self,
        connection: sqlite3.Connection,
        revision_id: str,
    ) -> list[sqlite3.Row]:
        return connection.execute(
            """
            SELECT
                sd.document_id,
                sd.source_path,
                sd.current_revision_id,
                sr.revision_id,
                sa.anchor_id,
                sa.structural_path,
                sa.heading_path_json,
                sa.anchor_kind,
                sa.start_offset,
                sa.end_offset,
                sa.quote,
                sa.quote_hash
            FROM source_anchor sa
            JOIN source_revision sr ON sr.revision_id = sa.revision_id
            JOIN source_document sd ON sd.document_id = sr.document_id
            WHERE sr.revision_id = ? AND sd.corpus_id = ?
            ORDER BY sa.start_offset, sa.end_offset, sa.anchor_id
            """,
            (revision_id, self.corpus_id),
        ).fetchall()

    def eligible_passages(
        self,
        *,
        subject: str | None = None,
        since: datetime | None = None,
        source_path: str | None = None,
    ) -> dict[str, str]:
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
        with self.database.connection() as connection:
            rows = connection.execute(
                f"""
                SELECT p.passage_id, p.document_id
                FROM passage p
                JOIN source_anchor sa ON sa.anchor_id = p.anchor_id
                JOIN source_revision sr ON sr.revision_id = sa.revision_id
                JOIN source_document sd ON sd.document_id = sr.document_id
                WHERE {" AND ".join(clauses)}
                """,
                parameters,
            ).fetchall()
        return {row["passage_id"]: row["document_id"] for row in rows}

    def passage_results(
        self,
        ranked_passage_ids: Sequence[tuple[str, float]],
    ) -> list[SearchResult]:
        if not ranked_passage_ids:
            return []
        passage_ids = [passage_id for passage_id, _ in ranked_passage_ids]
        placeholders = ", ".join("?" for _ in passage_ids)
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
                    sa.heading_path_json
                FROM passage p
                JOIN source_anchor sa ON sa.anchor_id = p.anchor_id
                JOIN source_revision sr ON sr.revision_id = sa.revision_id
                JOIN source_document sd ON sd.document_id = sr.document_id
                WHERE p.passage_id IN ({placeholders})
                  AND sd.corpus_id = ?
                  AND sd.is_active = 1
                  AND sr.revision_id = sd.current_revision_id
                """,
                (*passage_ids, self.corpus_id),
            ).fetchall()
        rows_by_id = {row["record_id"]: row for row in rows}
        related = self._related_entities(row["anchor_id"] for row in rows)
        results = []
        for passage_id, rank in ranked_passage_ids:
            row = rows_by_id.get(passage_id)
            if row is None:
                continue
            results.append(
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
                    rank=rank,
                )
            )
        return results

    def actions(
        self,
        subject: str | None = None,
        status: str | None = None,
        since: datetime | None = None,
        source_path: str | None = None,
    ) -> list[ActionResult]:
        with self.database.connection() as connection:
            connection.execute("BEGIN")
            return self._actions(
                connection,
                subject=subject,
                status=status,
                since=since,
                source_path=source_path,
            )

    def _actions(
        self,
        connection: sqlite3.Connection,
        *,
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
        state_clause, state_parameters = self._effective_record_clause(
            connection, "action_item", "ai", subject
        )
        clauses.append(state_clause)
        parameters.extend(state_parameters)
        if status:
            clauses.append("ai.status = ?")
            parameters.append(status)
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
        rows = connection.execute(query, parameters).fetchall()
        related = self._related_entities(
            (row["anchor_id"] for row in rows),
            connection,
        )
        return [
            self._action_from_row(row, related.get(row["anchor_id"], []))
            for row in rows
        ]

    def decisions(
        self,
        subject: str | None = None,
        since: datetime | None = None,
    ) -> list[EvidenceResult]:
        with self.database.connection() as connection:
            connection.execute("BEGIN")
            return self._explicit_records(connection, "decision", subject, since)

    def blockers(
        self,
        subject: str | None = None,
        since: datetime | None = None,
    ) -> list[EvidenceResult]:
        with self.database.connection() as connection:
            connection.execute("BEGIN")
            return self._explicit_records(connection, "blocker", subject, since)

    def conflicts(
        self,
        subject: str | None = None,
        since: datetime | None = None,
    ) -> list[EvidenceResult]:
        with self.database.connection() as connection:
            connection.execute("BEGIN")
            return self._explicit_records(connection, "conflict", subject, since)

    def connections(
        self,
        subject: str,
        since: datetime | None = None,
    ) -> list[EvidenceResult]:
        with self.database.connection() as connection:
            return self._connections(connection, subject, since)

    def _connections(
        self,
        connection: sqlite3.Connection,
        subject: str,
        since: datetime | None,
    ) -> list[EvidenceResult]:
        scope = self._subject_scope(subject, connection)
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
                """
                COALESCE(
                    datetime(p.event_time),
                    datetime(sr.observed_mtime),
                    datetime(sr.ingested_at)
                ) >= datetime(?)
                """
            )
            parameters.append(since.isoformat())
        filter_sql = "".join(f" AND {condition}" for condition in filters)
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
                    p.title,
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
                        p.title,
                        sd.source_path,
                        sr.revision_id,
                        sa.anchor_id,
                        sa.heading_path_json,
                        sa.quote
                    FROM {table} r
                    JOIN source_anchor sa ON sa.anchor_id = r.anchor_id
                    JOIN passage p ON p.anchor_id = sa.anchor_id
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
                    p.title,
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
        with self.database.connection() as connection:
            connection.execute("BEGIN")
            open_actions = self._actions(
                connection,
                subject=subject,
                status="open",
                since=since,
            )
            completed_actions = self._actions(
                connection,
                subject=subject,
                status="completed",
                since=since,
            )
            decisions = self._explicit_records(
                connection,
                "decision",
                subject,
                since,
            )
            blockers = self._explicit_records(
                connection,
                "blocker",
                subject,
                since,
            )
            conflicts = self._explicit_records(
                connection,
                "conflict",
                subject,
                since,
            )
            recent_material = self._subject_material(
                subject,
                since,
                connection=connection,
            )
            connected_entities = self._connections(connection, subject, since)
            state_warnings = resolve_record_state(connection, self.corpus_id).warnings
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
        gaps.extend(f"Corpus record-state warning: {warning}" for warning in state_warnings)
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

    def _subject_scope(
        self,
        subject: str,
        connection: sqlite3.Connection | None = None,
    ) -> SubjectScope:
        if connection is None:
            with self.database.connection() as managed_connection:
                return self._subject_scope(subject, managed_connection)
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
        *,
        connection: sqlite3.Connection | None = None,
    ) -> list[EvidenceResult]:
        if connection is None:
            with self.database.connection() as managed_connection:
                return self._subject_material(
                    subject,
                    since,
                    limit,
                    connection=managed_connection,
                )
        clauses = [
            "sd.corpus_id = ?",
            "sd.is_active = 1",
            "sr.revision_id = sd.current_revision_id",
        ]
        parameters: list[object] = [self.corpus_id]
        subject_clause, subject_parameters = self._subject_clause(
            subject,
            self._subject_scope(subject, connection),
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
        related = self._related_entities(
            (row["anchor_id"] for row in rows),
            connection,
        )
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
        connection: sqlite3.Connection,
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
        if table == "decision":
            state_clause, state_parameters = self._effective_record_clause(
                connection, table, "r", subject
            )
            clauses.append(state_clause)
            parameters.extend(state_parameters)
        elif subject:
            subject_clause, subject_parameters = self._subject_clause(
                subject,
                self._subject_scope(subject, connection),
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
        related = self._related_entities(
            (row["anchor_id"] for row in rows),
            connection,
        )
        return [
            self._evidence_from_row(
                row,
                table,
                related.get(row["anchor_id"], []),
            )
            for row in rows
        ]

    def record_state(
        self, *, include_quotes: bool = False, limit: int = 50,
    ) -> RecordStateReport:
        with self.database.connection() as connection:
            connection.execute("BEGIN")
            return resolve_record_state(
                connection, self.corpus_id, include_quotes=include_quotes
            ).report(limit)

    def _effective_record_clause(
        self,
        connection: sqlite3.Connection,
        table: str,
        alias: str,
        subject: str | None,
    ) -> tuple[str, list[object]]:
        if (table, alias) not in {("action_item", "ai"), ("decision", "r")}:
            raise ValueError("Unsupported effective-record query")
        state = resolve_record_state(connection, self.corpus_id)
        clauses = []
        parameters: list[object] = []
        if state.successors:
            connection.create_function(
                "kg_record_current", 1,
                lambda record_id: record_id not in state.successors,
                deterministic=True,
            )
            clauses.append(f"kg_record_current({alias}.record_id)")
        if subject:
            subject_clause, subject_parameters = self._subject_clause(
                subject, self._subject_scope(subject, connection),
                anchor_expression="sa.anchor_id", extra_expressions=(f"{alias}.text",),
            )
            if state.successors:
                direct = connection.execute(
                    f"""
                    SELECT {alias}.record_id FROM {table} {alias}
                    JOIN source_anchor sa ON sa.anchor_id = {alias}.anchor_id
                    JOIN source_revision sr ON sr.revision_id = sa.revision_id
                    JOIN source_document sd ON sd.document_id = sr.document_id
                    WHERE sd.corpus_id = ? AND sd.is_active = 1
                      AND sr.revision_id = sd.current_revision_id AND {subject_clause}
                    """,
                    (self.corpus_id, *subject_parameters),
                ).fetchall()
                inherited = {state.current_id(row["record_id"]) for row in direct}
                connection.create_function(
                    "kg_record_subject", 1,
                    lambda record_id: record_id in inherited,
                    deterministic=True,
                )
                subject_clause = f"({subject_clause} OR kg_record_subject({alias}.record_id))"
            clauses.append(subject_clause)
            parameters.extend(subject_parameters)
        return " AND ".join(clauses) or "1", parameters

    def _subject_clause(
        self,
        subject: str,
        scope: SubjectScope | None,
        *,
        anchor_expression: str,
        extra_expressions: tuple[str, ...] = (),
    ) -> tuple[str, list[object]]:
        text_expressions = ("sd.title", "sa.heading_path_json", *extra_expressions)
        clauses = [
            f"kg_matches_alias({expression}, ?)"
            for expression in text_expressions
        ]
        parameters: list[object] = [subject] * len(text_expressions)
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
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, list[str]]:
        unique_anchor_ids = sorted(set(anchor_ids))
        if not unique_anchor_ids:
            return {}
        if connection is None:
            with self.database.connection() as managed_connection:
                return self._related_entities(unique_anchor_ids, managed_connection)
        placeholders = ", ".join("?" for _ in unique_anchor_ids)
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

    @staticmethod
    def _source_range_from_row(row: sqlite3.Row) -> SourceRangeResult:
        return SourceRangeResult(
            document_id=row["document_id"],
            source_path=row["source_path"],
            source_revision_id=row["revision_id"],
            is_current=row["revision_id"] == row["current_revision_id"],
            anchor_id=row["anchor_id"],
            structural_path=row["structural_path"],
            heading_path=json.loads(row["heading_path_json"]),
            anchor_kind=row["anchor_kind"],
            start_offset=row["start_offset"],
            end_offset=row["end_offset"],
            quote=row["quote"],
            quote_hash=row["quote_hash"],
        )


def _fts_expression(
    value: str,
    query_mode: Literal["strict", "natural"] = "strict",
) -> str:
    if query_mode not in {"strict", "natural"}:
        raise SearchQueryError("query mode must be 'strict' or 'natural'")
    tokens = re.findall(r"\w+", value, flags=re.UNICODE)
    if not tokens:
        raise SearchQueryError("Search text must contain at least one letter or number")
    if query_mode == "strict":
        return " AND ".join(f'"{token}"' for token in tokens)
    unique_tokens = dict.fromkeys(token.casefold() for token in tokens)
    return " OR ".join(f'"{token}"' for token in unique_tokens)
