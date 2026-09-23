"""Typed application queries over a disposable, exact-scope graph."""

from typing import Literal, Self

from pydantic import Field, field_validator, model_validator

from kg.graph._session_types import GraphFailure, GraphGeneration
from kg.knowledge._graph_export import GraphAssertion
from kg.models.foundation import Label, Name, Path, Scope, Token, Value

type GraphDirection = Literal["outgoing", "incoming"]


class GraphEntitySelector(Value):
    name: Label | None = None
    entity_id: Token | None = None

    @model_validator(mode="after")
    def exactly_one(self) -> Self:
        if (self.name is None) == (self.entity_id is None):
            raise ValueError("Exactly one entity selector is required")
        return self


class GraphTraversalRequest(Value):
    interface_version: Literal["graph/1"] = "graph/1"
    request_id: Token
    scope: Scope
    start: GraphEntitySelector
    predicate: Name
    direction: GraphDirection
    max_hops: Literal[1] = 1

    @field_validator("max_hops", mode="before")
    @classmethod
    def one_hop(cls, value: object) -> object:
        if type(value) is not int or value != 1:
            raise ValueError("Only strict integer one-hop queries are supported")
        return value


class GraphRelationshipProof(Value):
    path: Path
    assertion: GraphAssertion

    @model_validator(mode="after")
    def exact_path(self) -> Self:
        assertion = self.assertion
        endpoints = (assertion.subject_id, assertion.object_entity_id)
        if (
            assertion.interpretation != "explicit"
            or assertion.object_entity_id is None
            or self.path.entity_ids not in (endpoints, endpoints[::-1])
            or self.path.assertion_ids != (assertion.assertion_id,)
            or self.path.support.evidence != tuple(e.captured.reference for e in assertion.support)
        ):
            raise ValueError("Path must preserve the complete explicit relationship proof")
        return self


class GraphTraversalResult(Value):
    interface_version: Literal["graph/1"] = "graph/1"
    request_id: Token
    scope: Scope
    outcome: Literal["complete", "empty", "ambiguous", "failed"]
    generation: GraphGeneration | None
    root_entity_id: Token | None = None
    candidate_ids: tuple[Token, ...] = Field(default=(), max_length=1000)
    paths: tuple[GraphRelationshipProof, ...] = Field(default=(), max_length=1000)
    error: GraphFailure | None = None

    @model_validator(mode="after")
    def coherent_result(self) -> Self:
        if self.outcome == "failed":
            if self.error is None or self.generation is not None or (
                self.root_entity_id is not None or self.candidate_ids or self.paths
            ):
                raise ValueError("Failed traversal cannot contain observed data")
            return self
        if self.error is not None or self.generation is None:
            raise ValueError("Successful traversal requires a generation and no error")
        if self.outcome == "ambiguous":
            if (
                self.root_entity_id is not None or self.paths or len(self.candidate_ids) < 2
                or self.candidate_ids != tuple(sorted(set(self.candidate_ids)))
            ):
                raise ValueError("Ambiguity requires distinct ordered candidates only")
        elif self.candidate_ids:
            raise ValueError("Only ambiguous results contain candidate IDs")
        elif self.outcome == "empty":
            if self.paths:
                raise ValueError("Empty traversal cannot contain paths")
        elif self.root_entity_id is None or not self.paths:
            raise ValueError("Complete traversal requires a root and paths")
        identifiers = tuple(item.assertion.assertion_id for item in self.paths)
        if identifiers != tuple(sorted(set(identifiers))):
            raise ValueError("Paths must be distinct and assertion-ID ordered")
        for item in self.paths:
            if item.path.entity_ids[0] != self.root_entity_id or any(
                ref.corpus_id != self.scope.corpus_id
                or ref.source_namespace not in self.scope.access.namespaces
                for ref in item.path.support.evidence
            ):
                raise ValueError("Path does not belong to the result root/scope")
        return self


class GraphCapabilities(Value):
    interface_version: Literal["graph/1"] = "graph/1"
    operations: tuple[Literal["traverse", "relationship_decisions"], ...] = (
        "traverse", "relationship_decisions",
    )
    runtime: Literal["available", "unavailable"]
    unavailable_reason: Literal["graph_unavailable"] | None = None
    path_semantics: Literal["explicit-one-hop/1"] = "explicit-one-hop/1"
    path_directions: tuple[GraphDirection, ...] = ("outgoing", "incoming")
    max_hops: Literal[1] = 1
    max_paths: Literal[1000] = 1000
    max_answer_bytes: Literal[8388608] = 8388608
    decision_count_identity: Literal["submitted_assertion_id"] = "submitted_assertion_id"
    decision_count_mode: Literal["exact"] = "exact"
    decision_display_limit: Literal[1000] = 1000
    decision_retained_inspection: Literal[False] = False

    @model_validator(mode="after")
    def availability(self) -> Self:
        if (self.runtime == "unavailable") != (self.unavailable_reason is not None):
            raise ValueError("Unavailable runtime requires an explicit reason")
        return self


class GraphRelationshipDecisionsRequest(GraphTraversalRequest):
    display_limit: int = Field(default=1000, ge=1, le=1000)


class GraphDecisionMember(Value):
    decision: GraphAssertion
    relationship_ids: tuple[Token, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def exact_decision(self) -> Self:
        if (
            self.decision.decision_member is None
            or self.decision.object_entity_id is not None
            or self.decision.interpretation != "explicit"
            or self.relationship_ids != tuple(sorted(set(self.relationship_ids)))
        ):
            raise ValueError("Member requires an explicit decision and ordered relationship IDs")
        return self


class GraphRelationshipDecisionsResult(Value):
    interface_version: Literal["graph/1"] = "graph/1"
    request_id: Token
    scope: Scope
    outcome: Literal["complete", "empty", "ambiguous", "failed"]
    generation: GraphGeneration | None
    root_entity_id: Token | None = None
    candidate_ids: tuple[Token, ...] = Field(default=(), max_length=1000)
    count: int | None = Field(default=None, ge=0)
    exact: Literal[True] | None = None
    members: tuple[GraphDecisionMember, ...] = Field(default=(), max_length=1000)
    relationships: tuple[GraphRelationshipProof, ...] = ()
    display_truncated: bool = False
    error: GraphFailure | None = None

    @model_validator(mode="after")
    def coherent_selection(self) -> Self:
        if self.outcome == "failed":
            if (
                self.error is None or self.generation is not None
                or self.root_entity_id is not None or self.candidate_ids
                or self.count is not None or self.exact is not None or self.members
                or self.relationships or self.display_truncated
            ):
                raise ValueError("Failed selection cannot contain observed data")
            return self
        if self.error is not None or self.generation is None:
            raise ValueError("Selection requires a generation and no error")
        if self.outcome == "ambiguous":
            if (
                self.root_entity_id is not None or len(self.candidate_ids) < 2
                or self.candidate_ids != tuple(sorted(set(self.candidate_ids)))
                or self.count is not None or self.exact is not None or self.members
                or self.relationships or self.display_truncated
            ):
                raise ValueError("Ambiguity contains only ordered candidate IDs")
            return self
        if self.candidate_ids or self.count is None or self.exact is not True:
            raise ValueError("Completed selection requires an exact count")
        if self.outcome == "empty":
            if self.count != 0 or self.members or self.relationships or self.display_truncated:
                raise ValueError("Empty selection requires zero and no members")
            return self
        if (
            self.root_entity_id is None or not self.members or self.count < len(self.members)
            or self.display_truncated != (self.count > len(self.members))
        ):
            raise ValueError("Display must correlate with the complete count")
        identifiers = tuple(m.decision.assertion_id for m in self.members)
        proofs = {p.assertion.assertion_id: p for p in self.relationships}
        if identifiers != tuple(sorted(set(identifiers))) or tuple(proofs) != tuple(sorted(proofs)):
            raise ValueError("Members and relationship proofs must be ordered and unique")
        if len(proofs) != len(self.relationships) or set(proofs) != {
            identifier for member in self.members for identifier in member.relationship_ids
        }:
            raise ValueError("Display must contain exactly its referenced relationship proofs")
        for proof in self.relationships:
            if any(
                ref.corpus_id != self.scope.corpus_id
                or ref.source_namespace not in self.scope.access.namespaces
                for ref in proof.path.support.evidence
            ):
                raise ValueError("Relationship support is outside result scope")
        for member in self.members:
            for identifier in member.relationship_ids:
                proof = proofs[identifier]
                if proof.path.entity_ids != (self.root_entity_id, member.decision.subject_id):
                    raise ValueError("Relationship must reach the decision subject")
            for evidence in member.decision.support:
                ref = evidence.captured.reference
                if (
                    ref.corpus_id != self.scope.corpus_id
                    or ref.source_namespace not in self.scope.access.namespaces
                ):
                    raise ValueError("Decision support is outside result scope")
        return self
