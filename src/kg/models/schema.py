"""Bounded schema proposals and explicit trusted-administrator outcomes."""

from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    Field,
    SerializerFunctionWrapHandler,
    field_validator,
    model_serializer,
    model_validator,
)

from kg.models.evidence import EvidenceView
from kg.models.foundation import (
    Attribution,
    DocumentDependency,
    EvidenceRef,
    Failure,
    Name,
    SchemaRevisionRef,
    Scope,
    Token,
    Value,
)
from kg.models.knowledge import PredicateDefinition

MAX_SCHEMA_BYTES = 1 << 20


def _text(value: str) -> str:
    if not value.strip() or len(value.encode("utf-8")) > 4096:
        raise ValueError("Schema text must be nonblank and at most 4096 UTF-8 bytes")
    return value


SchemaText = Annotated[str, AfterValidator(_text)]
Hash = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
TermKind = Literal["entity_type", "identifier_scheme", "predicate"]


def _unique(values: tuple[object, ...], message: str) -> None:
    if len(set(values)) != len(values):
        raise ValueError(message)


class SchemaValue(Value):
    interface_version: Literal["knowledge-schema/1"] = "knowledge-schema/1"


class EntityTypeDefinition(Value):
    name: Name
    description: SchemaText


class IdentifierSchemeDefinition(EntityTypeDefinition):
    pass


class SchemaPredicateDefinition(PredicateDefinition):
    description: SchemaText


class SchemaDefinition(SchemaValue):
    entity_types: tuple[EntityTypeDefinition, ...] = Field(min_length=1, max_length=1000)
    identifier_schemes: tuple[IdentifierSchemeDefinition, ...] = Field(default=(), max_length=1000)
    predicates: tuple[SchemaPredicateDefinition, ...] = Field(default=(), max_length=1000)

    @model_validator(mode="after")
    def integrity(self) -> Self:
        for collection in (self.entity_types, self.identifier_schemes, self.predicates):
            _unique(tuple(v.name for v in collection), "Duplicate schema term")
        known = {v.name for v in self.entity_types}
        if any(not set((*p.subject_types, *p.object_types)) <= known for p in self.predicates):
            raise ValueError("Predicate refers to an unknown type")
        if len(self.model_dump_json().encode()) > MAX_SCHEMA_BYTES:
            raise ValueError("Schema definition exceeds byte limit")
        return self


class TermRef(Value):
    kind: TermKind
    name: Name


class ConsideredTerm(Value):
    term: TermRef
    assessment: SchemaText


class TermReview(Value):
    candidates: tuple[ConsideredTerm, ...] = Field(default=(), max_length=100)
    no_existing_candidate_reason: SchemaText | None = None
    reuse_assessment: SchemaText
    extension_rationale: SchemaText
    defer_assessment: SchemaText
    example_ids: tuple[Token, ...] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def integrity(self) -> Self:
        _unique(self.example_ids, "Duplicate review example")
        _unique(tuple(c.term for c in self.candidates), "Duplicate considered term")
        if not self.candidates and self.no_existing_candidate_reason is None:
            raise ValueError("Explain why no existing candidate was considered")
        return self


class AddEntityType(Value):
    definition: EntityTypeDefinition
    review: TermReview


class AddIdentifierScheme(Value):
    definition: IdentifierSchemeDefinition
    review: TermReview


class AddPredicate(Value):
    definition: SchemaPredicateDefinition
    review: TermReview


class WidenPredicate(Value):
    name: Name
    add_subject_types: tuple[Name, ...] = Field(default=(), max_length=100)
    add_object_types: tuple[Name, ...] = Field(default=(), max_length=100)
    review: TermReview

    @model_validator(mode="after")
    def integrity(self) -> Self:
        _unique(self.add_subject_types, "Duplicate widening subject")
        _unique(self.add_object_types, "Duplicate widening object")
        if not self.add_subject_types and not self.add_object_types:
            raise ValueError("Widening must add an endpoint type")
        return self


class SchemaExample(Value):
    example_id: Token
    reference: EvidenceRef
    state_version: Token
    explanation: SchemaText


class UnresolvedConcept(Value):
    concept: SchemaText
    explanation: SchemaText
    reason: Literal["ambiguity", "vocabulary_gap", "unsupported_operation", "insufficient_sample"]
    example_ids: tuple[Token, ...] = Field(min_length=1, max_length=10)


class SchemaSampleCapture(Value):
    reference: EvidenceRef
    state_version: Token


class SchemaSample(Value):
    interface_version: Literal["schema-sample/1"] = "schema-sample/1"
    support: tuple[SchemaSampleCapture, ...] = Field(min_length=1, max_length=200)
    intended_use: SchemaText
    selection_rationale: SchemaText

    @model_validator(mode="after")
    def integrity(self) -> Self:
        _unique(tuple(c.reference for c in self.support), "Duplicate sample reference")
        states: dict[str, tuple[str, str, str]] = {}
        corpora = {c.reference.corpus_id for c in self.support}
        if len(corpora) != 1:
            raise ValueError("Cross-corpus sample")
        for capture in self.support:
            ref = capture.reference
            state = ref.source_namespace, ref.revision_id, capture.state_version
            if ref.document_id in states and states[ref.document_id] != state:
                raise ValueError("Conflicting sample document states")
            states[ref.document_id] = state
        if len(self.model_dump_json().encode()) > MAX_SCHEMA_BYTES:
            raise ValueError("Sample exceeds byte limit")
        return self


class SynonymDecision(Value):
    surface_forms: tuple[SchemaText, ...] = Field(min_length=1, max_length=20)
    term: TermRef
    rationale: SchemaText

    @model_validator(mode="after")
    def integrity(self) -> Self:
        _unique(self.surface_forms, "Duplicate synonym surface form")
        return self


class InitialSchemaGeneration(Value):
    sample: SchemaSample
    coverage_status: Literal["limited", "insufficient"]
    coverage_limitations: tuple[SchemaText, ...] = Field(min_length=1, max_length=100)
    synonym_decisions: tuple[SynonymDecision, ...] = Field(max_length=100)


class SchemaGenerationBrief(Value):
    interface_version: Literal["schema-generation/1"] = "schema-generation/1"
    status: Literal["awaiting_agent"] = "awaiting_agent"
    corpus_id: Token
    base_revision: None = None
    sample: SchemaSample
    evidence: tuple[EvidenceView, ...]
    interpretation: Literal["external_agent"] = "external_agent"
    coverage: Literal["selected_excerpts_only"] = "selected_excerpts_only"
    human_review_required: Literal[True] = True


class SchemaProposal(Value):
    interface_version: Literal["schema-proposal/1"] = "schema-proposal/1"
    corpus_id: Token
    base_revision: SchemaRevisionRef | None
    attribution: Attribution
    rationale: SchemaText
    add_entity_types: tuple[AddEntityType, ...] = Field(default=(), max_length=100)
    add_identifier_schemes: tuple[AddIdentifierScheme, ...] = Field(default=(), max_length=100)
    add_predicates: tuple[AddPredicate, ...] = Field(default=(), max_length=100)
    widen_predicates: tuple[WidenPredicate, ...] = Field(default=(), max_length=100)
    examples: tuple[SchemaExample, ...] = Field(min_length=1, max_length=200)
    unresolved_concepts: tuple[UnresolvedConcept, ...] = Field(default=(), max_length=100)
    initial_generation: InitialSchemaGeneration | None = None

    @model_serializer(mode="wrap")
    def preserve_existing_serialization(
        self, handler: SerializerFunctionWrapHandler,
    ) -> dict[str, Any]:
        value: dict[str, Any] = handler(self)
        # Historical request bytes/digests include other nulls, but not this new field.
        if self.initial_generation is None:
            value.pop("initial_generation", None)
        return value

    @model_validator(mode="after")
    def integrity(self) -> Self:
        changes: tuple[AddEntityType | AddIdentifierScheme | AddPredicate | WidenPredicate, ...] = (
            *self.add_entity_types,
            *self.add_identifier_schemes,
            *self.add_predicates,
            *self.widen_predicates,
        )
        if not 1 <= len(changes) <= 100:
            raise ValueError("Proposals require 1..100 schema changes")
        for entries in (self.add_entity_types, self.add_identifier_schemes, self.add_predicates):
            _unique(tuple(a.definition.name for a in entries), "Duplicate addition")
        _unique(tuple(w.name for w in self.widen_predicates), "Duplicate widening")
        _unique(tuple(e.example_id for e in self.examples), "Duplicate example ID")
        _unique(tuple(e.reference for e in self.examples), "Duplicate example reference")
        ids = {e.example_id for e in self.examples}
        used: set[str] = set()
        reviews: tuple[TermReview | UnresolvedConcept, ...] = (
            *tuple(c.review for c in changes),
            *self.unresolved_concepts,
        )
        for entry in reviews:
            _unique(entry.example_ids, "Duplicate example ID")
            used.update(entry.example_ids)
        if used != ids:
            raise ValueError("Every example must be used; references must name supplied examples")
        states: dict[str, tuple[str, str]] = {}
        for example in self.examples:
            ref = example.reference
            if ref.corpus_id != self.corpus_id:
                raise ValueError("Cross-corpus schema evidence")
            state = ref.revision_id, example.state_version
            if ref.document_id in states and states[ref.document_id] != state:
                raise ValueError("Conflicting example document states")
            states[ref.document_id] = state
        if self.initial_generation is not None:
            if self.base_revision is not None:
                raise ValueError("Initial generation requires an unconfigured schema")
            selected = set(self.initial_generation.sample.support)
            if any(c.reference.corpus_id != self.corpus_id for c in selected):
                raise ValueError("Cross-corpus initial sample")
            if any(
                SchemaSampleCapture(reference=e.reference, state_version=e.state_version)
                not in selected for e in self.examples
            ):
                raise ValueError("Every initial example must match selected sample evidence")
        if len(self.model_dump_json().encode()) > MAX_SCHEMA_BYTES:
            raise ValueError("Proposal exceeds byte limit")
        return self

    def captures(self) -> tuple[SchemaSampleCapture, ...]:
        examples = tuple(
            SchemaSampleCapture(reference=e.reference, state_version=e.state_version)
            for e in self.examples
        )
        sample = self.initial_generation.sample.support if self.initial_generation else ()
        return tuple(dict.fromkeys((*examples, *sample)))

    def dependencies(self) -> tuple[DocumentDependency, ...]:
        return tuple(
            {
                (e.reference.source_namespace, e.reference.document_id): DocumentDependency(
                    source_namespace=e.reference.source_namespace,
                    document_id=e.reference.document_id,
                    revision_id=e.reference.revision_id,
                    state_version=e.state_version,
                )
                for e in self.captures()
            }.values()
        )


class SchemaView(SchemaValue):
    status: Literal["configured", "unconfigured"]
    head: SchemaRevisionRef | None
    revision: SchemaRevisionRef | None
    sequence: int | None = Field(default=None, ge=1)
    definition: SchemaDefinition | None
    evolution: Literal["additions_and_endpoint_unions"] = "additions_and_endpoint_unions"

    @model_validator(mode="after")
    def configured(self) -> Self:
        values = (self.head, self.revision, self.sequence, self.definition)
        if self.status == "configured":
            if any(value is None for value in values):
                raise ValueError("Configured schema requires revision, sequence and definition")
        elif any(value is not None for value in values):
            raise ValueError("Unconfigured schema has no head or definition")
        return self


class SchemaRevisionSummary(SchemaValue):
    revision: SchemaRevisionRef
    sequence: int = Field(ge=1)
    parent_revision_id: Token | None
    committed_at: str
    origin: Literal["trusted_preset", "approved_proposal"]
    change_kinds: tuple[str, ...]


class SchemaRevisionPage(SchemaValue):
    head: SchemaRevisionRef | None
    entries: tuple[SchemaRevisionSummary, ...] = Field(max_length=100)
    has_more: bool
    next_after_sequence: int | None


class WideningEffect(Value):
    name: Name
    previous_subject_types: tuple[Name, ...]
    subject_types: tuple[Name, ...]
    previous_object_types: tuple[Name, ...]
    object_types: tuple[Name, ...]
    new_endpoint_combinations: int = Field(ge=1)
    semantics: Literal["cartesian_product"] = "cartesian_product"


class SchemaValidation(SchemaValue):
    proposal_digest: Hash
    definition_hash: Hash
    base_revision: SchemaRevisionRef | None
    definition: SchemaDefinition
    added_terms: tuple[TermRef, ...]
    widenings: tuple[WideningEffect, ...]
    unresolved_concepts: tuple[UnresolvedConcept, ...]
    semantic_review_required: Literal[True] = True
    initial_generation: InitialSchemaGeneration | None = None


class SchemaApproval(Value):
    human_reviewed: Literal[True]
    rationale: SchemaText

    @field_validator("human_reviewed", mode="before")
    @classmethod
    def explicit_boolean(cls, value: object) -> object:
        if value is not True:
            raise ValueError("Approval requires the explicit boolean true")
        return value


class SchemaApplyRequest(SchemaValue):
    request_id: Token
    retry_key: Token
    scope: Scope
    proposal: SchemaProposal
    approved_proposal_digest: Hash
    approval: SchemaApproval

    @model_validator(mode="after")
    def corpus(self) -> Self:
        if self.scope.corpus_id != self.proposal.corpus_id:
            raise ValueError("Proposal corpus must match scope")
        return self


class SchemaReceipt(SchemaValue):
    corpus_id: Token
    previous_revision: SchemaRevisionRef | None
    revision: SchemaRevisionRef
    sequence: int = Field(ge=1)
    proposal_digest: Hash
    principal_id: Token
    attribution: Attribution
    approval: SchemaApproval
    committed_at: str


CommitOutcome = Literal[
    "not_attempted",
    "confirmed_committed",
    "confirmed_rolled_back",
    "unknown",
]


class SchemaApplyOutcome(SchemaValue):
    request_id: Token
    status: Literal["applied", "rejected", "conflict", "failed", "uncertain"]
    commit_outcome: CommitOutcome
    receipt: SchemaReceipt | None = None
    error: Failure | None = None

    @model_validator(mode="after")
    def exclusive(self) -> Self:
        success = self.status == "applied"
        if success != (self.receipt is not None) or success == (self.error is not None):
            raise ValueError("Applied requires receipt only; failures require error only")
        if success and self.commit_outcome != "confirmed_committed":
            raise ValueError("Applied requires confirmed commit")
        if (self.status == "uncertain") != (self.commit_outcome == "unknown"):
            raise ValueError("Uncertain must reflect unknown commit")
        return self


class SchemaPresetRequest(SchemaValue):
    corpus_id: Token
    preset_name: Token
    definition: SchemaDefinition
    preset_rationale: SchemaText


class SchemaPresetRegistration(SchemaValue):
    corpus_id: Token
    revision: SchemaRevisionRef
    sequence: Literal[1] = 1
    status: Literal["applied", "unchanged"]


class SchemaChangeView(SchemaValue):
    revision: SchemaRevisionRef
    principal_id: Token
    proposal: SchemaProposal | None = None
    preset: SchemaPresetRequest | None = None
    approval: SchemaApproval | None = None

    @model_validator(mode="after")
    def origin(self) -> Self:
        if (self.proposal is None) == (self.preset is None):
            raise ValueError("A change has exactly one proposal or preset")
        if (self.proposal is not None) != (self.approval is not None):
            raise ValueError("Only approved proposals have human-review attestations")
        return self
