from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kg.config import load_manifest
from kg.db import Database
from kg.ingest import IngestService
from kg.legacy_cli import app
from kg.models import CorpusManifest, IngestReport, IngestWithWarnings
from kg.record_state import RecordState
from kg.retrieval import RetrievalService


def _manifest(tmp_path: Path, corpus_id: str = "test") -> CorpusManifest:
    vault = tmp_path / corpus_id
    vault.mkdir(exist_ok=True)
    return CorpusManifest(
        corpus_id=corpus_id, display_name=corpus_id, vault_root=vault,
        database=tmp_path / "index.sqlite3", include=["*.md"],
    )


def _note(manifest: CorpusManifest, name: str, body: str, title: str = "Follow-up") -> None:
    (manifest.vault_root / name).write_text(f"# {title}\n\n{body}\n", encoding="utf-8")


@pytest.mark.parametrize("kind", ["action", "decision"])
def test_explicit_update_changes_effective_state_but_preserves_evidence(
    tmp_path: Path, kind: str,
) -> None:
    manifest = _manifest(tmp_path)
    database = Database(manifest.database)
    ingestion = IngestService(database)
    retrieval = RetrievalService(database, manifest.corpus_id)
    before = (
        "- [ ] Confirm date. [owner:: Ada] [key:: review-v1]"
        if kind == "action" else "## Decisions\n\n- Use October 1. [key:: launch-v1]"
    )
    after = (
        "- [x] Confirmed date. [owner:: Ada] [key:: review-v2] [supersedes:: review-v1]"
        if kind == "action" else
        "## Decisions\n\n- Use October 8. [key:: launch-v2] [supersedes:: launch-v1]"
    )
    _note(manifest, "a.md", before, "Project")
    ingestion.ingest(manifest)
    original = (retrieval.actions() if kind == "action" else retrieval.decisions())[0]
    _note(manifest, "b.md", after)
    result = ingestion.ingest(manifest, explain=True, include_quotes=True)
    assert isinstance(result, IngestReport)
    assert result.record_state is not None
    link, = result.record_state.supersessions
    assert link.resolution == "applied" and link.target is not None
    assert link.target.record_id == original.record_id
    assert link.source.quote == after.split("\n")[-1]
    current = retrieval.actions("Project") if kind == "action" else retrieval.decisions("Project")
    assert [item.record_id for item in current] == [link.source.record_id]
    assert "[supersedes::" not in (current[0].summary or "")
    if kind == "action":
        assert retrieval.actions("Project", "open") == []
        assert retrieval.actions("Project", "completed")[0].owner == "Ada"
    assert retrieval.evidence(original.record_id).quote == original.quote
    assert retrieval.search("Confirm" if kind == "action" else "October", source_path="a.md")
    assert retrieval.status("Project").evidence_gaps == []
    assert ingestion.ingest(manifest).unchanged == 2
    # Removing the supporting update removes its effect; historical citations remain valid.
    (manifest.vault_root / "b.md").unlink()
    ingestion.ingest(manifest)
    restored = retrieval.actions("Project") if kind == "action" else retrieval.decisions("Project")
    assert [item.record_id for item in restored] == [original.record_id]
    assert retrieval.evidence(link.source.record_id).quote == link.source.quote


def test_forward_reference_resolves_without_reingesting_update(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    database = Database(manifest.database)
    ingestion = IngestService(database)
    _note(manifest, "update.md", "- [x] Finished. [supersedes:: pending]")
    unresolved = ingestion.ingest(manifest)
    assert isinstance(unresolved, IngestWithWarnings)
    assert "missing_target" in unresolved.state_warnings[0]
    _note(manifest, "original.md", "- [ ] Pending. [key:: pending]", "Project")
    resolved = ingestion.ingest(manifest, explain=True)
    assert isinstance(resolved, IngestReport)
    assert resolved.added == resolved.unchanged == 1
    assert resolved.record_state is not None and not resolved.record_state.warnings
    retrieval = RetrievalService(database, manifest.corpus_id)
    assert retrieval.actions("Project", "open") == []
    assert len(retrieval.actions("Project", "completed")) == 1


@pytest.mark.parametrize(
    ("sources", "reason"),
    [
        (["- [x] Done. [supersedes:: missing]"], "missing_target"),
        (["- [ ] First. [key:: duplicate]", "- [ ] Second. [key:: duplicate]",
          "- [x] Done. [supersedes:: duplicate]"], "ambiguous_target"),
        (["## Decisions\n\n- Approve. [key:: decision]",
          "- [x] Done. [supersedes:: decision]"], "type_mismatch"),
        (["- [ ] Self. [key:: self] [supersedes:: self]"], "self_reference"),
        (["- [ ] First. [key:: first] [supersedes:: second]",
          "- [x] Second. [key:: second] [supersedes:: first]"], "cycle"),
        (["- [ ] Original. [key:: original]", "- [x] Done. [supersedes:: original]",
          "- [ ] Deferred. [supersedes:: original]"], "competing_updates"),
    ],
)
def test_ambiguous_or_invalid_links_are_visible_and_never_choose_a_winner(
    tmp_path: Path, sources: list[str], reason: str,
) -> None:
    manifest = _manifest(tmp_path)
    database = Database(manifest.database)
    for index, source in enumerate(sources):
        _note(manifest, f"{index}.md", source, "Project")
    result = IngestService(database).ingest(manifest, explain=True)
    assert isinstance(result, IngestReport) and result.failed == 0
    assert result.record_state is not None
    assert {item.resolution for item in result.record_state.supersessions} == {reason}
    retrieval = RetrievalService(database, manifest.corpus_id)
    assert len(retrieval.actions()) + len(retrieval.decisions()) == len(sources)
    assert any(reason in item for item in retrieval.status("Project").evidence_gaps)
    assert any(reason in item for item in result.record_state.warnings)
    assert result.record_state.superseded_record_ids == []
    if reason == "ambiguous_target":
        assert len(result.record_state.supersessions[0].candidates) == 2
    if reason == "competing_updates":
        assert len(result.record_state.supersessions[0].conflicting_sources) == 1


def test_supersession_chain_is_independent_of_filename_order(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    _note(manifest, "z-original.md", "- [ ] Original. [key:: v1]", "Project")
    _note(manifest, "m-update.md", "- [x] Done. [key:: v2] [supersedes:: v1]")
    _note(manifest, "a-reopen.md", "- [ ] Reopened. [key:: v3] [supersedes:: v2]")
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    retrieval = RetrievalService(database, manifest.corpus_id)
    current, = retrieval.actions("Project")
    assert current.summary == "Reopened." and current.status == "open"
    assert len(retrieval.record_state().supersessions) == 2
    assert {
        link.effective_record_id for link in retrieval.record_state().supersessions
    } == {current.record_id}
    assert retrieval.record_state(limit=1).truncated


def test_state_is_corpus_scoped_and_follows_current_source_revisions(tmp_path: Path) -> None:
    first = _manifest(tmp_path, "first")
    second = _manifest(tmp_path, "second")
    database = Database(first.database)
    ingestion = IngestService(database)
    _note(first, "original.md", "- [ ] First. [key:: task]")
    _note(second, "update.md", "- [x] Second. [supersedes:: task]")
    ingestion.ingest(first)
    ingestion.ingest(second)
    assert len(RetrievalService(database, first.corpus_id).actions("Follow-up", "open")) == 1
    second_read = RetrievalService(database, second.corpus_id)
    assert second_read.record_state().supersessions[0].resolution == "missing_target"
    _note(second, "original.md", "- [ ] Local. [key:: task]")
    ingestion.ingest(second)
    assert second_read.actions(status="open") == []
    _note(second, "update.md", "Ordinary prose does not update another document.")
    ingestion.ingest(second)
    assert len(second_read.actions(status="open")) == 1
    assert second_read.record_state().supersessions == []


@pytest.mark.parametrize(
    "body",
    [
        "- [ ] Bad. [key:: contains spaces]",
        "- [ ] Bad. [supersedes:: ]",
        "- [ ] Bad. [supersedes::]",
        "- [ ] Bad. [key::]",
        "- [ ] Bad. [key:: first] [key:: second]",
        "Paragraph. [key:: unsupported]",
        "## Decisions\n\n- [ ] Ambiguous task/decision. [key:: ambiguous]",
        "## Blockers\n\n- Blocked. [supersedes:: unsupported]",
    ],
)
def test_malformed_annotations_fail_source_explicitly(tmp_path: Path, body: str) -> None:
    manifest = _manifest(tmp_path)
    _note(manifest, "bad.md", body)
    result = IngestService(Database(manifest.database)).ingest(manifest, explain=True)
    assert result.failed == 1 and result.errors
    with Database(manifest.database).connection() as connection:
        assert connection.execute("SELECT count(*) FROM record_binding").fetchone()[0] == 0


def test_literal_and_child_fields_do_not_create_parent_bindings(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    _note(
        manifest, "note.md",
        "## Decisions\n\n- Document `[key:: example]` and `[key::]` literally.\n\n"
        "## Actions\n\n- [ ] Parent\n  - [ ] Child [key:: child]\n",
    )
    result = IngestService(Database(manifest.database)).ingest(manifest)
    assert not result.failed
    with Database(manifest.database).connection() as connection:
        rows = connection.execute("SELECT record_key FROM record_binding").fetchall()
        assert [row["record_key"] for row in rows] == ["child"]


def test_record_state_cli_and_ingest_trace_do_not_leak_quotes(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    _note(manifest, "original.md", "- [ ] PRIVATE_ORIGINAL [key:: v1]")
    _note(manifest, "update.md", "- [x] PRIVATE_UPDATE [supersedes:: v1]")
    path = tmp_path / "corpus.yml"
    path.write_text(
        "corpus_id: test\ndisplay_name: Test\nvault_root: test\n"
        "database: index.sqlite3\ninclude: ['*.md']\n", encoding="utf-8",
    )
    runner = CliRunner()
    ingest = runner.invoke(
        app, ["ingest", "--manifest", str(path), "--explain", "--format", "json"]
    )
    assert ingest.exit_code == 0, ingest.output
    assert "PRIVATE_" not in ingest.stdout
    for quotes in (False, True):
        args = ["record-state", "--manifest", str(path), "--format", "json"]
        if quotes:
            args.append("--include-quotes")
        result = runner.invoke(app, args)
        assert result.exit_code == 0, result.output
        assert ("PRIVATE_" in result.stdout) is quotes
        payload = json.loads(result.stdout)
        assert payload["supersessions"][0]["resolution"] == "applied"


def test_atlas_explicit_updates_do_not_need_project_name_repetition(tmp_path: Path) -> None:
    manifest = load_manifest(Path("corpora/atlas-state.yml"))
    manifest.database = tmp_path / "index.sqlite3"
    database = Database(manifest.database)
    report = IngestService(database).ingest(manifest, explain=True)
    assert isinstance(report, IngestReport)
    assert report.added == 12 and report.failed == 0
    assert report.record_state is not None and not report.record_state.warnings
    assert len(report.record_state.superseded_record_ids) == 2
    retrieval = RetrievalService(database, manifest.corpus_id)
    status = retrieval.status("Atlas")
    assert len(status.open_actions) == 2
    assert len(status.completed_actions) == 1
    assert status.completed_actions[0].source_path == "atlas-updates/11-review-completion.md"
    assert len(status.decisions) == 3
    assert any("October 15" in item.summary for item in status.decisions)
    assert not any("October 8" in item.summary for item in status.decisions)
    assert retrieval.search("October 8", subject="Atlas")
    assert not status.evidence_gaps


def test_long_supersession_chains_resolve_without_recursive_traversal() -> None:
    successors = {str(index): str(index + 1) for index in range(2500)}
    state = RecordState("test", [], [], successors)
    assert state.current_id("0") == "2500"
    assert len(state.effective_ids) == 2500
    assert all(state.current_id(str(index)) == "2500" for index in range(2501))


def test_failed_update_source_restores_predecessor_and_explains_failure(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path)
    _note(manifest, "original.md", "- [ ] Original. [key:: v1]", "Project")
    _note(manifest, "update.md", "- [x] Finished. [supersedes:: v1]")
    database = Database(manifest.database)
    ingestion = IngestService(database)
    ingestion.ingest(manifest)
    retrieval = RetrievalService(database, manifest.corpus_id)
    completed, = retrieval.actions("Project")
    (manifest.vault_root / "update.md").write_bytes(b"\xff")
    result = ingestion.ingest(manifest, explain=True)
    assert isinstance(result, IngestReport) and result.failed == 1
    assert result.record_state is not None and not result.record_state.superseded_record_ids
    assert result.documents[-1].outcome == "failed"
    restored, = retrieval.actions("Project")
    assert restored.status == "open"
    assert retrieval.evidence(completed.record_id).quote == completed.quote
