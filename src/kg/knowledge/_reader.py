"""Actual snapshot-bound selections; Q1 owns release and membership retention."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Generator

from pydantic import ValidationError

from kg._execution_budget import DeadlineStop, PrivateResourceStop, PublicBudgetStop
from kg.evidence._read_context import CanonicalReadContext
from kg.evidence._reads import check_token
from kg.evidence._values import validated
from kg.evidence.errors import EvidenceServiceError, storage_error
from kg.knowledge._selection import (
    DecisionDependencies,
    DecisionSelectionItem,
    EligibleEOF,
    EntitySelectionItem,
    EntitySelector,
    SelectionFailure,
    SelectionPage,
    SelectionStopped,
    SelectionTerminal,
    SourceWitness,
)
from kg.knowledge._store import Store
from kg.models.evidence import LocalIdentity
from kg.models.foundation import AddAssertion, Record, SourceSupport, StoredEntity, StringObject


class Cursor[T]:
    def __init__(
        self,
        context: CanonicalReadContext,
        producer: Callable[[Store], Generator[T, None, None]],
        stage: str,
    ) -> None:
        context.check_active()
        self.context = context
        self.budget = context.meter.private_budget.limited(max_visits=10_000)
        self.store = Store(context.connection, context.scope, self.budget, context=context)
        self.iterator = producer(self.store)
        self.stage = stage
        self.terminal: SelectionTerminal | None = None
        self.closed = False

    def read(self, *, limit: int = 200) -> SelectionPage[T]:
        if self.closed or type(limit) is not int or not 1 <= limit <= 200:
            raise EvidenceServiceError("invalid_request")
        items: list[T] = []
        try:
            self.context.check_active()
            if self.terminal is not None:
                return SelectionPage(items=(), terminal=self.terminal)
            with self.context.using_budget(self.budget):
                while len(items) < limit:
                    try:
                        item = next(self.iterator)
                    except StopIteration:
                        self.terminal = EligibleEOF()
                        break
                    self.context.meter.reserve_public(
                        "resolve_entity" if self.stage == "entity" else "decision_record",
                    )
                    items.append(item)
        except PublicBudgetStop:
            self.terminal = SelectionStopped(kind="public_budget_stop")
        except (PrivateResourceStop, DeadlineStop) as error:
            self.terminal = SelectionStopped(
                kind="deadline_stop"
                if isinstance(error, DeadlineStop)
                else "private_resource_stop",
            )
            items.clear()
        except EvidenceServiceError as error:
            if error.failure.code == "invalid_request":
                raise
            self.terminal = SelectionFailure(failure=error.failure)
            items.clear()
        except (sqlite3.Error, ValidationError) as error:
            self.terminal = SelectionFailure(failure=storage_error(error).failure)
            items.clear()
        if self.terminal is not None:
            self.iterator.close()
        return SelectionPage(items=tuple(items), terminal=self.terminal)

    def close(self) -> None:
        self.closed = True
        self.iterator.close()
        self.store.close()


class KnowledgeReader:
    def __init__(self, context: CanonicalReadContext, identity: LocalIdentity) -> None:
        context.check_active()
        if validated(LocalIdentity, identity) != context.identity:
            raise EvidenceServiceError("forbidden")
        self.context = context

    def resolve_entity(self, selector: EntitySelector) -> Cursor[EntitySelectionItem]:
        selector = validated(EntitySelector, selector)

        def produce(store: Store) -> Generator[EntitySelectionItem, None, None]:
            rows = store.connection.execute(
                "SELECT e.entity_id,e.name FROM entity e WHERE e.corpus_id=? AND "
                "((? IS NOT NULL AND e.entity_id=?) OR (? IS NOT NULL AND "
                "(e.name=? OR EXISTS (SELECT 1 FROM alias a WHERE a.corpus_id=e.corpus_id "
                "AND a.entity_id=e.entity_id AND a.alias=?)))) ORDER BY e.entity_id COLLATE BINARY",
                (
                    store.scope.corpus_id,
                    selector.entity_id,
                    selector.entity_id,
                    selector.name,
                    selector.name,
                    selector.name,
                ),
            )
            for row in rows:
                witness = store.basis(row["entity_id"])
                if witness is None:
                    continue
                if selector.name is not None and row["name"] != selector.name:
                    matched = False
                    for alias in store.connection.execute(
                        "SELECT contribution_id FROM alias WHERE corpus_id=? AND entity_id=? "
                        "AND alias=? ORDER BY contribution_id",
                        (store.scope.corpus_id, row["entity_id"], selector.name),
                    ):
                        try:
                            store.contribution(alias[0])
                            matched = True
                            break
                        except EvidenceServiceError as error:
                            if error.failure.code != "not_found":
                                raise
                    if not matched:
                        continue
                yield EntitySelectionItem(entity_id=row["entity_id"], witness=witness)

        return Cursor(self.context, produce, "entity")

    def select_decisions(self, subject_id: str) -> Cursor[DecisionSelectionItem]:
        subject_id = check_token(subject_id)

        def produce(store: Store) -> Generator[DecisionSelectionItem, None, None]:
            schema = store.schema()
            predicates = tuple(p.name for p in schema.predicates if p.record_projection is not None)
            if not predicates:
                raise EvidenceServiceError("unsupported")
            witness = store.require_entity(subject_id)
            rows = store.connection.execute(
                "SELECT c.*,a.interpretation,a.object_kind FROM assertion a JOIN contribution c "
                "USING(corpus_id,contribution_id) WHERE a.corpus_id=? AND a.subject_id=? "
                f"AND a.predicate IN ({','.join('?' for _ in predicates)}) "
                "ORDER BY a.contribution_id COLLATE BINARY",
                (store.scope.corpus_id, subject_id, *predicates),
            )
            for row in rows:
                if (
                    row["interpretation"] != "explicit"
                    or row["object_kind"] != "string"
                    or row["schema_version"] != schema.schema_version
                ):
                    raise EvidenceServiceError("internal_error")
                try:
                    support, current = store.support(row)
                except EvidenceServiceError as error:
                    if error.failure.code == "not_found":
                        continue
                    raise
                if not isinstance(support, SourceWitness):
                    raise EvidenceServiceError("internal_error")
                if not current:
                    continue
                store.hold(2048)
                yield DecisionSelectionItem(
                    record=Record(
                        record_id=row["contribution_id"],
                        record_type="decision",
                        support=SourceSupport(
                            kind="source",
                            evidence=tuple(p.reference for p in support.evidence),
                        ),
                    ),
                    subject_id=subject_id,
                    schema_version=schema.schema_version,
                    dependencies=DecisionDependencies(
                        assertion_support=support.evidence,
                        subject_witness=witness,
                    ),
                )

        return Cursor(self.context, produce, "decision")

    def revalidate_member(self, member: DecisionSelectionItem) -> None:
        member = validated(DecisionSelectionItem, member)
        self.context.check_active()
        budget = self.context.meter.private_budget.limited(max_visits=10_000)
        with self.context.using_budget(budget):
            store = Store(
                self.context.connection,
                self.context.scope,
                budget,
                context=self.context,
            )
            try:
                view = store.contribution(member.record.record_id)
                schema = store.schema()
                payload = view.payload
                if (
                    not isinstance(payload, AddAssertion)
                    or not isinstance(payload.subject, StoredEntity)
                    or payload.subject.entity_id != member.subject_id
                    or not isinstance(payload.object, StringObject)
                    or payload.interpretation != "explicit"
                    or not any(
                        p.name == payload.predicate and p.record_projection is not None
                        for p in schema.predicates
                    )
                ):
                    raise EvidenceServiceError("state_changed")
                witness = member.dependencies.subject_witness
                row = store.row("contribution", witness.contribution_id)
                basis, current = store.support(row)
                endpoint = store.connection.execute(
                    "SELECT entity_id FROM entity_support WHERE corpus_id=? AND contribution_id=?",
                    (store.scope.corpus_id, witness.contribution_id),
                ).fetchone()
                if (
                    endpoint is None
                    or endpoint[0] != member.subject_id
                    or not current
                    or basis != witness.basis
                    or row["sequence"] != witness.contribution_sequence
                    or view.evidence != member.dependencies.assertion_support
                    or view.schema_version != member.schema_version
                ):
                    raise EvidenceServiceError("state_changed")
            finally:
                store.close()
