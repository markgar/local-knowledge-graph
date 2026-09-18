import pytest
from pydantic import ValidationError

from kg.models.manifest import CorpusManifest


def test_manifest_rejects_path_traversal() -> None:
    with pytest.raises(ValidationError, match="must not traverse"):
        CorpusManifest.model_validate(
            {
                "corpus_id": "test",
                "display_name": "Test",
                "vault_root": ".",
                "database": "index.sqlite3",
                "include": ["../outside/*.md"],
            }
        )


def test_manifest_rejects_ambiguous_aliases() -> None:
    with pytest.raises(ValidationError, match="assigned to both"):
        CorpusManifest.model_validate(
            {
                "corpus_id": "test",
                "display_name": "Test",
                "vault_root": ".",
                "database": "index.sqlite3",
                "include": ["*.md"],
                "seed_entities": [
                    {
                        "entity_id": "one",
                        "name": "One",
                        "entity_type": "topic",
                        "aliases": ["Shared"],
                    },
                    {
                        "entity_id": "two",
                        "name": "Two",
                        "entity_type": "topic",
                        "aliases": ["shared"],
                    },
                ],
            }
        )


def test_manifest_rejects_duplicate_entity_ids() -> None:
    with pytest.raises(ValidationError, match="IDs must be unique"):
        CorpusManifest.model_validate(
            {
                "corpus_id": "test",
                "display_name": "Test",
                "vault_root": ".",
                "database": "index.sqlite3",
                "include": ["*.md"],
                "seed_entities": [
                    {"entity_id": "same", "name": "One", "entity_type": "topic"},
                    {"entity_id": "same", "name": "Two", "entity_type": "topic"},
                ],
            }
        )
