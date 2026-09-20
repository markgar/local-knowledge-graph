"""Gate the manually dispatched Python matrix on code-affecting changes."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path, PurePosixPath


def is_code_change(name: str) -> bool:
    path = PurePosixPath(name)
    if name == "corpora/foundation/README.md":
        return False
    if path.parts[0] in {"src", "tests", "corpora"}:
        return True
    if path.suffix.lower() in {".md", ".rst"}:
        return False
    if name == "LICENSE":
        return False
    # Unknown paths stay in scope so new code/configuration cannot silently bypass CI.
    return not (
        name.startswith(".github/ISSUE_TEMPLATE/") and path.suffix in {".yml", ".yaml"}
    )


def git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    return subprocess.run(
        ["git", *args], cwd=repo, input=input_bytes, capture_output=True, check=True
    ).stdout


def comparison_base(repo: Path, ref: str, default_branch: str) -> str:
    if ref == f"refs/heads/{default_branch}":
        parents = git(repo, "rev-list", "--parents", "-n", "1", "HEAD").decode().split()
        if len(parents) > 1:
            return parents[1]
        return git(repo, "hash-object", "-t", "tree", "--stdin", input_bytes=b"").decode().strip()
    return git(repo, "merge-base", "HEAD", f"refs/remotes/origin/{default_branch}").decode().strip()


def changed_paths(repo: Path, base: str) -> list[str]:
    output = git(repo, "diff", "--name-only", "--no-renames", "-z", base, "HEAD", "--")
    return [os.fsdecode(name) for name in output.split(b"\0") if name]


def main() -> None:
    repo = Path.cwd()
    base = comparison_base(repo, os.environ["GITHUB_REF"], os.environ["DEFAULT_BRANCH"])
    paths = changed_paths(repo, base)
    run_tests = any(is_code_change(name) for name in paths)
    result = "true" if run_tests else "false"
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        output.write(f"run_tests={result}\n")
    print(f"Compared with {base}: {len(paths)} changed paths; run Python matrix: {result}")
    if not run_tests:
        print("Skipping dependencies, tests, lint, type checking and build: no code changes.")


if __name__ == "__main__":
    main()
