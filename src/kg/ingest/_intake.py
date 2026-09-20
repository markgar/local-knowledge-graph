"""Translate the existing local Markdown source into private persistence values."""

from __future__ import annotations

import re
from datetime import date, datetime
from pathlib import Path

from kg.config import read_source, relative_source_path
from kg.ingest._prepared import PreparedAnchor, PreparedDocument, PreparedTask
from kg.markdown import ParsedAnchor, parse_markdown
from kg.markdown.parser import INLINE_FIELD_RE
from kg.models.manifest import CorpusManifest


def prepare_document(manifest: CorpusManifest, path: Path) -> PreparedDocument:
    source_path = relative_source_path(manifest, path)
    source = read_source(manifest, path)
    parsed = parse_markdown(source.content.decode("utf-8"), path.stem)
    return PreparedDocument(
        source_path=source_path,
        content=source.content,
        observed_mtime=source.observed_mtime,
        title=parsed.title,
        metadata=parsed.frontmatter,
        event_time=_event_time(manifest, parsed.frontmatter),
        anchors=tuple(
            PreparedAnchor(
                structural_path=anchor.structural_path,
                heading_path=anchor.heading_path,
                kind=anchor.kind,
                start_offset=anchor.start_offset,
                end_offset=anchor.end_offset,
                quote=anchor.quote,
                mention_text=anchor.semantic_quote,
                metadata=anchor.metadata,
                task=PreparedTask(
                    text=anchor.task.text,
                    status=anchor.task.status,
                    owner=anchor.task.owner,
                    due_date=anchor.task.due_date,
                ) if anchor.task else None,
                record_type=anchor.record_type,
                record_text=(
                    _record_text(_without_state_fields(anchor))
                    if anchor.record_type in {"decision", "blocker", "conflict"} else ""
                ),
                linked_aliases=anchor.wikilinks,
                relationship_type="wikilink",
            )
            for anchor in parsed.anchors
        ),
    )


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


def _without_state_fields(anchor: ParsedAnchor) -> str:
    parts = []
    offset = 0
    for match in INLINE_FIELD_RE.finditer(anchor.semantic_quote):
        if match.group(1).casefold() in {"key", "supersedes"}:
            parts.append(anchor.quote[offset:match.start()])
            offset = match.end()
    parts.append(anchor.quote[offset:])
    return "".join(parts)


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
