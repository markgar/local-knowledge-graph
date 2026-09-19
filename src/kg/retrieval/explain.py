"""Opt-in lexical search traces, verified against an unchanged database state."""

from __future__ import annotations

import sqlite3
from collections import deque
from datetime import datetime
from typing import Literal

from pydantic import Field

from kg.db import Database
from kg.lexical import LEXICAL_INDEX_VERSION, corpus_fingerprint, lexical_table
from kg.models.contracts import ContractModel, SearchResult
from kg.retrieval.service import RetrievalService, SearchQueryError, _fts_expression


class SearchExplanationError(SearchQueryError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


class SearchFilters(ContractModel):
    subject: str | None = None
    since: datetime | None = None
    source_path: str | None = None
    limit: int
    since_semantics: str = "event_time, otherwise observed_mtime, otherwise ingested_at"


class ScopeEntity(ContractModel):
    entity_id: str
    distance: int = Field(ge=0, le=2)
    scope: Literal["direct", "graph_expanded"]
    supporting_edge_ids: list[str]
    supporting_anchor_ids: list[str]


class SubjectReason(ContractModel):
    kind: Literal["metadata_name_match", "exact_mention", "document_inheritance"]
    metadata_field: Literal["title", "heading_path"] | None = None
    entity_id: str | None = None
    mention_id: str | None = None
    anchor_id: str | None = None
    distance: int | None = Field(default=None, ge=0, le=2)
    scope: Literal["direct", "graph_expanded"] = "direct"
    supporting_edge_ids: list[str] = Field(default_factory=list)


class ExplainedSearchHit(ContractModel):
    position: int
    record_id: str
    record_type: Literal["passage"] = "passage"
    title: str | None
    event_time: str | None
    source_path: str
    source_revision_id: str
    anchor_id: str
    heading_path: list[str]
    related_entity_ids: list[str]
    rank: float
    subject_reasons: list[SubjectReason]
    quote: str | None = None


class SearchExplanation(ContractModel):
    report_version: Literal["1"] = "1"
    corpus_id: str
    corpus_fingerprint: str
    active_current_revisions_only: Literal[True] = True
    supersession_filter_applied: Literal[False] = Field(
        default=False,
        description="Matching source evidence can include explicitly superseded records.",
    )
    query: str
    query_mode: Literal["strict", "natural"]
    lexical_expression: str
    lexical_table: str
    lexical_index_version: str = LEXICAL_INDEX_VERSION
    lexical_match_fields: list[str] = Field(
        default_factory=lambda: ["title", "heading_text", "passage_text", "aliases"]
    )
    rank_semantics: str = "SQLite FTS5 BM25; lower is better; not confidence"
    tie_breakers: list[str] = Field(
        default_factory=lambda: ["source_path", "start_offset", "passage_id"]
    )
    filters: SearchFilters
    subject_scope: list[ScopeEntity]
    max_graph_distance: Literal[2] = 2
    graph_direction: Literal["undirected"] = "undirected"
    graph_path_policy: str = "One deterministic shortest supporting path per scoped entity"
    quotes_included: bool
    hits: list[ExplainedSearchHit]


def explain_search(
    database: Database,
    corpus_id: str,
    query: str,
    subject: str | None = None,
    limit: int = 20,
    since: datetime | None = None,
    source_path: str | None = None,
    query_mode: Literal["strict", "natural"] = "strict",
    *,
    include_quotes: bool = False,
) -> SearchExplanation:
    """Run ordinary lexical search and explain its actual eligibility predicates.

    The service uses multiple read connections. A persistent observer detects any
    intervening commit (including edit-and-restore), rather than attributing hits
    to a potentially different graph. This conservatively rejects unrelated
    corpus writes too; callers can retry. Attribution itself uses one snapshot.
    """
    if query_mode not in {"strict", "natural"}:
        raise SearchExplanationError(
            "unsupported_explanation_mode", "--explain supports only strict and natural search"
        )
    expression = _fts_expression(query, query_mode)
    service = RetrievalService(database, corpus_id)
    with database.connection() as connection:
        version = connection.execute("PRAGMA data_version").fetchone()[0]
        results = service.search(query, subject, limit, since, source_path, query_mode)
        if connection.execute("PRAGMA data_version").fetchone()[0] != version:
            raise _state_changed()
        connection.execute("BEGIN")
        try:
            fingerprint = corpus_fingerprint(connection, corpus_id)
            scope = _scope_paths(service, connection, subject) if subject else {}
            hits = [
                _explain_hit(connection, result, position, subject, scope, include_quotes)
                for position, result in enumerate(results, start=1)
            ]
        finally:
            connection.rollback()
        if connection.execute("PRAGMA data_version").fetchone()[0] != version:
            raise _state_changed()
    return SearchExplanation(
        corpus_id=corpus_id,
        corpus_fingerprint=fingerprint,
        query=query,
        query_mode=query_mode,
        lexical_expression=expression,
        lexical_table=lexical_table(corpus_id),
        filters=SearchFilters(subject=subject, since=since, source_path=source_path, limit=limit),
        subject_scope=sorted(
            scope.values(), key=lambda entity: (entity.distance, entity.entity_id)
        ),
        quotes_included=include_quotes,
        hits=hits,
    )


def _state_changed() -> SearchExplanationError:
    return SearchExplanationError(
        "search_state_changed",
        "The indexed database changed during search explanation; retry the query.",
    )


def _scope_paths(
    service: RetrievalService, connection: sqlite3.Connection, subject: str
) -> dict[str, ScopeEntity]:
    scope = service._subject_scope(subject, connection)
    names = dict(zip(scope.entity_keys, scope.entity_ids, strict=True))
    roots = connection.execute(
        """
        SELECT e.entity_key FROM entity e
        JOIN entity_alias ea ON ea.entity_key = e.entity_key
        WHERE e.corpus_id = ? AND ea.normalized_alias = ?
        ORDER BY e.entity_id
        """,
        (service.corpus_id, subject.casefold()),
    ).fetchall()
    paths = {
        row["entity_key"]: ScopeEntity(
            entity_id=names[row["entity_key"]], distance=0, scope="direct",
            supporting_edge_ids=[], supporting_anchor_ids=[],
        )
        for row in roots
    }
    if not paths:
        return paths
    edges = connection.execute(
        """
        SELECT r.* FROM relationship r
        JOIN source_anchor sa ON sa.anchor_id = r.anchor_id
        JOIN source_revision sr ON sr.revision_id = sa.revision_id
        JOIN source_document sd ON sd.document_id = sr.document_id
        WHERE sd.corpus_id = ? AND sd.is_active = 1
          AND sr.revision_id = sd.current_revision_id
        ORDER BY r.relationship_id
        """,
        (service.corpus_id,),
    ).fetchall()
    adjacency: dict[str, list[tuple[str, str, str]]] = {}
    for edge in edges:
        source, target = edge["source_entity_key"], edge["target_entity_key"]
        for start, end in ((source, target), (target, source)):
            adjacency.setdefault(start, []).append(
                (end, edge["relationship_id"], edge["anchor_id"])
            )
    pending = deque(paths)
    while pending:
        key = pending.popleft()
        path = paths[key]
        if path.distance == 2:
            continue
        for neighbor, edge_id, anchor_id in adjacency.get(key, []):
            if neighbor not in names or neighbor in paths:
                continue
            paths[neighbor] = ScopeEntity(
                entity_id=names[neighbor], distance=path.distance + 1, scope="graph_expanded",
                supporting_edge_ids=[*path.supporting_edge_ids, edge_id],
                supporting_anchor_ids=[*path.supporting_anchor_ids, anchor_id],
            )
            pending.append(neighbor)
    if paths.keys() != names.keys():
        raise SearchExplanationError(
            "search_explanation_mismatch", "Subject traversal and explanation disagree."
        )
    return paths


def _explain_hit(
    connection: sqlite3.Connection,
    result: SearchResult,
    position: int,
    subject: str | None,
    scope: dict[str, ScopeEntity],
    include_quotes: bool,
) -> ExplainedSearchHit:
    reasons: list[SubjectReason] = []
    if subject:
        metadata = connection.execute(
            """
            SELECT kg_matches_alias(sd.title, ?) AS title,
                   kg_matches_alias(sa.heading_path_json, ?) AS heading_path
            FROM source_anchor sa
            JOIN source_revision sr ON sr.revision_id = sa.revision_id
            JOIN source_document sd ON sd.document_id = sr.document_id
            WHERE sa.anchor_id = ?
            """,
            (subject, subject, result.anchor_id),
        ).fetchone()
        if metadata is None:
            raise _state_changed()
        for field in ("title", "heading_path"):
            if metadata[field]:
                reasons.append(
                    SubjectReason(kind="metadata_name_match", metadata_field=field)
                )
        mentions = connection.execute(
            """
            SELECT m.mention_id, m.entity_key, m.anchor_id
            FROM mention m JOIN source_anchor sa ON sa.anchor_id = m.anchor_id
            WHERE sa.revision_id = ?
            ORDER BY m.anchor_id, m.entity_key, m.mention_id
            """,
            (result.source_revision_id,),
        ).fetchall()
        for mention in mentions:
            entity = scope.get(mention["entity_key"])
            if entity is None:
                continue
            reasons.append(
                SubjectReason(
                    kind=(
                        "exact_mention" if mention["anchor_id"] == result.anchor_id
                        else "document_inheritance"
                    ),
                    entity_id=entity.entity_id, mention_id=mention["mention_id"],
                    anchor_id=mention["anchor_id"], distance=entity.distance, scope=entity.scope,
                    supporting_edge_ids=entity.supporting_edge_ids,
                )
            )
        if not reasons:
            raise SearchExplanationError(
                "search_explanation_mismatch", "A search hit has no qualifying subject predicate."
            )
    return ExplainedSearchHit(
        position=position, record_id=result.record_id, title=result.title,
        event_time=result.event_time, source_path=result.source_path,
        source_revision_id=result.source_revision_id, anchor_id=result.anchor_id,
        heading_path=result.heading_path, related_entity_ids=result.related_entity_ids,
        rank=result.rank, subject_reasons=reasons, quote=result.quote if include_quotes else None,
    )


def render_search_explanation(report: SearchExplanation) -> str:
    lines = [
        f"Search explanation — corpus {report.corpus_id} (active current revisions only)",
        f"Mode: {report.query_mode}; FTS expression: {report.lexical_expression}",
        f"Filters: subject={report.filters.subject!r}, source={report.filters.source_path!r}, "
        f"since={report.filters.since}, limit={report.filters.limit}",
        f"Rank: {report.rank_semantics}",
        "Supersession filter: not applied; matching superseded source evidence remains eligible",
        "Subject scope: undirected, at most 2 hops; one shortest supporting path per entity",
        (
            "Quotes: included" if report.quotes_included
            else "Quotes: omitted; metadata is not anonymized"
        ),
    ]
    for entity in report.subject_scope:
        lines.append(
            f"  {entity.entity_id}: {entity.scope}, distance={entity.distance}, "
            f"edges={', '.join(entity.supporting_edge_ids) or 'none'}"
        )
    lines.append(f"Hits: {len(report.hits)}")
    for hit in report.hits:
        lines.append(
            f"{hit.position}. {hit.record_id} | {hit.source_path} | BM25={hit.rank}"
        )
        for reason in hit.subject_reasons:
            detail = (
                f"metadata field={reason.metadata_field}"
                if reason.kind == "metadata_name_match"
                else f"entity={reason.entity_id}, {reason.scope}, distance={reason.distance}, "
                f"mention={reason.mention_id}, anchor={reason.anchor_id}, "
                f"edges={', '.join(reason.supporting_edge_ids) or 'none'}"
            )
            lines.append(f"   {reason.kind}: {detail}")
        if hit.quote is not None:
            lines.append(f"   Quote: {hit.quote}")
    return "\n".join(lines)
