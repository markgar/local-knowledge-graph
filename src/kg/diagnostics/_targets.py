"""Bounded retained authorization dependencies, never user callbacks."""

from collections.abc import Iterator
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol

from pydantic import Field

from kg.evidence._read_context import ObserverReference, ReleaseFence
from kg.models.evidence import Grant, LocalIdentity, StoredCitation
from kg.models.foundation import EvidenceRef, ExternalDocument, Scope, Token, Value


class DocumentTarget(Value):
    kind: Literal["document"] = "document"
    document_id: Token
    revision_id: Token | None = None
    state_version: Token | None = None


class EvidenceTarget(Value):
    kind: Literal["evidence"] = "evidence"
    reference: EvidenceRef
    citation: StoredCitation | None = None


class WriterTarget(Value):
    kind: Literal["writer"] = "writer"
    document: ExternalDocument
    owner_id: Token
    writer_id: Token


class KnowledgeTarget(Value):
    kind: Literal["knowledge"] = "knowledge"
    contribution_id: Token
    witness_ids: tuple[Token, ...] = Field(max_length=200)


class KnowledgeWriterTarget(Value):
    """Exact binding key; corpus and principal come from AuthorizationBinding."""

    kind: Literal["knowledge_writer"] = "knowledge_writer"
    namespace: Token
    owner_id: Token
    writer_id: Token


class ClassificationTarget(Value):
    kind: Literal["classification"] = "classification"
    contribution_ids: tuple[Token, ...] = Field(min_length=1, max_length=302)


class ClassificationEventTarget(Value):
    kind: Literal["classification_event"] = "classification_event"
    entity_id: Token
    event_id: Token


class SeedSetTarget(Value):
    """Owned set identity, including an empty set with no contribution IDs."""

    kind: Literal["seed_set"] = "seed_set"
    namespace: Token
    owner_id: Token
    writer_id: Token
    seed_set_id: Token


class IndexTarget(Value):
    kind: Literal["indexing"] = "indexing"
    document_id: Token
    configuration_id: Token


class ProcessingTarget(Value):
    kind: Literal["processing"] = "processing"
    job_id: Token | None = None
    batch_id: Token | None = None
    run_id: Token | None = None


class ProcessingSelectionTarget(Value):
    """Exact registered selection, including operations with no durable job."""

    kind: Literal["processing_selection"] = "processing_selection"
    namespace: Token
    owner_id: Token
    writer_id: Token
    plan_id: Token
    plan_version: Token
    worker_id: Token


ReportTarget = Annotated[
    DocumentTarget
    | EvidenceTarget
    | WriterTarget
    | KnowledgeTarget
    | ClassificationTarget
    | ClassificationEventTarget
    | KnowledgeWriterTarget
    | SeedSetTarget
    | IndexTarget
    | ProcessingTarget
    | ProcessingSelectionTarget,
    Field(discriminator="kind"),
]


class ReportTargets(Value):
    values: tuple[ReportTarget, ...] = Field(default=(), max_length=200)


@dataclass
class AuthorizationBinding:
    identity: LocalIdentity
    scope: Scope
    required: Grant
    targets: list[ReportTarget]


class ReportAuthorizer(Protocol):
    """Trusted owner implementation; never supplied as part of a public request.

    Unknown owner target kinds must fail closed, not be silently skipped.
    The fence covers ALL bindings and original observers as one release.
    Knowledge writer/set checks must bind the originating corpus/principal,
    namespace, grants and exact owner/writer (and set), even for empty results.
    A target describes a dependency; constructing one grants no authority.
    Processing selections require current read grants and the exact enabled
    plan/worker registration, even when the operation returned no jobs.
    """

    def fence(
        self,
        bindings: tuple[AuthorizationBinding, ...],
        observers: tuple[ObserverReference, ...],
    ) -> AbstractContextManager[ReleaseFence]: ...


def binding_targets(bindings: tuple[AuthorizationBinding, ...]) -> Iterator[ReportTarget]:
    for binding in bindings:
        yield from binding.targets
