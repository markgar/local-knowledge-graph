from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_example() -> ModuleType:
    path = Path("examples/cited_status.py")
    spec = importlib.util.spec_from_file_location("cited_status", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.functional
def test_external_client_renders_exact_citations() -> None:
    module = _load_example()

    rendered = module.render_status(
        {
            "subject": "Example",
            "decisions": [
                {
                    "summary": "Use SQLite.",
                    "quote": "- Use SQLite.",
                    "source_path": "note.md",
                    "heading_path": ["Example", "Decisions"],
                    "source_revision_id": "abcdef1234567890",
                }
            ],
            "open_actions": [],
            "completed_actions": [],
            "blockers": [],
            "conflicts": [],
            "evidence_gaps": [],
        }
    )

    assert "Use SQLite." in rendered
    assert "note.md @ Example / Decisions" in rendered
    assert "revision abcdef123456" in rendered


@pytest.mark.functional
def test_external_client_invokes_cli_and_loads_status(tmp_path: Path) -> None:
    module = _load_example()
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text(
        "# Example\n\n## Decisions\n\n- Use SQLite.\n",
        encoding="utf-8",
    )
    manifest = tmp_path / "corpus.yml"
    manifest.write_text(
        "corpus_id: example\n"
        "display_name: Example\n"
        "vault_root: vault\n"
        "database: index.sqlite3\n"
        "include: ['*.md']\n",
        encoding="utf-8",
    )
    module.subprocess.run(
        [sys.executable, "-m", "kg.legacy_cli", "ingest", "--manifest", str(manifest)],
        check=True,
        capture_output=True,
        text=True,
    )

    status = module.load_status(
        manifest,
        "Example",
        "30d",
    )

    assert status["decisions"][0]["summary"] == "Use SQLite."


@pytest.mark.functional
def test_external_client_preserves_executable_override_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    module = _load_example()
    calls: list[list[str]] = []

    def run(
        argv: list[str], *, check: bool, capture_output: bool, text: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert not check and capture_output and text
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="{}", stderr="")

    monkeypatch.setattr(module.subprocess, "run", run)
    assert module.load_status(
        Path("corpus.yml"), "Example", "30d", executable="/custom/demo-cli",
    ) == {}
    assert calls == [[
        "/custom/demo-cli", "status", "Example", "--manifest", "corpus.yml",
        "--since", "30d", "--format", "json",
    ]]


@pytest.mark.functional
def test_evidence_example_executes_and_replays(tmp_path: Path) -> None:
    import json
    import subprocess

    command = [sys.executable, "examples/evidence_intake.py", "--database", str(tmp_path / "e.db")]
    first = json.loads(subprocess.run(command, check=True, capture_output=True, text=True).stdout)
    second = json.loads(subprocess.run(command, check=True, capture_output=True, text=True).stdout)
    assert first == second
    assert first["quote"] == "Cafe\u0301 \U0001f680"
    assert first["start"] == 3 and first["end"] == 10
