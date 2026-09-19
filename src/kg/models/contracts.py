from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from kg.models.manifest import SeedEntity


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class IngestResult(ContractModel):
    corpus_id: str
    run_id: str
    added: int = 0
    changed: int = 0
    unchanged: int = 0
    missing: int = 0
    failed: int = 0
    errors: list[str] = Field(default_factory=list)


class IngestRecordCounts(ContractModel):
    anchors: int = 0
    passages: int = 0
    mentions: int = 0
    relationships: int = 0
    actions: int = 0
    decisions: int = 0
    blockers: int = 0
    conflicts: int = 0


class IngestMentionedEntity(ContractModel):
    entity_id: str
    name: str
    entity_type: str
    mention_count: int
    anchor_ids: list[str]


class IngestAnchorExplanation(ContractModel):
    passage_id: str
    anchor_id: str
    anchor_kind: str
    heading_path: list[str]
    start_offset: int
    end_offset: int
    quote: str | None = None


class IngestRecordExplanation(ContractModel):
    record_id: str
    record_type: Literal["relationship", "action", "decision", "blocker", "conflict"]
    rule: str
    anchor_id: str
    heading_path: list[str]
    start_offset: int
    end_offset: int
    source_entity_id: str | None = None
    target_entity_id: str | None = None
    status: str | None = None
    owner: str | None = None
    due_date: str | None = None
    record_key: str | None = None
    supersedes_key: str | None = None
    quote: str | None = None


class IngestDocumentReport(ContractModel):
    source_path: str
    outcome: Literal["added", "changed", "unchanged", "failed", "deactivated"]
    reasons: list[str]
    document_id: str | None = None
    source_revision_id: str | None = None
    previous_revision_id: str | None = None
    previous_source_path: str | None = None
    revision_state: Literal["new", "reused", "unchanged", "unavailable"] = "unavailable"
    records_rebuilt: bool = False
    active: bool = False
    counts: IngestRecordCounts | None = None
    anchors: list[IngestAnchorExplanation] = Field(default_factory=list)
    anchors_truncated: bool = False
    mentioned_entities: list[IngestMentionedEntity] = Field(default_factory=list)
    details: list[IngestRecordExplanation] = Field(default_factory=list)
    details_total: int = 0
    details_truncated: bool = False
    error: str | None = None


class StateEvidence(ContractModel):
    record_id: str
    record_type: Literal["action", "decision"]
    key: str | None = None
    source_path: str
    source_revision_id: str
    anchor_id: str
    quote: str | None = None


class SupersessionExplanation(ContractModel):
    source: StateEvidence
    target_key: str
    target: StateEvidence | None = None
    candidates: list[StateEvidence] = Field(default_factory=list)
    conflicting_sources: list[StateEvidence] = Field(default_factory=list)
    effective_record_id: str | None = None
    resolution: Literal[
        "applied", "missing_target", "ambiguous_target", "type_mismatch",
        "self_reference", "cycle", "competing_updates",
    ]


class RecordStateReport(ContractModel):
    report_version: str = "1"
    corpus_id: str
    supersessions: list[SupersessionExplanation] = Field(default_factory=list)
    supersessions_total: int = 0
    truncated: bool = False
    warnings: list[str] = Field(default_factory=list)
    superseded_record_ids: list[str] = Field(default_factory=list)


class IngestWithWarnings(IngestResult):
    state_warnings: list[str]


class IngestReport(IngestResult):
    report_version: str = "1"
    configured_entities: list[SeedEntity] = Field(default_factory=list)
    documents: list[IngestDocumentReport] = Field(default_factory=list)
    unmatched_patterns: list[str] = Field(default_factory=list)
    include_quotes: bool = False
    detail_limit: int = 50
    record_state: RecordStateReport | None = None


class DenseIndexResult(ContractModel):
    corpus_id: str
    contextual: bool = False
    embedding_profile: str
    projection_id: str
    model_name: str
    model_revision: str
    model_license: str
    pipeline_version: str
    dimensions: int
    normalization: str
    context_behavior: str
    query_encoding: str
    document_encoding: str
    passage_count: int
    built: bool
    duration_ms: float
    index_path: str
    index_bytes: int


class ErrorResult(ContractModel):
    error: str
    message: str


class EvidenceResult(ContractModel):
    record_id: str
    record_type: str
    title: str | None = None
    summary: str | None = None
    status: str | None = None
    event_time: str | None = None
    source_path: str
    source_revision_id: str
    anchor_id: str
    heading_path: list[str] = Field(default_factory=list)
    quote: str
    related_entity_ids: list[str] = Field(default_factory=list)


class SearchResult(EvidenceResult):
    rank: float


class SourceRangeResult(ContractModel):
    document_id: str
    source_path: str
    source_revision_id: str
    is_current: bool
    anchor_id: str
    structural_path: str
    heading_path: list[str] = Field(default_factory=list)
    anchor_kind: str
    start_offset: int
    end_offset: int
    quote: str
    quote_hash: str


class RevisionResult(ContractModel):
    document_id: str
    source_path: str
    source_revision_id: str
    content_hash: str
    observed_mtime: str | None = None
    ingested_at: str
    is_current: bool


class SourceContextResult(ContractModel):
    selected: SourceRangeResult
    section_heading: SourceRangeResult | None = None
    anchors: list[SourceRangeResult]
    total_anchors: int
    truncated: bool


class RevisionRangeChange(ContractModel):
    structural_path: str
    before: SourceRangeResult
    after: SourceRangeResult


class RevisionComparisonResult(ContractModel):
    document_id: str
    source_path: str
    from_revision_id: str
    to_revision_id: str
    added: list[SourceRangeResult] = Field(default_factory=list)
    removed: list[SourceRangeResult] = Field(default_factory=list)
    modified: list[RevisionRangeChange] = Field(default_factory=list)
    unchanged_count: int = 0


class ActionResult(EvidenceResult):
    owner: str | None = None
    due_date: str | None = None


class StatusResult(ContractModel):
    subject: str
    recent_material: list[EvidenceResult] = Field(default_factory=list)
    decisions: list[EvidenceResult] = Field(default_factory=list)
    open_actions: list[ActionResult] = Field(default_factory=list)
    completed_actions: list[ActionResult] = Field(default_factory=list)
    blockers: list[EvidenceResult] = Field(default_factory=list)
    connected_entities: list[EvidenceResult] = Field(default_factory=list)
    evidence_gaps: list[str] = Field(default_factory=list)
    conflicts: list[EvidenceResult] = Field(default_factory=list)
