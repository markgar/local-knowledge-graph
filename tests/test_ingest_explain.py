from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kg.cli import app
from kg.config import load_manifest
from kg.db import Database
from kg.ingest import IngestService
from kg.models import CorpusManifest, IngestReport, SeedEntity
from kg.retrieval.service import RetrievalService

NOTE = """---
title: Atlas launch planning
date: 2026-09-18
---

# Atlas launch planning

Atlas needs approval from [[Security Review]] before launch.

## Decisions

- Keep Atlas in pilot until the security review is complete.

## Actions

- [ ] Confirm the review date. [owner:: Priya] [due:: 2026-09-23]

## Blockers

- The security approval for Atlas is still pending.

## Conflicts

- Atlas dates differ between the two sources.
"""
RUNNER = CliRunner()


@pytest.fixture
def manifest_path(tmp_path: Path) -> Path:
    vault = tmp_path / "notes"
    vault.mkdir()
    (vault / "meeting.md").write_text(NOTE, encoding="utf-8")
    path = tmp_path / "corpus.yml"
    path.write_text(
        "corpus_id: atlas-test\n"
        "display_name: Atlas test\n"
        "vault_root: notes\n"
        "database: index.sqlite3\n"
        "include: ['*.md']\n"
        "metadata_fields: {event_time: date}\n"
        "seed_entities:\n"
        "  - {entity_id: atlas, name: Atlas, entity_type: project}\n"
        "  - {entity_id: security, name: Security Review, entity_type: workstream}\n"
        "  - {entity_id: priya, name: Priya, entity_type: person}\n"
        "  - {entity_id: unused, name: Unmentioned, entity_type: project}\n",
        encoding="utf-8",
    )
    return path


def _explain(
    manifest: CorpusManifest, *, quotes: bool = False, limit: int = 50
) -> IngestReport:
    result = IngestService(Database(manifest.database)).ingest(
        manifest, explain=True, include_quotes=quotes, detail_limit=limit
    )
    assert isinstance(result, IngestReport)
    return result


def test_report_matches_stored_records_and_exact_evidence(manifest_path: Path) -> None:
    manifest = load_manifest(manifest_path)
    report = _explain(manifest, quotes=True)
    assert (report.added, report.changed, report.failed) == (1, 0, 0)
    assert len(report.configured_entities) == 4
    document, = report.documents
    assert document.reasons == ["new_document", "new_revision"]
    assert document.revision_state == "new"
    assert document.active and document.records_rebuilt
    assert document.counts is not None
    assert document.counts.model_dump() == {
        "anchors": 10, "passages": 10, "mentions": 8,
        "relationships": 1, "actions": 1, "decisions": 1, "blockers": 1, "conflicts": 1,
    }
    assert [entity.entity_id for entity in document.mentioned_entities] == [
        "atlas", "priya", "security",
    ]
    assert sum(entity.mention_count for entity in document.mentioned_entities) == 8
    assert document.details_total == 5
    assert [detail.rule for detail in document.details] == [
        "explicit_wikilink", "decision_heading", "checkbox_task",
        "blocker_heading", "conflict_heading",
    ]
    relationship = document.details[0]
    assert (relationship.source_entity_id, relationship.target_entity_id) == ("atlas", "security")
    action = document.details[2]
    assert (action.status, action.owner, action.due_date) == ("open", "Priya", "2026-09-23")
    retrieval = RetrievalService(Database(manifest.database), manifest.corpus_id)
    for detail in document.details:
        evidence = retrieval.evidence(detail.record_id)
        assert evidence.anchor_id == detail.anchor_id
        assert evidence.quote == detail.quote == NOTE[detail.start_offset:detail.end_offset]
        assert evidence.source_revision_id == document.source_revision_id
    for anchor in document.anchors:
        evidence = retrieval.evidence(anchor.passage_id)
        assert evidence.anchor_id == anchor.anchor_id
        assert evidence.quote == anchor.quote == NOTE[anchor.start_offset:anchor.end_offset]
    for entity in document.mentioned_entities:
        assert set(entity.anchor_ids) <= {anchor.anchor_id for anchor in document.anchors}


def test_unchanged_and_bounded_reports_do_not_claim_new_records(manifest_path: Path) -> None:
    manifest = load_manifest(manifest_path)
    first = _explain(manifest)
    second = _explain(manifest)
    document = second.documents[0]
    assert second.unchanged == 1
    assert document.outcome == "unchanged"
    assert document.revision_state == "unchanged"
    assert document.reasons == ["already_indexed"]
    assert not document.records_rebuilt
    assert document.details == first.documents[0].details
    assert document.anchors == first.documents[0].anchors
    assert document.mentioned_entities == first.documents[0].mentioned_entities
    assert all(detail.quote is None for detail in document.details)
    limited = _explain(manifest, limit=2).documents[0]
    assert limited.details == document.details[:2]
    assert limited.anchors == document.anchors[:2]
    assert limited.details_truncated and limited.anchors_truncated
    assert limited.counts == document.counts
    assert limited.details_total == 5


def test_report_tracks_changes_restores_moves_and_reactivation(manifest_path: Path) -> None:
    manifest = load_manifest(manifest_path)
    original = _explain(manifest).documents[0]
    note = manifest.vault_root / "meeting.md"
    note.write_text(NOTE + "\nAnother passage.\n", encoding="utf-8")
    changed = _explain(manifest).documents[0]
    assert changed.outcome == "changed" and changed.records_rebuilt
    assert changed.reasons == ["new_revision"]
    assert changed.previous_revision_id == original.source_revision_id
    note.write_text(NOTE, encoding="utf-8")
    restored = _explain(manifest).documents[0]
    assert restored.outcome == "changed"
    assert restored.revision_state == "reused" and not restored.records_rebuilt
    assert restored.source_revision_id == original.source_revision_id
    assert restored.previous_revision_id == changed.source_revision_id
    assert restored.reasons == ["restored_revision"]
    moved_note = note.rename(note.with_name("renamed.md"))
    moved = _explain(manifest).documents[0]
    assert moved.outcome == "changed" and not moved.records_rebuilt
    assert moved.document_id == original.document_id
    assert moved.previous_source_path == "meeting.md"
    assert moved.reasons == ["moved_source"]
    moved_note.unlink()
    removed_report = _explain(manifest)
    removed, = removed_report.documents
    assert removed.outcome == "deactivated" and not removed.active
    assert removed.source_revision_id == original.source_revision_id
    assert removed_report.unmatched_patterns == ["*.md"]
    assert removed_report.missing == 1
    moved_note.write_text(NOTE, encoding="utf-8")
    reactivated = _explain(manifest).documents[0]
    assert reactivated.reasons == ["reactivated_source"]
    assert reactivated.active and not reactivated.records_rebuilt


def test_configuration_and_parser_rebuild_are_not_new_revisions(manifest_path: Path) -> None:
    manifest = load_manifest(manifest_path)
    original = _explain(manifest).documents[0]
    manifest.seed_entities.append(
        SeedEntity(entity_id="pilot", name="pilot", entity_type="phase")
    )
    configured = _explain(manifest).documents[0]
    assert configured.reasons == ["index_configuration_changed"]
    assert configured.outcome == "changed" and configured.records_rebuilt
    assert configured.revision_state == "unchanged"
    assert configured.source_revision_id == original.source_revision_id
    assert "pilot" in {entity.entity_id for entity in configured.mentioned_entities}
    with Database(manifest.database).transaction() as connection:
        connection.execute("UPDATE source_revision SET indexed_parser_version = 'old'")
    reparsed = _explain(manifest).documents[0]
    assert reparsed.reasons == ["parser_changed"]
    assert reparsed.records_rebuilt and reparsed.revision_state == "unchanged"


def test_failed_sources_are_inactive_and_other_sources_still_ingest(
    manifest_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    manifest = load_manifest(manifest_path)
    first = _explain(manifest).documents[0]
    (manifest.vault_root / "meeting.md").write_text(
        "---\ntitle: [DO_NOT_LEAK_THIS\n---\n# Broken\n", encoding="utf-8"
    )
    (manifest.vault_root / "other.md").write_text("# Other\n\nNew passage.\n", encoding="utf-8")
    report = _explain(manifest)
    assert report.failed == 1 and report.added == 1
    failed, added = report.documents
    assert failed.outcome == "failed" and not failed.active
    assert failed.document_id == first.document_id
    assert failed.source_revision_id == first.source_revision_id
    assert failed.counts is None
    assert failed.revision_state == "unavailable"
    assert "Invalid YAML frontmatter" in (failed.error or "")
    assert added.outcome == "added" and added.active
    assert "DO_NOT_LEAK_THIS" not in report.model_dump_json()
    assert "DO_NOT_LEAK_THIS" not in caplog.text
    with Database(manifest.database).connection() as connection:
        assert connection.execute(
            "SELECT is_active FROM source_document WHERE document_id = ?", (failed.document_id,)
        ).fetchone()[0] == 0


def test_failed_new_file_has_no_invented_identity(manifest_path: Path) -> None:
    manifest = load_manifest(manifest_path)
    (manifest.vault_root / "meeting.md").write_bytes(b"\xff")
    result = _explain(manifest)
    assert result.failed == 1
    assert result.documents[0].document_id is None
    assert result.documents[0].source_revision_id is None


def test_reports_are_corpus_isolated(manifest_path: Path) -> None:
    manifest = load_manifest(manifest_path)
    first = _explain(manifest).documents[0]
    other = manifest.model_copy(update={"corpus_id": "other"})
    report = _explain(other)
    document, = report.documents
    assert report.added == 1
    assert document.document_id != first.document_id
    assert document.counts == first.counts
    (manifest.vault_root / "meeting.md").unlink()
    removed = _explain(other)
    assert len(removed.documents) == 1
    assert removed.documents[0].document_id == document.document_id
    with Database(manifest.database).connection() as connection:
        assert connection.execute(
            "SELECT is_active FROM source_document WHERE document_id = ?", (first.document_id,)
        ).fetchone()[0] == 1


def test_report_failure_rolls_back_run_instead_of_deactivating_source(
    manifest_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = load_manifest(manifest_path)

    def fail_report(*args: object, **kwargs: object) -> None:
        raise sqlite3.OperationalError("report query failed")

    monkeypatch.setattr("kg.ingest.service.explain_document", fail_report)
    with pytest.raises(sqlite3.OperationalError, match="report query failed"):
        _explain(manifest)
    with Database(manifest.database).connection() as connection:
        assert connection.execute("SELECT count(*) FROM source_document").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM ingest_run").fetchone()[0] == 0


def test_default_contract_and_persisted_summary_stay_small(manifest_path: Path) -> None:
    manifest = load_manifest(manifest_path)
    default = RUNNER.invoke(app, ["ingest", "--manifest", str(manifest_path), "--format", "json"])
    assert default.exit_code == 0, default.output
    assert set(json.loads(default.stdout)) == {
        "corpus_id", "run_id", "added", "changed", "unchanged", "missing", "failed", "errors",
    }
    _explain(manifest, quotes=True)
    with Database(manifest.database).connection() as connection:
        for row in connection.execute("SELECT counts_json FROM ingest_run"):
            assert set(json.loads(row["counts_json"])) == {
                "added", "changed", "unchanged", "missing", "failed",
            }


@pytest.mark.parametrize("quotes", [False, True])
def test_cli_explain_json_and_text(manifest_path: Path, quotes: bool) -> None:
    arguments = ["ingest", "--manifest", str(manifest_path), "--explain"]
    if quotes:
        arguments.append("--include-quotes")
    result = RUNNER.invoke(app, [*arguments, "--format", "json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.stdout)
    assert payload["report_version"] == "1"
    assert payload["include_quotes"] is quotes
    assert ("quote" in payload["documents"][0]["details"][0]) is quotes
    assert ("quote" in payload["documents"][0]["anchors"][0]) is quotes
    text = RUNNER.invoke(app, arguments)
    assert text.exit_code == 0, text.output
    assert "UNCHANGED" in text.stdout
    assert "not newly created counts" in text.stdout
    assert "atlas --wikilink--> security" in text.stdout
    assert "owner=Priya, due=2026-09-23" in text.stdout
    assert ("Keep Atlas in pilot" in text.stdout) is quotes


def test_cli_limits_errors_and_invalid_flag_combinations(manifest_path: Path) -> None:
    arguments = ["ingest", "--manifest", str(manifest_path), "--format", "json"]
    invalid = RUNNER.invoke(app, [*arguments, "--include-quotes"])
    assert invalid.exit_code == 2
    assert json.loads(invalid.stderr)["error"] == "invalid_ingest_options"
    manifest = load_manifest(manifest_path)
    assert not manifest.database.exists()
    invalid_limit = RUNNER.invoke(app, [*arguments, "--explain", "--explain-limit", "0"])
    assert invalid_limit.exit_code == 2
    limited = RUNNER.invoke(
        app, [*arguments, "--explain", "--explain-limit", "1"],
    )
    assert limited.exit_code == 0
    assert json.loads(limited.stdout)["documents"][0]["details_truncated"]
    (manifest.vault_root / "meeting.md").write_bytes(b"\xff")
    failed = RUNNER.invoke(app, [*arguments, "--explain"])
    assert failed.exit_code == 1
    assert json.loads(failed.stdout)["documents"][0]["outcome"] == "failed"


def test_api_rejects_invalid_options_before_writing(manifest_path: Path) -> None:
    manifest = load_manifest(manifest_path)
    service = IngestService(Database(manifest.database))
    with pytest.raises(ValueError, match="requires explain"):
        service.ingest(manifest, include_quotes=True)
    with pytest.raises(ValueError, match="between 1 and 200"):
        service.ingest(manifest, explain=True, detail_limit=201)
    assert not manifest.database.exists()
