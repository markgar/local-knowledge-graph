"""SearchService is the product API; other services are low-level components."""

from kg.retrieval.dense import DenseRetrievalService, EmbeddingProfile
from kg.retrieval.explain import SearchExplanation, SearchExplanationError
from kg.retrieval.hybrid import HybridRetrievalService
from kg.retrieval.product_explain import ProductSearchExplanation
from kg.retrieval.rerank import RerankedRetrievalService
from kg.retrieval.search import SearchService, SearchStateChangedError
from kg.retrieval.service import RetrievalService

__all__ = [
    "DenseRetrievalService",
    "EmbeddingProfile",
    "HybridRetrievalService",
    "ProductSearchExplanation",
    "RerankedRetrievalService",
    "RetrievalService",
    "SearchExplanation",
    "SearchExplanationError",
    "SearchService",
    "SearchStateChangedError",
]
