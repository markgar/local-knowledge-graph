"""Single trusted-local profile; canonical services remain the policy authority."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import Field

from kg.evidence import EvidenceAdministration, EvidenceDatabase
from kg.knowledge import KnowledgeAdministration, KnowledgeService
from kg.models.evidence import (
    CorpusRegistration,
    KnowledgeWriterBinding,
    LocalAdminAuthority,
    LocalIdentity,
    LocalPolicy,
    PolicyGrant,
    WriterBinding,
)
from kg.models.foundation import AccessContext, Attribution, Scope, Value
from kg.models.indexing import DEFAULT_CONFIGURATION, IndexConfiguration
from kg.models.knowledge import RecordProjection
from kg.models.schema import (
    EntityTypeDefinition,
    SchemaDefinition,
    SchemaPredicateDefinition,
    SchemaPresetRequest,
)


class ClientError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class Profile(Value):
    version: int = Field(default=1, ge=1, le=1)
    store: str
    scope: Scope
    attribution: Attribution
    namespace: str
    synchronization_scope: str
    models_approved: bool = False
    model_cache: str | None = None
    index: IndexConfiguration = DEFAULT_CONFIGURATION

    @property
    def identity(self) -> LocalIdentity:
        return LocalIdentity(principal_id=self.scope.access.principal_id)

    def database(self) -> EvidenceDatabase:
        path = Path(self.store)
        if not path.is_absolute() or path.is_symlink() or not path.is_file():
            raise ClientError("invalid_store", "Configured store must be an existing regular file.")
        return EvidenceDatabase(path)


def profile_path() -> Path:
    root = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return root / "local-knowledge-graph" / "profile.json"


def default_store() -> Path:
    root = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
    return root / "local-knowledge-graph" / "evidence.sqlite3"


def load_profile(path: Path | None = None) -> Profile:
    path = path or profile_path()
    if path.is_symlink() or not path.is_file():
        raise ClientError("not_configured", "Run kg setup first; no local profile was found.")
    if path.stat().st_size > 64 * 1024:
        raise ClientError("invalid_profile", "Local profile exceeds the supported size.")
    return Profile.model_validate_json(path.read_bytes())


def starter_schema() -> SchemaPresetRequest:
    return SchemaPresetRequest(
        corpus_id="personal",
        preset_name="personal/1",
        preset_rationale="Explicit local setup example vocabulary, not extracted knowledge.",
        definition=SchemaDefinition(
            entity_types=(
                EntityTypeDefinition(name="person", description="An individual person."),
                EntityTypeDefinition(
                    name="project", description="An explicitly identified project.",
                ),
            ),
            predicates=(
                SchemaPredicateDefinition(
                    name="owns",
                    description="The subject is explicitly accountable for the object.",
                    subject_types=("person",), object_kind="entity", object_types=("project",),
                ),
                SchemaPredicateDefinition(
                    name="decision",
                    description="An explicitly recorded decision about the subject.",
                    subject_types=("person", "project"), object_kind="string",
                    record_projection=RecordProjection(encoding="direct-subject-decision/1"),
                ),
            ),
        ),
    )


def configure(
    store: Path,
    *,
    attach: Path | None,
    models_approved: bool,
    cache: Path | None,
    schema_preset: str | None = None,
) -> Profile:
    if schema_preset not in {None, "personal/1"}:
        raise ClientError("invalid_option", "The only example schema preset is personal/1.")
    if attach is not None and schema_preset is not None:
        raise ClientError("invalid_option", "Attaching never installs a schema preset.")
    destination = profile_path()
    if destination.exists() or destination.is_symlink():
        raise ClientError("already_configured", f"Profile already exists: {destination}")
    if cache is not None:
        if not cache.is_dir():
            raise ClientError("invalid_cache", "Model cache must be an existing directory.")
        cache = cache.resolve()
    if attach is not None:
        profile = load_profile(attach)
        database = profile.database()
        # Public scoped inspection validates admission and authority without provisioning.
        KnowledgeService(database, profile.identity).capabilities(profile.scope)
        profile = profile.model_copy(
            update={
                "models_approved": models_approved,
                "model_cache": str(cache) if cache is not None else profile.model_cache,
            }
        )
    else:
        store = store.expanduser().absolute()
        if store.exists() or store.is_symlink():
            raise ClientError(
                "store_exists",
                "New setup needs a new store path. Attach using its existing profile.",
            )
        store.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        # Exclusive allocation prevents initializing an unrelated existing empty file.
        fd = os.open(store, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        database = EvidenceDatabase(store)
        database.initialize()
        authority = LocalAdminAuthority(principal_id="local")
        registration = EvidenceAdministration(database, authority).register(
            CorpusRegistration(
                corpus_id="personal",
                namespaces=("documents",),
                policy=LocalPolicy(
                    corpus_id="personal",
                    bindings=(
                        WriterBinding(
                            namespace="documents",
                            owner_id="local",
                            writer_id="cli",
                            synchronization_scope="personal",
                        ),
                    ),
                    knowledge_bindings=(
                        KnowledgeWriterBinding(
                            namespace="documents",
                            principal_id="local",
                            owner_id="local",
                            writer_id="cli",
                        ),
                    ),
                    grants=(
                        PolicyGrant(principal_id="local", namespace="documents", grant="read"),
                        PolicyGrant(
                            principal_id="local",
                            namespace="documents",
                            grant="write_documents",
                            owner_id="local",
                            writer_id="cli",
                            synchronization_scope="personal",
                        ),
                        PolicyGrant(
                            principal_id="local",
                            namespace="documents",
                            grant="write_knowledge",
                        ),
                    ),
                ),
            )
        )
        if schema_preset is not None:
            KnowledgeAdministration(database, authority).register_knowledge_schema(starter_schema())
        profile = Profile(
            store=str(store.resolve()),
            scope=Scope(
                corpus_id="personal",
                access=AccessContext(
                    principal_id="local",
                    policy_version=registration.policy_version,
                    namespaces=("documents",),
                    grants=("read", "write_documents", "write_knowledge"),
                ),
            ),
            attribution=Attribution(
                owner_id="local",
                writer_id="cli",
                producer="kg-cli",
                producer_version="1",
            ),
            namespace="documents",
            synchronization_scope="personal",
            models_approved=models_approved,
            model_cache=str(cache) if cache else None,
        )
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(profile.model_dump_json(indent=2) + "\n")
        stream.flush()
        os.fsync(stream.fileno())
    return profile
