from __future__ import annotations

import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from kg.config import load_manifest
from kg.db import Database
from kg.ingest import IngestService
from kg.legacy_cli import app
from kg.retrieval import RetrievalService

RUNNER = CliRunner()


def _manifest(tmp_path: Path) -> Path:
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "atlas.md").write_text(
        "# Atlas\n\n## Decisions\n\n- Use SQLite.\n\n## Blockers\n\n- Approval is pending.\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "corpus.yml"
    manifest.write_text(
        "corpus_id: agent-test\n"
        "display_name: Agent test\n"
        "vault_root: vault\n"
        "database: index.sqlite3\n"
        "include: ['*.md']\n"
        "seed_entities:\n"
        "  - entity_id: atlas\n"
        "    name: Atlas\n"
        "    entity_type: project\n",
        encoding="utf-8",
    )
    return manifest


@pytest.mark.functional
def test_agent_cli_publishes_versioned_capabilities() -> None:
    result = RUNNER.invoke(app, ["capabilities", "--format", "json"])

    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["interface_version"] == "2"
    assert payload["transport"] == "local_cli_json"
    assert {tool["command"] for tool in payload["tools"]} >= {
        "search",
        "source-range",
        "revisions",
        "compare-revisions",
        "evidence",
    }


@pytest.mark.functional
def test_agent_cli_reads_ranges_and_compares_revisions(tmp_path: Path) -> None:
    manifest_path = _manifest(tmp_path)
    manifest = load_manifest(manifest_path)
    database = Database(manifest.database)
    ingestion = IngestService(database)
    ingestion.ingest(manifest)
    retrieval = RetrievalService(database, manifest.corpus_id)
    original = retrieval.search("SQLite")[0]

    source_result = RUNNER.invoke(
        app,
        [
            "source-range",
            original.anchor_id,
            "--manifest",
            str(manifest_path),
            "--format",
            "json",
        ],
    )
    assert source_result.exit_code == 0
    source = json.loads(source_result.stdout)
    assert source["quote"] == "- Use SQLite."
    assert source["source_revision_id"] == original.source_revision_id
    assert source["is_current"] is True
    assert source["end_offset"] > source["start_offset"]

    (manifest.vault_root / "atlas.md").write_text(
        "# Atlas\n\n"
        "## Decisions\n\n"
        "- Use PostgreSQL.\n\n"
        "## Blockers\n\n"
        "- Approval is pending.\n\n"
        "## Actions\n\n"
        "- [ ] Confirm the migration window.\n",
        encoding="utf-8",
    )
    ingestion.ingest(manifest)

    revisions_result = RUNNER.invoke(
        app,
        [
            "revisions",
            "atlas.md",
            "--manifest",
            str(manifest_path),
            "--format",
            "json",
        ],
    )
    assert revisions_result.exit_code == 0
    revisions = json.loads(revisions_result.stdout)
    assert len(revisions) == 2
    assert [revision["is_current"] for revision in revisions] == [False, True]

    comparison_result = RUNNER.invoke(
        app,
        [
            "compare-revisions",
            "atlas.md",
            "--manifest",
            str(manifest_path),
            "--format",
            "json",
        ],
    )
    assert comparison_result.exit_code == 0
    comparison = json.loads(comparison_result.stdout)
    assert comparison["from_revision_id"] == revisions[0]["source_revision_id"]
    assert comparison["to_revision_id"] == revisions[1]["source_revision_id"]
    assert [
        (change["before"]["quote"], change["after"]["quote"])
        for change in comparison["modified"]
    ] == [("- Use SQLite.", "- Use PostgreSQL.")]
    assert "- [ ] Confirm the migration window." in [
        item["quote"] for item in comparison["added"]
    ]

    historical_result = RUNNER.invoke(
        app,
        [
            "source-range",
            original.anchor_id,
            "--manifest",
            str(manifest_path),
            "--format",
            "json",
        ],
    )
    assert historical_result.exit_code == 0
    assert json.loads(historical_result.stdout)["is_current"] is False


@pytest.mark.functional
def test_agent_cli_reports_invalid_source_and_revision_as_json(tmp_path: Path) -> None:
    manifest_path = _manifest(tmp_path)
    manifest = load_manifest(manifest_path)
    IngestService(Database(manifest.database)).ingest(manifest)

    missing_source = RUNNER.invoke(
        app,
        [
            "source-range",
            "missing-anchor",
            "--manifest",
            str(manifest_path),
            "--format",
            "json",
        ],
    )
    comparison = RUNNER.invoke(
        app,
        [
            "compare-revisions",
            "atlas.md",
            "--manifest",
            str(manifest_path),
            "--format",
            "json",
        ],
    )

    assert missing_source.exit_code == 2
    assert json.loads(missing_source.stderr)["error"] == "source_range_not_found"
    assert comparison.exit_code == 2
    assert json.loads(comparison.stderr)["error"] == "invalid_revision_comparison"
