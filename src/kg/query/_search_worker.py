"""Actual E3 invocation within Q1's inherited spawned read context."""

import logging

from kg.diagnostics._collector import Capture
from kg.evidence._read_context import CanonicalReadContext
from kg.evidence.database import EvidenceDatabase
from kg.evidence.errors import EvidenceServiceError
from kg.indexing import EvidenceSearchService
from kg.models.foundation import SearchStep
from kg.query._dispatch import Stopped
from kg.query._meter import Frame, RemoteBudget
from kg.query._plan_worker import transmit
from kg.query._search import SearchWork
from kg.query._search_capture import transmit_capture


def execute(
    database: EvidenceDatabase,
    context: CanonicalReadContext,
    budget: RemoteBudget,
    work: SearchWork,
) -> None:
    output = next(s for s in work.request.steps if s.step_id == work.request.output_step)
    if not isinstance(output, SearchStep):
        raise ValueError("Invalid search output")
    budget.rpc(Frame(action="begin", step_id=output.step_id))
    service = EvidenceSearchService(database, context.identity)
    capture = (
        service._collector.begin_capture(
            "search", context.scope, required="read", options=work.options,
        )
        if work.capture else service._collector._not_collected
    )
    def reclaim_capture() -> None:
        logging.getLogger(__name__).warning("Query search report unavailable: business scratch")
        capture.group.discard()
        if isinstance(capture, Capture):
            capture.binding.targets.clear()

    budget.reclaim_capture = reclaim_capture if work.capture else None
    try:
        try:
            selection = service._search_in_context(
                context, output.text, work.configuration, limit=output.limit, capture=capture,
            )
        except EvidenceServiceError as error:
            if error.failure.code == "unsupported":
                raise Stopped("unsupported", "provider_unavailable") from None
            if error.failure.code == "stale_index":
                raise Stopped("stale_index", "index_not_ready") from None
            raise
        selection.ranked.projection.check(context)
        transmit_capture(budget, capture)
        transmit(budget, selection.ranked.result, "ranked")
    finally:
        budget.reclaim_capture = None
        capture.group.close()
