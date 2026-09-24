"""Bounded knowledge workflows over public canonical services."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue, TypeAdapter

from kg.client.config import ClientError, Profile
from kg.client.documents import Response, citation_target, submit, token
from kg.evidence.errors import EvidenceServiceError
from kg.knowledge import KnowledgeService
from kg.knowledge._selection import CapturedEvidence
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
    StringObject,
    Token,
    Value,
    WithdrawAssertion,
)
from kg.models.knowledge import ContributionView, EntityView, KnowledgePage
from kg.models.query import SupportInspection, SupportInspectionRequest
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
        value = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_keys)
    except (ValueError, UnicodeError):
        raise ClientError("invalid_input", "Record input must be valid UTF-8 JSON.") from None
    return RecordInput.model_validate_json(json.dumps(normalize_record(value)))


def normalize_record(value: JsonValue) -> JsonValue:
    """Expand request-local names only; native validation still owns change shapes."""
    if not isinstance(value, dict):
        return value
    declarations = value.get("support")
    if not isinstance(declarations, dict):
        return value
    if not 1 <= len(declarations) <= MAX_SUPPORTS:
        raise ClientError("invalid_support", "Named support requires 1..200 declarations.")
    changes = value.get("changes")
    if not isinstance(changes, list) or not 1 <= len(changes) <= MAX_CHANGES:
        raise ClientError("invalid_input", "Named support requires 1..100 changes.")
    declared = TypeAdapter(dict[Token, CapturedSupport]).validate_json(
        json.dumps(declarations)
    )
    used: set[str] = set()
    expanded: list[JsonValue] = []
    occurrences = 0
    for change in changes:
        if not isinstance(change, dict):
            raise ClientError("invalid_input", "Each change must be an object.")
        support = change.get("support")
        if not isinstance(support, dict) or support.get("kind") != "source":
            raise ClientError("invalid_support", "Named support requires source evidence names.")
        names = support.get("evidence")
        if not isinstance(names, list) or not 1 <= len(names) <= MAX_SUPPORTS:
            raise ClientError("invalid_support", "Each change requires 1..200 evidence names.")
        occurrences += len(names)
        if occurrences > MAX_SUPPORTS:
            raise ClientError(
                "invalid_support", "Changes exceed 200 expanded evidence occurrences."
            )
        if any(not isinstance(name, str) for name in names):
            raise ClientError("invalid_support", "Do not mix evidence names and native references.")
        if len(set(names)) != len(names):
            raise ClientError("invalid_support", "Duplicate evidence name in a change.")
        evidence: list[JsonValue] = []
        for name in names:
            if not isinstance(name, str) or name not in declared:
                raise ClientError("invalid_support", "Unknown request-local support name.")
            used.add(name)
            evidence.append(declared[name].reference.model_dump(mode="json"))
        expanded.append({**change, "support": {**support, "evidence": evidence}})
    if used != set(declared):
        raise ClientError("invalid_support", "Every declared support name must be used.")
    return {
        **value,
        "support": [entry.model_dump(mode="json") for entry in declared.values()],
        "changes": expanded,
    }


def record_schema() -> dict[str, JsonValue]:
    schema = RecordInput.model_json_schema()
    native = schema["properties"]["support"]
    named = {
        "type": "object",
        "minProperties": 1,
        "maxProperties": MAX_SUPPORTS,
        "propertyNames": TypeAdapter(Token).json_schema(),
        "additionalProperties": {"$ref": "#/$defs/CapturedSupport"},
    }
    schema["properties"]["support"] = {"oneOf": [native, named]}
    reference = schema["$defs"]["SourceSupport"]["properties"]["evidence"]["items"]
    name = TypeAdapter(Token).json_schema()
    schema["$defs"]["SourceSupport"]["properties"]["evidence"]["items"] = {
        "oneOf": [reference, name]
    }
    schema["oneOf"] = [
        {
            "properties": {
                "support": {"type": mode},
                "changes": {
                    "items": {
                        "properties": {
                            "support": {
                                "properties": {"evidence": {"items": item}},
                            }
                        }
                    }
                },
            }
        }
        for mode, item in (("array", reference), ("object", name))
    ]
    schema["description"] = (
        "Use a captured-support array with native evidence references, OR a named map "
        "with evidence names only. Names must resolve and all declarations must be used. "
        "Duplicate JSON keys/evidence, conflicting states and seeds are rejected. "
        "Expanded canonical change/evidence/request-byte limits apply at runtime."
    )
    return schema


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
                        or not isinstance(payload, AddAssertion)
                        or payload.interpretation != "explicit"
                        or not isinstance(payload.object, StringObject)
                        or payload.support != record.support
                        or tuple(item.reference for item in contribution.evidence)
                        != record.support.evidence
                    ):
                        raise EvidenceServiceError("state_changed")
                    decisions.append({
                        "assertion_id": record.record_id,
                        "target": f"fact:{record.record_id}",
                        "text": payload.object.value,
                        **captured_fields(contribution.evidence),
                    })
                final = inspect()
                if (
                    final.records != support.records
                    or final.next_ordinal != support.next_ordinal
                    or final.exhausted != support.exhausted
                ):
                    raise EvidenceServiceError("state_changed")
                data.update({
                    "inspection": final.model_dump(mode="json"),
                    "targets": [f"fact:{record.record_id}" for record in final.records],
                    "count": result.data.count,
                    "exact": result.data.exact,
                    "selection_complete": result.data.exact,
                    "display_complete": result.data.exact and final.exhausted,
                    "display_truncated": not final.exhausted,
                    "decisions": decisions,
                })
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
            data.update({
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
                        **captured_fields(tuple(item.captured for item in member.decision.support)),
                        "relationship_ids": list(member.relationship_ids),
                    }
                    for member in result.members
                ],
            })
        return Response(
            status=result.outcome,
            code=result.error.code if result.error else None,
            exit_code=0 if result.outcome in {"complete", "empty"} else 3,
            message="Explicit relationship decisions, exact submitted-ID count and full proofs. "
            "Display truncation does not truncate the native selection or its budgets.",
            result=data,
        )
