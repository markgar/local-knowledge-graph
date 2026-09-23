from __future__ import annotations

import re
import shutil
from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from kg.config import load_manifest
from kg.db import Database
from kg.ingest import IngestService
from kg.models import IngestReport
from kg.retrieval import RetrievalService

STAGES = yaml.safe_load(Path("corpora/fixtures/atlas-stages.yml").read_text())["stages"]
RECORD_KINDS = ("relationships", "actions", "decisions", "blockers", "conflicts")


@pytest.mark.parametrize("stop_after", range(1, len(STAGES) + 1))
def test_incremental_atlas_evidence(tmp_path: Path, stop_after: int) -> None:
    manifest = load_manifest(Path("corpora/atlas.yml"))
    source_root = manifest.vault_root
    manifest.vault_root = tmp_path / "notes"
    manifest.vault_root.mkdir()
    manifest.database = tmp_path / "index.sqlite3"
    database = Database(manifest.database)
    ingestion = IngestService(database)
    retrieval = RetrievalService(database, manifest.corpus_id)
    previous_reports = {}

    for index, stage in enumerate(STAGES[:stop_after]):
        source = stage["source"]
        shutil.copyfile(source_root / source, manifest.vault_root / source)
        result = ingestion.ingest(manifest, explain=True, include_quotes=True)
        assert isinstance(result, IngestReport)
        assert (result.added, result.unchanged, result.changed, result.failed) == (1, index, 0, 0)
        document = next(item for item in result.documents if item.source_path == source)
        assert document.counts is not None
        for kind in RECORD_KINDS:
            assert getattr(document.counts, kind) == stage["records"].get(kind, 0), (
                source, kind, stage["purpose"]
            )
        assert not document.details_truncated and not document.anchors_truncated
        text = (manifest.vault_root / source).read_text()
        for anchor in document.anchors:
            assert text[anchor.start_offset:anchor.end_offset] == anchor.quote
            assert retrieval.evidence(anchor.passage_id).quote == anchor.quote
        for detail in document.details:
            assert text[detail.start_offset:detail.end_offset] == detail.quote
            evidence = retrieval.evidence(detail.record_id)
            assert evidence.anchor_id == detail.anchor_id
            assert evidence.source_revision_id == document.source_revision_id
        for existing in result.documents:
            if existing.source_path == source:
                continue
            previous = previous_reports[existing.source_path]
            assert existing.outcome == "unchanged"
            assert not existing.records_rebuilt
            assert existing.source_revision_id == previous.source_revision_id
            assert existing.details == previous.details
            assert existing.anchors == previous.anchors
        previous_reports[source] = document
        with database.connection() as connection:
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            count = connection.execute("SELECT count(*) FROM source_document").fetchone()[0]
            assert count == index + 1

    status = retrieval.status("Atlas")
    all_status_evidence = [
        *status.recent_material, *status.decisions, *status.open_actions,
        *status.completed_actions, *status.blockers, *status.conflicts, *status.connected_entities,
    ]
    assert all(item.source_path != "05-atlascope-planning.md" for item in all_status_evidence)
    assert retrieval.search("separate", subject="Atlas") == []
    if stop_after >= 3:
        assert "Require the signed access inventory before approval." in {
            item.summary for item in status.decisions
        }
        assert {
            "atlas wikilink security-review",
            "security-review wikilink audit-trail",
        } == {item.summary for item in status.connected_entities}
        numbered = next(item for item in status.open_actions if item.owner == "Morgan")
        assert numbered.summary == "Upload access evidence."
        assert numbered.due_date == "2026-09-24"
    if stop_after >= 4:
        assert "- The archive signing certificate is unavailable." in {
            item.quote for item in retrieval.search("archive signing certificate", subject="Atlas")
        }
    if stop_after >= 6:
        assert {
            entity.entity_id
            for entity in previous_reports["06-integration-notes.md"].mentioned_entities
        } == {"atlas"}
    if stop_after >= 7:
        new_decisions = retrieval.decisions("Atlas", since=datetime(2026, 9, 23, tzinfo=UTC))
        assert len(new_decisions) == 1
        assert "October 8" in new_decisions[0].quote
        old_date = retrieval.search("tentative launch date", subject="Atlas")
        assert "02-follow-up-email.md" in {item.source_path for item in old_date}
    assert len(status.conflicts) == (1 if stop_after >= 8 else 0)
    # A separate completion email is evidence, not permission to edit a prior source.
    assert any(item.owner == "Priya" for item in status.open_actions)
    assert not status.completed_actions
    repeated = ingestion.ingest(manifest, explain=True)
    assert (repeated.added, repeated.changed, repeated.unchanged) == (0, 0, stop_after)


def test_atlas_task_edit_preserves_prior_citation_and_revert(tmp_path: Path) -> None:
    manifest = load_manifest(Path("corpora/atlas.yml"))
    source_root = manifest.vault_root
    manifest.vault_root = tmp_path / "notes"
    shutil.copytree(source_root, manifest.vault_root)
    manifest.database = tmp_path / "index.sqlite3"
    database = Database(manifest.database)
    ingestion = IngestService(database)
    ingestion.ingest(manifest)
    retrieval = RetrievalService(database, manifest.corpus_id)
    original = next(item for item in retrieval.actions("Atlas", "open") if item.owner == "Priya")
    path = manifest.vault_root / "01-planning-meeting.md"
    before = path.read_text()
    path.write_text(before.replace("- [ ] Confirm", "- [x] Confirm"), encoding="utf-8")
    result = ingestion.ingest(manifest, explain=True)
    assert result.changed == 1 and result.unchanged == 9
    assert not any(item.owner == "Priya" for item in retrieval.actions("Atlas", "open"))
    completed, = retrieval.actions("Atlas", "completed")
    assert completed.owner == "Priya"
    assert retrieval.evidence(original.record_id).quote == original.quote
    assert not retrieval.source_range(original.anchor_id).is_current
    comparison = retrieval.compare_revisions(path.name)
    assert comparison.from_revision_id == original.source_revision_id
    assert comparison.to_revision_id == completed.source_revision_id
    assert len(comparison.modified) == 1
    path.write_text(before, encoding="utf-8")
    restored = ingestion.ingest(manifest, explain=True)
    assert restored.changed == 1
    assert retrieval.evidence(original.record_id).quote == original.quote
    assert retrieval.source_range(original.anchor_id).is_current
    assert retrieval.actions("Atlas", "completed") == []


def test_similar_project_name_does_not_match_atlas(tmp_path: Path) -> None:
    manifest = load_manifest(Path("corpora/atlas.yml"))
    manifest.database = tmp_path / "index.sqlite3"
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    retrieval = RetrievalService(database, manifest.corpus_id)
    assert retrieval.search("separate", subject="Atlas") == []
    assert all("separate" not in item.quote for item in retrieval.actions("Atlas"))
    assert all("separate" not in item.quote for item in retrieval.decisions("Atlas"))
    assert all("separate" not in item.quote for item in retrieval.blockers("Atlas"))
    eligible = retrieval.eligible_passages(subject="Atlas")
    assert all(
        retrieval.evidence(passage_id).source_path != "05-atlascope-planning.md"
        for passage_id in eligible
    )


def test_numbered_task_upgrade_retains_revision_and_original_anchor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = load_manifest(Path("corpora/atlas.yml"))
    manifest.include = ["03-security-review.md"]
    manifest.database = tmp_path / "index.sqlite3"
    database = Database(manifest.database)
    ingestion = IngestService(database)
    with monkeypatch.context() as old_parser:
        old_parser.setattr("kg.ingest.service.PARSER_VERSION", "4")
        old_parser.setattr(
            "kg.markdown.parser.TASK_RE",
            re.compile(r"^[ \t]*[-*+][ \t]+\[([ xX])\][ \t]+(.+?)\s*$"),
        )
        ingestion.ingest(manifest)
    retrieval = RetrievalService(database, manifest.corpus_id)
    old_evidence = retrieval.search("Upload access evidence")[0]
    assert retrieval.actions() == []
    report = ingestion.ingest(manifest, explain=True)
    assert isinstance(report, IngestReport)
    document, = report.documents
    assert document.reasons == ["parser_changed"]
    assert document.revision_state == "unchanged" and document.records_rebuilt
    action, = retrieval.actions()
    assert action.source_revision_id == old_evidence.source_revision_id
    assert action.quote == old_evidence.quote
    assert retrieval.source_range(old_evidence.anchor_id).quote == old_evidence.quote
    assert len(retrieval.revisions("03-security-review.md")) == 1
