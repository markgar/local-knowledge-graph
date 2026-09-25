"""Canonical classification claims, selections and immutable assertion captures."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from kg.diagnostics._targets import ClassificationTarget, KnowledgeWriterTarget
from kg.evidence._reads import check_token
from kg.evidence._values import canonical, sha, token
from kg.evidence.errors import EvidenceServiceError
from kg.knowledge._authorization import writer
from kg.knowledge._selection import ClassificationWitness, SourceWitness
from kg.knowledge._write_models import (
    EntityClassificationReceipt,
    LocalClassificationRef,
    SelectClassification,
)
from kg.models.authoring import ClassificationReviewWitness
from kg.models.foundation import (
    Attribution,
    StoredClassificationRef,
    StoredEntity,
)
from kg.models.knowledge import (
    ClassificationClaim,
    ClassificationEvent,
    ClassificationHistory,
    ClassificationReview,
    ClassificationSummary,
    Eligibility,
)

if TYPE_CHECKING:
    from kg.evidence._transactions import CanonicalWriteContext
    from kg.knowledge._store import Store
    from kg.models.foundation import WriteRequest


def head(store: Store, entity_id: str) -> sqlite3.Row:
    if entity_id in store.classification_heads:
        return store.classification_heads[entity_id]
    row = store.connection.execute(
        "SELECT s.event_id,s.claim_id,s.sequence FROM classification_head h "
        "JOIN classification_selection s "
        "ON s.corpus_id=h.corpus_id AND s.event_id=h.event_id "
        "WHERE h.corpus_id=? AND h.entity_id=?",
        (store.scope.corpus_id, entity_id),
    ).fetchone()
    if not isinstance(row, sqlite3.Row):
        raise EvidenceServiceError("internal_error")
    if store._context is not None and len(store.classification_heads) < 200:
        store.hold(4096)
        store.classification_heads[entity_id] = row
    return row


def claim(store: Store, claim_id: str, *, entity_id: str | None = None) -> ClassificationClaim:
    cached = store.classification_claims.get(claim_id)
    if cached is not None:
        if entity_id is not None and cached.entity_id != entity_id:
            raise EvidenceServiceError("not_found")
        return cached
    row = store.row("contribution", claim_id)
    if row["kind"] != "classification":
        raise EvidenceServiceError("not_found")
    basis, current = store.support(row)
    detail = store.connection.execute(
        "SELECT * FROM classification WHERE corpus_id=? AND contribution_id=?",
        (store.scope.corpus_id, claim_id),
    ).fetchone()
    if detail is None:
        raise EvidenceServiceError("internal_error")
    if entity_id is not None and detail["entity_id"] != entity_id:
        raise EvidenceServiceError("not_found")
    store.require_entity(detail["entity_id"], history=True)
    authored = store.registry.authored(row["schema_version"])
    if detail["entity_type"] not in authored.entity_types:
        raise EvidenceServiceError("internal_error")
    withdrawn = (
        store.connection.execute(
            "SELECT 1 FROM classification_withdrawal WHERE corpus_id=? AND contribution_id=?",
            (store.scope.corpus_id, claim_id),
        ).fetchone()
        is not None
    )
    result = ClassificationClaim(
        claim_id=claim_id,
        entity_id=detail["entity_id"],
        entity_type=detail["entity_type"],
        interpretation=detail["interpretation"],
        schema_version=row["schema_version"],
        basis=basis,
        withdrawn=withdrawn,
        is_current=current and not withdrawn and store.basis(detail["entity_id"]) is not None,
    )
    if store._context is not None and len(store.classification_claims) < 200:
        store.hold(4096)
        store.classification_claims[claim_id] = result
    return result


def selected(store: Store, entity_id: str) -> ClassificationWitness | None:
    event = head(store, entity_id)
    if event["claim_id"] is None:
        return None
    try:
        value = claim(store, event["claim_id"], entity_id=entity_id)
    except EvidenceServiceError as error:
        if error.failure.code == "not_found":
            return None
        raise
    if not value.is_current:
        return None
    return witness(value, event["event_id"])


def witness(value: ClassificationClaim, selection_id: str) -> ClassificationWitness:
    return ClassificationWitness(
        entity_id=value.entity_id,
        selection_id=selection_id,
        claim_id=value.claim_id,
        entity_type=value.entity_type,
        schema_version=value.schema_version,
        interpretation=value.interpretation,
        basis=value.basis,
    )


def summary(store: Store, entity_id: str) -> ClassificationSummary:
    value = selected(store, entity_id)
    return ClassificationSummary(
        selection_id=head(store, entity_id)["event_id"],
        status="selected" if value else "unresolved",
        selected=value,
    )


def review(
    store: Store,
    entity_id: str,
    claim_ids: tuple[str, ...] | None = None,
) -> ClassificationReview:
    check_token(entity_id)
    if claim_ids is not None:
        if (
            type(claim_ids) is not tuple
            or len(claim_ids) > 200
            or len(set(claim_ids)) != len(claim_ids)
        ):
            raise EvidenceServiceError("invalid_request")
        for cid in claim_ids:
            check_token(cid)
    store.require_entity(entity_id)
    event = head(store, entity_id)
    current = None
    if event["claim_id"] is not None:
        try:
            current = claim(store, event["claim_id"], entity_id=entity_id)
        except EvidenceServiceError as error:
            if error.failure.code != "not_found":
                raise
    values: list[ClassificationClaim] = []
    if claim_ids is not None:
        for cid in sorted(claim_ids):
            value = claim(store, cid, entity_id=entity_id)
            if not value.is_current:
                raise EvidenceServiceError("state_conflict")
            values.append(value)
    else:
        rows = store.connection.execute(
            "SELECT contribution_id FROM classification WHERE corpus_id=? AND entity_id=? "
            "ORDER BY contribution_id COLLATE BINARY",
            (store.scope.corpus_id, entity_id),
        )
        try:
            for row in rows:
                try:
                    value = claim(store, row[0], entity_id=entity_id)
                except EvidenceServiceError as error:
                    if error.failure.code == "not_found":
                        continue
                    raise
                if not value.is_current:
                    continue
                if len(values) == 200:
                    raise EvidenceServiceError(
                        "budget_exceeded",
                        explanation=(
                            "Complete classification review exceeds 200 claims; "
                            "explicitly review a subset."
                        ),
                    )
                values.append(value)
        finally:
            rows.close()
    coverage: Literal["complete", "selected_subset"] = (
        "complete" if claim_ids is None else "selected_subset"
    )
    schema_head = store.registry.head()
    encoded = canonical(
        {
            "domain": "classification-review/1",
            "corpus": store.scope.corpus_id,
            "entity": entity_id,
            "head": event["event_id"],
            "schema": schema_head.model_dump(mode="json") if schema_head else None,
            "principal": store.scope.access.principal_id,
            "namespaces": sorted(store.scope.access.namespaces),
            "coverage": coverage,
            "claims": [c.model_dump(mode="json") for c in values],
            "previous": current.model_dump(mode="json") if current else None,
        }
    )
    store.hold(len(encoded.encode()) * 8 + 4096)
    if schema_head is None:
        raise EvidenceServiceError("internal_error")
    review_witness = ClassificationReviewWitness(
        interface_version="classification-review-witness/1",
        entity_id=entity_id,
        schema_revision=schema_head,
        selection_id=event["event_id"],
        selected_claim_id=current.claim_id if current is not None else None,
        reviewed_claim_ids=tuple(c.claim_id for c in values),
        reviewed_candidates_digest=sha(encoded.encode()),
        review_coverage=coverage,
        accept_incomplete_review=coverage == "selected_subset",
    )
    return ClassificationReview(
        entity_id=entity_id,
        selection_id=event["event_id"],
        selected=current,
        claims=tuple(values),
        reviewed_claim_ids=tuple(c.claim_id for c in values),
        review_coverage=coverage,
        conflicting_types=len({c.entity_type for c in values}) > 1,
        reviewed_candidates_digest=sha(encoded.encode()),
        review_witness=review_witness,
    )


def origin(store: Store, entity_id: str) -> str:
    row = store.connection.execute(
        "SELECT contribution_id FROM entity_origin WHERE corpus_id=? AND entity_id=?",
        (store.scope.corpus_id, entity_id),
    ).fetchone()
    if row is None:
        raise EvidenceServiceError("internal_error")
    return str(row[0])


def authorize_selection(
    store: Store,
    context: CanonicalWriteContext,
    request: WriteRequest,
    entity_id: str,
) -> tuple[KnowledgeWriterTarget, ...]:
    store.require_entity(entity_id)
    row = store.row("contribution", origin(store, entity_id))
    basis, _ = store.support(row)
    if (row["owner_id"], row["writer_id"]) != (
        request.attribution.owner_id,
        request.attribution.writer_id,
    ):
        raise EvidenceServiceError("forbidden")
    namespaces = (
        {e.reference.source_namespace for e in basis.evidence}
        if isinstance(basis, SourceWitness)
        else {basis.namespace}
    )
    targets = []
    for namespace in sorted(namespaces):
        writer(
            context.connection,
            context.identity,
            request.scope,
            namespace,
            row["owner_id"],
            row["writer_id"],
        )
        targets.append(
            KnowledgeWriterTarget(
                namespace=namespace,
                owner_id=row["owner_id"],
                writer_id=row["writer_id"],
            )
        )
    old = head(store, entity_id)
    if old["claim_id"] is not None:
        claim(store, old["claim_id"], entity_id=entity_id)
    return tuple(targets)


def create_genesis(store: Store, entity_id: str, contribution_id: str, key_id: str) -> None:
    event_id = token()
    store.connection.execute(
        "INSERT INTO entity_origin VALUES (?,?,?)",
        (store.scope.corpus_id, entity_id, contribution_id),
    )
    store.connection.execute(
        "INSERT INTO classification_selection VALUES (?,?,?,1,NULL,NULL,?,?)",
        (
            store.scope.corpus_id,
            event_id,
            entity_id,
            key_id,
            "Identity admitted without classification.",
        ),
    )
    store.connection.execute(
        "INSERT INTO classification_head VALUES (?,?,?)",
        (store.scope.corpus_id, entity_id, event_id),
    )
    store.connection.execute(
        "INSERT INTO classification_selection_targets VALUES (?,?,?)",
        (store.scope.corpus_id, event_id, contribution_id),
    )


def apply_selection(
    store: Store,
    change: SelectClassification,
    key_id: str,
    *,
    claims: dict[str, str],
    reviewed: ClassificationReview | None,
    is_new: bool,
    new_claims: tuple[str, ...],
) -> tuple[str, bool, ClassificationTarget]:
    assert isinstance(change.entity, StoredEntity)
    entity_id = change.entity.entity_id
    old = head(store, entity_id)
    if is_new:
        if (
            change.expected_selection_id is not None
            or change.reviewed_candidates_digest is not None
            or change.reviewed_claim_ids
            or change.review_coverage != "complete"
        ):
            raise EvidenceServiceError("invalid_request")
    elif (
        reviewed is None
        or change.expected_selection_id != old["event_id"]
        or change.reviewed_candidates_digest != reviewed.reviewed_candidates_digest
        or set(change.reviewed_claim_ids) != set(reviewed.reviewed_claim_ids)
        or change.review_coverage != reviewed.review_coverage
    ):
        raise EvidenceServiceError("state_conflict")
    claim_id = (
        claims[change.claim.local_id]
        if isinstance(change.claim, LocalClassificationRef)
        else change.claim.contribution_id
        if isinstance(change.claim, StoredClassificationRef)
        else None
    )
    candidates = set(new_claims)
    if reviewed is not None:
        candidates.update(reviewed.reviewed_claim_ids)
    if claim_id is not None:
        if claim_id not in candidates:
            raise EvidenceServiceError("invalid_request")
        if not claim(store, claim_id, entity_id=entity_id).is_current:
            raise EvidenceServiceError("state_conflict")
    targets = candidates | {origin(store, entity_id)}
    if old["claim_id"] is not None:
        targets.add(old["claim_id"])
    manifest = ClassificationTarget(contribution_ids=tuple(sorted(targets)))
    changed = old["claim_id"] != claim_id
    if not changed:
        return str(old["event_id"]), False, manifest
    event_id = token()
    store.connection.execute(
        "INSERT INTO classification_selection VALUES (?,?,?,?,?,?,?,?)",
        (
            store.scope.corpus_id,
            event_id,
            entity_id,
            old["sequence"] + 1,
            old["event_id"],
            claim_id,
            key_id,
            change.rationale,
        ),
    )
    for cid in manifest.contribution_ids:
        store.connection.execute(
            "INSERT INTO classification_selection_targets VALUES (?,?,?)",
            (store.scope.corpus_id, event_id, cid),
        )
    store.connection.execute(
        "UPDATE classification_head SET event_id=? "
        "WHERE corpus_id=? AND entity_id=? AND event_id=?",
        (event_id, store.scope.corpus_id, entity_id, old["event_id"]),
    )
    return event_id, True, manifest


def receipt(store: Store, entity_id: str) -> EntityClassificationReceipt:
    row = head(store, entity_id)
    return EntityClassificationReceipt(
        entity_id=entity_id,
        selection_id=row["event_id"],
        selected_claim_id=row["claim_id"],
    )


def authorize_manifest(store: Store, target: ClassificationTarget) -> None:
    for cid in target.contribution_ids:
        row = store.row("contribution", cid)
        if row["kind"] == "classification":
            claim(store, cid)
        elif row["kind"] == "entity_support":
            store.support(row)
        else:
            raise EvidenceServiceError("internal_error")


def event(store: Store, entity_id: str, event_id: str) -> ClassificationEvent:
    targets = tuple(
        r[0]
        for r in store.connection.execute(
            "SELECT contribution_id FROM classification_selection_targets "
            "WHERE corpus_id=? AND event_id=? ORDER BY contribution_id LIMIT 303",
            (store.scope.corpus_id, event_id),
        )
    )
    if not targets:
        raise EvidenceServiceError("not_found")
    if len(targets) > 302:
        raise EvidenceServiceError("internal_error")
    authorize_manifest(store, ClassificationTarget(contribution_ids=targets))
    join = (
        " FROM classification_selection s JOIN write_key k USING(corpus_id,key_id) "
        "JOIN knowledge_write_provenance p USING(corpus_id,key_id) "
        "WHERE s.corpus_id=? AND s.entity_id=? AND s.event_id=?"
    )
    parameters = store.scope.corpus_id, entity_id, event_id
    sizes = store.connection.execute(
        "SELECT length(CAST(s.rationale AS BLOB)),length(CAST(p.attribution_json AS BLOB))" + join,
        parameters,
    ).fetchone()
    if sizes is None:
        raise EvidenceServiceError("not_found")
    store.hold(4096 + sum(sizes) * 8)
    row = store.connection.execute(
        "SELECT s.*,k.committed_at,p.schema_version,p.attribution_json" + join,
        parameters,
    ).fetchone()
    return ClassificationEvent(
        event_id=event_id,
        entity_id=entity_id,
        selected=claim(store, row["claim_id"], entity_id=entity_id) if row["claim_id"] else None,
        rationale=row["rationale"],
        attribution=Attribution.model_validate_json(row["attribution_json"]),
        schema_version=row["schema_version"],
        committed_at=row["committed_at"],
        is_head=head(store, entity_id)["event_id"] == event_id,
    )


def history(
    store: Store,
    entity_id: str,
    after_event_id: str | None,
    limit: int,
) -> ClassificationHistory:
    check_token(entity_id)
    if type(limit) is not int or not 1 <= limit <= 200:
        raise EvidenceServiceError("invalid_request")
    store.require_entity(entity_id, history=True)
    after = 0
    if after_event_id is not None:
        check_token(after_event_id)
        event(store, entity_id, after_event_id)
        after = store.connection.execute(
            "SELECT sequence FROM classification_selection WHERE corpus_id=? AND event_id=?",
            (store.scope.corpus_id, after_event_id),
        ).fetchone()[0]
    entries = []
    rows = store.connection.execute(
        "SELECT event_id FROM classification_selection WHERE corpus_id=? AND entity_id=? "
        "AND sequence>? ORDER BY sequence",
        (store.scope.corpus_id, entity_id, after),
    )
    try:
        for row in rows:
            try:
                entries.append(event(store, entity_id, row[0]))
            except EvidenceServiceError as error:
                if error.failure.code != "not_found":
                    raise
            if len(entries) > limit:
                break
    finally:
        rows.close()
    more = len(entries) > limit
    return ClassificationHistory(
        entries=tuple(entries[:limit]),
        has_more=more,
        next_after_event_id=entries[limit - 1].event_id if more else None,
    )


def assertion_captures(store: Store, assertion_id: str) -> tuple[ClassificationWitness, ...]:
    sizes = store.connection.execute(
        "SELECT length(CAST(witness_json AS BLOB)) FROM assertion_classification "
        "WHERE corpus_id=? AND contribution_id=? LIMIT 3",
        (store.scope.corpus_id, assertion_id),
    ).fetchall()
    if not 1 <= len(sizes) <= 2:
        raise EvidenceServiceError("internal_error")
    store.hold(4096 + sum(row[0] for row in sizes) * 8)
    rows = store.connection.execute(
        "SELECT * FROM assertion_classification "
        "WHERE corpus_id=? AND contribution_id=? ORDER BY role DESC",
        (store.scope.corpus_id, assertion_id),
    ).fetchall()
    if not 1 <= len(rows) <= 2 or rows[0]["role"] != "subject":
        raise EvidenceServiceError("internal_error")
    results = []
    for row in rows:
        try:
            captured = ClassificationWitness.model_validate_json(row["witness_json"])
        except ValidationError:
            raise EvidenceServiceError("internal_error") from None
        original = claim(store, row["claim_id"], entity_id=row["entity_id"])
        expected = witness(original, row["event_id"])
        chosen = store.connection.execute(
            "SELECT claim_id FROM classification_selection WHERE corpus_id=? AND entity_id=? "
            "AND event_id=?",
            (store.scope.corpus_id, row["entity_id"], row["event_id"]),
        ).fetchone()
        if captured != expected or chosen is None or chosen[0] != row["claim_id"]:
            raise EvidenceServiceError("internal_error")
        results.append(captured)
    return tuple(results)


def capture_status(store: Store, captures: tuple[ClassificationWitness, ...]) -> Eligibility:
    for captured in captures:
        if head(store, captured.entity_id)["event_id"] != captured.selection_id:
            return "classification_changed"
    claims = tuple(claim(store, c.claim_id, entity_id=c.entity_id) for c in captures)
    if any(c.withdrawn for c in claims):
        return "classification_withdrawn"
    if any(not c.is_current for c in claims):
        return "classification_stale"
    return "current"
