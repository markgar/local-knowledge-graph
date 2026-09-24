"""Bounded scoped knowledge reads. Writes use EvidenceService's canonical owner."""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import ExitStack, closing
from typing import Literal

from kg._execution_budget import (
    Deadline,
    DeadlineStop,
    LocalExecutionMeter,
    PrivateBudget,
    PrivateResourceStop,
)
from kg.diagnostics import DiagnosticService
from kg.diagnostics._collector import Capture, CaptureUnavailable, Collector
from kg.diagnostics._targets import KnowledgeTarget, ReportTargets
from kg.evidence import _reporting
from kg.evidence._read_context import observe, read_context, release_fence
from kg.evidence._reads import _page, check_token
from kg.evidence._values import validated
from kg.evidence.database import EvidenceDatabase
from kg.evidence.errors import EvidenceServiceError
from kg.knowledge._reports import KnowledgeReportAuthorizer
from kg.knowledge._store import Store
from kg.models.evidence import LocalIdentity
from kg.models.execution import SUMMARY_OPTIONS, Explained, ExplainOptions, OperationName
from kg.models.foundation import Scope, Value
from kg.models.knowledge import (
    ContributionView,
    EntityView,
    KnowledgeCapabilities,
    KnowledgePage,
)
from kg.models.knowledge_events import KnowledgeSelection
from kg.models.schema import (
    SchemaChangeView,
    SchemaProposal,
    SchemaRevisionPage,
    SchemaValidation,
    SchemaView,
)

Mode = Literal["current", "history"]
Kind = Literal["entity_support", "alias", "identifier", "mention", "assertion"]


def _history(mode: Mode) -> bool:
    if type(mode) is not str or mode not in {"current", "history"}:
        raise EvidenceServiceError("invalid_request")
    return mode == "history"


def _retain(capture: Capture | CaptureUnavailable, result: object) -> None:
    if isinstance(result, EntityView):
        capture.retain(
            ReportTargets(
                values=(
                    KnowledgeTarget(
                        contribution_id=result.witness.contribution_id,
                        witness_ids=(result.witness.contribution_id,),
                    ),
                )
            )
        )
    elif isinstance(result, ContributionView):
        capture.retain(
            ReportTargets(
                values=(
                    KnowledgeTarget(
                        contribution_id=result.contribution_id,
                        witness_ids=tuple(w.contribution_id for w in result.witnesses),
                    ),
                )
            )
        )
    elif isinstance(result, KnowledgePage):
        for entry in result.entries:
            _retain(capture, entry)


class KnowledgeService:
    def __init__(self, database: EvidenceDatabase, identity: LocalIdentity) -> None:
        self.database, self.identity = database, validated(LocalIdentity, identity)
        self._collector = Collector("knowledge", self.identity)
        self.diagnostics = DiagnosticService(self._collector, KnowledgeReportAuthorizer(database))

    def _read[T](self, scope: Scope, operation: OperationName, run: Callable[[Store], T]) -> T:
        scope = validated(Scope, scope)
        budget = PrivateBudget(Deadline(time.monotonic() + 30))
        selection_budget = budget.limited(max_visits=10_000)
        meter = LocalExecutionMeter(budget, max_operations=1, max_items=100_000).begin_step("read")
        capture = self._collector.begin_capture(
            operation,
            scope,
            required="read",
            options=_reporting.options(),
            observation_kind="current_inspection",
        )
        try:
            with observe(self.database, self.identity, scope, budget.deadline, budget) as observer:
                with read_context(
                    self.database,
                    self.identity,
                    scope,
                    observer.session_id,
                    budget.deadline,
                    meter,
                ) as context:
                    store = Store(
                        context.connection,
                        scope,
                        selection_budget,
                        context=context,
                    )
                    try:
                        with context.using_budget(selection_budget):
                            result = run(store)
                        with capture.guard():
                            _retain(capture, result)
                            capture.append(
                                KnowledgeSelection(
                                    semantics="entity"
                                    if operation in {"entity", "entities"}
                                    else "contribution",
                                    state="nonempty"
                                    if not isinstance(result, KnowledgePage) or result.entries
                                    else "empty",
                                )
                            )
                    finally:
                        store.close()
                with release_fence(observer, self.identity, scope, budget.deadline, budget=budget):
                    pass
        except (PrivateResourceStop, DeadlineStop):
            error = EvidenceServiceError("budget_exceeded")
            capture.group.redact(error.failure.code)
            capture.finish(
                "failed", reason=error.failure.code, diagnostic_id=error.failure.diagnostic_id
            )
            _reporting.deliver(self.diagnostics._publish(capture))
            raise error from None
        except EvidenceServiceError as error:
            capture.group.redact(error.failure.code)
            capture.finish(
                "failed", reason=error.failure.code, diagnostic_id=error.failure.diagnostic_id
            )
            _reporting.deliver(self.diagnostics._publish(capture))
            raise
        except BaseException:
            capture.group.close()
            raise
        capture.finish("succeeded")
        _reporting.deliver(self.diagnostics._publish(capture))
        return result

    def capabilities(self, scope: Scope) -> KnowledgeCapabilities:
        def result(store: Store) -> KnowledgeCapabilities:
            head = store.registry.head()
            if head is None:
                return KnowledgeCapabilities(
                    schema_status="unconfigured", schema_revision=None, decision_encoding=None,
                    change_kinds=(), withdrawal=None,
                    reads=("schema", "schema_history", "schema_change", "validate_schema"),
                )
            return KnowledgeCapabilities(
                schema_revision=head, decision_encoding="direct-subject-decision/1"
                if any(p.record_projection for p in store.schema().predicates) else None,
            )

        return self._read(
            scope, "capabilities", result,
        )

    def _schema_read[T: Value](self, scope: Scope, run: Callable[[Store], T]) -> T:
        scope = validated(Scope, scope)
        budget = PrivateBudget(Deadline(time.monotonic() + 30))
        selection = budget.limited(max_visits=10_000)
        meter = LocalExecutionMeter(budget, max_operations=1, max_items=100_000).begin_step("read")
        try:
            with ExitStack() as output, observe(
                self.database, self.identity, scope, budget.deadline, budget,
            ) as observer:
                with read_context(
                    self.database, self.identity, scope,
                    observer.session_id, budget.deadline, meter,
                ) as context, context.using_budget(selection), closing(
                    Store(context.connection, scope, selection, context=context),
                ) as store:
                    result = run(store)
                    output.enter_context(
                        budget.reserve_scratch(
                            len(result.model_dump_json().encode()) * 8, "general",
                        ),
                    )
                with release_fence(observer, self.identity, scope, budget.deadline, budget=budget):
                    pass
                return result
        except (DeadlineStop, PrivateResourceStop):
            raise EvidenceServiceError("budget_exceeded") from None

    def schema(self, scope: Scope, *, revision_id: str | None = None) -> SchemaView:
        from kg.knowledge import _schema_operations

        if revision_id is not None:
            revision_id = check_token(revision_id)
        return self._schema_read(scope, lambda store: _schema_operations.view(store, revision_id))

    def schema_history(
        self, scope: Scope, *, after_sequence: int = 0, limit: int = 20,
    ) -> SchemaRevisionPage:
        from kg.knowledge import _schema_operations

        return self._schema_read(
            scope, lambda store: _schema_operations.history(store, after_sequence, limit),
        )

    def schema_change(self, scope: Scope, revision_id: str) -> SchemaChangeView:
        from kg.knowledge import _schema_operations

        revision_id = check_token(revision_id)
        return self._schema_read(
            scope, lambda store: _schema_operations.change(store, self.identity, revision_id),
        )

    def validate_schema(self, scope: Scope, proposal: SchemaProposal) -> SchemaValidation:
        from kg.knowledge import _schema_operations

        proposal = validated(SchemaProposal, proposal)
        return self._schema_read(scope, lambda store: _schema_operations.validate(store, proposal))

    def entity(self, scope: Scope, entity_id: str, *, mode: Mode = "current") -> EntityView:
        history, entity_id = _history(mode), check_token(entity_id)
        return self._read(scope, "entity", lambda store: store.entity(entity_id, history=history))

    def contribution(
        self, scope: Scope, contribution_id: str, *, mode: Mode = "current"
    ) -> ContributionView:
        history, contribution_id = _history(mode), check_token(contribution_id)
        return self._read(
            scope,
            "contribution",
            lambda store: store.contribution(contribution_id, history=history),
        )

    def entities(
        self,
        scope: Scope,
        *,
        name: str | None = None,
        scheme: str | None = None,
        identifier: str | None = None,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> KnowledgePage[EntityView]:
        from pydantic import TypeAdapter, ValidationError

        from kg.models.foundation import Label, Name, Token

        _page(after_sequence, limit)
        try:
            if name is not None:
                TypeAdapter(Label).validate_python(name, strict=True)
            if scheme is not None:
                TypeAdapter(Name).validate_python(scheme, strict=True)
            if identifier is not None:
                TypeAdapter(Token).validate_python(identifier, strict=True)
        except ValidationError:
            raise EvidenceServiceError("invalid_request") from None
        if (scheme is None) != (identifier is None) or (name is not None and scheme is not None):
            raise EvidenceServiceError("invalid_request")

        def run(store: Store) -> KnowledgePage[EntityView]:
            results = []
            for row in store.connection.execute(
                "SELECT * FROM entity WHERE corpus_id=? AND creation_sequence>? "
                "ORDER BY creation_sequence",
                (store.scope.corpus_id, after_sequence),
            ):
                try:
                    view = store.entity(row["entity_id"])
                except EvidenceServiceError as error:
                    if error.failure.code == "not_found":
                        continue
                    raise
                if scheme is not None or (name is not None and name != view.name):
                    table = "identifier" if scheme is not None else "alias"
                    predicate = "scheme=? AND value=?" if scheme is not None else "alias=?"
                    params = (scheme, identifier) if scheme is not None else (name,)
                    found = False
                    for contribution in store.connection.execute(
                        f"SELECT contribution_id FROM {table} WHERE corpus_id=? "
                        f"AND entity_id=? AND {predicate}",
                        (store.scope.corpus_id, view.entity_id, *params),
                    ):
                        try:
                            store.contribution(contribution[0])
                            found = True
                            break
                        except EvidenceServiceError as error:
                            if error.failure.code != "not_found":
                                raise
                    if not found:
                        continue
                results.append(view)
                if len(results) > limit:
                    break
            return KnowledgePage(
                entries=tuple(results[:limit]),
                has_more=len(results) > limit,
                next_after_sequence=results[limit - 1].sequence if len(results) > limit else None,
            )

        return self._read(scope, "entities", run)

    def contributions(
        self,
        scope: Scope,
        entity_id: str,
        *,
        kind: Kind | None = None,
        mode: Mode = "current",
        after_sequence: int = 0,
        limit: int = 100,
    ) -> KnowledgePage[ContributionView]:
        _page(after_sequence, limit)
        history, entity_id = _history(mode), check_token(entity_id)
        if kind is not None and (
            type(kind) is not str
            or kind not in {"entity_support", "alias", "identifier", "mention", "assertion"}
        ):
            raise EvidenceServiceError("invalid_request")

        def run(store: Store) -> KnowledgePage[ContributionView]:
            store.require_entity(entity_id, history=history)
            entries = []
            for row in store.connection.execute(
                "SELECT c.contribution_id FROM contribution c WHERE c.corpus_id=? AND c.sequence>? "
                "AND (? IS NULL OR c.kind=?) AND ("
                "EXISTS(SELECT 1 FROM entity_support s WHERE s.corpus_id=c.corpus_id "
                "AND s.contribution_id=c.contribution_id AND s.entity_id=?) OR "
                "EXISTS(SELECT 1 FROM alias s WHERE s.corpus_id=c.corpus_id "
                "AND s.contribution_id=c.contribution_id AND s.entity_id=?) OR "
                "EXISTS(SELECT 1 FROM identifier s WHERE s.corpus_id=c.corpus_id "
                "AND s.contribution_id=c.contribution_id AND s.entity_id=?) OR "
                "EXISTS(SELECT 1 FROM mention s WHERE s.corpus_id=c.corpus_id "
                "AND s.contribution_id=c.contribution_id AND s.entity_id=?) OR "
                "EXISTS(SELECT 1 FROM assertion s WHERE s.corpus_id=c.corpus_id "
                "AND s.contribution_id=c.contribution_id "
                "AND (s.subject_id=? OR s.object_entity_id=?))) "
                "ORDER BY c.sequence",
                (store.scope.corpus_id, after_sequence, kind, kind, *(entity_id,) * 6),
            ):
                try:
                    entries.append(store.contribution(row[0], history=history))
                except EvidenceServiceError as error:
                    if error.failure.code == "not_found":
                        continue
                    raise
                if len(entries) > limit:
                    break
            return KnowledgePage(
                entries=tuple(entries[:limit]),
                has_more=len(entries) > limit,
                next_after_sequence=entries[limit - 1].sequence if len(entries) > limit else None,
            )

        return self._read(scope, "contributions", run)

    def entity_explained(
        self,
        scope: Scope,
        entity_id: str,
        *,
        mode: Mode = "current",
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[EntityView]:
        return _reporting.explained(options, self.entity, scope, entity_id, mode=mode)

    def contribution_explained(
        self,
        scope: Scope,
        contribution_id: str,
        *,
        mode: Mode = "current",
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[ContributionView]:
        return _reporting.explained(options, self.contribution, scope, contribution_id, mode=mode)

    def capabilities_explained(
        self,
        scope: Scope,
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[KnowledgeCapabilities]:
        return _reporting.explained(options, self.capabilities, scope)

    def entities_explained(
        self,
        scope: Scope,
        *,
        name: str | None = None,
        scheme: str | None = None,
        identifier: str | None = None,
        after_sequence: int = 0,
        limit: int = 100,
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[KnowledgePage[EntityView]]:
        return _reporting.explained(
            options,
            self.entities,
            scope,
            name=name,
            scheme=scheme,
            identifier=identifier,
            after_sequence=after_sequence,
            limit=limit,
        )

    def contributions_explained(
        self,
        scope: Scope,
        entity_id: str,
        *,
        kind: Kind | None = None,
        mode: Mode = "current",
        after_sequence: int = 0,
        limit: int = 100,
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[KnowledgePage[ContributionView]]:
        return _reporting.explained(
            options,
            self.contributions,
            scope,
            entity_id,
            kind=kind,
            mode=mode,
            after_sequence=after_sequence,
            limit=limit,
        )
