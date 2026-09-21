"""Typed local service values; foundation/1 remains the write contract."""

from __future__ import annotations

from typing import Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from kg.models.foundation import (
    EvidenceRef,
    ExternalDocument,
    MetadataSnapshot,
    ProcessingState,
    Text,
    Token,
    Value,
)

Grant = Literal["read", "write_documents", "write_knowledge", "seed"]


class EvidenceValue(Value):
    interface_version: Literal["evidence/2"] = "evidence/2"


class LocalIdentity(Value):
    principal_id: Token


class LocalAdminAuthority(Value):
    """Provisioned by the trusted embedding application, never a request credential."""

    principal_id: Token


class WriterBinding(Value):
    namespace: Token
    owner_id: Token
    writer_id: Token
    synchronization_scope: Token


class PolicyGrant(Value):
    principal_id: Token
    namespace: Token
    grant: Grant
    owner_id: Token | None = None
    writer_id: Token | None = None
    synchronization_scope: Token | None = None

    @model_validator(mode="after")
    def writer_scope(self) -> Self:
        fields = (self.owner_id, self.writer_id, self.synchronization_scope)
        if self.grant == "write_documents":
            if any(value is None for value in fields):
                raise ValueError("Document grants require an owner/writer/synchronization scope")
        elif any(value is not None for value in fields):
            raise ValueError("Only document grants carry a document writer binding")
        return self


class LocalPolicy(EvidenceValue):
    corpus_id: Token
    bindings: tuple[WriterBinding, ...] = ()
    grants: tuple[PolicyGrant, ...] = ()

    @model_validator(mode="after")
    def unique_bindings(self) -> Self:
        if len(set(self.bindings)) != len(self.bindings) or len(set(self.grants)) != len(
            self.grants
        ):
            raise ValueError("Duplicate policy entries")
        for grant in self.grants:
            if grant.grant == "write_documents" and not any(
                grant.namespace == binding.namespace
                and grant.owner_id == binding.owner_id
                and grant.writer_id == binding.writer_id
                and grant.synchronization_scope == binding.synchronization_scope
                for binding in self.bindings
            ):
                raise ValueError("Document grant must refer to a registered writer binding")
        return self


class CorpusRegistration(EvidenceValue):
    corpus_id: Token
    namespaces: tuple[Token, ...] = Field(min_length=1, max_length=100)
    policy: LocalPolicy

    @model_validator(mode="after")
    def registered_scope(self) -> Self:
        if len(set(self.namespaces)) != len(self.namespaces):
            raise ValueError("Duplicate namespaces")
        if self.corpus_id != self.policy.corpus_id:
            raise ValueError("Policy corpus mismatch")
        if any(item.namespace not in self.namespaces for item in self.policy.bindings) or any(
            item.namespace not in self.namespaces for item in self.policy.grants
        ):
            raise ValueError("Policy namespace not registered")
        return self


class RegistrationResult(EvidenceValue):
    corpus_id: Token
    policy_version: Token
    status: Literal["applied", "unchanged"]


class PolicyChangeResult(RegistrationResult):
    previous_policy_version: Token
    affected_namespaces: tuple[Token, ...]
    changed_documents: int = Field(ge=0)


class DocumentView(EvidenceValue):
    corpus_id: Token
    document: ExternalDocument
    document_id: Token
    owner_id: Token
    revision_id: Token
    state_version: Token
    sequence: int = Field(gt=0)
    previous_state: Token | None
    change_kind: Literal["put", "remove", "policy"]
    committed_at: AwareDatetime
    processing: ProcessingState
    metadata: MetadataSnapshot
    anchor_set_id: Token
    passage_policy: Token
    is_latest_state: bool
    indexing_reason: Literal[
        "processor_not_available", "not_processed", "unsupported_policy",
        "provider_unavailable", "invalid_provider_output", "state_changed",
        "storage_failure", "superseded_attempt", "claim_lost", "inactive", "ready",
    ]
    enrichment_reason: Literal["processor_not_available"]


class StatePage(EvidenceValue):
    document_id: Token
    entries: tuple[DocumentView, ...]
    has_more: bool
    next_after_sequence: int | None


class RevisionView(Value):
    revision_id: Token
    sequence: int = Field(gt=0)
    content_hash: Token
    byte_length: int = Field(ge=0)
    created_at: AwareDatetime


class RevisionPage(EvidenceValue):
    document_id: Token
    entries: tuple[RevisionView, ...]
    has_more: bool
    next_after_sequence: int | None


class StoredCitation(EvidenceValue):
    reference: EvidenceRef
    metadata_snapshot_id: Token
    state_version: Token


class EvidenceView(EvidenceValue):
    reference: EvidenceRef
    citation: StoredCitation
    local_id: Token
    start: int = Field(ge=0)
    end: int = Field(gt=0)
    quote: Text
    quote_hash: Token
    metadata: MetadataSnapshot
    state_version: Token
    is_current_revision: bool
    is_active: bool
    is_current_support: bool
    member_of_current_anchor_set: bool


class AnchorEntry(EvidenceView):
    ordinal: int = Field(gt=0)


class AnchorPage(EvidenceValue):
    document_id: Token
    state_version: Token
    entries: tuple[AnchorEntry, ...]
    has_more: bool
    next_after_ordinal: int | None


class RevisionAnchorPage(EvidenceValue):
    document_id: Token
    revision_id: Token
    entries: tuple[AnchorEntry, ...]
    has_more: bool
    next_after_ordinal: int | None


class EvidenceCapabilities(EvidenceValue):
    operations: tuple[str, ...] = (
        "put_document",
        "remove_document",
        "write_batch",
        "current",
        "document",
        "state",
        "history",
        "revisions",
        "content",
        "anchors",
        "revision_anchors",
        "evidence",
        "citation",
    )
    unsupported: tuple[str, ...] = (
        "enrich",
        "synchronization",
        "passages",
        "indexing",
        "search",
        "purge",
        "provider_acls",
    )
    authorization: Literal["trusted_local_namespace_policy"] = "trusted_local_namespace_policy"
    passage_policy: Literal["retained_intent_only"] = "retained_intent_only"
    max_text_bytes: int = 5_000_000
    max_anchors: int = 1_000
    max_request_bytes: int = 8_000_000
    max_batch_items: int = 100
    max_batch_bytes: int = 16_000_000
    max_page_items: int = 200
