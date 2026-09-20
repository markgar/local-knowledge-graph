"""Versioned foundation values, not storage, authorization, or execution services."""

from __future__ import annotations

import hashlib
from typing import Annotated, Final, Literal, Self

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    model_validator,
)

MAX_TEXT_BYTES: Final = 5_000_000
MAX_REQUEST_BYTES: Final = 8_000_000
MAX_BATCH_BYTES: Final = 16_000_000
MAX_BATCH_ITEMS: Final = 100
MAX_CHANGES: Final = 100
MAX_SUPPORTS: Final = 200


def _unicode(value: str) -> str:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise ValueError("text must be valid Unicode") from error
    return value


def _nonblank(value: str) -> str:
    if not value.strip():
        raise ValueError("value must not be blank")
    return _unicode(value)


Token = Annotated[str, Field(min_length=1, max_length=256), AfterValidator(_nonblank)]
Label = Annotated[str, Field(min_length=1, max_length=4096), AfterValidator(_nonblank)]
Text = Annotated[str, AfterValidator(_unicode)]
Name = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.:-]{0,127}$")]
Scalar = StrictStr | StrictInt | StrictBool


class Value(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class Versioned(Value):
    contract_version: Literal["foundation/1"]


class AccessContext(Value):
    """An effective context supplied by a trusted boundary, never a credential."""

    principal_id: Token
    policy_version: Token
    namespaces: tuple[Token, ...] = Field(min_length=1, max_length=100)
    grants: tuple[Literal["read", "write_documents", "write_knowledge", "seed"], ...] = Field(
        min_length=1, max_length=4
    )

    @model_validator(mode="after")
    def unique_scope(self) -> Self:
        if len(set(self.namespaces)) != len(self.namespaces):
            raise ValueError("duplicate namespaces")
        if len(set(self.grants)) != len(self.grants):
            raise ValueError("duplicate grants")
        return self


class Scope(Value):
    corpus_id: Token
    access: AccessContext


class Attribution(Value):
    owner_id: Token
    writer_id: Token
    producer: Token
    producer_version: Token
    model_id: Token | None = None
    configuration_id: Token | None = None


class ExternalDocument(Value):
    source_namespace: Token
    synchronization_scope: Token
    external_id: Token


class SynchronizationSnapshot(Versioned):
    scope: Scope
    attribution: Attribution
    source_namespace: Token
    synchronization_scope: Token
    run_id: Token
    expected_generation: Token
    enumeration: Literal["partial", "failed", "complete"]

    @model_validator(mode="after")
    def declared_namespace(self) -> Self:
        if self.source_namespace not in self.scope.access.namespaces:
            raise ValueError("snapshot outside declared namespaces")
        if "write_documents" not in self.scope.access.grants:
            raise ValueError("document grant required")
        return self


class MetadataEntry(Value):
    key: Name
    value: Scalar


class SourceMetadata(Value):
    title: Label
    location: Label
    event_time: AwareDatetime | None = None
    attributes: tuple[MetadataEntry, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def unique_keys(self) -> Self:
        if len({entry.key for entry in self.attributes}) != len(self.attributes):
            raise ValueError("duplicate metadata keys")
        for entry in self.attributes:
            if isinstance(entry.value, str):
                _unicode(entry.value)
                if len(entry.value) > 4096:
                    raise ValueError("metadata value too long")
        return self


class MetadataSnapshot(Versioned):
    document_id: Token
    revision_id: Token
    state_version: Token
    metadata_snapshot_id: Token
    metadata: SourceMetadata


class SuppliedAnchor(Value):
    local_id: Token
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: Text

    @model_validator(mode="after")
    def ordered(self) -> Self:
        if self.end <= self.start:
            raise ValueError("anchor must be a nonempty half-open range")
        return self


class SuppliedContent(Value):
    text: Text
    anchors: tuple[SuppliedAnchor, ...] = Field(default=(), max_length=1000)
    passage_policy: Token

    @model_validator(mode="after")
    def exact_anchors(self) -> Self:
        if len(self.text.encode("utf-8")) > MAX_TEXT_BYTES:
            raise ValueError("supplied content exceeds byte limit")
        if len({anchor.local_id for anchor in self.anchors}) != len(self.anchors):
            raise ValueError("duplicate anchor local IDs")
        for anchor in self.anchors:
            if anchor.end > len(self.text) or self.text[anchor.start:anchor.end] != anchor.quote:
                raise ValueError("anchor must match exact source slice")
        return self

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


class CreateOnly(Value):
    kind: Literal["create"]


class ExpectedState(Value):
    kind: Literal["match"]
    state_version: Token


Precondition = Annotated[CreateOnly | ExpectedState, Field(discriminator="kind")]


class PutDocument(Value):
    operation: Literal["put_document"]
    document: ExternalDocument
    precondition: Precondition
    content: SuppliedContent
    metadata: SourceMetadata


class RemoveDocument(Value):
    operation: Literal["remove_document"]
    document: ExternalDocument
    precondition: ExpectedState


class EvidenceRef(Value):
    corpus_id: Token
    source_namespace: Token
    document_id: Token
    revision_id: Token
    anchor_id: Token
    passage_id: Token | None = None


class DocumentDependency(Value):
    source_namespace: Token
    document_id: Token
    revision_id: Token
    state_version: Token


class LocalEntity(Value):
    kind: Literal["local"]
    local_id: Token


class StoredEntity(Value):
    kind: Literal["stored"]
    entity_id: Token


EntityRef = Annotated[LocalEntity | StoredEntity, Field(discriminator="kind")]


class SourceSupport(Value):
    kind: Literal["source"]
    evidence: tuple[EvidenceRef, ...] = Field(min_length=1, max_length=MAX_SUPPORTS)

    @model_validator(mode="after")
    def unique_evidence(self) -> Self:
        if len(set(self.evidence)) != len(self.evidence):
            raise ValueError("duplicate evidence support")
        return self


class SeedSupport(Value):
    kind: Literal["seed"]
    seed_key: Token


Support = Annotated[SourceSupport | SeedSupport, Field(discriminator="kind")]


class CreateEntity(Value):
    kind: Literal["entity"]
    local_id: Token
    name: Label
    entity_type: Name
    support: Support


class AddAlias(Value):
    kind: Literal["alias"]
    local_id: Token
    entity: EntityRef
    alias: Label
    support: Support


class AddIdentifier(Value):
    kind: Literal["identifier"]
    local_id: Token
    entity: EntityRef
    scheme: Name
    value: Token
    support: Support


class AddMention(Value):
    kind: Literal["mention"]
    local_id: Token
    entity: EntityRef
    support: SourceSupport

    @model_validator(mode="after")
    def passage_required(self) -> Self:
        if any(ref.passage_id is None for ref in self.support.evidence):
            raise ValueError("mentions require passage evidence")
        return self


class EntityObject(Value):
    kind: Literal["entity"]
    entity: EntityRef


class StringObject(Value):
    kind: Literal["string"]
    value: Label


class IntegerObject(Value):
    kind: Literal["integer"]
    value: int


class BooleanObject(Value):
    kind: Literal["boolean"]
    value: bool


class TimestampObject(Value):
    kind: Literal["timestamp"]
    value: AwareDatetime


AssertionObject = Annotated[
    EntityObject | StringObject | IntegerObject | BooleanObject | TimestampObject,
    Field(discriminator="kind"),
]


class AddAssertion(Value):
    kind: Literal["assertion"]
    local_id: Token
    subject: EntityRef
    predicate: Name
    object: AssertionObject
    interpretation: Literal["explicit", "inferred"]
    support: SourceSupport


Change = Annotated[
    CreateEntity | AddAlias | AddIdentifier | AddMention | AddAssertion,
    Field(discriminator="kind"),
]


class ChangeSet(Value):
    operation: Literal["enrich"]
    dependencies: tuple[DocumentDependency, ...] = Field(default=(), max_length=MAX_SUPPORTS)
    changes: tuple[Change, ...] = Field(min_length=1, max_length=MAX_CHANGES)

    @model_validator(mode="after")
    def references_and_dependencies(self) -> Self:
        local_ids = [change.local_id for change in self.changes]
        if len(set(local_ids)) != len(local_ids):
            raise ValueError("change local IDs must be unique")
        entities = {change.local_id for change in self.changes if isinstance(change, CreateEntity)}
        evidence: list[EvidenceRef] = []
        for change in self.changes:
            refs: list[EntityRef] = []
            if isinstance(change, (AddAlias, AddIdentifier, AddMention)):
                refs.append(change.entity)
            elif isinstance(change, AddAssertion):
                refs.append(change.subject)
                if isinstance(change.object, EntityObject):
                    refs.append(change.object.entity)
            if any(isinstance(ref, LocalEntity) and ref.local_id not in entities for ref in refs):
                raise ValueError("unresolved request-local entity")
            if isinstance(change.support, SourceSupport):
                evidence.extend(change.support.evidence)
        if len(evidence) > MAX_SUPPORTS:
            raise ValueError("change set exceeds total evidence reference limit")
        dependencies = {
            (dep.source_namespace, dep.document_id): dep for dep in self.dependencies
        }
        if len(dependencies) != len(self.dependencies):
            raise ValueError("duplicate supporting document dependency")
        used: set[tuple[str, str]] = set()
        for ref in evidence:
            key = (ref.source_namespace, ref.document_id)
            dependency = dependencies.get(key)
            if dependency is None or dependency.revision_id != ref.revision_id:
                raise ValueError("every evidence revision requires a matching document dependency")
            used.add(key)
        if used != set(dependencies):
            raise ValueError("unused supporting document dependency")
        return self


WritePayload = Annotated[PutDocument | RemoveDocument | ChangeSet, Field(discriminator="operation")]


class WriteRequest(Versioned):
    request_id: Token
    retry_key: Token
    scope: Scope
    attribution: Attribution
    payload: WritePayload

    @model_validator(mode="after")
    def declared_scope_and_size(self) -> Self:
        access = self.scope.access
        if isinstance(self.payload, ChangeSet):
            if "write_knowledge" not in access.grants:
                raise ValueError("knowledge grant required")
            for change in self.payload.changes:
                if isinstance(change.support, SeedSupport):
                    if "seed" not in access.grants:
                        raise ValueError("seed grant required")
                else:
                    for ref in change.support.evidence:
                        if ref.corpus_id != self.scope.corpus_id:
                            raise ValueError("cross-corpus evidence")
                        if ref.source_namespace not in access.namespaces:
                            raise ValueError("evidence outside declared namespaces")
        else:
            if "write_documents" not in access.grants:
                raise ValueError("document grant required")
            if self.payload.document.source_namespace not in access.namespaces:
                raise ValueError("document outside declared namespaces")
        if len(self.model_dump_json().encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError("request exceeds serialized byte limit")
        return self


class WriteBatch(Versioned):
    batch_id: Token
    items: tuple[WriteRequest, ...] = Field(min_length=1, max_length=MAX_BATCH_ITEMS)

    @model_validator(mode="after")
    def unique_items_and_size(self) -> Self:
        if len({item.request_id for item in self.items}) != len(self.items):
            raise ValueError("duplicate request IDs")
        if len(self.model_dump_json().encode("utf-8")) > MAX_BATCH_BYTES:
            raise ValueError("batch exceeds serialized byte limit")
        return self


class ProcessingState(Value):
    state_version: Token
    source: Literal["active", "inactive"]
    indexing: Literal["pending", "ready", "failed"]
    enrichment: Literal["pending", "partial", "complete", "failed"]


class DocumentReceipt(Value):
    kind: Literal["document"]
    document_id: Token
    revision_id: Token
    metadata_snapshot_id: Token
    processing: ProcessingState


class IDMapping(Value):
    local_id: Token
    stored_id: Token


class ChangeSetReceipt(Value):
    kind: Literal["enrichment"]
    mappings: tuple[IDMapping, ...] = Field(min_length=1, max_length=MAX_CHANGES)

    @model_validator(mode="after")
    def unique_local_ids(self) -> Self:
        if len({mapping.local_id for mapping in self.mappings}) != len(self.mappings):
            raise ValueError("duplicate local ID mapping")
        return self


Receipt = Annotated[DocumentReceipt | ChangeSetReceipt, Field(discriminator="kind")]
ErrorCode = Literal[
    "invalid_request", "forbidden", "not_found", "state_conflict", "retry_conflict",
    "retry_expired", "unsupported", "state_changed", "stale_index", "budget_exceeded",
    "internal_error",
]


class Failure(Value):
    code: ErrorCode
    diagnostic_id: Token


class WriteOutcome(Value):
    request_id: Token
    status: Literal["applied", "unchanged", "rejected", "conflict", "failed"]
    receipt: Receipt | None = None
    error: Failure | None = None

    @model_validator(mode="after")
    def exclusive_outcome(self) -> Self:
        success = self.status in {"applied", "unchanged"}
        if success != (self.receipt is not None) or success == (self.error is not None):
            raise ValueError("success requires receipt only; failure requires error only")
        if self.error is not None:
            conflict = self.error.code in {"state_conflict", "retry_conflict"}
            if conflict != (self.status == "conflict"):
                raise ValueError("conflict status and code must agree")
        return self


class BatchResult(Versioned):
    batch_id: Token
    status: Literal["complete", "partial", "failed"]
    outcomes: tuple[WriteOutcome, ...] = Field(min_length=1, max_length=MAX_BATCH_ITEMS)

    @model_validator(mode="after")
    def summary(self) -> Self:
        if len({outcome.request_id for outcome in self.outcomes}) != len(self.outcomes):
            raise ValueError("duplicate outcome request IDs")
        successes = sum(outcome.receipt is not None for outcome in self.outcomes)
        expected = (
            "complete" if successes == len(self.outcomes) else "partial" if successes else "failed"
        )
        if self.status != expected:
            raise ValueError("batch status disagrees with outcomes")
        return self

    def validate_for(self, request: WriteBatch) -> None:
        if self.batch_id != request.batch_id or tuple(o.request_id for o in self.outcomes) != tuple(
            item.request_id for item in request.items
        ):
            raise ValueError("batch outcomes must correlate in input order")
        for item, outcome in zip(request.items, self.outcomes, strict=True):
            if outcome.receipt is None:
                continue
            if isinstance(item.payload, ChangeSet):
                if not isinstance(outcome.receipt, ChangeSetReceipt) or {
                    mapping.local_id for mapping in outcome.receipt.mappings
                } != {change.local_id for change in item.payload.changes}:
                    raise ValueError("enrichment receipt must map every change")
            elif not isinstance(outcome.receipt, DocumentReceipt):
                raise ValueError("document write requires document receipt")


class ContentResult(Versioned):
    document_id: Token
    revision_id: Token
    state: Literal["available", "unavailable"]
    text: Text | None

    @model_validator(mode="after")
    def availability(self) -> Self:
        if (self.state == "available") != (self.text is not None):
            raise ValueError("content availability and text must agree")
        return self


class QueryBudget(Value):
    max_operations: int = Field(default=16, ge=1, le=16)
    max_records: int = Field(default=10_000, ge=1, le=10_000)
    max_milliseconds: int = Field(default=5000, ge=1, le=30_000)


class SearchStep(Value):
    operation: Literal["search"]
    step_id: Token
    text: Label
    limit: int = Field(default=20, ge=1, le=100)


class ResolveStep(Value):
    operation: Literal["resolve"]
    step_id: Token
    name: Label


class RecordsStep(Value):
    operation: Literal["records"]
    step_id: Token
    entity_step: Token
    record_type: Literal["action", "decision", "blocker", "conflict"]
    status: Literal["open", "completed"] | None = None

    @model_validator(mode="after")
    def task_status(self) -> Self:
        if self.status is not None and self.record_type != "action":
            raise ValueError("status constraint is only defined for actions")
        return self


class CountStep(Value):
    operation: Literal["count"]
    step_id: Token
    records_step: Token


class PathsStep(Value):
    operation: Literal["paths"]
    step_id: Token
    entity_step: Token
    predicate: Name
    max_hops: int = Field(default=2, ge=1, le=3)


class EvidenceStep(Value):
    operation: Literal["evidence"]
    step_id: Token
    evidence: EvidenceRef


QueryStep = Annotated[
    SearchStep | ResolveStep | RecordsStep | CountStep | PathsStep | EvidenceStep,
    Field(discriminator="operation"),
]


class QueryRequest(Versioned):
    request_id: Token
    scope: Scope
    budget: QueryBudget = Field(default_factory=QueryBudget)
    steps: tuple[QueryStep, ...] = Field(min_length=1, max_length=16)
    output_step: Token

    @model_validator(mode="after")
    def dependencies(self) -> Self:
        if "read" not in self.scope.access.grants:
            raise ValueError("read grant required")
        if len(self.steps) > self.budget.max_operations:
            raise ValueError("plan exceeds operation budget")
        seen: dict[str, QueryStep] = {}
        for step in self.steps:
            if step.step_id in seen:
                raise ValueError("duplicate step ID")
            if isinstance(step, (RecordsStep, PathsStep)):
                if not isinstance(seen.get(step.entity_step), ResolveStep):
                    raise ValueError("entity dependency must be an earlier resolve step")
            elif isinstance(step, CountStep):
                if not isinstance(seen.get(step.records_step), RecordsStep):
                    raise ValueError("count requires an earlier exhaustive records selection")
            elif isinstance(step, EvidenceStep) and (
                step.evidence.corpus_id != self.scope.corpus_id
                or step.evidence.source_namespace not in self.scope.access.namespaces
            ):
                raise ValueError("evidence outside declared scope")
            seen[step.step_id] = step
        if self.output_step not in seen:
            raise ValueError("unknown output step")
        return self


class RankedEvidence(Value):
    evidence: EvidenceRef
    score: float = Field(allow_inf_nan=False)


class RankedResult(Value):
    kind: Literal["ranked"]
    hits: tuple[RankedEvidence, ...] = Field(max_length=100)


class Record(Value):
    record_id: Token
    record_type: Name
    support: SourceSupport


class RecordsResult(Value):
    kind: Literal["records"]
    records: tuple[Record, ...] = Field(max_length=1000)


class Path(Value):
    entity_ids: tuple[Token, ...] = Field(min_length=2, max_length=4)
    assertion_ids: tuple[Token, ...] = Field(min_length=1, max_length=3)
    support: SourceSupport

    @model_validator(mode="after")
    def connected_shape(self) -> Self:
        if len(self.entity_ids) != len(self.assertion_ids) + 1:
            raise ValueError("path needs one assertion per edge")
        return self


class PathsResult(Value):
    kind: Literal["paths"]
    paths: tuple[Path, ...] = Field(max_length=1000)


class AggregateResult(Value):
    kind: Literal["aggregate"]
    count: int = Field(ge=0)
    exact: bool
    supporting_records_step: Token


class EntitiesResult(Value):
    kind: Literal["entities"]
    entity_ids: tuple[Token, ...] = Field(max_length=1000)


QueryData = Annotated[
    RankedResult | RecordsResult | PathsResult | AggregateResult | EntitiesResult,
    Field(discriminator="kind"),
]


class QueryResult(Versioned):
    request_id: Token
    scope: Scope
    outcome: Literal[
        "complete", "empty", "ambiguous", "unsupported", "partial",
        "stale_index", "state_changed", "failed",
    ]
    read_state_id: Token | None
    result_set_id: Token | None
    continuation: Token | None = None
    operations_executed: int = Field(ge=0, le=16)
    records_examined: int = Field(ge=0, le=10_000)
    truncated: bool = False
    exhaustion: Literal["none", "candidate_pool", "eligible_set"]
    data: QueryData | None = None
    error: Failure | None = None

    @model_validator(mode="after")
    def result_consistency(self) -> Self:
        failed = self.outcome in {"unsupported", "stale_index", "state_changed", "failed"}
        if failed:
            if self.error is None or self.data is not None or self.continuation is not None:
                raise ValueError("failed queries require an error and no data/continuation")
            if self.outcome != "failed" and self.error.code != self.outcome:
                raise ValueError("query error code disagrees with outcome")
        elif self.data is None or self.error is not None or self.read_state_id is None:
            raise ValueError("query data requires a coherent read state and no error")
        if self.continuation is not None and self.result_set_id is None:
            raise ValueError("continuation requires a result set")
        if isinstance(self.data, AggregateResult) and self.data.exact:
            if self.outcome not in {"complete", "empty"} or self.truncated:
                raise ValueError("partial/truncated aggregates cannot be exact")
            if self.exhaustion != "eligible_set":
                raise ValueError("exact aggregate requires complete eligible set")
        if self.outcome == "ambiguous" and not (
            isinstance(self.data, EntitiesResult) and len(self.data.entity_ids) > 1
        ):
            raise ValueError("ambiguity requires multiple entity candidates")
        if self.outcome == "empty" and self.data is not None:
            if isinstance(self.data, AggregateResult):
                nonempty = self.data.count != 0 or not self.data.exact
            elif isinstance(self.data, RankedResult):
                nonempty = bool(self.data.hits)
            elif isinstance(self.data, RecordsResult):
                nonempty = bool(self.data.records)
            elif isinstance(self.data, PathsResult):
                nonempty = bool(self.data.paths)
            else:
                nonempty = bool(self.data.entity_ids)
            if nonempty or self.truncated:
                raise ValueError("empty outcome cannot contain results or be truncated")
        evidence: list[EvidenceRef] = []
        if isinstance(self.data, RankedResult):
            evidence = [hit.evidence for hit in self.data.hits]
        elif isinstance(self.data, RecordsResult):
            evidence = [ref for record in self.data.records for ref in record.support.evidence]
        elif isinstance(self.data, PathsResult):
            evidence = [ref for path in self.data.paths for ref in path.support.evidence]
        if any(
            ref.corpus_id != self.scope.corpus_id
            or ref.source_namespace not in self.scope.access.namespaces for ref in evidence
        ):
            raise ValueError("result evidence outside declared scope")
        return self

    def validate_for(self, request: QueryRequest) -> None:
        if self.request_id != request.request_id or self.scope != request.scope:
            raise ValueError("query result must preserve request identity and effective scope")
        if self.operations_executed > len(request.steps) or (
            self.records_examined > request.budget.max_records
        ):
            raise ValueError("query result exceeds request budget")
        if self.data is None:
            return
        output = next(step for step in request.steps if step.step_id == request.output_step)
        kinds = {
            "search": "ranked", "resolve": "entities", "records": "records",
            "count": "aggregate", "paths": "paths", "evidence": "records",
        }
        # An ambiguous dependency stops the plan before its requested output.
        if self.outcome == "ambiguous":
            return
        if self.data.kind != kinds[output.operation]:
            raise ValueError("query output kind disagrees with plan")
        if isinstance(output, CountStep) and isinstance(self.data, AggregateResult):
            if self.data.supporting_records_step != output.records_step:
                raise ValueError("aggregate support must name its records selection")
            if self.outcome in {"complete", "empty"} and not self.data.exact:
                raise ValueError("completed count must be exact")
        if (
            isinstance(output, SearchStep) and isinstance(self.data, RankedResult)
            and len(self.data.hits) > output.limit
        ):
            raise ValueError("search result exceeds requested limit")


class FoundationCapabilities(Versioned):
    implementation: Literal["validation_only"] = "validation_only"
    max_text_bytes: Literal[5_000_000] = MAX_TEXT_BYTES
    max_request_bytes: Literal[8_000_000] = MAX_REQUEST_BYTES
    max_batch_bytes: Literal[16_000_000] = MAX_BATCH_BYTES
    max_batch_items: Literal[100] = MAX_BATCH_ITEMS
    max_changes: Literal[100] = MAX_CHANGES
    max_supports: Literal[200] = MAX_SUPPORTS
    write_operations: tuple[str, ...] = ("put_document", "remove_document", "enrich")
    query_operations: tuple[str, ...] = (
        "search", "resolve", "records", "count", "paths", "evidence",
    )
