"""Logical pins and actual initialized provider identity, separate by construction."""

from kg.evidence._values import canonical, sha
from kg.evidence.errors import EvidenceServiceError
from kg.ids import digest
from kg.models.indexing import ExecutionIdentity, IndexConfiguration
from kg.retrieval.dense import (
    EMBEDDING_PROFILES,
    ENCODING_PIPELINE_VERSION,
    EmbeddingProfile,
    EmbeddingProvider,
)


def descriptor(configuration: IndexConfiguration) -> str:
    profile = EMBEDDING_PROFILES[EmbeddingProfile(configuration.embedding_profile)]
    return canonical(
        {
            **configuration.model_dump(),
            "model": profile.model_name,
            "revision": profile.model_revision,
            "dimensions": profile.dimensions,
            "query_encoding": profile.query_encoding,
            "document_encoding": profile.document_encoding,
            "context_behavior": profile.context_behavior,
            "normalization": "l2",
            "vector_encoding": "little-endian-float32/1",
            "pipeline": ENCODING_PIPELINE_VERSION,
        }
    )


def configuration_id(configuration: IndexConfiguration) -> str:
    return digest("e3-configuration/1", descriptor(configuration))


def execution_identity(
    configuration: IndexConfiguration,
    provider: EmbeddingProvider,
) -> ExecutionIdentity:
    profile = EMBEDDING_PROFILES[EmbeddingProfile(configuration.embedding_profile)]
    if (
        provider.profile != profile.profile
        or provider.name != profile.model_name
        or provider.revision != profile.model_revision
        or type(provider.dimensions) is not int
        or provider.dimensions != profile.dimensions
        or provider.normalization != "l2"
        or provider.context_behavior != profile.context_behavior
        or provider.query_encoding != profile.query_encoding
        or provider.document_encoding != profile.document_encoding
        or not provider.pipeline_version.startswith(ENCODING_PIPELINE_VERSION + "|")
        or len(provider.pipeline_version) > 2048
    ):
        raise EvidenceServiceError("unsupported")
    return ExecutionIdentity(
        profile=configuration.embedding_profile,
        model=provider.name,
        revision=provider.revision,
        pipeline=provider.pipeline_version,
        dimensions=provider.dimensions,
        query_encoding=provider.query_encoding,
        document_encoding=provider.document_encoding,
        context_behavior=provider.context_behavior,
    )


def matches(configuration: IndexConfiguration, identity: ExecutionIdentity) -> bool:
    profile = EMBEDDING_PROFILES[EmbeddingProfile(configuration.embedding_profile)]
    return (
        identity.profile == configuration.embedding_profile
        and identity.model == profile.model_name
        and identity.revision == profile.model_revision
        and identity.dimensions == profile.dimensions
        and identity.query_encoding == profile.query_encoding
        and identity.document_encoding == profile.document_encoding
        and identity.context_behavior == profile.context_behavior
        and identity.pipeline.startswith(ENCODING_PIPELINE_VERSION + "|")
    )


def identity_json(identity: ExecutionIdentity) -> str:
    return canonical(identity.model_dump())


def identity_hash(identity: ExecutionIdentity) -> str:
    return sha(identity_json(identity).encode())
