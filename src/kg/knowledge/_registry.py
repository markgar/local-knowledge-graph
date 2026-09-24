"""Immutable schema snapshots and additive comparison on canonical connections."""

from __future__ import annotations

import sqlite3
from contextlib import ExitStack

from pydantic import ValidationError

from kg._execution_budget import PrivateBudget, ScratchReservation
from kg.evidence._values import canonical, sha
from kg.evidence.errors import EvidenceServiceError, storage_error
from kg.models.foundation import SchemaRevisionRef
from kg.models.knowledge import KnowledgeSchema, PredicateDefinition
from kg.models.schema import (
    MAX_SCHEMA_BYTES,
    AddEntityType,
    AddIdentifierScheme,
    AddPredicate,
    SchemaDefinition,
    SchemaProposal,
    SchemaRevisionSummary,
    SchemaValidation,
    TermRef,
    WideningEffect,
)


def _ordered(value: object) -> object:
    if isinstance(value, dict):
        return {k: _ordered(v) for k, v in value.items()}
    if isinstance(value, list):
        return sorted((_ordered(v) for v in value), key=canonical)
    return value


def definition_json(definition: SchemaDefinition) -> str:
    return canonical(_ordered(definition.model_dump(mode="json")))


def definition_hash(corpus_id: str, definition: SchemaDefinition) -> str:
    return sha(
        b"knowledge-schema/1\0" + corpus_id.encode() + b"\0" + definition_json(definition).encode()
    )


def proposal_json(proposal: SchemaProposal) -> str:
    return canonical(_ordered(proposal.model_dump(mode="json")))


def proposal_digest(proposal: SchemaProposal) -> str:
    return sha(b"schema-proposal/1\0" + proposal_json(proposal).encode())


def runtime_schema(
    corpus_id: str,
    revision: SchemaRevisionRef,
    definition: SchemaDefinition,
) -> KnowledgeSchema:
    return KnowledgeSchema(
        corpus_id=corpus_id,
        schema_version=revision.revision_id,
        entity_types=tuple(t.name for t in definition.entity_types),
        identifier_schemes=tuple(t.name for t in definition.identifier_schemes),
        predicates=tuple(
            PredicateDefinition.model_validate(p.model_dump(exclude={"description"}))
            for p in definition.predicates
        ),
    )


def compatible(authored: SchemaDefinition, current: SchemaDefinition) -> bool:
    types = {t.name: t for t in current.entity_types}
    schemes = {t.name: t for t in current.identifier_schemes}
    predicates = {p.name: p for p in current.predicates}
    if any(types.get(t.name) != t for t in authored.entity_types):
        return False
    if any(schemes.get(t.name) != t for t in authored.identifier_schemes):
        return False
    for old in authored.predicates:
        new = predicates.get(old.name)
        if (
            new is None
            or old.model_dump(exclude={"subject_types", "object_types"})
            != new.model_dump(exclude={"subject_types", "object_types"})
            or not set(old.subject_types) <= set(new.subject_types)
            or not set(old.object_types) <= set(new.object_types)
        ):
            return False
    return True


class Registry:
    def __init__(
        self,
        connection: sqlite3.Connection,
        corpus_id: str,
        budget: PrivateBudget,
        *,
        custody: list[ScratchReservation] | None = None,
    ) -> None:
        self.connection, self.corpus_id, self.budget = connection, corpus_id, budget
        self._resources = ExitStack()
        self._custody = custody
        self._cache: dict[str, tuple[SchemaRevisionSummary, SchemaDefinition]] = {}
        self._authored: dict[str, KnowledgeSchema] = {}

    def close(self) -> None:
        self._cache.clear()
        self._authored.clear()
        self._resources.close()

    def hold(self, size: int) -> None:
        reservation = self.budget.reserve_scratch(max(1, size), "general")
        self._resources.enter_context(reservation)
        if self._custody is not None:
            self._custody.append(reservation)

    def head(self) -> SchemaRevisionRef | None:
        row = self.connection.execute(
            "SELECT (SELECT revision_id FROM knowledge_schema_head WHERE corpus_id=?) AS head,"
            "(SELECT revision_id FROM knowledge_schema_revision WHERE corpus_id=? "
            "ORDER BY sequence DESC LIMIT 1) AS latest",
            (self.corpus_id, self.corpus_id),
        ).fetchone()
        if row is None or row["head"] != row["latest"]:
            raise EvidenceServiceError("internal_error")
        if row["head"] is None:
            return None
        try:
            return self.load(row["head"])[0].revision
        except EvidenceServiceError as error:
            if error.failure.code == "not_found":
                raise EvidenceServiceError("internal_error") from None
            raise

    def load(self, revision_id: str) -> tuple[SchemaRevisionSummary, SchemaDefinition]:
        if revision_id in self._cache:
            return self._cache[revision_id]
        size = self.connection.execute(
            "SELECT length(CAST(definition_json AS BLOB)) FROM knowledge_schema_revision "
            "WHERE corpus_id=? AND revision_id=?",
            (self.corpus_id, revision_id),
        ).fetchone()
        if size is None:
            raise EvidenceServiceError("not_found")
        if size[0] > MAX_SCHEMA_BYTES:
            raise EvidenceServiceError("internal_error")
        self.hold(size[0] * 8 + 4096)
        row = self.connection.execute(
            "SELECT * FROM knowledge_schema_revision WHERE corpus_id=? AND revision_id=?",
            (self.corpus_id, revision_id),
        ).fetchone()
        if row is None:
            raise EvidenceServiceError("internal_error")
        try:
            definition = SchemaDefinition.model_validate_json(row["definition_json"])
            if (
                definition_json(definition) != row["definition_json"]
                or definition_hash(self.corpus_id, definition) != row["definition_hash"]
                or (row["sequence"] == 1) != (row["parent_revision_id"] is None)
            ):
                raise EvidenceServiceError("internal_error")
            result = (
                SchemaRevisionSummary(
                    revision=SchemaRevisionRef(
                        revision_id=revision_id,
                        definition_hash=row["definition_hash"],
                    ),
                    sequence=row["sequence"],
                    parent_revision_id=row["parent_revision_id"],
                    committed_at=row["committed_at"],
                    origin=row["origin"],
                    change_kinds=(),
                ),
                definition,
            )
        except ValidationError as error:
            raise storage_error(error) from None
        self._cache[revision_id] = result
        return result

    def authored(self, revision_id: str) -> KnowledgeSchema:
        self.budget.check_deadline()
        if revision_id in self._authored:
            return self._authored[revision_id]
        head = self.head()
        if head is None:
            raise EvidenceServiceError("unsupported")
        try:
            summary, authored = self.load(revision_id)
        except EvidenceServiceError as error:
            if error.failure.code == "not_found":
                raise EvidenceServiceError("internal_error") from None
            raise
        current_summary, current = self.load(head.revision_id)
        if summary.sequence > current_summary.sequence or not compatible(authored, current):
            raise EvidenceServiceError("internal_error")
        result = runtime_schema(self.corpus_id, summary.revision, authored)
        self.hold(len(result.model_dump_json().encode()) * 8)
        self._authored[revision_id] = result
        return result


def compare(proposal: SchemaProposal, current: SchemaDefinition | None) -> SchemaValidation:
    types = {t.name: t for t in current.entity_types} if current else {}
    schemes = {t.name: t for t in current.identifier_schemes} if current else {}
    predicates = {p.name: p for p in current.predicates} if current else {}
    candidates = {
        "entity_type": set(types),
        "identifier_scheme": set(schemes),
        "predicate": set(predicates),
    }
    additions: tuple[AddEntityType | AddIdentifierScheme | AddPredicate, ...] = (
        *proposal.add_entity_types,
        *proposal.add_identifier_schemes,
        *proposal.add_predicates,
    )
    reviews = tuple(a.review for a in additions) + tuple(
        w.review for w in proposal.widen_predicates
    )
    for review in reviews:
        for candidate in review.candidates:
            if candidate.term.name not in candidates[candidate.term.kind]:
                raise EvidenceServiceError("invalid_request")
    added: list[TermRef] = []
    for addition in proposal.add_entity_types:
        if addition.definition.name in types:
            raise EvidenceServiceError("invalid_request")
        types[addition.definition.name] = addition.definition
        added.append(TermRef(kind="entity_type", name=addition.definition.name))
    for addition_scheme in proposal.add_identifier_schemes:
        if addition_scheme.definition.name in schemes:
            raise EvidenceServiceError("invalid_request")
        schemes[addition_scheme.definition.name] = addition_scheme.definition
        added.append(TermRef(kind="identifier_scheme", name=addition_scheme.definition.name))
    for addition_predicate in proposal.add_predicates:
        if addition_predicate.definition.name in predicates:
            raise EvidenceServiceError("invalid_request")
        predicates[addition_predicate.definition.name] = addition_predicate.definition
        added.append(TermRef(kind="predicate", name=addition_predicate.definition.name))
    effects: list[WideningEffect] = []
    for widening in proposal.widen_predicates:
        old = predicates.get(widening.name)
        if (
            old is None
            or widening.name not in candidates["predicate"]
            or set(widening.add_subject_types) & set(old.subject_types)
            or set(widening.add_object_types) & set(old.object_types)
            or (old.object_kind != "entity" and widening.add_object_types)
        ):
            raise EvidenceServiceError("invalid_request")
        subjects = tuple(sorted((*old.subject_types, *widening.add_subject_types)))
        objects = tuple(sorted((*old.object_types, *widening.add_object_types)))
        predicates[old.name] = type(old).model_validate(
            {
                **old.model_dump(),
                "subject_types": subjects,
                "object_types": objects,
            }
        )
        effects.append(
            WideningEffect(
                name=old.name,
                previous_subject_types=old.subject_types,
                subject_types=subjects,
                previous_object_types=old.object_types,
                object_types=objects,
                new_endpoint_combinations=(
                    len(subjects) * max(1, len(objects))
                    - len(old.subject_types) * max(1, len(old.object_types))
                ),
            )
        )
    try:
        definition = SchemaDefinition(
            entity_types=tuple(types.values()),
            identifier_schemes=tuple(schemes.values()),
            predicates=tuple(predicates.values()),
        )
    except ValidationError:
        raise EvidenceServiceError("invalid_request") from None
    return SchemaValidation(
        proposal_digest=proposal_digest(proposal),
        definition_hash=definition_hash(proposal.corpus_id, definition),
        base_revision=proposal.base_revision,
        definition=definition,
        added_terms=tuple(added),
        widenings=tuple(effects),
        unresolved_concepts=proposal.unresolved_concepts,
    )
