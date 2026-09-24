"""Bounded knowledge workflows over public canonical services."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue

from kg.client.config import ClientError, Profile
from kg.client.documents import Response, citation_target, submit, token
from kg.knowledge import KnowledgeService
from kg.models.evidence import StoredCitation
from kg.models.foundation import (
    MAX_CHANGES,
    MAX_REQUEST_BYTES,
    MAX_SUPPORTS,
    AddAssertion,
    AggregateResult,
    Change,
    ChangeSet,
    CountStep,
    DocumentDependency,
    EntityObject,
    EvidenceRef,
    QueryRequest,
    RecordsStep,
    ResolveStep,
    SourceSupport,
    StoredEntity,
    Token,
    Value,
    WithdrawAssertion,
)
from kg.models.knowledge import ContributionView, EntityView, KnowledgePage
from kg.models.query import SupportInspectionRequest
from kg.query import QueryService


class CapturedSupport(Value):
    reference: EvidenceRef
    state_version: Token


class RecordInput(Value):
    """Native changes plus the unmodified support objects returned by read."""

    support: tuple[CapturedSupport, ...] = Field(min_length=1, max_length=MAX_SUPPORTS)
    changes: tuple[Change, ...] = Field(min_length=1, max_length=MAX_CHANGES)

    def payload(self) -> ChangeSet:
        dependencies: dict[tuple[str, str], DocumentDependency] = {}
        captured = {entry.reference for entry in self.support}
        if len(captured) != len(self.support):
            raise ClientError("invalid_support", "Duplicate captured evidence.")
        used: set[EvidenceRef] = set()
        for change in self.changes:
            if not isinstance(change.support, SourceSupport):
                raise ClientError(
                    "invalid_support", "Record requires exact source support, not seeds."
                )
            used.update(change.support.evidence)
        if used != captured:
            raise ClientError("invalid_support", "Copy exactly the support used by these changes.")
        for entry in self.support:
            ref = entry.reference
            dependency = DocumentDependency(
                source_namespace=ref.source_namespace,
                document_id=ref.document_id,
                revision_id=ref.revision_id,
                state_version=entry.state_version,
            )
            key = (ref.source_namespace, ref.document_id)
            if key in dependencies and dependencies[key] != dependency:
                raise ClientError("invalid_support", "Conflicting captured document states.")
            dependencies[key] = dependency
        return ChangeSet(
            operation="enrich", dependencies=tuple(dependencies.values()), changes=self.changes
        )


def _unique_keys(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, value in pairs:
        if key in result:
            raise ClientError("invalid_input", f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def record_input(file: Path) -> RecordInput:
    if file.is_symlink() or not file.is_file():
        raise ClientError("invalid_file", "Record input must be a regular UTF-8 JSON file.")
    with file.open("rb") as stream:
        data = stream.read(MAX_REQUEST_BYTES + 1)
    if len(data) > MAX_REQUEST_BYTES:
        raise ClientError("invalid_file", "Record input exceeds the canonical request byte limit.")
    try:
        json.loads(data.decode("utf-8"), object_pairs_hook=_unique_keys)
    except (ValueError, UnicodeError):
        raise ClientError("invalid_input", "Record input must be valid UTF-8 JSON.") from None
    return RecordInput.model_validate_json(data)


def entity_entry(entity: EntityView) -> dict[str, JsonValue]:
    basis = entity.witness.basis
    return {
        "target": f"entity:{entity.entity_id}",
        "reference": StoredEntity(kind="stored", entity_id=entity.entity_id).model_dump(
            mode="json"
        ),
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
        "support": [
            {
                "reference": item.reference.model_dump(mode="json"),
                "state_version": item.dependency.state_version,
            }
            for item in contribution.evidence
        ],
        "evidence_targets": [
            citation_target(
                StoredCitation(
                    reference=item.reference,
                    state_version=item.dependency.state_version,
                    metadata_snapshot_id=item.metadata_snapshot_id,
                )
            )
            for item in contribution.evidence
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
        self.service = KnowledgeService(self.database, profile.identity)

    def entities(self, name: str | None, *, after: int, limit: int) -> Response:
        page = self.service.entities(
            self.profile.scope, name=name, after_sequence=after, limit=limit
        )
        return Response(
            status="complete" if page.entries else "empty",
            message="Eligible entities on this page. Exact names/aliases only; no fuzzy matching.",
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
            message="Choose an exact entity:ID from kg find entities; no entity selected.",
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
            if isinstance(payload, AddAssertion) and isinstance(payload.object, EntityObject):
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

    def record(self, file: Path) -> Response:
        return self._write(record_input(file).payload())

    def remove(self, target: str) -> Response:
        return self._write(
            WithdrawAssertion(
                operation="withdraw_assertion", contribution_id=target.removeprefix("fact:")
            )
        )

    def _write(self, payload: ChangeSet | WithdrawAssertion) -> Response:
        outcome = submit(self.profile, payload)
        if isinstance(outcome, Response):
            return outcome
        success = outcome.receipt is not None
        return Response(
            status=outcome.status,
            code=outcome.error.code if outcome.error else None,
            exit_code=0 if success else 4,
            message="Knowledge committed; complete canonical receipt and mappings returned."
            if success
            else "Knowledge write did not complete.",
            result={"write": outcome.model_dump(mode="json")},
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
            data: dict[str, JsonValue] = {"query": execution.model_dump(mode="json")}
            if isinstance(result.data, AggregateResult) and result.result_set_id is not None:
                support = service.inspect_support(
                    SupportInspectionRequest(
                        request_id=token(),
                        scope=self.profile.scope,
                        result_set_id=result.result_set_id,
                        records_step_id="decisions",
                        limit=limit,
                    )
                )
                if support.error:
                    return Response(
                        status=support.outcome,
                        code=support.error.code,
                        exit_code=3,
                        message="Decision support unavailable; rerun after writes finish.",
                        result={"inspection": support.model_dump(mode="json")},
                    )
                data["inspection"] = support.model_dump(mode="json")
                data["targets"] = [f"fact:{record.record_id}" for record in support.records]
                data["display_truncated"] = not support.exhausted
            data["retained_handles_usable"] = False
        return Response(
            status=result.outcome,
            code=execution.stop_reason,
            exit_code=0 if result.outcome in {"complete", "empty"} else 3,
            message="Explicit decisions; count means distinct submitted assertion IDs, not events. "
            "Display may be bounded; retained handles expire when this command exits.",
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
        return Response(
            status=result.outcome,
            code=result.error.code if result.error else None,
            exit_code=0 if result.outcome in {"complete", "empty"} else 3,
            message="Explicit relationship decisions, exact submitted-ID count and full proofs. "
            "Display truncation does not truncate the native selection or its budgets.",
            result={"graph": result.model_dump(mode="json")},
        )
