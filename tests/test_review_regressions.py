from __future__ import annotations

from pathlib import Path

import pytest

from kg.db import Database
from kg.ingest import IngestService
from kg.lexical import lexical_table
from kg.models.manifest import CorpusManifest, SeedEntity
from kg.retrieval import RetrievalService
from kg.retrieval.service import RevisionComparisonError, SearchQueryError


def _corpus(root: Path, database: Path, corpus_id: str = "test") -> CorpusManifest:
    root.mkdir(parents=True, exist_ok=True)
    return CorpusManifest(
        corpus_id=corpus_id,
        display_name=corpus_id,
        vault_root=root,
        database=database,
        include=["*.md"],
    )


@pytest.mark.parametrize("moved_path", ["a.md", "zz.md"])
@pytest.mark.parametrize("replacement", ["Replacement.", "Original."])
def test_reusing_moved_path_allocates_a_new_identity(
    tmp_path: Path, moved_path: str, replacement: str,
) -> None:
    manifest = _corpus(tmp_path / "vault", tmp_path / "index.sqlite3")
    source = manifest.vault_root / "z.md"
    source.write_text("# Title\n\nOriginal.\n", encoding="utf-8")
    database = Database(manifest.database)
    ingestion = IngestService(database)
    ingestion.ingest(manifest)
    retrieval = RetrievalService(database, manifest.corpus_id)
    original = retrieval.search("Original")[0]
    original_document = retrieval.source_range(original.anchor_id).document_id
    source.rename(manifest.vault_root / moved_path)
    ingestion.ingest(manifest)
    source.write_text(f"# Title\n\n{replacement}\n", encoding="utf-8")
    result = ingestion.ingest(manifest)
    assert (result.added, result.failed) == (1, 0)
    old = retrieval.search("Original", source_path=moved_path)[0]
    new = retrieval.search(replacement, source_path="z.md")[0]
    assert retrieval.source_range(old.anchor_id).document_id == original_document
    assert retrieval.source_range(new.anchor_id).document_id != original_document
    assert retrieval.evidence(original.record_id).source_path == moved_path
    source.rename(manifest.vault_root / "third.md")
    ingestion.ingest(manifest)
    source.write_text("# Title\n\nThird incarnation.\n", encoding="utf-8")
    assert ingestion.ingest(manifest).added == 1
    with database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM source_document").fetchone()[0] == 3
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_reverts_record_transitions_without_duplicating_content(tmp_path: Path) -> None:
    manifest = _corpus(tmp_path / "vault", tmp_path / "index.sqlite3")
    source = manifest.vault_root / "note.md"
    database = Database(manifest.database)
    ingestion = IngestService(database)
    retrieval = RetrievalService(database, manifest.corpus_id)
    revisions = []
    for index, content in enumerate(("Original", "Edited", "Original", "Edited", "Original")):
        source.write_text(f"# Note\n\n{content}.\n", encoding="utf-8")
        result = ingestion.ingest(manifest)
        assert result.added == (index == 0)
        assert result.changed == (index != 0)
        revision = retrieval.search(content)[0].source_revision_id
        if revisions:
            comparison = retrieval.compare_revisions("note.md")
            assert comparison.from_revision_id == revisions[-1]
            assert comparison.to_revision_id == revision
            assert len(comparison.modified) == 1
        revisions.append(revision)
        assert ingestion.ingest(manifest).unchanged == 1
    assert len(set(revisions)) == 2
    assert len(retrieval.revisions("note.md")) == 2
    with database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM revision_activation").fetchone()[0] == 5
    explicit = retrieval.compare_revisions("note.md", to_revision_id=revisions[1])
    assert explicit.from_revision_id == revisions[0]
    assert explicit.to_revision_id == revisions[1]


def test_activation_order_does_not_depend_on_wall_clock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _corpus(tmp_path / "vault", tmp_path / "index.sqlite3")
    monkeypatch.setattr("kg.ingest.service._now", lambda: "2026-01-01T00:00:00+00:00")
    database = Database(manifest.database)
    retrieval = RetrievalService(database, manifest.corpus_id)
    revisions = []
    for content in ("First", "Second", "Third", "First"):
        (manifest.vault_root / "note.md").write_text(content, encoding="utf-8")
        IngestService(database).ingest(manifest)
        revisions.append(retrieval.search(content)[0].source_revision_id)
    comparison = retrieval.compare_revisions("note.md")
    assert comparison.from_revision_id == revisions[-2]
    assert comparison.to_revision_id == revisions[-1]


def test_legacy_upgrade_does_not_invent_missing_transitions(tmp_path: Path) -> None:
    manifest = _corpus(tmp_path / "vault", tmp_path / "index.sqlite3")
    database = Database(manifest.database)
    retrieval = RetrievalService(database, manifest.corpus_id)
    ingestion = IngestService(database)
    revisions = []
    for content in ("First", "Second"):
        (manifest.vault_root / "note.md").write_text(content, encoding="utf-8")
        ingestion.ingest(manifest)
        revisions.append(retrieval.search(content)[0].source_revision_id)
    with database.transaction() as connection:
        connection.execute("DELETE FROM revision_activation")
        connection.execute("DELETE FROM lexical_projection")
    with pytest.raises(SearchQueryError, match="kg ingest"):
        retrieval.search("Second")
    ingestion.ingest(manifest)
    assert retrieval.search("Second")
    with pytest.raises(RevisionComparisonError, match="specify --from"):
        retrieval.compare_revisions("note.md")
    assert retrieval.compare_revisions(
        "note.md", from_revision_id=revisions[0], to_revision_id=revisions[1]
    ).from_revision_id == revisions[0]
    (manifest.vault_root / "note.md").write_text("First", encoding="utf-8")
    assert ingestion.ingest(manifest).changed == 1
    assert retrieval.compare_revisions("note.md").from_revision_id == revisions[1]


def test_historical_titles_are_revision_bound_for_every_record_type(tmp_path: Path) -> None:
    manifest = _corpus(tmp_path / "vault", tmp_path / "index.sqlite3")
    manifest.seed_entities = [
        SeedEntity(entity_id="atlas", name="Atlas", entity_type="project"),
        SeedEntity(entity_id="relay", name="Relay", entity_type="project"),
    ]
    text = (
        "# Old\n\nAtlas depends on [[Relay]].\n\n"
        "- [ ] Task.\n\n## Decisions\n\nUse SQLite.\n\n"
        "## Blockers\n\nApproval pending.\n\n"
        "## Conflicts\n\nReports disagree.\n"
    )
    source = manifest.vault_root / "note.md"
    source.write_text(text, encoding="utf-8")
    database = Database(manifest.database)
    ingestion = IngestService(database)
    ingestion.ingest(manifest)
    retrieval = RetrievalService(database, manifest.corpus_id)
    records = [
        *retrieval.actions(), *retrieval.decisions("Atlas"),
        *retrieval.blockers("Atlas"), *retrieval.conflicts("Atlas"),
        *retrieval.connections("Atlas"), *retrieval.search("SQLite"),
    ]
    assert {record.record_type for record in records} == {
        "action", "decision", "blocker", "conflict", "relationship", "passage",
    }
    source.write_text(text.replace("# Old", "# New").replace("[ ]", "[x]"), encoding="utf-8")
    ingestion.ingest(manifest)
    for record in records:
        old = retrieval.evidence(record.record_id)
        assert old.title == "Old"
        assert old.quote == record.quote
        assert old.source_revision_id == record.source_revision_id
    assert retrieval.actions()[0].title == "New"


def test_other_corpora_and_history_cannot_change_current_bm25(tmp_path: Path) -> None:
    database = Database(tmp_path / "shared.sqlite3")
    manifest = _corpus(tmp_path / "first", database.path)
    (manifest.vault_root / "a.md").write_text("apple apple", encoding="utf-8")
    (manifest.vault_root / "b.md").write_text("banana", encoding="utf-8")
    ingestion = IngestService(database)
    ingestion.ingest(manifest)
    retrieval = RetrievalService(database, manifest.corpus_id)

    def ranking() -> list[tuple[str, float]]:
        return [
            (result.record_id, result.rank)
            for result in retrieval.search("apple banana", query_mode="natural")
        ]

    original = ranking()
    fingerprint = retrieval.index_fingerprint()
    other = _corpus(tmp_path / "second", database.path, "other")
    for index in range(20):
        (other.vault_root / f"{index}.md").write_text("apple apple", encoding="utf-8")
    ingestion.ingest(other)
    assert ranking() == original
    assert retrieval.index_fingerprint() == fingerprint
    for index in range(5):
        (manifest.vault_root / "a.md").write_text(
            "apple " * (index + 10), encoding="utf-8",
        )
        ingestion.ingest(manifest)
    (manifest.vault_root / "a.md").write_text("apple apple", encoding="utf-8")
    ingestion.ingest(manifest)
    assert ranking() == original
    assert retrieval.index_fingerprint() == fingerprint


@pytest.mark.parametrize("removal", ["deleted", "unselected", "failed"])
def test_inactive_sources_do_not_influence_ranking(tmp_path: Path, removal: str) -> None:
    database = Database(tmp_path / "index.sqlite3")
    manifest = _corpus(tmp_path / "vault", database.path)
    (manifest.vault_root / "a.md").write_text("apple apple", encoding="utf-8")
    (manifest.vault_root / "b.md").write_text("banana", encoding="utf-8")
    ingestion = IngestService(database)
    ingestion.ingest(manifest)
    retrieval = RetrievalService(database, manifest.corpus_id)
    original = retrieval.search("apple banana", query_mode="natural")
    for index in range(20):
        (manifest.vault_root / f"noise{index}.md").write_text("apple apple", encoding="utf-8")
    ingestion.ingest(manifest)
    if removal == "unselected":
        manifest.include = ["a.md", "b.md"]
    else:
        for path in manifest.vault_root.glob("noise*.md"):
            if removal == "deleted":
                path.unlink()
            else:
                path.write_bytes(b"\xff")
    result = ingestion.ingest(manifest)
    assert result.failed == (20 if removal == "failed" else 0)
    assert retrieval.search("apple banana", query_mode="natural") == original
    with database.connection() as connection:
        count = connection.execute(
            f"SELECT count(*) FROM {lexical_table(manifest.corpus_id)}"
        ).fetchone()[0]
    assert count == 2


def test_lexical_projection_is_stable_on_unchanged_ingestion(tmp_path: Path) -> None:
    database = Database(tmp_path / "index.sqlite3")
    manifest = _corpus(tmp_path / "vault", database.path, "hyphenated_corpus-1")
    (manifest.vault_root / "note.md").write_text("Evidence.", encoding="utf-8")
    ingestion = IngestService(database)
    ingestion.ingest(manifest)
    with database.transaction() as connection:
        connection.execute(
            """
            CREATE TRIGGER reject_projection_update BEFORE UPDATE ON lexical_projection
            BEGIN SELECT RAISE(ABORT, 'unchanged projection was rebuilt'); END
            """
        )
    assert ingestion.ingest(manifest).unchanged == 1
    assert RetrievalService(database, manifest.corpus_id).search("Evidence")


def test_nested_task_assignments_survive_ingestion(tmp_path: Path) -> None:
    manifest = _corpus(tmp_path / "vault", tmp_path / "index.sqlite3")
    (manifest.vault_root / "note.md").write_text(
        "# Actions\n\n"
        "- [ ] Parent [owner:: Alice] [due:: 2026-10-01]\n"
        "  - [ ] Child [owner:: Bob] [due:: 2026-11-01]\n"
        "- [ ] Unassigned parent\n"
        "  - [ ] Assigned child [owner:: Casey]\n",
        encoding="utf-8",
    )
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    tasks = {
        task.summary: (task.owner, task.due_date)
        for task in RetrievalService(database, manifest.corpus_id).actions()
    }
    assert tasks == {
        "Parent": ("Alice", "2026-10-01"),
        "Child": ("Bob", "2026-11-01"),
        "Unassigned parent": (None, None),
        "Assigned child": ("Casey", None),
    }


@pytest.mark.parametrize(
    "code",
    [
        "  ~~~\n  Atlas [[Relay]]\n  ~~~\n",
        "  ```\n  Atlas [[Relay]]\n  ```\n",
        "      Atlas [[Relay]]\n",
    ],
)
def test_nested_code_cannot_create_canonical_relationships(tmp_path: Path, code: str) -> None:
    manifest = _corpus(tmp_path / "vault", tmp_path / "index.sqlite3")
    manifest.seed_entities = [
        SeedEntity(entity_id="atlas", name="Atlas", entity_type="project"),
        SeedEntity(entity_id="relay", name="Relay", entity_type="project"),
    ]
    (manifest.vault_root / "note.md").write_text("- Example:\n\n" + code, encoding="utf-8")
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    assert RetrievalService(database, manifest.corpus_id).connections("Atlas") == []
    with database.connection() as connection:
        assert connection.execute("SELECT count(*) FROM mention").fetchone()[0] == 0


@pytest.mark.parametrize("separator", ["\u2028", "\u2029", "\x85"])
def test_unicode_separators_preserve_search_and_explicit_records(
    tmp_path: Path, separator: str,
) -> None:
    manifest = _corpus(tmp_path / "vault", tmp_path / "index.sqlite3")
    text = f"# Note\n\n## Decisions\n\nFirst{separator}line.\n\nLast paragraph.\n"
    (manifest.vault_root / "note.md").write_text(text, encoding="utf-8")
    database = Database(manifest.database)
    assert IngestService(database).ingest(manifest).failed == 0
    retrieval = RetrievalService(database, manifest.corpus_id)
    assert retrieval.search("Last")[0].quote == "Last paragraph."
    first = retrieval.search("First")[0]
    source = retrieval.source_range(first.anchor_id)
    assert text[source.start_offset : source.end_offset] == f"First{separator}line."
    assert f"First{separator}line." in [
        decision.summary for decision in retrieval.decisions("Note")
    ]
