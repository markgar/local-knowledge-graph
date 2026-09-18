from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from kg.cli import app

RUNNER = CliRunner()


def test_json_manifest_error_is_machine_readable(tmp_path: Path) -> None:
    result = RUNNER.invoke(
        app,
        [
            "search",
            "anything",
            "--manifest",
            str(tmp_path / "missing.yml"),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 2
    error = json.loads(result.stderr)
    assert error["error"] == "invalid_manifest"
    assert "Could not read manifest" in error["message"]


def test_json_query_error_is_machine_readable(tmp_path: Path) -> None:
    manifest = tmp_path / "corpus.yml"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\n", encoding="utf-8")
    manifest.write_text(
        "corpus_id: test\n"
        "display_name: Test\n"
        "vault_root: vault\n"
        "database: index.sqlite3\n"
        "include: ['*.md']\n",
        encoding="utf-8",
    )
    ingest = RUNNER.invoke(app, ["ingest", "--manifest", str(manifest)])
    assert ingest.exit_code == 0

    result = RUNNER.invoke(
        app,
        [
            "search",
            "!!!",
            "--manifest",
            str(manifest),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"] == "invalid_query"


def test_evidence_command_returns_exact_record_anchor(tmp_path: Path) -> None:
    manifest = tmp_path / "corpus.yml"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# Note\n\nEvidence passage.\n", encoding="utf-8")
    manifest.write_text(
        "corpus_id: test\n"
        "display_name: Test\n"
        "vault_root: vault\n"
        "database: index.sqlite3\n"
        "include: ['*.md']\n",
        encoding="utf-8",
    )
    assert RUNNER.invoke(app, ["ingest", "--manifest", str(manifest)]).exit_code == 0
    search = RUNNER.invoke(
        app,
        [
            "search",
            "Evidence",
            "--manifest",
            str(manifest),
            "--format",
            "json",
        ],
    )
    record_id = json.loads(search.stdout)[0]["record_id"]

    evidence = RUNNER.invoke(
        app,
        [
            "evidence",
            record_id,
            "--manifest",
            str(manifest),
            "--format",
            "json",
        ],
    )

    assert evidence.exit_code == 0
    assert json.loads(evidence.stdout)["quote"] == "Evidence passage."


def test_natural_query_mode_is_available_through_cli(tmp_path: Path) -> None:
    manifest = tmp_path / "corpus.yml"
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text(
        "# Note\n\nEvidence passage.\n",
        encoding="utf-8",
    )
    manifest.write_text(
        "corpus_id: test\n"
        "display_name: Test\n"
        "vault_root: vault\n"
        "database: index.sqlite3\n"
        "include: ['*.md']\n",
        encoding="utf-8",
    )
    assert RUNNER.invoke(app, ["ingest", "--manifest", str(manifest)]).exit_code == 0

    result = RUNNER.invoke(
        app,
        [
            "search",
            "unrelated evidence",
            "--query-mode",
            "natural",
            "--manifest",
            str(manifest),
            "--format",
            "json",
        ],
    )

    assert result.exit_code == 0
    assert json.loads(result.stdout)[0]["quote"] == "Evidence passage."
