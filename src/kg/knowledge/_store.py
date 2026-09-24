"""Bounded snapshot/transaction eligibility, shared by reads and writes."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from typing import TYPE_CHECKING

from pydantic import TypeAdapter

from kg._execution_budget import PrivateBudget, ScratchReservation
from kg.diagnostics._bounds import bounded_size
from kg.evidence._reads import evidence_location, evidence_view
from kg.evidence._sql import AccountedConnection
from kg.evidence.errors import EvidenceServiceError
from kg.knowledge._registry import Registry, runtime_schema
from kg.knowledge._selection import CapturedEvidence, EntityWitness, SeedWitness, SourceWitness
from kg.models.foundation import (
    Attribution,
    Change,
    DocumentDependency,
    EvidenceRef,
    Scope,
    SeedSupport,
    SourceSupport,
)
from kg.models.knowledge import AssertionWithdrawal, ContributionView, EntityView, KnowledgeSchema

_CHANGE: TypeAdapter[Change] = TypeAdapter(Change)

if TYPE_CHECKING:
    from kg.evidence._read_context import CanonicalReadContext


class RevalidationCache:
    """Bounded immutable proofs for one reader in one live read context."""

    def __init__(self, context: CanonicalReadContext) -> None:
        self.context = context
        self.proofs: dict[tuple[EvidenceRef, str], tuple[CapturedEvidence, bool]] = {}
        self.bases: dict[tuple[str, bool], EntityWitness | None] = {}
        self.witnesses: set[EntityWitness] = set()
        self.schema: KnowledgeSchema | None = None
        self.registry = Registry(
            context.connection, context.scope.corpus_id, context.meter.private_budget,
            custody=context._scratch,
        )
        self.reservations: list[ScratchReservation] = []
        self.closed = False

    def check(self) -> None:
        self.context.check_active()
        if self.closed:
            raise EvidenceServiceError("invalid_request")

    def hold(self, size: int) -> None:
        self.check()
        reservation = self.context.meter.reserve_scratch(max(1, size), "general")
        self.reservations.append(reservation)
        self.context._scratch.append(reservation)

    def hold_witness(self, witness: EntityWitness | None) -> None:
        self.hold(4096 + bounded_size(witness, 64 << 20))

    def close(self) -> None:
        self.closed = True
        self.proofs.clear()
        self.bases.clear()
        self.witnesses.clear()
        self.schema = None
        self.registry.close()
        for reservation in self.reservations:
            reservation.release()
        self.reservations.clear()


class Store:
    def __init__(
        self,
        connection: AccountedConnection,
        scope: Scope,
        budget: PrivateBudget,
        *,
        context: CanonicalReadContext | None = None,
        cache: RevalidationCache | None = None,
    ) -> None:
        if cache is not None:
            cache.check()
            if cache.context is not context:
                raise EvidenceServiceError("invalid_request")
        self.connection, self.scope, self.budget = connection, scope, budget
        self._scratch = ExitStack()
        self._proofs: dict[tuple[EvidenceRef, str], tuple[CapturedEvidence, bool]] = (
            {} if cache is None else cache.proofs
        )
        self._bases: dict[tuple[str, bool], EntityWitness | None] = (
            {} if cache is None else cache.bases
        )
        self._context = context
        self._cache = cache
        self.registry = (
            Registry(
                connection, scope.corpus_id, budget,
                custody=context._scratch if context is not None else None,
            ) if cache is None else cache.registry
        )

    def close(self) -> None:
        if self._cache is None:
            self._proofs.clear()
            self._bases.clear()
        self._scratch.close()
        if self._cache is None:
            self.registry.close()

    def refresh_entities(self) -> None:
        """Re-evaluate activation after the owner's staged knowledge mutations."""
        self._bases.clear()

    def hold(self, size: int) -> None:
        reservation = self.budget.reserve_scratch(max(1, size), "general")
        self._scratch.enter_context(reservation)
        if self._context is not None:
            self._context._scratch.append(reservation)

    def schema(self) -> KnowledgeSchema:
        if self._cache is not None:
            self._cache.check()
            if self._cache.schema is not None:
                return self._cache.schema
        head = self.registry.head()
        if head is None:
            raise EvidenceServiceError("unsupported")
        _, definition = self.registry.load(head.revision_id)
        schema = runtime_schema(self.scope.corpus_id, head, definition)
        if self._cache is not None:
            self._cache.hold(len(schema.model_dump_json().encode()) * 8)
            self._cache.schema = schema
        else:
            self.hold(len(schema.model_dump_json().encode()) * 8)
        return schema

    def row(self, table: str, identifier: str) -> sqlite3.Row:
        if table not in {"entity", "contribution"}:
            raise EvidenceServiceError("invalid_request")
        key = "entity_id" if table == "entity" else "contribution_id"
        row = self.connection.execute(
            f"SELECT * FROM {table} WHERE corpus_id=? AND {key}=?",
            (self.scope.corpus_id, identifier),
        ).fetchone()
        if not isinstance(row, sqlite3.Row):
            raise EvidenceServiceError("not_found")
        return row

    def proof(self, reference: EvidenceRef, state: str) -> tuple[CapturedEvidence, bool]:
        self.budget.check_deadline()
        key = reference, state
        if key in self._proofs:
            return self._proofs[key]
        evidence_location(self.connection, self.scope, reference, state)
        # Hydration remains in E3's common resolver, but nested support is not a
        # public evidence operation. Reserve its live working memory privately.
        sizes = self.connection.execute(
            "SELECT r.byte_length,length(CAST(m.metadata_json AS BLOB)),"
            "length(CAST(a.quote AS BLOB)) FROM revision r "
            "JOIN metadata_snapshot m ON m.document_id=r.document_id "
            "AND m.revision_id=r.revision_id JOIN anchor a ON a.revision_id=r.revision_id "
            "AND a.document_id=r.document_id WHERE r.document_id=? AND r.revision_id=? "
            "AND m.state_version=? AND a.anchor_id=?",
            (reference.document_id, reference.revision_id, state, reference.anchor_id),
        ).fetchone()
        if sizes is None:
            raise EvidenceServiceError("internal_error")
        with (
            self.budget.reserve_scratch(max(1, sizes[0] * 4), "text"),
            self.budget.reserve_scratch(max(1, sizes[2] * 4), "text"),
            self.budget.reserve_scratch(max(1, sizes[1] * 4), "context"),
            self.budget.reserve_scratch(
                max(1, sizes[0] + sizes[2] * 5 + sizes[1] * 8),
                "general",
            ),
        ):
            view = evidence_view(self.connection, self.scope, reference, state)
            metadata_id, active = view.citation.metadata_snapshot_id, view.is_current_support
            del view
        row = self.connection.execute(
            "SELECT s.namespace_token,d.current_state,n.policy_token FROM document_state s "
            "JOIN document d ON d.document_id=s.document_id "
            "JOIN source_namespace n ON n.corpus_id=d.corpus_id AND n.namespace=d.namespace "
            "WHERE s.state_version=? AND s.document_id=?",
            (state, reference.document_id),
        ).fetchone()
        if row is None:
            raise EvidenceServiceError("internal_error")
        proof = CapturedEvidence(
            reference=reference,
            dependency=DocumentDependency(
                source_namespace=reference.source_namespace,
                document_id=reference.document_id,
                revision_id=reference.revision_id,
                state_version=state,
            ),
            metadata_snapshot_id=metadata_id,
            namespace_token=row["namespace_token"],
        )
        result = (
            proof,
            bool(
                active
                and row["current_state"] == state
                and row["namespace_token"] == row["policy_token"]
            ),
        )
        if len(self._proofs) < 200:
            if self._cache is None:
                self.hold(4096)
            else:
                self._cache.hold(
                    bounded_size(key, 64 << 20) + bounded_size(result, 64 << 20),
                )
            self._proofs[key] = result
        return result

    def support(
        self,
        row: sqlite3.Row,
    ) -> tuple[SourceWitness | SeedWitness, bool]:
        cid = row["contribution_id"]
        if row["support_kind"] == "source":
            size = self.connection.execute(
                "SELECT COALESCE(sum(length(namespace)+length(document_id)+length(revision_id)+"
                "length(anchor_id)+length(state_version)+length(metadata_snapshot_id)+"
                "COALESCE(length(passage_set_id),0)+COALESCE(length(passage_id),0)),0),count(*), "
                "CASE WHEN ? != 'assertion' THEN 1 ELSE NOT EXISTS ("
                "SELECT 1 FROM assertion_withdrawal WHERE corpus_id=? AND contribution_id=?"
                ") END "
                "FROM contribution_evidence WHERE corpus_id=? AND contribution_id=?",
                (row["kind"], self.scope.corpus_id, cid, self.scope.corpus_id, cid),
            ).fetchone()
            if size is None or not 1 <= size[1] <= 200:
                raise EvidenceServiceError("internal_error")
            self.hold(size[0] * 8 + size[1] * 1024)
            rows = self.connection.execute(
                "SELECT * FROM contribution_evidence WHERE corpus_id=? AND contribution_id=? "
                "ORDER BY ordinal LIMIT 201",
                (self.scope.corpus_id, cid),
            ).fetchall()
            if not rows or len(rows) > 200:
                raise EvidenceServiceError("internal_error")
            if any(r["namespace"] not in self.scope.access.namespaces for r in rows):
                raise EvidenceServiceError("not_found")
            proofs, current = [], True
            for ordinal, ref in enumerate(rows, 1):
                if ref["ordinal"] != ordinal:
                    raise EvidenceServiceError("internal_error")
                proof, active = self.proof(
                    EvidenceRef(
                        corpus_id=self.scope.corpus_id,
                        source_namespace=ref["namespace"],
                        document_id=ref["document_id"],
                        revision_id=ref["revision_id"],
                        anchor_id=ref["anchor_id"],
                        passage_id=ref["passage_id"],
                    ),
                    ref["state_version"],
                )
                if proof.metadata_snapshot_id != ref["metadata_snapshot_id"]:
                    raise EvidenceServiceError("internal_error")
                if ref["passage_id"] is not None:
                    passage = self.connection.execute(
                        "SELECT passage_set_id FROM passage WHERE passage_id=?",
                        (ref["passage_id"],),
                    ).fetchone()
                    if passage is None or passage[0] != ref["passage_set_id"]:
                        raise EvidenceServiceError("internal_error")
                proofs.append(proof)
                current = current and active
            return SourceWitness(evidence=tuple(proofs)), current and bool(size[2])
        seed = self.connection.execute(
            "SELECT s.*,sl.current_contribution_id,e.event_id,e.generation "
            "FROM contribution_seed s "
            "JOIN seed_slot sl USING(corpus_id,namespace,owner_id,writer_id,seed_set_id,seed_key) "
            "JOIN seed_membership_event e ON e.corpus_id=s.corpus_id "
            "AND e.contribution_id=s.contribution_id AND e.event_kind='activated' "
            "WHERE s.corpus_id=? AND s.contribution_id=?",
            (self.scope.corpus_id, cid),
        ).fetchone()
        if seed is None:
            raise EvidenceServiceError("internal_error")
        if seed["namespace"] not in self.scope.access.namespaces:
            raise EvidenceServiceError("not_found")
        return SeedWitness(
            namespace=seed["namespace"],
            owner_id=seed["owner_id"],
            writer_id=seed["writer_id"],
            seed_set_id=seed["seed_set_id"],
            seed_key=seed["seed_key"],
            contribution_id=cid,
            membership_event_id=seed["event_id"],
            generation=seed["generation"],
        ), seed["current_contribution_id"] == cid

    @contextmanager
    def _activation_support(
        self, row: sqlite3.Row,
    ) -> Iterator[tuple[SourceWitness | SeedWitness, bool]]:
        if self.budget.resource_profile != "graph-build/1":
            yield self.support(row)
            return
        with closing(Store(self.connection, self.scope, self.budget)) as trial:
            yield trial.support(row)

    def basis(self, entity_id: str, *, history: bool = False) -> EntityWitness | None:
        key = entity_id, history
        if key in self._bases:
            return self._bases[key]
        entity = self.row("entity", entity_id)
        if entity["retired"] and not history:
            return None
        rows = self.connection.execute(
            "SELECT c.*,s.attested_name,s.attested_type FROM entity_support s "
            "JOIN contribution c USING(corpus_id,contribution_id) "
            "WHERE s.corpus_id=? AND s.entity_id=? ORDER BY c.sequence",
            (self.scope.corpus_id, entity_id),
        )
        result = None
        with closing(rows):
            for row in rows:
                authored = self.registry.authored(row["schema_version"])
                if row["attested_type"] not in authored.entity_types:
                    raise EvidenceServiceError("internal_error")
                try:
                    with self._activation_support(row) as (support, current):
                        if (
                            row["attested_name"] != entity["name"]
                            or row["attested_type"] != entity["entity_type"]
                        ):
                            raise EvidenceServiceError("internal_error")
                        if history or current:
                            if self.budget.resource_profile == "graph-build/1":
                                self.hold(4096 + bounded_size(support, 64 << 20))
                                support = support.model_copy(deep=True)
                            result = EntityWitness(
                                entity_id=entity_id,
                                contribution_id=row["contribution_id"],
                                contribution_sequence=row["sequence"],
                                basis=support,
                            )
                            break
                        del support
                except EvidenceServiceError as error:
                    if error.failure.code == "not_found":
                        continue
                    raise
        if len(self._bases) < 200:
            if self._cache is None:
                self.hold(4096)
            else:
                self._cache.hold_witness(result)
            self._bases[key] = result
        return result

    def require_entity(self, entity_id: str, *, history: bool = False) -> EntityWitness:
        witness = self.basis(entity_id, history=history)
        if witness is None:
            raise EvidenceServiceError("not_found")
        return witness

    def entity(self, entity_id: str, *, history: bool = False) -> EntityView:
        witness = self.require_entity(entity_id, history=history)
        row = self.row("entity", entity_id)
        more = False
        for support in self.connection.execute(
            "SELECT c.* FROM entity_support s JOIN contribution c USING(corpus_id,contribution_id) "
            "WHERE s.corpus_id=? AND s.entity_id=? AND c.sequence>? ORDER BY c.sequence",
            (self.scope.corpus_id, entity_id, witness.contribution_sequence),
        ):
            try:
                _, active = self.support(support)
            except EvidenceServiceError as error:
                if error.failure.code == "not_found":
                    continue
                raise
            if history or active:
                more = True
                break
        return EntityView(
            entity_id=entity_id,
            name=row["name"],
            entity_type=row["entity_type"],
            sequence=row["creation_sequence"],
            is_current=self.basis(entity_id) is not None,
            witness=witness,
            has_more_support=more,
        )

    def contribution(self, cid: str, *, history: bool = False) -> ContributionView:
        self.hold(4096)
        row = self.row("contribution", cid)
        basis, current = self.support(row)
        kind = row["kind"]
        if kind not in {"entity_support", "alias", "identifier", "mention", "assertion"}:
            raise EvidenceServiceError("unsupported")
        text_columns = {
            "entity_support": ("attested_name", "attested_type"),
            "alias": ("alias",),
            "identifier": ("scheme", "value"),
            "mention": (),
            "assertion": (
                "predicate",
                "object_string",
                "object_integer",
                "object_timestamp",
            ),
        }[kind]
        size_expr = "+".join(f"COALESCE(length(CAST({c} AS BLOB)),0)" for c in text_columns) or "0"
        size = self.connection.execute(
            f"SELECT {size_expr} FROM {kind} WHERE corpus_id=? AND contribution_id=?",
            (self.scope.corpus_id, cid),
        ).fetchone()
        if size is None:
            raise EvidenceServiceError("internal_error")
        self.hold(size[0] * 8)
        detail = self.connection.execute(
            f"SELECT * FROM {kind} WHERE corpus_id=? AND contribution_id=?",
            (self.scope.corpus_id, cid),
        ).fetchone()
        if detail is None:
            raise EvidenceServiceError("internal_error")
        support = (
            SourceSupport(kind="source", evidence=tuple(p.reference for p in basis.evidence))
            if isinstance(basis, SourceWitness)
            else SeedSupport(
                kind="seed",
                source_namespace=basis.namespace,
                seed_set_id=basis.seed_set_id,
                seed_key=basis.seed_key,
            )
        )
        payload: dict[str, object] = dict(kind=kind, local_id=row["local_id"], support=support)
        endpoints = []
        if kind == "assertion":
            endpoints.append(detail["subject_id"])
            obj_kind = detail["object_kind"]
            obj: dict[str, object] = {"kind": obj_kind}
            if obj_kind == "entity":
                endpoints.append(detail["object_entity_id"])
                obj["entity"] = {"kind": "stored", "entity_id": detail["object_entity_id"]}
            else:
                value = detail[f"object_{obj_kind}"]
                if obj_kind == "integer":
                    value = int(value)
                elif obj_kind == "boolean":
                    value = bool(value)
                elif obj_kind == "timestamp":
                    from datetime import datetime

                    value = datetime.fromisoformat(value)
                obj["value"] = value
            payload.update(
                subject={"kind": "stored", "entity_id": detail["subject_id"]},
                predicate=detail["predicate"],
                interpretation=detail["interpretation"],
                object=obj,
            )
        else:
            endpoints.append(detail["entity_id"])
            payload["entity"] = {"kind": "stored", "entity_id": detail["entity_id"]}
            if kind == "entity_support":
                entity = self.row("entity", detail["entity_id"])
                if (detail["attested_name"], detail["attested_type"]) != (
                    entity["name"],
                    entity["entity_type"],
                ):
                    raise EvidenceServiceError("internal_error")
                payload.update(name=detail["attested_name"], entity_type=detail["attested_type"])
            elif kind == "alias":
                payload["alias"] = detail["alias"]
            elif kind == "identifier":
                payload.update(scheme=detail["scheme"], value=detail["value"])
        witnesses = tuple(self.require_entity(e, history=history) for e in endpoints)
        current = current and all(self.basis(e) is not None for e in endpoints)
        if not current and not history:
            raise EvidenceServiceError("not_found")
        authored = self.registry.authored(row["schema_version"])
        if kind == "assertion":
            predicate = next(
                (p for p in authored.predicates if p.name == detail["predicate"]), None,
            )
            subject = self.row("entity", detail["subject_id"])
            if (
                predicate is None or predicate.object_kind != detail["object_kind"]
                or subject["entity_type"] not in predicate.subject_types
                or (predicate.record_projection and detail["interpretation"] != "explicit")
            ):
                raise EvidenceServiceError("internal_error")
            if predicate.object_kind == "entity":
                obj_entity = self.row("entity", detail["object_entity_id"])
                if obj_entity["entity_type"] not in predicate.object_types:
                    raise EvidenceServiceError("internal_error")
        elif (
            kind == "entity_support" and detail["attested_type"] not in authored.entity_types
        ) or (kind == "identifier" and detail["scheme"] not in authored.identifier_schemes):
            raise EvidenceServiceError("internal_error")
        withdrawal = None
        if history and kind == "assertion":
            event = self.connection.execute(
                "SELECT w.withdrawal_id,k.committed_at,p.attribution_json "
                "FROM assertion_withdrawal w "
                "LEFT JOIN write_key k ON k.corpus_id=w.corpus_id AND k.key_id=w.key_id "
                "LEFT JOIN knowledge_write_provenance p "
                "ON p.corpus_id=w.corpus_id AND p.key_id=w.key_id "
                "WHERE w.corpus_id=? AND w.contribution_id=?",
                (self.scope.corpus_id, cid),
            ).fetchone()
            if event is not None:
                if event["committed_at"] is None or event["attribution_json"] is None:
                    raise EvidenceServiceError("internal_error")
                self.hold(4096 + len(event["attribution_json"]) * 8)
                withdrawal = AssertionWithdrawal(
                    withdrawal_id=event["withdrawal_id"],
                    committed_at=event["committed_at"],
                    attribution=Attribution.model_validate_json(event["attribution_json"]),
                )
        return ContributionView(
            contribution_id=cid,
            sequence=row["sequence"],
            schema_version=row["schema_version"],
            attribution=Attribution(
                owner_id=row["owner_id"],
                writer_id=row["writer_id"],
                producer=row["producer"],
                producer_version=row["producer_version"],
                model_id=row["model_id"],
                configuration_id=row["configuration_id"],
            ),
            committed_at=row["committed_at"],
            payload=_CHANGE.validate_python(payload),
            evidence=basis.evidence if isinstance(basis, SourceWitness) else (),
            is_current=current,
            witnesses=witnesses,
            withdrawal=withdrawal,
        )
