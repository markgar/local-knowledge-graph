"""Owned knowledge services and trusted immutable schema registration."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from kg.knowledge.administration import KnowledgeAdministration as KnowledgeAdministration
    from kg.knowledge.service import KnowledgeService as KnowledgeService

__all__ = ["KnowledgeAdministration", "KnowledgeService"]


def __getattr__(name: str) -> object:
    if name == "KnowledgeAdministration":
        from kg.knowledge.administration import KnowledgeAdministration

        return KnowledgeAdministration
    if name == "KnowledgeService":
        from kg.knowledge.service import KnowledgeService

        return KnowledgeService
    raise AttributeError(name)
