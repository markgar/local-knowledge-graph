"""Bounded knowledge workflows over public canonical services."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import JsonValue, TypeAdapter

from kg.client.config import ClientError, Profile
from kg.client.documents import Response, citation_target, parse_citation_target, submit, token
from kg.evidence import EvidenceService
from kg.evidence.errors import EvidenceServiceError
from kg.knowledge import KnowledgeService
from kg.knowledge._selection import CapturedEvidence
from kg.models.authoring import RecordAuthoringDocument, RecordAuthoringRequest
from kg.models.evidence import StoredCitation
from kg.models.foundation import (
    MAX_REQUEST_BYTES,
    MAX_SUPPORTS,
    AggregateResult,
    CountStep,
    QueryRequest,
    RecordsStep,
    ResolveStep,
    StoredEntity,
    StringObject,
    Token,
    WithdrawAssertion,
    WithdrawClassification,
)
from kg.models.knowledge import (
    AssertionPayload,
    ContributionEntityObject,
    ContributionView,
    EntityView,
    KnowledgePage,
)
from kg.models.query import SupportInspection, SupportInspectionRequest
from kg.query import QueryService

LOGGER = logging.getLogger(__name__)


def _unique_keys(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ClientError("invalid_input", f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def record_input(file: Path) -> RecordAuthoringDocument:
    if file.is_symlink() or not file.is_file():
        raise ClientError("invalid_file", "Record input must be a regular UTF-8 JSON file.")
    with file.open("rb") as stream:
        data = stream.read(MAX_REQUEST_BYTES + 1)
    if len(data) > MAX_REQUEST_BYTES:
        raise ClientError("invalid_file", "Record input exceeds the canonical request byte limit.")
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_keys)
    except (ValueError, UnicodeError):
        raise ClientError("invalid_input", "Record input must be valid UTF-8 JSON.") from None
    return RecordAuthoringDocument.model_validate_json(json.dumps(value), strict=True)


def record_schema() -> dict[str, JsonValue]:
    return RecordAuthoringDocument.model_json_schema()


def entity_entry(entity: EntityView) -> dict[str, JsonValue]:
    basis = entity.witness.basis
    return {
        "target": f"entity:{entity.entity_id}",
        "entity": entity.model_dump(mode="json"),
        "evidence_targets": [
            citation_target(
                StoredCitation(
                    reference=item.reference,
                    state_version=item.dependency.state_version,
                    metadata_snapshot_id=item.metadata_snapshot_id,
                )
            )
            for item in basis.evidence
        ]
        if basis.kind == "source"
        else [],
    }


def contribution_entry(contribution: ContributionView) -> dict[str, JsonValue]:
    return {
        "target": f"fact:{contribution.contribution_id}",
        "contribution": contribution.model_dump(mode="json"),
        **captured_fields(contribution.evidence),
    }


def captured_fields(evidence: tuple[CapturedEvidence, ...]) -> dict[str, JsonValue]:
    return {
        "support": [
            {
                "reference": item.reference.model_dump(mode="json"),
                "state_version": item.dependency.state_version,
            }
            for item in evidence
        ],
        "evidence_targets": [
            citation_target(
                StoredCitation(
                    reference=item.reference,
                    state_version=item.dependency.state_version,
                    metadata_snapshot_id=item.metadata_snapshot_id,
                )
            )
            for item in evidence
        ],
    }


def page_result(
    page: KnowledgePage[EntityView] | KnowledgePage[ContributionView],
    entries: list[JsonValue],
) -> dict[str, JsonValue]:
    return {
        "entries": entries,
        "has_more": page.has_more,
        "next_after": page.next_after_sequence,
        "selection_complete": not page.has_more,
    }


class Knowledge:
    def __init__(self, profile: Profile) -> None:
        self.profile = profile
        self.database = profile.database()
        self.evidence = EvidenceService(self.database, profile.identity)
        self.service = KnowledgeService(self.database, profile.identity)

    def entities(self, name: str | None, *, after: int, limit: int) -> Response:
        page = self.service.entities(
            self.profile.scope, name=name, after_sequence=after, limit=limit
        )
        return Response(
            status="complete" if page.entries else "empty",
            message="Eligible entities on this page. Exact case-sensitive names/aliases, "
            "not semantic search. Empty/incomplete results do not prove an entity is new. "
            "List/page with kg find entities --after NEXT_AFTER, or inspect source evidence.",
            result=page_result(page, [entity_entry(item) for item in page.entries]),
        )

    def select(self, target: str) -> EntityView | Response:
        if target.startswith("entity:"):
            return self.service.entity(self.profile.scope, target.removeprefix("entity:"))
        # One fenced service call scans eligible matches to EOF or its budget.
        # Two matches suffice to disprove uniqueness; a lone match requires EOF.
        page = self.service.entities(self.profile.scope, name=target, limit=2)
        if len(page.entries) == 1 and not page.has_more:
            return page.entries[0]
        return Response(
            status="ambiguous" if page.entries else "empty",
            code="ambiguous_entity" if page.entries else "entity_not_found",
            message="Choose an exact entity:ID from kg find entities; no entity selected. "
            "Names/aliases match exactly, not semantically. Empty/incomplete results do not "
            "prove an entity is new; list/page entities or inspect source evidence.",
            result=page_result(page, [entity_entry(item) for item in page.entries]),
            exit_code=2,
        )

    def relationships(self, target: str, *, after: int, limit: int) -> Response:
        selected = self.select(target)
        if isinstance(selected, Response):
            return selected
        page = self.service.contributions(
            self.profile.scope,
            selected.entity_id,
            kind="assertion",
            after_sequence=after,
            limit=limit,
        )
        entries: list[JsonValue] = []
        for item in page.entries:
            payload = item.payload
            if isinstance(payload, AssertionPayload) and isinstance(
                payload.object, ContributionEntityObject
            ):
                entry = contribution_entry(item)
                entry["directions"] = [
                    direction
                    for direction, endpoint in (
                        ("outgoing", payload.subject),
                        ("incoming", payload.object.entity),
                    )
                    if isinstance(endpoint, StoredEntity)
                    and endpoint.entity_id == selected.entity_id
                ]
                entries.append(entry)
        return Response(
            status="complete" if entries or page.has_more else "empty",
            message="Entity-valued relationships on this assertion page. "
            "An empty filtered page is not proof of no relationships; follow continuation.",
            result={"selected": entity_entry(selected), **page_result(page, entries)},
        )

    def read(self, target: str, *, history: bool, after: int, limit: int) -> Response:
        mode: Literal["history", "current"] = "history" if history else "current"
        if target.startswith("fact:"):
            item = self.service.contribution(
                self.profile.scope, target.removeprefix("fact:"), mode=mode
            )
            return Response(
                status="complete",
                message="Authorized contribution and exact captured support.",
                result=contribution_entry(item),
            )
        entity_id = target.removeprefix("entity:")
        entity = self.service.entity(self.profile.scope, entity_id, mode=mode)
        page = self.service.contributions(
            self.profile.scope, entity_id, mode=mode, after_sequence=after, limit=limit
        )
        return Response(
            status="complete",
            message="Authorized entity and contributions on this page.",
            result={
                **entity_entry(entity),
                **page_result(page, [contribution_entry(item) for item in page.entries]),
            },
        )

    def record(self, file: Path, *, retry_key: str) -> Response:
        TypeAdapter(Token).validate_python(retry_key, strict=True)
        request = RecordAuthoringRequest(
            interface_version="record-authoring/1",
            request_id=str(uuid4()),
            retry_key=retry_key,
            scope=self.profile.scope,
            attribution=self.profile.attribution,
            document=record_input(file),
        )
        try:
            outcome = self.service.record(request)
        except (Exception, KeyboardInterrupt):
            LOGGER.error("Record authoring raised; commit outcome is unknown.")
            from kg.knowledge._record import digest

            return Response(
                status="uncertain",
                code="write_outcome_unknown",
                exit_code=7,
                message="Write outcome unknown. Retry only the exact input and saved retry key.",
                result={
                    "request_id": request.request_id,
                    "retry_key": request.retry_key,
                    "prepared_input_digest": digest(request),
                },
            )
        return Response(
            status=outcome.status,
            code=outcome.error.code if outcome.error else None,
            exit_code=0 if outcome.receipt is not None else 4,
            message=(
                "Authored knowledge committed with exact provenance and canonical receipt."
                if outcome.receipt is not None
                else "Knowledge authoring did not complete."
            ),
            result=outcome.model_dump(mode="json"),
        )

    def record_template(self, targets: tuple[str, ...]) -> Response:
        if not 1 <= len(targets) <= MAX_SUPPORTS:
            raise ClientError("invalid_support", "Record preparation requires 1..200 evidence.")
        citations = [parse_citation_target(target) for target in targets]
        identities = [citation.model_dump_json() for citation in citations]
        if len(set(identities)) != len(identities):
            raise ClientError("invalid_support", "Duplicate captured evidence.")
        support: dict[str, dict[str, JsonValue]] = {}
        for index, citation in enumerate(citations, start=1):
            view = self.evidence.citation(self.profile.scope, citation)
            if not view.is_current_support:
                raise ClientError(
                    "state_conflict",
                    "Evidence is no longer current support. Read current evidence and reassess.",
                )
            support[f"evidence-{index}"] = {
                "reference": view.reference.model_dump(mode="json"),
                "state_version": view.state_version,
            }
        schema = self.service.schema(self.profile.scope)
        if schema.status != "configured" or schema.revision is None:
            raise ClientError(
                "schema_not_configured",
                "Record preparation requires an approved schema. Inspect schema workflow help.",
            )
        return Response(
            status="incomplete_template",
            message=(
                "Exact support and schema revision prepared. Fill identity reuse/newness, names, "
                "types, predicates, values, interpretation, selections and rationales before "
                "submitting; no write occurred."
            ),
            result={
                "record_template": {
                    "interface_version": "record-authoring/1",
                    "expected_schema_revision": schema.revision.model_dump(mode="json"),
                    "support": {
                        name: {"kind": "source", **capture} for name, capture in support.items()
                    },
                    "entities": [],
                    "assertions": [],
                }
            },
        )

    def classifications(
        self,
        target: str,
        *,
        history: bool,
        after_event_id: str | None,
        limit: int,
        claim_ids: tuple[str, ...] | None,
    ) -> Response:
        if not target.startswith("entity:"):
            raise ClientError("invalid_target", "Use the exact entity:ID from a receipt or read.")
        entity_id = target.removeprefix("entity:")
        value = (
            self.service.classification_history(
                self.profile.scope,
                entity_id,
                after_event_id=after_event_id,
                limit=limit,
            )
            if history
            else self.service.classification_review(
                self.profile.scope,
                entity_id,
                claim_ids=claim_ids,
            )
        )
        return Response(
            status="complete",
            message="Authorized classification history."
            if history
            else (
                "Explicit subset review; selecting requires accept_incomplete_review=true."
                if claim_ids is not None
                else "Complete review of authorized current claims."
            ),
            result=value.model_dump(mode="json"),
        )

    def withdraw_classification(self, target: str, *, retry_key: str) -> Response:
        if not target.startswith("fact:"):
            raise ClientError("invalid_target", "Use the exact fact:ID of a classification claim.")
        TypeAdapter(Token).validate_python(retry_key, strict=True)
        return self._write(
            WithdrawClassification(
                operation="withdraw_classification",
                contribution_id=target.removeprefix("fact:"),
            ),
            retry_key=retry_key,
        )

    def remove(self, target: str) -> Response:
        return self._write(
            WithdrawAssertion(
                operation="withdraw_assertion", contribution_id=target.removeprefix("fact:")
            )
        )

    def _write(
        self,
        payload: WithdrawAssertion | WithdrawClassification,
        *,
        retry_key: str | None = None,
    ) -> Response:
        outcome = submit(self.profile, payload, retry_key=retry_key)
        if isinstance(outcome, Response):
            return outcome
        success = outcome.receipt is not None
        result: dict[str, JsonValue] = {"knowledge_write": outcome.model_dump(mode="json")}
        if success:
            message = "Owned knowledge withdrawal committed. Canonical receipt returned."
        else:
            message = "Knowledge write did not complete."
        return Response(
            status=outcome.status,
            code=outcome.error.code if outcome.error else None,
            exit_code=0 if success else 4,
            message=message,
            result=result,
        )

    def decisions(self, target: str, *, through: str | None, limit: int) -> Response:
        if through is not None:
            return self._relationship_decisions(target, through, limit)
        selector = ResolveStep(
            operation="resolve",
            step_id="entity",
            entity_id=target.removeprefix("entity:") if target.startswith("entity:") else None,
            name=None if target.startswith("entity:") else target,
        )
        request = QueryRequest(
            contract_version="foundation/1",
            request_id=token(),
            scope=self.profile.scope,
            steps=(
                selector,
                RecordsStep(
                    operation="records",
                    step_id="decisions",
                    entity_step="entity",
                    record_type="decision",
                ),
                CountStep(operation="count", step_id="count", records_step="decisions"),
            ),
            output_step="count",
        )
        with QueryService(self.database, self.profile.identity) as service:
            execution = service.execute(request)
            result = execution.result
            execution_summary: dict[str, JsonValue] = {
                **result.model_dump(
                    mode="json",
                    exclude={"data", "continuation"},
                ),
                "query_elapsed_milliseconds": execution.elapsed_milliseconds,
                "inspection_elapsed_milliseconds": None,
                "steps": [step.model_dump(mode="json") for step in execution.steps],
                "stop_reason": execution.stop_reason,
                "work_accounting": execution.work_accounting,
                "support_inspection": None,
            }
            data: dict[str, JsonValue] = {"execution": execution_summary}
            if isinstance(result.data, AggregateResult) and result.result_set_id is not None:
                aggregate = result.data
                inspection = SupportInspectionRequest(
                    request_id=token(),
                    scope=self.profile.scope,
                    result_set_id=result.result_set_id,
                    records_step_id="decisions",
                    limit=limit,
                )

                def inspect() -> SupportInspection:
                    support = service.inspect_support(
                        inspection.model_copy(update={"request_id": token()})
                    )
                    if support.error:
                        raise EvidenceServiceError(support.error.code)
                    if (
                        support.read_state_id != result.read_state_id
                        or support.result_set_id != result.result_set_id
                        or support.records_step_id != "decisions"
                        or support.total != aggregate.count
                        or support.exact != aggregate.exact
                        or len(support.records) > limit
                    ):
                        raise EvidenceServiceError("state_changed")
                    return support

                support = inspect()
                decisions: list[JsonValue] = []
                for record in support.records:
                    contribution = self.service.contribution(self.profile.scope, record.record_id)
                    payload = contribution.payload
                    if (
                        contribution.contribution_id != record.record_id
                        or record.record_type != "decision"
                        or not contribution.is_current
                        or not isinstance(payload, AssertionPayload)
                        or payload.interpretation != "explicit"
                        or not isinstance(payload.object, StringObject)
                        or payload.support != record.support
                        or tuple(item.reference for item in contribution.evidence)
                        != record.support.evidence
                    ):
                        raise EvidenceServiceError("state_changed")
                    decisions.append(
                        {
                            "assertion_id": record.record_id,
                            "target": f"fact:{record.record_id}",
                            "text": payload.object.value,
                            **captured_fields(contribution.evidence),
                        }
                    )
                final = inspect()
                if (
                    final.records != support.records
                    or final.next_ordinal != support.next_ordinal
                    or final.exhausted != support.exhausted
                ):
                    raise EvidenceServiceError("state_changed")
                data.update(
                    {
                        "execution": {
                            **execution_summary,
                            "inspection_elapsed_milliseconds": final.elapsed_milliseconds,
                            "support_inspection": {
                                "records_step_id": final.records_step_id,
                                "total": final.total,
                                "exact": final.exact,
                                "next_ordinal": final.next_ordinal,
                                "exhausted": final.exhausted,
                                "stop_reason": final.stop_reason,
                                "error": (
                                    final.error.model_dump(mode="json")
                                    if final.error is not None
                                    else None
                                ),
                            },
                        },
                        "count": result.data.count,
                        "exact": result.data.exact,
                        "selection_complete": result.data.exact,
                        "display_complete": result.data.exact and final.exhausted,
                        "display_truncated": not final.exhausted,
                        "decisions": decisions,
                    }
                )
            data["retained_handles_usable"] = False
        return Response(
            status=result.outcome,
            code=execution.stop_reason,
            exit_code=0 if result.outcome in {"complete", "empty"} else 3,
            message="Explicit decisions; count means distinct submitted assertion IDs, not events. "
            "Text/support are separate authorized reads checked against retained membership, "
            "not one atomic snapshot. Display may be bounded; handles expire on exit.",
            result=data,
        )

    def _relationship_decisions(self, target: str, through: str, limit: int) -> Response:
        from kg.graph import GraphSessionError, LocalGraphSession
        from kg.models.graph import GraphEntitySelector, GraphRelationshipDecisionsRequest

        request = GraphRelationshipDecisionsRequest(
            request_id=token(),
            scope=self.profile.scope,
            start=GraphEntitySelector(
                entity_id=target.removeprefix("entity:") if target.startswith("entity:") else None,
                name=None if target.startswith("entity:") else target,
            ),
            predicate=through.removeprefix("^"),
            direction="incoming" if through.startswith("^") else "outgoing",
            display_limit=limit,
        )
        # The session exclusively owns its fresh child directory and cleanup.
        directory = Path(self.profile.store).parent / "graphs"
        try:
            with LocalGraphSession(
                self.database, self.profile.identity, self.profile.scope, graph_directory=directory
            ) as session:
                result = session.relationship_decisions(request)
        except GraphSessionError as error:
            return Response(
                status="failed",
                code=error.failure.code,
                exit_code=3,
                message="Graph operation/cleanup failed; no successful result released.",
            )
        data: dict[str, JsonValue] = {"graph": result.model_dump(mode="json")}
        if result.count is not None:
            data.update(
                {
                    "count": result.count,
                    "exact": result.exact,
                    "selection_complete": result.exact,
                    "display_complete": not result.display_truncated,
                    "display_truncated": result.display_truncated,
                    "decisions": [
                        {
                            "assertion_id": member.decision.assertion_id,
                            "target": f"fact:{member.decision.assertion_id}",
                            "text": member.decision.decision_text,
                            **captured_fields(
                                tuple(item.captured for item in member.decision.support)
                            ),
                            "relationship_ids": list(member.relationship_ids),
                        }
                        for member in result.members
                    ],
                }
            )
        return Response(
            status=result.outcome,
            code=result.error.code if result.error else None,
            exit_code=0 if result.outcome in {"complete", "empty"} else 3,
            message="Explicit relationship decisions, exact submitted-ID count and full proofs. "
            "Display truncation does not truncate the native selection or its budgets.",
            result=data,
        )
