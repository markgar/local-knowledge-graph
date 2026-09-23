"""Complete private mapping from one canonical snapshot, never a query prefix."""

from __future__ import annotations

from collections.abc import Generator
from contextlib import closing
from typing import Literal, Self

from pydantic import Field, model_validator

from kg._execution_budget import PrivateResourceStop, ScratchReservation
from kg.diagnostics._bounds import bounded_size
from kg.evidence._read_context import CanonicalReadContext
from kg.evidence._values import sha
from kg.evidence.errors import EvidenceServiceError
from kg.knowledge._reader import _decision_item
from kg.knowledge._registry import definition_json
from kg.knowledge._selection import (
    CapturedEvidence,
    DecisionSelectionItem,
    EntityWitness,
    SourceWitness,
)
from kg.knowledge._store import Store
from kg.models.foundation import AddAssertion, Attribution, Label, Name, Token, Value

PAGE_BYTES = 8 << 20


class GraphExportError(Exception):
    def __init__(
        self, code: Literal["unsupported_mapping", "unsupported_support", "invalid_projection"],
    ) -> None:
        self.code = code
        super().__init__(code)


class GraphCoverage(Value):
    mapping_version: Literal["canonical-relationships-decisions/1"] = (
        "canonical-relationships-decisions/1"
    )
    schema_version: Token
    schema_definition_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    relationship_predicates: tuple[Name, ...]
    decision_predicates: tuple[Name, ...]

    @model_validator(mode="after")
    def ordered(self) -> Self:
        for names in (self.relationship_predicates, self.decision_predicates):
            if tuple(sorted(set(names))) != names:
                raise ValueError("Coverage predicates must be ordered and unique")
        if set(self.relationship_predicates).intersection(self.decision_predicates):
            raise ValueError("Coverage families overlap")
        return self


class GraphEvidence(Value):
    captured: CapturedEvidence
    passage_set_id: Token | None

    @model_validator(mode="after")
    def membership(self) -> Self:
        if (self.passage_set_id is None) != (self.captured.reference.passage_id is None):
            raise ValueError("Passage membership mismatch")
        return self


class GraphEntity(Value):
    kind: Literal["entity"] = "entity"
    entity_id: Token
    name: Label
    entity_type: Name
    creation_sequence: int = Field(ge=1)
    witness: EntityWitness
    witness_evidence: tuple[GraphEvidence, ...] = Field(max_length=200)

    @model_validator(mode="after")
    def proof(self) -> Self:
        basis = self.witness.basis
        expected = basis.evidence if isinstance(basis, SourceWitness) else ()
        if (
            self.witness.entity_id != self.entity_id
            or tuple(e.captured for e in self.witness_evidence) != expected
        ):
            raise ValueError("Entity proof mismatch")
        return self


class GraphAssertion(Value):
    kind: Literal["assertion"] = "assertion"
    assertion_id: Token
    contribution_sequence: int = Field(ge=1)
    schema_version: Token
    predicate: Name
    interpretation: Literal["explicit", "inferred"]
    subject_id: Token
    object_entity_id: Token | None
    decision_text: str | None
    attribution: Attribution
    committed_at: str
    support: tuple[GraphEvidence, ...] = Field(min_length=1, max_length=200)
    subject_witness: EntityWitness
    object_witness: EntityWitness | None
    decision_member: DecisionSelectionItem | None

    @model_validator(mode="after")
    def proof(self) -> Self:
        if self.subject_witness.entity_id != self.subject_id:
            raise ValueError("Subject proof mismatch")
        if len(set(self.support)) != len(self.support):
            raise ValueError("Duplicate support")
        if any(e.captured.reference.corpus_id != self.support[0].captured.reference.corpus_id
               for e in self.support):
            raise ValueError("Cross-corpus support")
        if self.object_entity_id is not None:
            if (
                self.object_witness is None
                or self.object_witness.entity_id != self.object_entity_id
                or self.decision_text is not None or self.decision_member is not None
            ):
                raise ValueError("Relationship proof mismatch")
        else:
            member = self.decision_member
            if (
                self.decision_text is None or self.object_witness is not None
                or self.interpretation != "explicit" or member is None
                or member.record.record_id != self.assertion_id
                or member.subject_id != self.subject_id
                or member.schema_version != self.schema_version
                or member.dependencies.subject_witness != self.subject_witness
                or member.dependencies.assertion_support != tuple(e.captured for e in self.support)
            ):
                raise ValueError("Decision proof mismatch")
        return self


GraphExportItem = GraphEntity | GraphAssertion


class GraphExportPage:
    def __init__(
        self, items: tuple[GraphExportItem, ...], eof: bool,
        reservations: list[ScratchReservation],
    ) -> None:
        self.items, self.eof = items, eof
        self._reservations = reservations
        self.closed = False

    def __copy__(self) -> Self:
        raise TypeError("Page ownership cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> Self:
        raise TypeError("Page ownership cannot be copied")

    def close(self) -> None:
        self.items = ()
        for reservation in self._reservations:
            reservation.release()
        self._reservations.clear()
        self.closed = True


class GraphExportCursor:
    def __init__(
        self, context: CanonicalReadContext, expected_coverage: GraphCoverage | None,
    ) -> None:
        context.check_active()
        if context.meter.private_budget.resource_profile != "graph-build/1":
            raise EvidenceServiceError("invalid_request")
        self.context = context
        self._registry = Store(context.connection, context.scope, context.meter.private_budget)
        self._page: GraphExportPage | None = None
        self._pending: tuple[GraphExportItem, ScratchReservation, int] | None = None
        self._closed = False
        self._eof = False
        try:
            self.schema = self._registry.schema()
            self.coverage = GraphCoverage(
                schema_version=self.schema.schema_version,
                schema_definition_hash=sha(definition_json(self.schema).encode()),
                relationship_predicates=tuple(sorted(
                    p.name for p in self.schema.predicates if p.object_kind == "entity"
                )),
                decision_predicates=tuple(sorted(
                    p.name for p in self.schema.predicates if p.record_projection is not None
                )),
            )
            if expected_coverage is not None and expected_coverage != self.coverage:
                raise GraphExportError("unsupported_mapping")
            self._iterator = self._produce()
        except BaseException:
            self._registry.close()
            raise

    def _evidence(self, proofs: tuple[CapturedEvidence, ...]) -> tuple[GraphEvidence, ...]:
        result = []
        for proof in proofs:
            set_id = None
            if proof.reference.passage_id is not None:
                with closing(self.context.connection.execute(
                    "SELECT passage_set_id FROM passage WHERE corpus_id=? AND passage_id=?",
                    (self.context.scope.corpus_id, proof.reference.passage_id),
                )) as cursor:
                    row = cursor.fetchone()
                if row is None:
                    raise GraphExportError("invalid_projection")
                set_id = row[0]
            result.append(GraphEvidence(captured=proof, passage_set_id=set_id))
        return tuple(result)

    def _entity(self, store: Store, identifier: str) -> GraphEntity:
        witness = store.require_entity(identifier)
        row = store.row("entity", identifier)
        proofs = witness.basis.evidence if isinstance(witness.basis, SourceWitness) else ()
        return GraphEntity(
            entity_id=identifier, name=row["name"], entity_type=row["entity_type"],
            creation_sequence=row["creation_sequence"], witness=witness,
            witness_evidence=self._evidence(proofs),
        )

    def _assertion(self, store: Store, identifier: str) -> GraphAssertion | None:
        with closing(store.connection.execute(
            "SELECT c.*,a.subject_id,a.predicate,a.interpretation,a.object_kind "
            "FROM contribution c JOIN assertion a USING(corpus_id,contribution_id) "
            "WHERE c.corpus_id=? AND c.contribution_id=?",
            (self.context.scope.corpus_id, identifier),
        )) as cursor:
            row = cursor.fetchone()
        if row is None:
            raise GraphExportError("invalid_projection")
        relation = row["predicate"] in self.coverage.relationship_predicates
        decision = row["predicate"] in self.coverage.decision_predicates
        if not relation and not decision:
            return None
        view = store.contribution(identifier)
        payload = view.payload
        if (
            not isinstance(payload, AddAssertion)
            or view.schema_version != self.schema.schema_version
        ):
            raise GraphExportError("invalid_projection")
        member = None
        obj, text = None, None
        if decision:
            member = _decision_item(
                store, row, row["subject_id"], view.witnesses[0], self.schema,
            )
            if member is None:
                return None
            if payload.object.kind != "string":
                raise GraphExportError("invalid_projection")
            text = payload.object.value
        elif payload.object.kind == "entity" and payload.object.entity.kind == "stored":
            obj = payload.object.entity.entity_id
        else:
            raise GraphExportError("invalid_projection")
        return GraphAssertion(
            assertion_id=identifier, contribution_sequence=view.sequence,
            schema_version=view.schema_version, predicate=payload.predicate,
            interpretation=payload.interpretation, subject_id=row["subject_id"],
            object_entity_id=obj, decision_text=text, attribution=view.attribution,
            committed_at=view.committed_at, support=self._evidence(view.evidence),
            subject_witness=view.witnesses[0],
            object_witness=view.witnesses[1] if obj is not None else None,
            decision_member=member,
        )

    def _produce(self) -> Generator[tuple[GraphExportItem, ScratchReservation, int], None, None]:
        for table, key in (("entity", "entity_id"), ("assertion", "contribution_id")):
            after = ""
            while True:
                self.context.check_active()
                with self.context.meter.reserve_scratch(256 << 10, "general"):
                    with closing(self.context.connection.execute(
                        f"SELECT {key} FROM {table} WHERE corpus_id=? AND {key}>? "
                        f"COLLATE BINARY ORDER BY {key} COLLATE BINARY LIMIT 200",
                        (self.context.scope.corpus_id, after),
                    )) as cursor:
                        identifiers = tuple(row[0] for row in cursor)
                    return_after_scan = not identifiers
                    for identifier in identifiers:
                        after = identifier
                        self.context.check_active()
                        with closing(Store(
                            self.context.connection, self.context.scope,
                            self.context.meter.private_budget,
                        )) as store:
                            try:
                                item = (self._entity(store, identifier) if table == "entity"
                                        else self._assertion(store, identifier))
                            except EvidenceServiceError as error:
                                if error.failure.code == "not_found":
                                    continue
                                if error.failure.code == "unsupported":
                                    raise GraphExportError("unsupported_support") from None
                                raise
                            if item is None:
                                continue
                            reservation = self.context.meter.reserve_scratch(
                                4096 + bounded_size(item, 64 << 20) * 4, "general",
                            )
                            try:
                                size = len(item.model_dump_json().encode())
                                if size > PAGE_BYTES:
                                    raise PrivateResourceStop()
                            except BaseException:
                                reservation.release()
                                raise
                        yield item, reservation, size
                        del item
                    if return_after_scan:
                        break

    def read(self, *, limit: int = 200) -> GraphExportPage:
        if (
            self._closed or type(limit) is not int or not 1 <= limit <= 200
            or (self._page is not None and not self._page.closed)
        ):
            raise EvidenceServiceError("invalid_request")
        self.context.check_active()
        items: list[GraphExportItem] = []
        reservations: list[ScratchReservation] = []
        size = 0
        try:
            while len(items) < limit and not self._eof:
                if self._pending is None:
                    self._pending = next(self._iterator, None)
                if self._pending is None:
                    self._eof = True
                    break
                item, reservation, item_size = self._pending
                if items and size + item_size > PAGE_BYTES:
                    break
                reservations.append(reservation)
                self._pending = None
                self.context.meter.reserve_public(
                    "resolve_entity" if isinstance(item, GraphEntity)
                    else "decision_record" if item.decision_member is not None
                    else "support_member",
                )
                items.append(item)
                size += item_size
            self._page = GraphExportPage(tuple(items), self._eof, reservations)
            return self._page
        except BaseException:
            items.clear()
            for reservation in reservations:
                reservation.release()
            self.close()
            raise

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._iterator.close()
            if self._page is not None:
                self._page.close()
            if self._pending is not None:
                self._pending[1].release()
                self._pending = None
            self._registry.close()


def graph_export(
    context: CanonicalReadContext, *, expected_coverage: GraphCoverage | None = None,
) -> GraphExportCursor:
    try:
        return GraphExportCursor(context, expected_coverage)
    except EvidenceServiceError as error:
        if error.failure.code == "unsupported":
            raise GraphExportError("unsupported_mapping") from None
        raise
