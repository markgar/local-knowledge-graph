"""Strict values for the delivered processing control plane, not executors."""

from typing import Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from kg.models.foundation import Scope, Token, Value

JobStatus = Literal[
    "queued",
    "running",
    "retry_wait",
    "succeeded",
    "failed",
    "superseded",
    "blocked",
    "cancelled",
]
CoordinationReason = Literal[
    "lease_lost",
    "awaiting_input",
    "dependency_changed",
    "retry_scheduled",
    "retry_exhausted",
    "purge_blocked",
    "processor_unavailable",
    "plan_disabled",
    "authority_unavailable",
]


class ProcessingValue(Value):
    interface_version: Literal["processing/1"] = "processing/1"


class PlanRegistration(ProcessingValue):
    corpus_id: Token
    plan_id: Token
    plan_version: Token
    kind: Literal["index"] = "index"
    producer: Token
    producer_version: Token
    configuration_id: Token | None = None
    enabled: bool = True


class WorkerSelection(Value):
    namespace: Token
    owner_id: Token
    writer_id: Token
    plan_id: Token
    plan_version: Token
    worker_id: Token


class WorkerRegistration(ProcessingValue):
    corpus_id: Token
    principal_id: Token
    selection: WorkerSelection


class RegistrationResult(ProcessingValue):
    status: Literal["applied", "unchanged"]


class DocumentTarget(Value):
    """Corpus/namespace are supplied by the request's scope/selection."""

    document_id: Token
    revision_id: Token
    state_version: Token
    namespace_token: Token


class WorkerRequest(ProcessingValue):
    scope: Scope
    selection: WorkerSelection
    request_id: Token


class ScheduleRequest(WorkerRequest):
    retry_key: Token
    target: DocumentTarget


class JobRequest(WorkerRequest):
    job_id: Token


class HeartbeatRequest(JobRequest):
    claim_fence: int = Field(ge=1)


class JobsRequest(WorkerRequest):
    after_sequence: int = Field(default=0, ge=0, le=2**63 - 1)
    limit: int = Field(default=100, ge=1, le=200)


class ScheduleReceipt(ProcessingValue):
    job_id: Token
    status: Literal["applied", "unchanged"]


class JobView(ProcessingValue):
    job_id: Token
    target: DocumentTarget
    status: JobStatus
    creation_sequence: int = Field(ge=1)
    status_version: int = Field(ge=1)
    lifetime_attempts: int = Field(ge=0)
    retry_episode: int = Field(ge=1)
    episode_attempts: int = Field(ge=0, le=10)
    claim_fence: int = Field(ge=0)
    worker_id: Token | None
    lease_deadline: AwareDatetime | None
    next_due_at: AwareDatetime
    reason: CoordinationReason | None


class Claim(ProcessingValue):
    job_id: Token
    worker_id: Token
    claim_fence: int = Field(ge=1)
    lease_deadline: AwareDatetime
    attempt: int = Field(ge=1, le=10)
    target: DocumentTarget


class ClaimResult(ProcessingValue):
    status: Literal["claimed", "no_work"]
    claim: Claim | None = None
    inspected: int = Field(ge=0, le=200)
    scan_truncated: bool = False

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if (self.status == "claimed") != (self.claim is not None):
            raise ValueError("Only claimed results carry a claim")
        return self


class JobPage(ProcessingValue):
    entries: tuple[JobView, ...] = Field(max_length=200)
    next_after: int | None = Field(default=None, ge=1)


class ProcessingCapabilities(ProcessingValue):
    operations: tuple[Literal["schedule", "claim", "heartbeat", "job", "jobs"], ...] = (
        "schedule",
        "claim",
        "heartbeat",
        "job",
        "jobs",
    )
    lease_seconds: Literal[60] = 60
    heartbeat_interval_seconds: Literal[20] = 20
    max_attempts: Literal[10] = 10
    claim_scan_limit: Literal[200] = 200
    max_page_size: Literal[200] = 200
    executes_work: Literal[False] = False
    acknowledges_work: Literal[False] = False
