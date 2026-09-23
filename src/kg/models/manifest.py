from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SeedEntity(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    name: str = Field(min_length=1)
    entity_type: str = Field(min_length=1)
    aliases: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def name_must_not_be_blank(cls, name: str) -> str:
        cleaned = name.strip()
        if not cleaned:
            raise ValueError("name must not be blank")
        return cleaned

    @field_validator("aliases")
    @classmethod
    def aliases_must_be_unique(cls, aliases: list[str]) -> list[str]:
        cleaned = [alias.strip() for alias in aliases]
        if any(not alias for alias in cleaned):
            raise ValueError("aliases must not be blank")
        normalized = [alias.casefold() for alias in cleaned]
        if len(normalized) != len(set(normalized)):
            raise ValueError("aliases must be unique, ignoring case")
        return cleaned


class CorpusManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    corpus_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]*$")
    display_name: str = Field(min_length=1)
    vault_root: Path
    include: list[str] = Field(min_length=1)
    database: Path
    allow_symlinks: bool = False
    max_source_bytes: int = Field(default=5_000_000, ge=1)
    seed_entities: list[SeedEntity] = Field(default_factory=list)
    metadata_fields: dict[str, str] = Field(default_factory=dict)
    manifest_path: Path | None = Field(default=None, exclude=True)

    @field_validator("include")
    @classmethod
    def includes_must_be_unique(cls, includes: list[str]) -> list[str]:
        if any(not pattern.strip() for pattern in includes):
            raise ValueError("include entries must not be blank")
        if len(includes) != len(set(includes)):
            raise ValueError("include entries must be unique")
        if any(Path(pattern).is_absolute() for pattern in includes):
            raise ValueError("include entries must be relative to vault_root")
        if any(".." in Path(pattern).parts for pattern in includes):
            raise ValueError("include entries must not traverse outside vault_root")
        return includes

    @model_validator(mode="after")
    def aliases_must_be_unambiguous(self) -> CorpusManifest:
        entity_ids = [entity.entity_id for entity in self.seed_entities]
        if len(entity_ids) != len(set(entity_ids)):
            raise ValueError("seed entity IDs must be unique")
        owners: dict[str, str] = {}
        for entity in self.seed_entities:
            for alias in {entity.name, *entity.aliases}:
                normalized = alias.casefold()
                owner = owners.get(normalized)
                if owner and owner != entity.entity_id:
                    raise ValueError(
                        f"alias {alias!r} is assigned to both {owner!r} and "
                        f"{entity.entity_id!r}"
                    )
                owners[normalized] = entity.entity_id
        return self
