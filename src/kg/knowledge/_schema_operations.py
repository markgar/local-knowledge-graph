"""Canonical schema intake, proof validation and atomic administrative mutations."""

from __future__ import annotations

import json
from contextlib import closing
from typing import Literal

from pydantic import ValidationError

from kg._execution_budget import PrivateBudget
from kg.evidence._authorization import authorize
from kg.evidence._transactions import CanonicalWriteContext
from kg.evidence._values import canonical, now, sha, timestamp, token
from kg.evidence.errors import EvidenceServiceError, storage_error
from kg.knowledge._registry import (
    Registry,
    compare,
    definition_hash,
    definition_json,
    proposal_digest,
    proposal_json,
)
from kg.knowledge._store import Store
from kg.models.evidence import LocalIdentity
from kg.models.foundation import SchemaRevisionRef
from kg.models.schema import (
    MAX_SCHEMA_BYTES,
    SchemaApplyRequest,
    SchemaChangeView,
    SchemaDefinition,
    SchemaPresetRegistration,
    SchemaPresetRequest,
    SchemaProposal,
    SchemaReceipt,
    SchemaRevisionPage,
    SchemaValidation,
    SchemaView,
)


def view(store: Store, revision_id: str | None) -> SchemaView:
    head = store.registry.head()
    if head is None:
        if revision_id is not None:
            raise EvidenceServiceError("not_found")
        return SchemaView(status="unconfigured", head=None, revision=None, definition=None)
    summary, definition = store.registry.load(revision_id or head.revision_id)
    return SchemaView(
        status="configured",
        head=head,
        revision=summary.revision,
        sequence=summary.sequence,
        definition=definition,
    )


def history(store: Store, after: int, limit: int) -> SchemaRevisionPage:
    if type(after) is not int or after < 0 or type(limit) is not int or not 1 <= limit <= 100:
        raise EvidenceServiceError("invalid_request")
    head = store.registry.head()
    rows = store.connection.execute(
        "SELECT revision_id FROM knowledge_schema_revision "
        "WHERE corpus_id=? AND sequence>? ORDER BY sequence LIMIT ?",
        (store.scope.corpus_id, after, limit + 1),
    ).fetchall()
    entries = []
    for row in rows[:limit]:
        summary, definition = store.registry.load(row["revision_id"])
        kinds: list[str] = []
        previous = None
        if summary.parent_revision_id is not None:
            _, previous = store.registry.load(summary.parent_revision_id)
        if previous is None or definition.entity_types != previous.entity_types:
            kinds.append("entity_type")
        if previous is None or definition.identifier_schemes != previous.identifier_schemes:
            kinds.append("identifier_scheme")
        if previous is None or definition.predicates != previous.predicates:
            kinds.append("predicate")
        entries.append(summary.model_copy(update={"change_kinds": tuple(kinds)}))
    return SchemaRevisionPage(
        head=head,
        entries=tuple(entries),
        has_more=len(rows) > limit,
        next_after_sequence=entries[-1].sequence if len(rows) > limit else None,
    )


def examples(store: Store, proposal: SchemaProposal, *, current: bool) -> None:
    if proposal.corpus_id != store.scope.corpus_id:
        raise EvidenceServiceError("not_found")
    for example in proposal.examples:
        _, active = store.proof(example.reference, example.state_version)
        if current and not active:
            raise EvidenceServiceError("state_conflict")


def validate(store: Store, proposal: SchemaProposal) -> SchemaValidation:
    if proposal.corpus_id != store.scope.corpus_id:
        raise EvidenceServiceError("invalid_request")
    head = store.registry.head()
    if head != proposal.base_revision:
        raise EvidenceServiceError("state_conflict")
    store.hold(len(proposal.model_dump_json().encode()) * 16 + 4096)
    examples(store, proposal, current=True)
    definition = None if head is None else store.registry.load(head.revision_id)[1]
    try:
        result = compare(proposal, definition)
    except ValidationError:
        raise EvidenceServiceError("invalid_request") from None
    store.hold(len(result.model_dump_json().encode()) * 8)
    return result


def change(store: Store, identity: LocalIdentity, revision_id: str) -> SchemaChangeView:
    summary, definition = store.registry.load(revision_id)
    size = store.connection.execute(
        "SELECT length(CAST(change_json AS BLOB)) FROM knowledge_schema_change "
        "WHERE corpus_id=? AND revision_id=?",
        (store.scope.corpus_id, revision_id),
    ).fetchone()
    if size is None or size[0] > MAX_SCHEMA_BYTES + 32768:
        raise EvidenceServiceError("internal_error")
    store.hold(size[0] * 8)
    row = store.connection.execute(
        "SELECT * FROM knowledge_schema_change WHERE corpus_id=? AND revision_id=?",
        (store.scope.corpus_id, revision_id),
    ).fetchone()
    if row is None:
        raise EvidenceServiceError("internal_error")
    try:
        result = SchemaChangeView.model_validate_json(row["change_json"])
    except ValidationError as error:
        raise storage_error(error) from None
    if (
        sha(row["change_json"].encode()) != row["change_hash"]
        or result.revision != summary.revision
        or result.principal_id != row["principal_id"]
        or (result.proposal is None) == (result.preset is None)
    ):
        raise EvidenceServiceError("internal_error")
    if summary.origin == "trusted_preset":
        if result.preset is None or result.approval is not None:
            raise EvidenceServiceError("internal_error")
        if identity.principal_id != row["principal_id"]:
            raise EvidenceServiceError("not_found")
        if (
            result.preset.corpus_id != store.scope.corpus_id
            or summary.sequence != 1
            or definition_json(result.preset.definition) != definition_json(definition)
        ):
            raise EvidenceServiceError("internal_error")
    else:
        if result.proposal is None or result.approval is None:
            raise EvidenceServiceError("internal_error")
        examples(store, result.proposal, current=False)
        base = result.proposal.base_revision
        previous = None
        if base is not None:
            prior, previous = store.registry.load(base.revision_id)
            if prior.revision != base or prior.sequence + 1 != summary.sequence:
                raise EvidenceServiceError("internal_error")
        try:
            accepted = compare(result.proposal, previous)
        except EvidenceServiceError as error:
            raise EvidenceServiceError("internal_error") from error
        if (
            summary.parent_revision_id != (base.revision_id if base else None)
            or accepted.definition_hash != summary.revision.definition_hash
        ):
            raise EvidenceServiceError("internal_error")
    return result


def _insert(
    context: CanonicalWriteContext,
    corpus_id: str,
    revision: SchemaRevisionRef,
    definition: SchemaDefinition,
    parent: SchemaRevisionRef | None,
    sequence: int,
    origin: Literal["trusted_preset", "approved_proposal"],
    at: str,
    detail: SchemaChangeView,
) -> None:
    connection = context.connection
    connection.execute(
        "INSERT INTO knowledge_schema_revision VALUES (?,?,?,?,?,?,?,?)",
        (
            corpus_id,
            revision.revision_id,
            sequence,
            parent.revision_id if parent else None,
            definition_json(definition),
            revision.definition_hash,
            origin,
            at,
        ),
    )
    text = canonical(detail.model_dump(mode="json"))
    connection.execute(
        "INSERT INTO knowledge_schema_change VALUES (?,?,?,?,?)",
        (corpus_id, revision.revision_id, context.identity.principal_id, text, sha(text.encode())),
    )
    if parent is None:
        connection.execute(
            "INSERT INTO knowledge_schema_head VALUES (?,?)",
            (corpus_id, revision.revision_id),
        )
    else:
        updated = connection.execute(
            "UPDATE knowledge_schema_head SET revision_id=? WHERE corpus_id=? AND revision_id=?",
            (revision.revision_id, corpus_id, parent.revision_id),
        )
        if updated.rowcount != 1:
            raise EvidenceServiceError("state_conflict")


def preset(
    context: CanonicalWriteContext,
    request: SchemaPresetRequest,
    budget: PrivateBudget,
) -> SchemaPresetRegistration:
    if (
        context.connection.execute(
            "SELECT 1 FROM corpus WHERE corpus_id=?",
            (request.corpus_id,),
        ).fetchone()
        is None
    ):
        raise EvidenceServiceError("not_found")
    with closing(Registry(context.connection, request.corpus_id, budget)) as registry:
        registry.hold(len(request.model_dump_json().encode()) * 16 + 4096)
        head = registry.head()
        if head is not None:
            summary, _ = registry.load(head.revision_id)
            size = context.connection.execute(
                "SELECT length(CAST(change_json AS BLOB)) FROM knowledge_schema_change "
                "WHERE corpus_id=? AND revision_id=?",
                (request.corpus_id, head.revision_id),
            ).fetchone()
            if size is None or size[0] > MAX_SCHEMA_BYTES + 32768:
                raise EvidenceServiceError("internal_error")
            registry.hold(size[0] * 8)
            row = context.connection.execute(
                "SELECT * FROM knowledge_schema_change WHERE corpus_id=? AND revision_id=?",
                (request.corpus_id, head.revision_id),
            ).fetchone()
            if row is None or sha(row["change_json"].encode()) != row["change_hash"]:
                raise EvidenceServiceError("internal_error")
            saved = SchemaChangeView.model_validate_json(row["change_json"])
            if (
                saved.revision != head
                or saved.principal_id != row["principal_id"]
                or (
                    saved.preset is not None
                    and (
                        saved.preset.corpus_id != request.corpus_id
                        or definition_hash(request.corpus_id, saved.preset.definition)
                        != head.definition_hash
                    )
                )
            ):
                raise EvidenceServiceError("internal_error")
            if (
                summary.sequence != 1
                or summary.origin != "trusted_preset"
                or saved.preset is None
                or saved.principal_id != context.identity.principal_id
                or saved.preset.preset_name != request.preset_name
                or saved.preset.preset_rationale != request.preset_rationale
                or definition_json(saved.preset.definition) != definition_json(request.definition)
            ):
                raise EvidenceServiceError("state_conflict")
            return SchemaPresetRegistration(
                corpus_id=request.corpus_id,
                revision=head,
                status="unchanged",
            )
        revision = SchemaRevisionRef(
            revision_id=token(),
            definition_hash=definition_hash(request.corpus_id, request.definition),
        )
        _insert(
            context,
            request.corpus_id,
            revision,
            request.definition,
            None,
            1,
            "trusted_preset",
            timestamp(now()),
            SchemaChangeView(
                revision=revision,
                principal_id=context.identity.principal_id,
                preset=request,
            ),
        )
        return SchemaPresetRegistration(
            corpus_id=request.corpus_id,
            revision=revision,
            status="applied",
        )


def apply(
    context: CanonicalWriteContext,
    request: SchemaApplyRequest,
    budget: PrivateBudget,
) -> SchemaReceipt:
    identity, scope = context.identity, request.scope
    authorize(context.connection, identity, scope, "read")
    semantic = canonical(
        {
            "proposal": json.loads(proposal_json(request.proposal)),
            "approved_proposal_digest": request.approved_proposal_digest,
            "approval": request.approval.model_dump(mode="json"),
        }
    )
    key = (scope.corpus_id, identity.principal_id, sha(request.retry_key.encode()))
    with closing(Store(context.connection, scope, budget)) as store:
        store.hold(len(semantic.encode()) * 16 + 4096)
        size = context.connection.execute(
            "SELECT length(CAST(request_json AS BLOB))+length(CAST(receipt_json AS BLOB)) "
            "FROM knowledge_schema_receipt WHERE corpus_id=? AND principal_id=? AND key_hash=?",
            key,
        ).fetchone()
        if size is not None:
            if size[0] > MAX_SCHEMA_BYTES + 65536:
                raise EvidenceServiceError("internal_error")
            store.hold(size[0] * 8)
            row = context.connection.execute(
                "SELECT * FROM knowledge_schema_receipt "
                "WHERE corpus_id=? AND principal_id=? AND key_hash=?",
                key,
            ).fetchone()
            if row is None or sha(row["request_json"].encode()) != row["request_hash"]:
                raise EvidenceServiceError("internal_error")
            detail = change(store, identity, row["revision_id"])
            if row["request_json"] != semantic:
                raise EvidenceServiceError("retry_conflict")
            receipt = SchemaReceipt.model_validate_json(row["receipt_json"])
            summary = store.registry.load(row["revision_id"])[0]
            if (
                detail.proposal is None
                or proposal_digest(detail.proposal) != receipt.proposal_digest
                or receipt.previous_revision != detail.proposal.base_revision
                or receipt.attribution != detail.proposal.attribution
                or receipt.sequence != summary.sequence
                or receipt.committed_at != summary.committed_at
                or receipt.revision != detail.revision
                or receipt.principal_id != identity.principal_id
                or receipt.approval != detail.approval
                or receipt.proposal_digest != request.approved_proposal_digest
                or receipt.corpus_id != scope.corpus_id
            ):
                raise EvidenceServiceError("internal_error")
            return receipt
        result = validate(store, request.proposal)
        if request.approved_proposal_digest != result.proposal_digest:
            raise EvidenceServiceError("invalid_request")
        parent = request.proposal.base_revision
        sequence = 1 if parent is None else store.registry.load(parent.revision_id)[0].sequence + 1
        revision = SchemaRevisionRef(revision_id=token(), definition_hash=result.definition_hash)
        at = timestamp(now())
        receipt = SchemaReceipt(
            corpus_id=scope.corpus_id,
            previous_revision=parent,
            revision=revision,
            sequence=sequence,
            proposal_digest=result.proposal_digest,
            principal_id=identity.principal_id,
            attribution=request.proposal.attribution,
            approval=request.approval,
            committed_at=at,
        )
        _insert(
            context,
            scope.corpus_id,
            revision,
            result.definition,
            parent,
            sequence,
            "approved_proposal",
            at,
            SchemaChangeView(
                revision=revision,
                principal_id=identity.principal_id,
                proposal=request.proposal,
                approval=request.approval,
            ),
        )
        context.connection.execute(
            "INSERT INTO knowledge_schema_receipt VALUES (?,?,?,?,?,?,?)",
            (
                *key,
                semantic,
                sha(semantic.encode()),
                revision.revision_id,
                receipt.model_dump_json(),
            ),
        )
        return receipt
