"""Passage read values; no vector readiness or processing result is implied."""

from typing import Literal, Self

from pydantic import Field, model_validator

from kg.models.evidence import EvidenceView
from kg.models.foundation import Token, Value


class PassageEntry(EvidenceView):
    ordinal: int = Field(gt=0)

    @model_validator(mode="after")
    def passage_reference(self) -> Self:
        if self.reference.passage_id is None:
            raise ValueError("A passage entry requires a passage reference")
        return self


class PassagePage(Value):
    interface_version: Literal["indexing/1"] = "indexing/1"
    document_id: Token
    revision_id: Token
    state_version: Token
    passage_policy: Token
    status: Literal["not_processed", "complete"]
    passage_set_id: Token | None
    entries: tuple[PassageEntry, ...] = Field(max_length=200)
    has_more: bool
    next_after_ordinal: int | None

    @model_validator(mode="after")
    def publication(self) -> Self:
        if (self.status == "complete") != (self.passage_set_id is not None):
            raise ValueError("Only a published passage set is complete")
        if self.status == "not_processed" and (self.entries or self.has_more):
            raise ValueError("Unprocessed state has no passages")
        if self.has_more != (self.next_after_ordinal is not None) or (
            self.has_more
            and (not self.entries or self.next_after_ordinal != self.entries[-1].ordinal)
        ):
            raise ValueError("Invalid passage cursor")
        return self
