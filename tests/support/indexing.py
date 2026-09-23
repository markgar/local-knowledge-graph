"""Controlled pinned-identity providers; no model downloads or acceptance claims."""

from collections.abc import Sequence

from kg.indexing import IndexService
from kg.models.foundation import SuppliedContent
from kg.retrieval.dense import (
    EMBEDDING_PROFILES,
    ENCODING_PIPELINE_VERSION,
    EmbeddingProfile,
)
from support.evidence import put


class ControlledProvider:
    def __init__(self, profile=EmbeddingProfile.gte_modernbert, *, runtime="controlled-test"):
        config = EMBEDDING_PROFILES[profile]
        self.profile = profile
        self.name, self.revision, self.license = (
            config.model_name,
            config.model_revision,
            config.model_license,
        )
        self.dimensions = config.dimensions
        self.normalization = "l2"
        self.context_behavior = config.context_behavior
        self.query_encoding = config.query_encoding
        self.document_encoding = config.document_encoding
        self.pipeline_version = ENCODING_PIPELINE_VERSION + "|" + runtime
        self.calls = []
        self.before_encode = None

    def encode_documents(self, texts: Sequence[str], *, batch_size: int) -> list[list[float]]:
        self.calls.append(tuple(texts))
        if self.before_encode:
            self.before_encode()
        return [[float(len(text) + 1), 1.0] + [0.0] * (self.dimensions - 2) for text in texts]

    def encode_query(self, text: str) -> list[float]:
        raise AssertionError("Indexing must not call query inference")


def service(env, provider=None):
    index = IndexService(env.database, env.service.identity)
    provider = provider or ControlledProvider()
    loads = []

    def factory(profile):
        loads.append(profile)
        return provider

    index._provider_factory = factory
    return index, provider, loads


def request(
    env, *, text="A\r\nCafe\u0301 \U0001f680", policy="codepoint-window/1", anchors=(), **kw
):
    value = put(env.scope, text=text, **kw)
    return value.model_copy(
        update={
            "payload": value.payload.model_copy(
                update={
                    "content": SuppliedContent(text=text, passage_policy=policy, anchors=anchors),
                }
            )
        }
    )


def process(index, env, value, saved, **kw):
    return index.process(
        env.scope,
        value.attribution,
        saved.document_id,
        saved.processing.state_version,
        **kw,
    )
