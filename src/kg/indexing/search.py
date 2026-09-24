"""Canonical full search and the borrowed-snapshot integration boundary."""

import time
from collections.abc import Callable
from functools import partial

from pydantic import TypeAdapter, ValidationError

from kg._execution_budget import (
    Deadline,
    DeadlineStop,
    LocalExecutionMeter,
    PrivateBudget,
    PrivateResourceStop,
    PublicBudgetStop,
)
from kg.diagnostics import DiagnosticService
from kg.diagnostics._collector import Capture, Collector, DisclosureGroup
from kg.evidence import _reporting as reporting
from kg.evidence._read_context import (
    CanonicalReadContext,
    ObserverReference,
    observe,
    read_context,
    release_fence,
)
from kg.evidence._values import validated
from kg.evidence.database import EvidenceDatabase
from kg.evidence.errors import EvidenceServiceError
from kg.indexing._process import CaptureType, ProviderFactory
from kg.indexing._search import Pipeline, SearchSelection, execute
from kg.indexing.service import IndexReportAuthorizer, _limit
from kg.models.evidence import LocalIdentity
from kg.models.execution import SUMMARY_OPTIONS, Explained, ExplainOptions
from kg.models.execution_events import SafeReason
from kg.models.foundation import Label, Scope
from kg.models.indexing import DEFAULT_CONFIGURATION, CanonicalSearchResult, IndexConfiguration
from kg.retrieval.dense import SentenceTransformerEmbeddingProvider
from kg.retrieval.rerank import (
    RerankerError,
    RerankerProvider,
    SentenceTransformerCrossEncoderProvider,
)

_QUERY = TypeAdapter(Label)


class EvidenceSearchService:
    def __init__(
        self, database: EvidenceDatabase, identity: LocalIdentity, *,
        local_files_only: bool = False, model_cache: str | None = None,
    ) -> None:
        self.database = database
        self.identity = validated(LocalIdentity, identity)
        self._provider_factory: ProviderFactory = SentenceTransformerEmbeddingProvider
        self._reranker_factory: Callable[[], RerankerProvider] = (
            SentenceTransformerCrossEncoderProvider
        )
        if local_files_only or model_cache is not None:
            self._provider_factory = partial(
                SentenceTransformerEmbeddingProvider,
                local_files_only=local_files_only, cache_folder=model_cache,
            )
            self._reranker_factory = partial(
                SentenceTransformerCrossEncoderProvider,
                local_files_only=local_files_only, cache_folder=model_cache,
            )
        self._collector = Collector("indexing", self.identity)
        self.diagnostics = DiagnosticService(self._collector, IndexReportAuthorizer(database))

    def _budget(self) -> PrivateBudget:
        return PrivateBudget(Deadline(time.monotonic() + 30))

    def _search_in_context(
        self, context: CanonicalReadContext, query: str,
        configuration: IndexConfiguration = DEFAULT_CONFIGURATION, *, limit: int = 20,
        capture: CaptureType,
    ) -> SearchSelection:
        """Borrow all authority/lifetime/ledgers; caller must release or discard output."""
        context.check_active()
        if context.identity != self.identity:
            raise EvidenceServiceError("invalid_request")
        try:
            query = _QUERY.validate_python(query, strict=True)
        except ValidationError:
            raise EvidenceServiceError("invalid_request") from None
        _limit(limit, 100)
        configuration = validated(IndexConfiguration, configuration)
        try:
            reranker = self._reranker_factory()
            context.check_active()
            return execute(Pipeline(
                context, query, configuration, limit, self._provider_factory, reranker, capture,
            ))
        except RerankerError:
            raise EvidenceServiceError("unsupported") from None
        except MemoryError:
            raise PrivateResourceStop() from None

    def _begin_child_capture(
        self, context: CanonicalReadContext, group: DisclosureGroup,
        observer: ObserverReference, *, step_id: str, options: ExplainOptions,
    ) -> CaptureType:
        """Stage in the parent's group; never independently publish a child."""
        context.check_active()
        if (
            context.identity != self.identity or observer.observer.session_id != context.session_id
            or observer.observer.scope != context.scope
            or observer.observer.database is not self.database
        ):
            raise EvidenceServiceError("invalid_request")
        return self._collector.begin_capture(
            "search", context.scope, required="read", options=options,
            group=group, observer=observer, step_id=step_id,
        )

    def search(
        self, scope: Scope, query: str, configuration: IndexConfiguration = DEFAULT_CONFIGURATION,
        *, limit: int = 20,
    ) -> CanonicalSearchResult:
        scope = validated(Scope, scope)
        budget = self._budget()
        meter = LocalExecutionMeter(budget, max_operations=1, max_items=10_000)
        capture: CaptureType = self._collector.begin_capture(
            "search", scope, required="read", options=reporting.options(),
        )
        stop_reason: SafeReason | None = None
        try:
            try:
                with (
                    observe(
                        self.database, self.identity, scope, budget.deadline, budget,
                    ) as observer,
                    read_context(
                        self.database, self.identity, scope, observer.session_id,
                        budget.deadline, meter.begin_step("search"),
                    ) as context,
                ):
                    with capture.guard():
                        if isinstance(capture, Capture):
                            capture.group.observers.append(observer.retain())
                    selection = self._search_in_context(
                        context, query, configuration, limit=limit, capture=capture,
                    )
                    result = selection.response.model_copy(update={
                        "items_consumed": meter.public_accounting().items_consumed,
                    })
                    with release_fence(observer, self.identity, scope, budget.deadline) as fence:
                        capture.finish("succeeded")
                        capture.group.release(fence)
                reporting.deliver(self.diagnostics._publish(capture))
                return result
            except DeadlineStop:
                stop_reason = "deadline"
                raise EvidenceServiceError("budget_exceeded") from None
            except PrivateResourceStop:
                stop_reason = "resource_budget"
                raise EvidenceServiceError("budget_exceeded") from None
            except PublicBudgetStop:
                raise EvidenceServiceError("budget_exceeded") from None
        except EvidenceServiceError as error:
            reason = stop_reason or error.failure.code
            capture.group.redact(reason)
            capture.finish("failed", reason=reason,
                           diagnostic_id=error.failure.diagnostic_id)
            reporting.deliver(self.diagnostics._publish(capture))
            raise
        except BaseException:
            capture.group.close()
            raise

    def search_explained(
        self, scope: Scope, query: str, configuration: IndexConfiguration = DEFAULT_CONFIGURATION,
        *, limit: int = 20, options: ExplainOptions = SUMMARY_OPTIONS,
    ) -> Explained[CanonicalSearchResult]:
        return reporting.explained(
            options, self.search, scope, query, configuration, limit=limit,
        )
