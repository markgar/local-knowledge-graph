from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass
from functools import cached_property
from typing import Literal

from kg.models.contracts import RecordStateReport, StateEvidence, SupersessionExplanation

KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")


def validate_state_fields(
    metadata: Mapping[str, str], *, is_action: bool, record_type: str | None,
) -> None:
    fields = {
        name: value for name, value in metadata.items() if name in {"key", "supersedes"}
    }
    if not fields:
        return
    if (not is_action and record_type != "decision") or (
        is_action and record_type is not None
    ):
        raise ValueError("State fields require an action or decision, not an ambiguous record kind")
    for name, value in fields.items():
        if not KEY_RE.fullmatch(value):
            raise ValueError(
                f"Invalid {name}: use 1-128 ASCII letters, digits, dots, underscores, "
                "colons or hyphens"
            )


def bind_record(
    connection: sqlite3.Connection,
    metadata: Mapping[str, str],
    anchor_id: str,
    record_id: str,
    record_type: Literal["action", "decision"],
) -> None:
    key = metadata.get("key")
    supersedes = metadata.get("supersedes")
    if key is not None or supersedes is not None:
        connection.execute(
            """
            INSERT INTO record_binding
                (record_id, anchor_id, record_type, record_key, supersedes_key)
            VALUES (?, ?, ?, ?, ?)
            """,
            (record_id, anchor_id, record_type, key, supersedes),
        )


@dataclass(frozen=True)
class RecordState:
    corpus_id: str
    supersessions: list[SupersessionExplanation]
    warnings: list[str]
    successors: dict[str, str]

    @cached_property
    def effective_ids(self) -> dict[str, str]:
        endpoints: dict[str, str] = {}
        for start in self.successors:
            path = []
            current = start
            while current in self.successors and current not in endpoints:
                path.append(current)
                current = self.successors[current]
            endpoint = endpoints.get(current, current)
            for record_id in path:
                endpoints[record_id] = endpoint
        return endpoints

    def current_id(self, record_id: str) -> str:
        return self.effective_ids.get(record_id, record_id)

    def report(self, limit: int = 50) -> RecordStateReport:
        if not 1 <= limit <= 200:
            raise ValueError("state limit must be between 1 and 200")
        return RecordStateReport(
            corpus_id=self.corpus_id,
            supersessions=self.supersessions[:limit],
            supersessions_total=len(self.supersessions),
            truncated=len(self.supersessions) > limit,
            warnings=self.warnings,
            superseded_record_ids=sorted(self.successors),
        )


def resolve_record_state(
    connection: sqlite3.Connection, corpus_id: str, *, include_quotes: bool = False
) -> RecordState:
    quote_column = "sa.quote" if include_quotes else "NULL"
    rows = connection.execute(
        f"""
        SELECT b.*, sd.source_path, sr.revision_id, {quote_column} AS quote
        FROM record_binding b
        JOIN source_anchor sa ON sa.anchor_id = b.anchor_id
        JOIN source_revision sr ON sr.revision_id = sa.revision_id
        JOIN source_document sd ON sd.document_id = sr.document_id
        WHERE sd.corpus_id = ? AND sd.is_active = 1
          AND sr.revision_id = sd.current_revision_id
        ORDER BY sd.source_path, sa.start_offset, b.record_id
        """,
        (corpus_id,),
    ).fetchall()
    by_key: dict[str, list[StateEvidence]] = defaultdict(list)
    evidence = {}
    for row in rows:
        item = StateEvidence(
            record_id=row["record_id"], record_type=row["record_type"],
            key=row["record_key"], source_path=row["source_path"],
            source_revision_id=row["revision_id"], anchor_id=row["anchor_id"], quote=row["quote"],
        )
        evidence[item.record_id] = item
        if item.key is not None:
            by_key[item.key].append(item)
    warnings = [
        f"Duplicate active record key '{key}' ({len(items)} records); references are ambiguous"
        for key, items in sorted(by_key.items()) if len(items) > 1
    ]
    links = []
    for row in rows:
        if row["supersedes_key"] is None:
            continue
        source = evidence[row["record_id"]]
        targets = by_key.get(row["supersedes_key"], [])
        link = SupersessionExplanation(
            source=source, target_key=row["supersedes_key"], resolution="applied",
        )
        if not targets:
            link.resolution = "missing_target"
        elif len(targets) != 1:
            link.resolution = "ambiguous_target"
            link.candidates = targets
        else:
            link.target = targets[0]
            if link.target.record_id == source.record_id:
                link.resolution = "self_reference"
            elif link.target.record_type != source.record_type:
                link.resolution = "type_mismatch"
        links.append(link)

    candidates = {
        link.source.record_id: link.target.record_id
        for link in links if link.resolution == "applied" and link.target is not None
    }
    cyclic: set[str] = set()
    checked: set[str] = set()
    for start in candidates:
        path: set[str] = set()
        current = start
        while current in candidates and current not in checked and current not in path:
            path.add(current)
            current = candidates[current]
        if current in path or current in cyclic:
            cyclic.update(path)
        checked.update(path)
    incoming: dict[str, list[SupersessionExplanation]] = defaultdict(list)
    for link in links:
        if link.source.record_id in cyclic:
            link.resolution = "cycle"
        if link.resolution == "applied" and link.target is not None:
            incoming[link.target.record_id].append(link)
    for group in incoming.values():
        if len(group) > 1:
            for link in group:
                link.resolution = "competing_updates"
                link.conflicting_sources = [
                    other.source for other in group if other is not link
                ]
    successors = {}
    for link in links:
        if link.resolution == "applied" and link.target is not None:
            successors[link.target.record_id] = link.source.record_id
        else:
            warnings.append(
                f"{link.source.source_path} [{link.source.record_id}]: "
                f"supersedes '{link.target_key}' is {link.resolution}"
            )
    state = RecordState(corpus_id, links, warnings, successors)
    for link in links:
        if link.resolution == "applied":
            link.effective_record_id = state.current_id(link.source.record_id)
    return state
