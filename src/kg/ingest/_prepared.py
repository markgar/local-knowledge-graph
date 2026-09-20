"""Private intake values, not a supported document-write API."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class PreparedTask:
    text: str
    status: str
    owner: str | None
    due_date: str | None


@dataclass(frozen=True)
class PreparedAnchor:
    structural_path: str
    heading_path: tuple[str, ...]
    kind: str
    start_offset: int
    end_offset: int
    quote: str
    mention_text: str
    metadata: dict[str, str]
    task: PreparedTask | None
    record_type: str | None
    record_text: str
    linked_aliases: tuple[str, ...]
    relationship_type: str


@dataclass(frozen=True)
class PreparedDocument:
    source_path: str
    content: bytes
    observed_mtime: str
    title: str
    metadata: dict[str, object]
    event_time: str | None
    anchors: tuple[PreparedAnchor, ...]
