from __future__ import annotations

import json
import sqlite3
from typing import Literal

from kg.models.contracts import (
    IngestAnchorExplanation,
    IngestDocumentReport,
    IngestMentionedEntity,
    IngestRecordCounts,
    IngestRecordExplanation,
    IngestReport,
)


def explain_document(
    connection: sqlite3.Connection,
    document: IngestDocumentReport,
    *,
    include_quotes: bool,
    detail_limit: int,
) -> None:
    revision = document.source_revision_id
    if revision is None or not document.active:
        return
    counts = IngestRecordCounts()
    counts.passages = connection.execute(
        """
        SELECT count(*) FROM passage p
        JOIN source_anchor sa ON sa.anchor_id = p.anchor_id
        WHERE sa.revision_id = ?
        """,
        (revision,),
    ).fetchone()[0]
    counts.anchors = connection.execute(
        "SELECT count(*) FROM source_anchor WHERE revision_id = ?", (revision,)
    ).fetchone()[0]
    for table, field in (
        ("mention", "mentions"), ("relationship", "relationships"),
        ("action_item", "actions"), ("decision", "decisions"),
        ("blocker", "blockers"), ("conflict", "conflicts"),
    ):
        count = connection.execute(
            f"""
            SELECT count(*) FROM {table} r
            JOIN source_anchor sa ON sa.anchor_id = r.anchor_id
            WHERE sa.revision_id = ?
            """,
            (revision,),
        ).fetchone()[0]
        setattr(counts, field, count)
    document.counts = counts
    quote_column = "sa.quote" if include_quotes else "NULL"
    anchor_rows = connection.execute(
        f"""
        SELECT p.passage_id, sa.anchor_id, sa.anchor_kind, sa.heading_path_json,
               sa.start_offset, sa.end_offset, {quote_column} AS quote
        FROM passage p JOIN source_anchor sa ON sa.anchor_id = p.anchor_id
        WHERE sa.revision_id = ?
        ORDER BY sa.start_offset, sa.end_offset, sa.anchor_id
        LIMIT ?
        """,
        (revision, detail_limit),
    ).fetchall()
    document.anchors = [
        IngestAnchorExplanation(
            passage_id=row["passage_id"], anchor_id=row["anchor_id"],
            anchor_kind=row["anchor_kind"],
            heading_path=json.loads(row["heading_path_json"]),
            start_offset=row["start_offset"], end_offset=row["end_offset"],
            quote=row["quote"],
        )
        for row in anchor_rows
    ]
    document.anchors_truncated = counts.passages > len(document.anchors)
    mentions = connection.execute(
        """
        SELECT e.entity_id, e.canonical_name, e.entity_type, m.anchor_id
        FROM mention m
        JOIN entity e ON e.entity_key = m.entity_key
        JOIN source_anchor sa ON sa.anchor_id = m.anchor_id
        WHERE sa.revision_id = ?
        ORDER BY e.entity_id, sa.start_offset, m.start_offset, m.mention_id
        """,
        (revision,),
    ).fetchall()
    entities: dict[str, IngestMentionedEntity] = {}
    for mention in mentions:
        entity_id = mention["entity_id"]
        if entity_id not in entities:
            entities[entity_id] = IngestMentionedEntity(
                entity_id=entity_id,
                name=mention["canonical_name"],
                entity_type=mention["entity_type"],
                mention_count=0,
                anchor_ids=[],
            )
        entity = entities[entity_id]
        entity.mention_count += 1
        if mention["anchor_id"] not in entity.anchor_ids:
            entity.anchor_ids.append(mention["anchor_id"])
    document.mentioned_entities = list(entities.values())
    document.details_total = (
        counts.relationships + counts.actions + counts.decisions
        + counts.blockers + counts.conflicts
    )
    # Fetch bounded candidates per kind, then select the global source-ordered window.
    kinds: tuple[
        tuple[str, Literal["relationship", "action", "decision", "blocker", "conflict"], str],
        ...,
    ] = (
        ("relationship", "relationship", "explicit_wikilink"),
        ("action_item", "action", "checkbox_task"),
        ("decision", "decision", "decision_heading"),
        ("blocker", "blocker", "blocker_heading"),
        ("conflict", "conflict", "conflict_heading"),
    )
    details: list[IngestRecordExplanation] = []
    for table, kind, rule in kinds:
        record_column = "r.relationship_id" if kind == "relationship" else "r.record_id"
        fields = "NULL AS source_entity_id, NULL AS target_entity_id"
        joins = ""
        if kind == "relationship":
            fields = "s.entity_id AS source_entity_id, t.entity_id AS target_entity_id"
            joins = (
                "JOIN entity s ON s.entity_key = r.source_entity_key "
                "JOIN entity t ON t.entity_key = r.target_entity_key"
            )
        fields += (
            ", r.status, r.owner, r.due_date"
            if kind == "action"
            else ", NULL AS status, NULL AS owner, NULL AS due_date"
        )
        fields += ", binding.record_key, binding.supersedes_key"
        joins += f" LEFT JOIN record_binding binding ON binding.record_id = {record_column}"
        rows = connection.execute(
            f"""
            SELECT {record_column} AS record_id, sa.anchor_id, sa.heading_path_json,
                   sa.start_offset, sa.end_offset, {quote_column} AS quote, {fields}
            FROM {table} r
            JOIN source_anchor sa ON sa.anchor_id = r.anchor_id
            {joins}
            WHERE sa.revision_id = ?
            ORDER BY sa.start_offset, sa.end_offset, {record_column}
            LIMIT ?
            """,
            (revision, detail_limit),
        ).fetchall()
        details.extend(
            IngestRecordExplanation(
                record_id=row["record_id"], record_type=kind, rule=rule,
                anchor_id=row["anchor_id"],
                heading_path=json.loads(row["heading_path_json"]),
                start_offset=row["start_offset"], end_offset=row["end_offset"],
                source_entity_id=row["source_entity_id"],
                target_entity_id=row["target_entity_id"], status=row["status"],
                owner=row["owner"], due_date=row["due_date"], quote=row["quote"],
                record_key=row["record_key"], supersedes_key=row["supersedes_key"],
            )
            for row in rows
        )
    document.details = sorted(
        details, key=lambda item: (item.start_offset, item.end_offset, item.record_id)
    )[:detail_limit]
    document.details_truncated = document.details_total > len(document.details)


def render_ingest_report(report: IngestReport) -> str:
    lines = [
        f"Ingestion: {report.corpus_id}",
        f"Run: {report.run_id}",
        f"{report.added} added, {report.changed} changed, {report.unchanged} unchanged, "
        f"{report.failed} failed; {report.missing} unmatched patterns",
        "Configured entities (from manifest, not discovered): "
        + (", ".join(f"{entity.name} [{entity.entity_id}]" for entity in report.configured_entities)
           or "none"),
    ]
    for document in report.documents:
        lines.extend((
            "",
            f"{document.source_path} -- {document.outcome.upper()}",
            f"  Why: {', '.join(document.reasons)}",
            f"  Revision: {document.revision_state}; "
            f"records rebuilt: {'yes' if document.records_rebuilt else 'no'}; "
            f"active: {'yes' if document.active else 'no'}",
        ))
        if document.previous_source_path:
            lines.append(f"  Previous path: {document.previous_source_path}")
        if document.source_revision_id:
            lines.append(f"  Stored revision ID: {document.source_revision_id}")
        if document.error:
            lines.append(f"  Error: {document.error}")
        if document.counts is not None:
            lines.append("  Stored current-revision counts (not newly created counts):")
            lines.append(
                "    " + ", ".join(
                    f"{value} {key}" for key, value in document.counts.model_dump().items()
                )
            )
            lines.append(
                "  Entities mentioned (approved names/aliases): "
                + (", ".join(
                    f"{entity.name} ({entity.mention_count})"
                    for entity in document.mentioned_entities
                ) or "none")
            )
        if document.anchors:
            lines.append("  Indexed anchors (CommonMark blocks, source order):")
        for anchor in document.anchors:
            lines.append(
                f"    {anchor.anchor_kind} [{anchor.start_offset}:{anchor.end_offset}] "
                f"{anchor.anchor_id}"
            )
            if anchor.quote is not None:
                lines.append(f"      Evidence: {anchor.quote}")
        if document.anchors_truncated and document.counts is not None:
            lines.append(
                f"  Anchors truncated: {len(document.anchors)} of {document.counts.passages}; "
                "increase --explain-limit."
            )
        for detail in document.details:
            description: str = detail.record_type
            if detail.record_type == "relationship":
                description += (
                    f": {detail.source_entity_id} --wikilink--> {detail.target_entity_id}"
                )
            if detail.record_type == "action":
                description += (
                    f": {detail.status}, owner={detail.owner or 'unassigned'}, "
                    f"due={detail.due_date or 'unspecified'}"
                )
            lines.append(f"    {description} [{detail.rule}]")
            if detail.record_key is not None:
                lines.append(f"      Key: {detail.record_key}")
            if detail.supersedes_key is not None:
                lines.append(f"      Supersedes: {detail.supersedes_key}")
            lines.append(f"      Record: {detail.record_id}")
            lines.append(f"      Anchor: {detail.anchor_id}")
            if detail.quote is not None:
                lines.append(f"      Evidence: {detail.quote}")
        if document.details_truncated:
            lines.append(
                f"  Details truncated: {len(document.details)} of {document.details_total}; "
                "increase --explain-limit or use evidence/source-range."
            )
    for pattern in report.unmatched_patterns:
        lines.append(f"Unmatched pattern: {pattern}")
    if report.record_state is not None:
        lines.append("")
        lines.append("Cross-document state (resolved after all source updates):")
        for link in report.record_state.supersessions:
            lines.append(
                f"  {link.source.source_path} [{link.source.record_id}] "
                f"supersedes {link.target_key}: {link.resolution}"
            )
            if link.target is not None:
                lines.append(f"    Target: {link.target.source_path} [{link.target.record_id}]")
        if report.record_state.truncated:
            lines.append(
                f"  State links truncated: {len(report.record_state.supersessions)} "
                f"of {report.record_state.supersessions_total}"
            )
        for warning in report.record_state.warnings:
            lines.append(f"  Warning: {warning}")
    return "\n".join(lines)
