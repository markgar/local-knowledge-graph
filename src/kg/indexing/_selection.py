"""Immutable E3 read identities, never reusable authorization credentials."""

from __future__ import annotations

from dataclasses import dataclass

from kg.evidence._coordination import DocumentTarget
from kg.evidence._read_context import CanonicalReadContext, ReadSessionId
from kg.evidence.errors import EvidenceServiceError
from kg.models.foundation import RankedResult, Scope, Token, Value


class ProjectionMember(Value):
    document: DocumentTarget
    projection_id: Token
    passage_set_id: Token


@dataclass(frozen=True)
class ProjectionHandle:
    session_id: ReadSessionId
    scope: Scope
    configuration_id: str
    members: tuple[ProjectionMember, ...]

    def check(self, context: CanonicalReadContext) -> None:
        context.check_session(self.session_id)
        if self.scope != context.scope or any(
            member.document.corpus_id != context.scope.corpus_id
            or member.document.namespace not in context.scope.access.namespaces
            for member in self.members
        ):
            raise EvidenceServiceError("invalid_request")


@dataclass(frozen=True)
class RankedSelection:
    result: RankedResult
    projection: ProjectionHandle
