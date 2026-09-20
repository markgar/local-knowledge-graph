from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from support.modules import module

scope = module(".github/scripts/ci_scope.py")


def test_workflow_is_manual_and_gates_matrix_before_installation() -> None:
    workflow = Path(scope.__file__).parents[1] / "workflows" / "ci.yml"
    config = yaml.load(workflow.read_text(), Loader=yaml.BaseLoader)
    assert set(config["on"]) == {"workflow_dispatch"}
    jobs = config["jobs"]
    assert jobs["test"]["needs"] == "changes"
    assert jobs["test"]["if"] == "needs.changes.outputs.run_tests == 'true'"
    assert jobs["changes"]["outputs"]["run_tests"] == "${{ steps.scope.outputs.run_tests }}"
    steps = jobs["changes"]["steps"]
    assert steps[0]["with"]["fetch-depth"] == "0"
    assert steps[1]["id"] == "scope"
    assert steps[1]["run"] == "python3 .github/scripts/ci_scope.py"
    assert len(steps) == 2


@pytest.mark.parametrize("name", [
    "README.md", "CONTRIBUTING.md", "SPEC.md", "CONTRACTS.md",
    ".github/copilot-instructions.md", ".github/skills/work-package/SKILL.md",
    ".github/agents/critic.md", ".github/ISSUE_TEMPLATE/feature_request.yml",
    ".github/ISSUE_TEMPLATE/config.yml", ".github/pull_request_template.md",
    "corpora/foundation/README.md", "benchmarks/foundation/README.md",
    "benchmarks/history/evidence-mvp.md", "docs/guide.rst", "LICENSE",
])
def test_documentation_does_not_require_matrix(name: str) -> None:
    assert not scope.is_code_change(name)


@pytest.mark.parametrize("name", [
    "src/kg/cli.py", "src/kg/schema.sql", "src/kg/py.typed",
    "tests/test_cli.py", "tests/fixtures/source.md", "corpora/example/note.md",
    "corpora/foundation/enrichment.json", "corpora/example.yaml",
    "benchmarks/foundation/workload.py", "benchmarks/qasper/config.json",
    "examples/client.py", "pyproject.toml", "uv.lock", "uv.toml", "MANIFEST.in",
    ".python-version", ".github/workflows/ci.yml", ".github/scripts/ci_scope.py",
    ".github/skills/example/check.sh", "new-code/new-language.ext",
])
def test_code_and_unknown_paths_require_matrix(name: str) -> None:
    assert scope.is_code_change(name)


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=CI scope test", "-c", "user.email=ci@example.invalid",
         "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
        cwd=repo, check=True, capture_output=True, text=True,
    ).stdout.strip()


def commit(repo: Path, name: str, content: str = "text\n") -> str:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    git(repo, "add", "--all")
    git(repo, "commit", "-m", "fixture")
    return git(repo, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    git(tmp_path, "init", "--template=", "--initial-branch=main")
    commit(tmp_path, "README.md")
    git(tmp_path, "update-ref", "refs/remotes/origin/main", "HEAD")
    return tmp_path


def test_feature_branch_checks_entire_change_not_just_last_commit(repo: Path) -> None:
    baseline = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-b", "feature")
    commit(repo, "src/change.py")
    commit(repo, "CONTRIBUTING.md")
    base = scope.comparison_base(repo, "refs/heads/feature", "main")
    assert base == baseline
    assert scope.changed_paths(repo, base) == ["CONTRIBUTING.md", "src/change.py"]
    assert any(map(scope.is_code_change, scope.changed_paths(repo, base)))


def test_default_branch_checks_latest_change_only(repo: Path) -> None:
    previous = commit(repo, "src/change.py")
    commit(repo, "CONTRIBUTING.md")
    base = scope.comparison_base(repo, "refs/heads/main", "main")
    assert base == previous
    assert scope.changed_paths(repo, base) == ["CONTRIBUTING.md"]
    assert not any(map(scope.is_code_change, scope.changed_paths(repo, base)))


def test_initial_commit_and_unchanged_branch(repo: Path) -> None:
    base = scope.comparison_base(repo, "refs/heads/main", "main")
    assert scope.changed_paths(repo, base) == ["README.md"]
    git(repo, "checkout", "-b", "unchanged")
    base = scope.comparison_base(repo, "refs/heads/unchanged", "main")
    assert scope.changed_paths(repo, base) == []


def test_merge_uses_first_parent(repo: Path) -> None:
    baseline = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-b", "feature")
    commit(repo, "src/change.py")
    git(repo, "checkout", "main")
    git(repo, "merge", "--no-ff", "-m", "merge fixture", "feature")
    assert scope.comparison_base(repo, "refs/heads/main", "main") == baseline


def test_renaming_code_to_markdown_keeps_deleted_code_in_scope(repo: Path) -> None:
    baseline = commit(repo, "example.py")
    git(repo, "mv", "example.py", "example.md")
    git(repo, "commit", "-m", "rename")
    paths = scope.changed_paths(repo, baseline)
    assert paths == ["example.md", "example.py"]
    assert any(map(scope.is_code_change, paths))


def test_deleted_code_and_unusual_filenames(repo: Path) -> None:
    baseline = commit(repo, "examples/space and\nnewline.py")
    git(repo, "rm", "examples/space and\nnewline.py")
    git(repo, "commit", "-m", "delete")
    paths = scope.changed_paths(repo, baseline)
    assert paths == ["examples/space and\nnewline.py"]
    assert any(map(scope.is_code_change, paths))


@pytest.mark.parametrize(("name", "expected"), [
    ("CONTRIBUTING.md", "false"),
    ("src/change.py", "true"),
])
def test_workflow_output(repo: Path, tmp_path: Path, name: str, expected: str) -> None:
    git(repo, "checkout", "-b", "feature")
    commit(repo, name)
    output = tmp_path / "github-output"
    subprocess.run(
        [sys.executable, scope.__file__], cwd=repo, check=True, capture_output=True,
        env={**os.environ, "GITHUB_REF": "refs/heads/feature", "DEFAULT_BRANCH": "main",
             "GITHUB_OUTPUT": str(output)},
    )
    assert output.read_text() == f"run_tests={expected}\n"


def test_missing_base_fails_instead_of_skipping(repo: Path) -> None:
    with pytest.raises(subprocess.CalledProcessError):
        scope.comparison_base(repo, "refs/heads/feature", "missing")
