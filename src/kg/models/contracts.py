from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


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


class DenseIndexResult(ContractModel):
    corpus_id: str
    projection_id: str
    model_name: str
    model_revision: str
    model_license: str
    pipeline_version: str
    dimensions: int
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
