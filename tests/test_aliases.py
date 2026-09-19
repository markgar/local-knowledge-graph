from __future__ import annotations

from pathlib import Path

import pytest

from kg.aliases import matches_alias
from kg.db import Database
from kg.ingest import IngestService
from kg.models import CorpusManifest, SeedEntity
from kg.retrieval import RetrievalService


@pytest.mark.parametrize(
    ("text", "alias", "expected"),
    [
        ("Atlascope", "Atlas", False),
        ("MetaAtlas", "Atlas", False),
        ("Atlas2", "Atlas", False),
        ("Atlas_backup", "Atlas", False),
        ("Project ATLAS.", "Atlas", True),
        ("Atlas-based release", "Atlas", True),
        ("[[Atlas]]", "Atlas", True),
        ("Security Review:", "Security Review", True),
        ("Security Reviewer", "Security Review", False),
        ("C++ migration", "C++", True),
        ("100%_done", "100%_done", True),
        ("100XYZdone", "100%_done", False),
        ("ÉCLAIR release", "éclair", True),
        (None, "Atlas", False),
    ],
)
def test_alias_matching_is_literal_and_boundary_aware(
    tmp_path: Path, text: str | None, alias: str, expected: bool,
) -> None:
    assert matches_alias(text, alias) is expected
    with Database(tmp_path / "index.sqlite3").connection() as connection:
        assert bool(connection.execute(
            "SELECT kg_matches_alias(?, ?)", (text, alias)
        ).fetchone()[0]) is expected


@pytest.mark.parametrize("configured", [False, True])
def test_subject_filter_matches_title_only_and_heading_only_evidence(
    tmp_path: Path, configured: bool,
) -> None:
    vault = tmp_path / "notes"
    vault.mkdir()
    (vault / "title.md").write_text(
        "---\ntitle: Atlas release\n---\n\n## Decisions\n\n- Keep the deployment gated.\n",
        encoding="utf-8",
    )
    (vault / "heading.md").write_text(
        "# Portfolio\n\n## Atlas release\n\nThe deployment is gated.\n",
        encoding="utf-8",
    )
    (vault / "similar.md").write_text(
        "---\ntitle: Atlascope release\n---\n\n## Decisions\n\n- Keep the deployment gated.\n",
        encoding="utf-8",
    )
    manifest = CorpusManifest(
        corpus_id="boundary", display_name="Boundary", vault_root=vault,
        database=tmp_path / "index.sqlite3", include=["*.md"],
        seed_entities=[SeedEntity(entity_id="atlas", name="Atlas", entity_type="project")]
        if configured else [],
    )
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    retrieval = RetrievalService(database, manifest.corpus_id)
    assert {item.source_path for item in retrieval.search("deployment", subject="Atlas")} == {
        "title.md", "heading.md",
    }
    assert [item.source_path for item in retrieval.decisions("Atlas")] == ["title.md"]
    assert {
        retrieval.evidence(passage_id).source_path
        for passage_id in retrieval.eligible_passages(subject="Atlas")
    } == {"title.md", "heading.md"}
