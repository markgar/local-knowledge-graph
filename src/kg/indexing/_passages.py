"""Private canonical passage kernel; not an indexing/readiness service."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Literal

from kg.evidence import _store
from kg.evidence._authorization import authorize, authorize_writer
from kg.evidence._reads import check_token, scoped_document
from kg.evidence._transactions import CanonicalWriteContext, writing
from kg.evidence._values import sha, validated
from kg.evidence.database import EvidenceDatabase
from kg.evidence.errors import EvidenceServiceError
from kg.ids import digest
from kg.models.evidence import LocalIdentity
from kg.models.foundation import Attribution, ExternalDocument, Scope


class PassagePolicyError(EvidenceServiceError):
    def __init__(self, detail: Literal["unsupported_policy", "boundaries_required"]) -> None:
        self.detail = detail
        super().__init__("unsupported")


@dataclass(frozen=True)
class Boundary:
    anchor_id: str
    local_id: str
    start: int
    end: int
    quote: str
    generated: bool


@dataclass(frozen=True)
class PreparedPassages:
    document_id: str
    revision_id: str
    state_version: str
    namespace: str
    policy: str
    definition: str
    definition_hash: str
    passage_set_id: str
    manifest_hash: str
    boundaries: tuple[Boundary, ...]


@dataclass(frozen=True)
class PassagePublication:
    passage_set_id: str
    member_count: int
    outcome: Literal["produced", "reused"]


def _authorized_state(
    connection: sqlite3.Connection,
    identity: LocalIdentity,
    scope: Scope,
    attribution: Attribution,
    document_id: str,
    expected_state: str,
) -> sqlite3.Row:
    authorize(connection, identity, scope, "read")
    doc = scoped_document(connection, scope, document_id)
    authorize_writer(
        connection,
        identity,
        scope,
        attribution,
        ExternalDocument(
            source_namespace=doc["namespace"],
            synchronization_scope=doc["synchronization_scope"],
            external_id=doc["external_id"],
        ),
    )
    if doc["owner_id"] != attribution.owner_id:
        raise EvidenceServiceError("forbidden")
    state = _store.head(connection, document_id)
    namespace = connection.execute(
        "SELECT policy_token FROM source_namespace WHERE corpus_id=? AND namespace=?",
        (scope.corpus_id, doc["namespace"]),
    ).fetchone()
    if (
        state["state_version"] != expected_state
        or state["source"] != "active"
        or namespace is None
        or namespace[0] != state["namespace_token"]
    ):
        raise EvidenceServiceError("state_conflict")
    return state


def prepare(
    database: EvidenceDatabase,
    identity: LocalIdentity,
    scope: Scope,
    attribution: Attribution,
    document_id: str,
    expected_state: str,
) -> PreparedPassages:
    identity, scope = validated(LocalIdentity, identity), validated(Scope, scope)
    attribution = validated(Attribution, attribution)
    check_token(document_id)
    check_token(expected_state)
    with database.connection() as connection:
        connection.execute("BEGIN")
        state = _authorized_state(
            connection,
            identity,
            scope,
            attribution,
            document_id,
            expected_state,
        )
        policy = state["passage_policy"]
        if policy not in ("codepoint-window/1", "supplied-anchors/1"):
            raise PassagePolicyError("unsupported_policy")
        revision = state["revision_id"]
        content = _store.content_bytes(connection, document_id, revision)
        if len(content) > 5_000_000:
            raise EvidenceServiceError("internal_error")
        text = content.decode()
        definition = json.dumps(
            {"policy": policy, "order": "start,end,local_id"}
            if policy == "supplied-anchors/1"
            else {"policy": policy, "window_codepoints": 1024},
            sort_keys=True,
            separators=(",", ":"),
        )
        definition_hash = digest("e3-policy/1", definition)
        boundaries = []
        if policy == "codepoint-window/1":
            for start in range(0, len(text), 1024):
                end = min(start + 1024, len(text))
                quote = text[start:end]
                anchor_id = digest(
                    "e3-generated-anchor/1",
                    revision,
                    definition_hash,
                    str(start),
                    str(end),
                    quote,
                )
                boundaries.append(
                    Boundary(
                        anchor_id,
                        f"e3-window-{start}",
                        start,
                        end,
                        quote,
                        True,
                    )
                )
        else:
            rows = connection.execute(
                "SELECT a.* FROM anchor_set_member m JOIN anchor a "
                "ON a.anchor_id=m.anchor_id AND a.revision_id=m.revision_id "
                "WHERE m.set_id=? ORDER BY a.start_offset,a.end_offset,a.local_id LIMIT 1001",
                (state["set_id"],),
            ).fetchall()
            if len(rows) > 1000:
                raise EvidenceServiceError("internal_error")
            for row in rows:
                if (
                    row["document_id"] != document_id
                    or row["revision_id"] != revision
                    or row["origin_kind"] != "supplied"
                    or row["start_offset"] < 0
                    or not row["start_offset"] < row["end_offset"] <= len(text)
                    or text[row["start_offset"] : row["end_offset"]] != row["quote"]
                    or sha(row["quote"].encode()) != row["quote_hash"]
                ):
                    raise EvidenceServiceError("internal_error")
                boundaries.append(
                    Boundary(
                        row["anchor_id"],
                        row["local_id"],
                        row["start_offset"],
                        row["end_offset"],
                        row["quote"],
                        False,
                    )
                )
            if text and not boundaries:
                raise PassagePolicyError("boundaries_required")
        manifest = digest(
            "e3-boundaries/1",
            *(
                digest("e3-boundary/1", b.anchor_id, str(b.start), str(b.end), b.quote)
                for b in boundaries
            ),
        )
        set_id = digest("e3-passage-set/1", revision, definition_hash, manifest)
        namespace = scoped_document(connection, scope, document_id)["namespace"]
        return PreparedPassages(
            document_id,
            revision,
            expected_state,
            namespace,
            policy,
            definition,
            definition_hash,
            set_id,
            manifest,
            tuple(boundaries),
        )


def publish(
    context: CanonicalWriteContext,
    scope: Scope,
    attribution: Attribution,
    prepared: PreparedPassages,
) -> PassagePublication:
    """Compare or publish the entire set inside the caller's live owner transaction."""
    scope, attribution = validated(Scope, scope), validated(Attribution, attribution)
    connection = context.connection
    state = _authorized_state(
        connection,
        context.identity,
        scope,
        attribution,
        prepared.document_id,
        prepared.state_version,
    )
    doc = scoped_document(connection, scope, prepared.document_id)
    if (
        state["revision_id"] != prepared.revision_id
        or state["passage_policy"] != prepared.policy
        or doc["namespace"] != prepared.namespace
    ):
        raise EvidenceServiceError("state_conflict")
    definition = connection.execute(
        "SELECT definition_hash,definition_json FROM passage_policy_definition "
        "WHERE policy_token=?",
        (prepared.policy,),
    ).fetchone()
    if definition is None:
        connection.execute(
            "INSERT INTO passage_policy_definition VALUES (?,?,?)",
            (prepared.policy, prepared.definition_hash, prepared.definition),
        )
    elif tuple(definition) != (prepared.definition_hash, prepared.definition):
        raise EvidenceServiceError("internal_error")
    chain = (
        scope.corpus_id,
        prepared.namespace,
        prepared.document_id,
        prepared.revision_id,
    )
    old_set = connection.execute(
        "SELECT * FROM passage_set WHERE passage_set_id=?",
        (prepared.passage_set_id,),
    ).fetchone()
    expected = (
        *chain,
        prepared.policy,
        prepared.definition_hash,
        prepared.manifest_hash,
        len(prepared.boundaries),
    )
    if old_set is None:
        connection.execute(
            "INSERT INTO passage_set VALUES (?,?,?,?,?,?,?,?,?,?)",
            (prepared.passage_set_id, *expected, prepared.state_version),
        )
    elif (
        tuple(
            old_set[k]
            for k in (
                "corpus_id",
                "namespace",
                "document_id",
                "revision_id",
                "policy_token",
                "definition_hash",
                "manifest_hash",
                "member_count",
            )
        )
        != expected
    ):
        raise EvidenceServiceError("internal_error")
    inventory_ordinal = connection.execute(
        "SELECT coalesce(max(ordinal),0) FROM anchor WHERE revision_id=?",
        (prepared.revision_id,),
    ).fetchone()[0]
    passage_ids = []
    for ordinal, boundary in enumerate(prepared.boundaries, 1):
        row = connection.execute(
            "SELECT * FROM anchor WHERE anchor_id=?",
            (boundary.anchor_id,),
        ).fetchone()
        anchor_values = (
            prepared.document_id,
            prepared.revision_id,
            boundary.local_id,
            boundary.start,
            boundary.end,
            boundary.quote,
            sha(boundary.quote.encode()),
            "generated" if boundary.generated else "supplied",
            boundary.anchor_id,
        )
        if row is None and boundary.generated and old_set is None:
            inventory_ordinal += 1
            connection.execute(
                "INSERT INTO anchor(anchor_id,document_id,revision_id,local_id,start_offset,"
                "end_offset,quote,quote_hash,origin_kind,origin_key,ordinal,origin_state) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (boundary.anchor_id, *anchor_values, inventory_ordinal, prepared.state_version),
            )
        elif (
            row is None
            or tuple(
                row[k]
                for k in (
                    "document_id",
                    "revision_id",
                    "local_id",
                    "start_offset",
                    "end_offset",
                    "quote",
                    "quote_hash",
                    "origin_kind",
                    "origin_key",
                )
            )
            != anchor_values
        ):
            raise EvidenceServiceError("internal_error")
        passage_id = digest(
            "e3-passage/1",
            prepared.passage_set_id,
            str(ordinal),
            boundary.anchor_id,
        )
        passage_ids.append((passage_id, ordinal))
        values = (*chain, prepared.passage_set_id, boundary.anchor_id, ordinal)
        if old_set is None:
            connection.execute(
                "INSERT INTO passage VALUES (?,?,?,?,?,?,?,?,?)",
                (passage_id, *values, prepared.state_version),
            )
            connection.execute(
                "INSERT INTO passage_set_member VALUES (?,?,?)",
                (prepared.passage_set_id, passage_id, ordinal),
            )
        else:
            row = connection.execute(
                "SELECT corpus_id,namespace,document_id,revision_id,"
                "passage_set_id,anchor_id,ordinal "
                "FROM passage WHERE passage_id=?",
                (passage_id,),
            ).fetchone()
            if row is None or tuple(row) != values:
                raise EvidenceServiceError("internal_error")
    members = connection.execute(
        "SELECT passage_id,ordinal FROM passage_set_member WHERE passage_set_id=? ORDER BY ordinal",
        (prepared.passage_set_id,),
    ).fetchall()
    if [tuple(row) for row in members] != passage_ids or connection.execute(
        "SELECT count(*) FROM passage WHERE passage_set_id=?",
        (prepared.passage_set_id,),
    ).fetchone()[0] != len(passage_ids):
        raise EvidenceServiceError("internal_error")
    mapping = connection.execute(
        "SELECT corpus_id,namespace,document_id,revision_id,passage_set_id FROM state_passage_set "
        "WHERE state_version=?",
        (prepared.state_version,),
    ).fetchone()
    if mapping is None:
        connection.execute(
            "INSERT INTO state_passage_set VALUES (?,?,?,?,?,?)",
            (*chain, prepared.state_version, prepared.passage_set_id),
        )
    elif tuple(mapping) != (*chain, prepared.passage_set_id):
        raise EvidenceServiceError("internal_error")
    return PassagePublication(
        prepared.passage_set_id,
        len(passage_ids),
        "reused" if mapping is not None else "produced",
    )


def produce(
    database: EvidenceDatabase,
    identity: LocalIdentity,
    scope: Scope,
    attribution: Attribution,
    document_id: str,
    expected_state: str,
) -> PassagePublication:
    prepared = prepare(database, identity, scope, attribution, document_id, expected_state)
    with writing(database, identity) as context:
        result = publish(context, scope, attribution, prepared)
    return result
