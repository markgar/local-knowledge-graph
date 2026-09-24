"""Public bounded standalone index lifecycle; full search has a separate facade."""

import sqlite3
import time
from collections.abc import Callable
from functools import partial
from typing import Literal

from kg._execution_budget import (
    Deadline,
    DeadlineStop,
    LocalExecutionMeter,
    PrivateBudget,
    PrivateResourceStop,
)
from kg.diagnostics import DiagnosticService
from kg.diagnostics._collector import Collector
from kg.diagnostics._targets import (
    AuthorizationBinding,
    IndexTarget,
    ReportTarget,
    ReportTargets,
    WriterTarget,
)
from kg.evidence import _reporting as reporting
from kg.evidence._authorization import authorize
from kg.evidence._coordination import CoordinatedIndexParticipant
from kg.evidence._diagnostic_authorization import EvidenceReportAuthorizer
from kg.evidence._read_context import observe, read_context, release_fence
from kg.evidence._reads import check_token, scoped_document
from kg.evidence._sql import AccountedConnection
from kg.evidence._transactions import writing
from kg.evidence._values import validated
from kg.evidence.database import EvidenceDatabase
from kg.evidence.errors import EvidenceServiceError
from kg.indexing import _configuration, _inspection, _storage
from kg.indexing._process import CaptureType, Process, ProviderFactory
from kg.models.evidence import LocalIdentity
from kg.models.execution import SUMMARY_OPTIONS, Explained, ExplainOptions, OperationName
from kg.models.foundation import Attribution, ExternalDocument, Scope
from kg.models.indexing import (
    DEFAULT_CONFIGURATION,
    CleanupResult,
    IndexCapabilities,
    IndexConfiguration,
    IndexStatus,
    PendingPage,
    ProcessResult,
)
from kg.models.indexing_events import IndexPhase
from kg.retrieval.dense import SentenceTransformerEmbeddingProvider


class IndexReportAuthorizer(EvidenceReportAuthorizer):
    def authorize_target(
        self,
        connection: AccountedConnection,
        binding: AuthorizationBinding,
        target: ReportTarget,
    ) -> None:
        if isinstance(target, IndexTarget):
            scoped_document(connection, binding.scope, target.document_id)
        else:
            super().authorize_target(connection, binding, target)


def _limit(value: int, maximum: int) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        raise EvidenceServiceError("invalid_request")


class IndexService:
    def __init__(
        self, database: EvidenceDatabase, identity: LocalIdentity, *,
        local_files_only: bool = False, model_cache: str | None = None,
    ) -> None:
        self.database = database
        self.identity = validated(LocalIdentity, identity)
        self._provider_factory: ProviderFactory = SentenceTransformerEmbeddingProvider
        if local_files_only or model_cache is not None:
            self._provider_factory = partial(
                SentenceTransformerEmbeddingProvider,
                local_files_only=local_files_only, cache_folder=model_cache,
            )
        self._collector = Collector("indexing", self.identity)
        self.diagnostics = DiagnosticService(self._collector, IndexReportAuthorizer(database))

    def capabilities(self) -> IndexCapabilities:
        return IndexCapabilities()

    def _budget(self) -> PrivateBudget:
        return PrivateBudget(Deadline(time.monotonic() + 30))

    def process(
        self,
        scope: Scope,
        attribution: Attribution,
        document_id: str,
        expected_state: str,
        configuration: IndexConfiguration = DEFAULT_CONFIGURATION,
        *,
        mode: Literal["incremental", "rebuild"] = "incremental",
    ) -> ProcessResult:
        scope = validated(Scope, scope)
        attribution = validated(Attribution, attribution)
        configuration = validated(IndexConfiguration, configuration)
        check_token(document_id)
        check_token(expected_state)
        if mode not in ("incremental", "rebuild"):
            raise EvidenceServiceError("invalid_request")
        capture = self._collector.begin_capture(
            "process",
            scope,
            required="read",
            options=reporting.options(),
        )
        with capture.guard():
            capture.retain(
                ReportTargets(
                    values=(
                        IndexTarget(
                            document_id=document_id,
                            configuration_id=_configuration.configuration_id(configuration),
                        ),
                    )
                )
            )
        process = Process(
            self.database,
            self.identity,
            scope,
            attribution,
            document_id,
            expected_state,
            configuration,
            mode,
            self._provider_factory,
            self._budget(),
            capture,
        )
        try:
            try:
                result = process.run()
            except (DeadlineStop, PrivateResourceStop):
                raise EvidenceServiceError("budget_exceeded") from None
        except EvidenceServiceError as error:
            capture.group.redact(error.failure.code)
            capture.finish(
                "failed",
                process.context.commit_outcome if process.context else "not_attempted",
                reason=error.failure.code,
                diagnostic_id=error.failure.diagnostic_id,
            )
            reporting.deliver(self.diagnostics._publish(capture))
            raise
        except BaseException:
            capture.group.close()
            raise
        capture.finish(
            "succeeded" if result.outcome in ("ready", "unchanged") else "failed",
            process.context.commit_outcome if process.context else "not_attempted",
            reason=result.reason,
            diagnostic_id=result.diagnostic_id,
        )
        reporting.deliver(self.diagnostics._publish(capture))
        return result

    def _process_coordinated(
        self,
        participant: CoordinatedIndexParticipant,
    ) -> ProcessResult:
        # No E4 owner-held claim/atomic-ack adapter has been delivered.
        raise EvidenceServiceError("unsupported")

    def _read[T](
        self,
        operation: OperationName,
        scope: Scope,
        action: Callable[[sqlite3.Connection, PrivateBudget, CaptureType], T],
    ) -> T:
        scope = validated(Scope, scope)
        budget = self._budget()
        try:
            with observe(self.database, self.identity, scope, budget.deadline, budget) as observer:
                reference = observer.retain()
                try:
                    capture = self._collector.begin_capture(
                        operation,
                        scope,
                        required="read",
                        options=reporting.options(),
                        observation_kind="current_inspection",
                        observer=reference,
                    )
                    try:
                        meter = LocalExecutionMeter(
                            budget, max_operations=1, max_items=1
                        ).begin_step(
                            operation,
                        )
                        with read_context(
                            self.database,
                            self.identity,
                            scope,
                            observer.session_id,
                            budget.deadline,
                            meter,
                        ) as context:
                            result = action(context.connection, budget, capture)
                        with release_fence(observer, self.identity, scope, budget.deadline):
                            capture.finish("succeeded")
                        reporting.deliver(self.diagnostics._publish(capture))
                        return result
                    except EvidenceServiceError as error:
                        capture.group.redact(error.failure.code)
                        capture.finish("failed", reason=error.failure.code)
                        reporting.deliver(self.diagnostics._publish(capture))
                        raise
                    except BaseException:
                        capture.group.close()
                        raise
                finally:
                    reference.close()
        except (DeadlineStop, PrivateResourceStop):
            raise EvidenceServiceError("budget_exceeded") from None

    def _status(
        self,
        connection: sqlite3.Connection,
        scope: Scope,
        document_id: str,
        configuration: IndexConfiguration,
        budget: PrivateBudget,
        capture: CaptureType,
    ) -> IndexStatus:
        result = _inspection.status(
            connection,
            self.identity,
            scope,
            document_id,
            configuration,
            budget,
        )
        with capture.guard():
            capture.retain(
                ReportTargets(
                    values=(
                        IndexTarget(
                            document_id=document_id,
                            configuration_id=result.configuration_id,
                        ),
                    )
                )
            )
            capture.append(
                IndexPhase(
                    phase="readiness",
                    status="ready" if result.status == "ready" else "not_ready",
                    logical_configuration_id=result.configuration_id,
                    passage_set_id=result.passage_set_id,
                    reason=result.reason,
                )
            )
        return result

    def status(
        self,
        scope: Scope,
        document_id: str,
        configuration: IndexConfiguration = DEFAULT_CONFIGURATION,
    ) -> IndexStatus:
        scope = validated(Scope, scope)
        configuration = validated(IndexConfiguration, configuration)
        check_token(document_id)
        return self._read(
            "status",
            scope,
            lambda connection, budget, capture: self._status(
                connection,
                scope,
                document_id,
                configuration,
                budget,
                capture,
            ),
        )

    def pending(
        self,
        scope: Scope,
        configuration: IndexConfiguration = DEFAULT_CONFIGURATION,
        *,
        after_document_id: str | None = None,
        limit: int = 100,
    ) -> PendingPage:
        scope = validated(Scope, scope)
        configuration = validated(IndexConfiguration, configuration)
        _limit(limit, 200)
        if after_document_id is not None:
            check_token(after_document_id)

        def action(
            connection: sqlite3.Connection, budget: PrivateBudget, capture: CaptureType
        ) -> PendingPage:
            authorize(connection, self.identity, scope, "read")
            placeholders = ",".join("?" for _ in scope.access.namespaces)
            rows = connection.execute(
                "SELECT d.document_id FROM document d JOIN document_state s "
                "ON s.state_version=d.current_state WHERE d.corpus_id=? "
                f"AND d.namespace IN ({placeholders}) AND s.source='active' "
                "AND d.document_id>? ORDER BY d.document_id LIMIT ?",
                (scope.corpus_id, *scope.access.namespaces, after_document_id or "", limit + 1),
            ).fetchall()
            results = tuple(
                self._status(
                    connection,
                    scope,
                    row[0],
                    configuration,
                    budget,
                    capture,
                )
                for row in rows[:limit]
            )
            return PendingPage(
                entries=tuple(result for result in results if result.status != "ready"),
                has_more=len(rows) > limit,
                next_after_document_id=rows[limit - 1][0] if len(rows) > limit else None,
            )

        return self._read("pending", scope, action)

    def cleanup(
        self,
        scope: Scope,
        attribution: Attribution,
        document_id: str,
        configuration: IndexConfiguration = DEFAULT_CONFIGURATION,
        *,
        limit: int = 1000,
    ) -> CleanupResult:
        scope, attribution = validated(Scope, scope), validated(Attribution, attribution)
        configuration = validated(IndexConfiguration, configuration)
        check_token(document_id)
        _limit(limit, 1000)
        config_id = _configuration.configuration_id(configuration)
        capture = self._collector.begin_capture(
            "cleanup",
            scope,
            required="read",
            options=reporting.options(),
        )
        with capture.guard():
            capture.retain(
                ReportTargets(
                    values=(
                        IndexTarget(
                            document_id=document_id,
                            configuration_id=config_id,
                        ),
                    )
                )
            )
        context = None
        try:
            try:
                with writing(self.database, self.identity, budget=self._budget()) as context:
                    doc = _storage.owner(
                        context.connection,
                        self.identity,
                        scope,
                        attribution,
                        document_id,
                    )
                    with capture.guard():
                        capture.retain(
                            ReportTargets(
                                values=(
                                    WriterTarget(
                                        document=ExternalDocument(
                                            source_namespace=doc["namespace"],
                                            synchronization_scope=doc["synchronization_scope"],
                                            external_id=doc["external_id"],
                                        ),
                                        owner_id=attribution.owner_id,
                                        writer_id=attribution.writer_id,
                                    ),
                                )
                            )
                        )
                    result = _storage.cleanup(
                        context.connection,
                        scope,
                        document_id,
                        config_id,
                        limit,
                    )
            except (DeadlineStop, PrivateResourceStop):
                raise EvidenceServiceError("budget_exceeded") from None
        except EvidenceServiceError as error:
            capture.group.redact(error.failure.code)
            capture.finish(
                "failed",
                context.commit_outcome if context else "not_attempted",
                reason=error.failure.code,
            )
            reporting.deliver(self.diagnostics._publish(capture))
            raise
        except BaseException:
            capture.group.close()
            raise
        capture.finish("succeeded", context.commit_outcome)
        reporting.deliver(self.diagnostics._publish(capture))
        return result

    def process_explained(
        self,
        scope: Scope,
        attribution: Attribution,
        document_id: str,
        expected_state: str,
        configuration: IndexConfiguration = DEFAULT_CONFIGURATION,
        *,
        mode: Literal["incremental", "rebuild"] = "incremental",
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[ProcessResult]:
        return reporting.explained(
            options,
            self.process,
            scope,
            attribution,
            document_id,
            expected_state,
            configuration,
            mode=mode,
        )

    def status_explained(
        self,
        scope: Scope,
        document_id: str,
        configuration: IndexConfiguration = DEFAULT_CONFIGURATION,
        *,
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[IndexStatus]:
        return reporting.explained(options, self.status, scope, document_id, configuration)

    def pending_explained(
        self,
        scope: Scope,
        configuration: IndexConfiguration = DEFAULT_CONFIGURATION,
        *,
        after_document_id: str | None = None,
        limit: int = 100,
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[PendingPage]:
        return reporting.explained(
            options,
            self.pending,
            scope,
            configuration,
            after_document_id=after_document_id,
            limit=limit,
        )

    def cleanup_explained(
        self,
        scope: Scope,
        attribution: Attribution,
        document_id: str,
        configuration: IndexConfiguration = DEFAULT_CONFIGURATION,
        *,
        limit: int = 1000,
        options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[CleanupResult]:
        return reporting.explained(
            options,
            self.cleanup,
            scope,
            attribution,
            document_id,
            configuration,
            limit=limit,
        )
