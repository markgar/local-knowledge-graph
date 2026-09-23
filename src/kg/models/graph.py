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
    operations: tuple[Literal["traverse"], ...] = ("traverse",)
    runtime: Literal["available", "unavailable"]
    unavailable_reason: Literal["graph_unavailable"] | None = None
    path_semantics: Literal["explicit-one-hop/1"] = "explicit-one-hop/1"
    path_directions: tuple[GraphDirection, ...] = ("outgoing", "incoming")
    max_hops: Literal[1] = 1
    max_paths: Literal[1000] = 1000
    max_answer_bytes: Literal[8388608] = 8388608

    @model_validator(mode="after")
    def availability(self) -> Self:
        if (self.runtime == "unavailable") != (self.unavailable_reason is not None):
            raise ValueError("Unavailable runtime requires an explicit reason")
        return self
