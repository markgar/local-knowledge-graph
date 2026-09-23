"""Model-free completeness inspection; durable completeness is not model usability."""

import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager, nullcontext
from typing import Literal

from pydantic import ValidationError

from kg._execution_budget import Deadline, DeadlineStop, PrivateBudget, PrivateResourceStop
from kg.evidence._authorization import authorize
from kg.evidence._reads import scoped_document
from kg.evidence._sql import AccountedConnection
from kg.evidence._values import sha
from kg.evidence.errors import EvidenceServiceError
from kg.ids import digest
from kg.indexing import _configuration, _storage, _vectors
from kg.models.evidence import LocalIdentity
from kg.models.foundation import Scope, SourceMetadata
from kg.models.indexing import (
    DEFAULT_CONFIGURATION,
    AttemptView,
    ExecutionIdentity,
    IndexConfiguration,
    IndexReason,
    IndexStatus,
)


def representation(quote: str, title: str, configuration: IndexConfiguration) -> str:
    return title + "\n\n" + quote if configuration.contextual else quote


@contextmanager
def title(
    connection: sqlite3.Connection,
    document_id: str,
    state_version: str,
    budget: PrivateBudget,
) -> Iterator[str]:
    size = connection.execute(
        "SELECT length(CAST(m.metadata_json AS BLOB)) FROM document_state s "
        "JOIN metadata_snapshot m ON m.snapshot_id=s.metadata_snapshot_id "
        "WHERE s.document_id=? AND s.state_version=?",
        (document_id, state_version),
    ).fetchone()
    if size is None:
        raise EvidenceServiceError("internal_error")
    with budget.reserve_scratch(max(4096, size[0] * 12), "general"):
        row = connection.execute(
            "SELECT m.metadata_json FROM document_state s "
            "JOIN metadata_snapshot m ON m.snapshot_id=s.metadata_snapshot_id "
            "WHERE s.document_id=? AND s.state_version=?",
            (document_id, state_version),
        ).fetchone()
        if row is None:
            raise EvidenceServiceError("internal_error")
        metadata = SourceMetadata.model_validate_json(row[0])
        yield metadata.title


def member_hash(row: sqlite3.Row) -> str:
    return digest(
        "e3-projection-member/1",
        row["passage_id"],
        str(row["ordinal"]),
        sha(row["lexical_input"].encode()),
        row["representation_hash"],
        row["vector_hash"],
        str(row["dimensions"]),
    )


def members(
    connection: sqlite3.Connection,
    projection_id: str,
    budget: PrivateBudget,
) -> Iterator[sqlite3.Row]:
    cursor = connection.execute(
        "SELECT ordinal,length(CAST(lexical_input AS BLOB)),length(vector),dimensions "
        "FROM projection_member WHERE projection_id=? ORDER BY ordinal",
        (projection_id,),
    )
    for size in cursor:
        if size[1] > 5_000_000 or size[2] != size[3] * 4 or not 1 <= size[3] <= 1024:
            raise EvidenceServiceError("internal_error")
        with budget.reserve_scratch(max(1, size[1] * 16 + size[2] * 12 + 4096), "general"):
            row = connection.execute(
                "SELECT m.*,a.quote FROM projection_member m JOIN passage p "
                "ON p.passage_id=m.passage_id JOIN anchor a ON a.anchor_id=p.anchor_id "
                "WHERE m.projection_id=? AND m.ordinal=?",
                (projection_id, size[0]),
            ).fetchone()
            if row is None:
                raise EvidenceServiceError("internal_error")
            yield row


def verify(
    connection: sqlite3.Connection,
    projection: sqlite3.Row,
    configuration: IndexConfiguration,
    budget: PrivateBudget,
) -> ExecutionIdentity:
    try:
        identity = ExecutionIdentity.model_validate_json(projection["execution_identity_json"])
    except ValidationError:
        raise EvidenceServiceError("internal_error") from None
    if _configuration.identity_hash(identity) != projection[
        "execution_identity_hash"
    ] or not _configuration.matches(configuration, identity):
        raise EvidenceServiceError("internal_error")
    configured = _configuration.descriptor(configuration)
    stored = connection.execute(
        "SELECT descriptor_json,descriptor_hash FROM index_configuration WHERE configuration_id=?",
        (projection["configuration_id"],),
    ).fetchone()
    if (
        projection["configuration_id"] != _configuration.configuration_id(configuration)
        or stored is None
        or tuple(stored) != (configured, sha(configured.encode()))
    ):
        raise EvidenceServiceError("internal_error")
    mapping = connection.execute(
        "SELECT s.passage_set_id,p.member_count FROM state_passage_set s JOIN passage_set p "
        "ON p.passage_set_id=s.passage_set_id WHERE s.state_version=?",
        (projection["state_version"],),
    ).fetchone()
    if (
        mapping is None
        or mapping[0] != projection["passage_set_id"]
        or mapping[1] != projection["lexical_count"]
        or mapping[1] != projection["vector_count"]
        or not 0 <= mapping[1] <= 4883
    ):
        raise EvidenceServiceError("internal_error")
    hashes = []
    with (
        title(connection, projection["document_id"], projection["state_version"], budget)
        if configuration.contextual
        else nullcontext("")
    ) as title_text:
        for ordinal, row in enumerate(members(connection, projection["projection_id"], budget), 1):
            expected = connection.execute(
                "SELECT passage_id FROM passage_set_member WHERE passage_set_id=? AND ordinal=?",
                (projection["passage_set_id"], ordinal),
            ).fetchone()
            if (
                row["ordinal"] != ordinal
                or expected is None
                or expected[0] != row["passage_id"]
                or row["lexical_input"] != row["quote"]
                or row["dimensions"] != identity.dimensions
                or sha(row["vector"]) != row["vector_hash"]
                or sha(representation(row["quote"], title_text, configuration).encode())
                != row["representation_hash"]
            ):
                raise EvidenceServiceError("internal_error")
            try:
                _vectors.validate(row["vector"], row["dimensions"])
            except _vectors.InvalidVector:
                raise EvidenceServiceError("internal_error") from None
            hashes.append(member_hash(row))
    if (
        len(hashes) != mapping[1]
        or digest("e3-projection/1", *hashes) != projection["manifest_hash"]
    ):
        raise EvidenceServiceError("internal_error")
    return identity


def effective_default(
    connection: sqlite3.Connection,
    doc: sqlite3.Row,
    state: sqlite3.Row,
) -> tuple[
    Literal["pending", "ready", "failed"],
    IndexReason | Literal["ready", "processor_not_available"],
]:
    """Historical completion stays historical; current default readiness is verified."""
    if state["state_version"] != doc["current_state"]:
        return state["indexing"], state["indexing_reason"]
    if state["source"] != "active":
        return "pending", "inactive"
    _, policy_current = _storage.current_source(connection, doc)
    if not policy_current:
        return "pending", "state_changed"
    projection = connection.execute(
        "SELECT p.* FROM active_document_projection a JOIN document_projection p "
        "ON p.projection_id=a.projection_id WHERE a.corpus_id=? AND a.document_id=? "
        "AND a.configuration_id=? AND p.state_version=?",
        (
            doc["corpus_id"],
            doc["document_id"],
            _configuration.configuration_id(DEFAULT_CONFIGURATION),
            state["state_version"],
        ),
    ).fetchone()
    if projection is None:
        return (
            ("pending", "not_processed")
            if state["indexing"] == "ready"
            else (state["indexing"], state["indexing_reason"])
        )
    budget = connection._budget if isinstance(connection, AccountedConnection) else None
    budget = budget if budget is not None else PrivateBudget(Deadline(time.monotonic() + 30))
    try:
        verify(connection, projection, DEFAULT_CONFIGURATION, budget)
    except (DeadlineStop, PrivateResourceStop):
        raise EvidenceServiceError("budget_exceeded") from None
    return "ready", "ready"


def status(
    connection: sqlite3.Connection,
    identity: LocalIdentity,
    scope: Scope,
    document_id: str,
    configuration: IndexConfiguration,
    budget: PrivateBudget,
) -> IndexStatus:
    authorize(connection, identity, scope, "read")
    doc = scoped_document(connection, scope, document_id)
    state, policy_current = _storage.current_source(connection, doc)
    config_id = _configuration.configuration_id(configuration)
    mapping = connection.execute(
        "SELECT passage_set_id FROM state_passage_set WHERE state_version=?",
        (state["state_version"],),
    ).fetchone()
    latest = connection.execute(
        "SELECT a.* FROM index_work_slot s "
        "JOIN index_attempt a ON a.attempt_id=s.latest_attempt_id "
        "WHERE s.corpus_id=? AND s.document_id=? AND s.configuration_id=?",
        (scope.corpus_id, document_id, config_id),
    ).fetchone()
    attempt = (
        None
        if latest is None
        else AttemptView(
            attempt_id=latest["attempt_id"],
            state_version=latest["state_version"],
            status=latest["status"],
            reason=latest["reason"],
            execution_identity=ExecutionIdentity.model_validate_json(
                latest["execution_identity_json"]
            )
            if latest["execution_identity_json"] is not None
            else None,
        )
    )
    projection = _storage.active(connection, scope, document_id, config_id)
    ready = (
        state["source"] == "active"
        and policy_current
        and projection is not None
        and projection["state_version"] == state["state_version"]
    )
    observed = (
        verify(connection, projection, configuration, budget)
        if ready and projection is not None
        else None
    )
    failed = (
        attempt is not None
        and attempt.state_version == state["state_version"]
        and attempt.status == "failed"
    )
    return IndexStatus(
        document_id=document_id,
        state_version=state["state_version"],
        configuration_id=config_id,
        source=state["source"],
        status="inactive"
        if state["source"] != "active"
        else "ready"
        if ready
        else "failed"
        if failed and policy_current
        else "pending",
        passages="complete" if mapping is not None else "not_processed",
        lexical="complete" if ready else "not_processed",
        vectors="complete" if ready else "not_processed",
        passage_set_id=mapping[0] if mapping is not None else None,
        projection_id=projection["projection_id"] if ready and projection is not None else None,
        execution_identity=observed,
        latest_attempt=attempt,
        reason="inactive"
        if state["source"] != "active"
        else "state_changed"
        if not policy_current
        else None
        if ready
        else attempt.reason
        if failed and attempt is not None
        else "not_processed",
    )
