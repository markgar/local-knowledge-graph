from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, cast


def _run_cli(manifest: Path, *arguments: str) -> Any:
    command, *rest = arguments
    entrypoint = (
        [str(Path(__file__).resolve().parents[1] / "_lexical_search.py")]
        if command == "search" else ["-m", "kg.legacy_cli", command]
    )
    process = subprocess.run(
        [
            sys.executable,
            *entrypoint,
            *rest,
            "--manifest",
            str(manifest),
            "--format",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode:
        raise RuntimeError(process.stderr.strip() or process.stdout.strip())
    return json.loads(process.stdout)


def _contains_all(actual: list[str], expected: list[str]) -> bool:
    return set(expected) <= set(actual)


def _evaluate_task(
    task: dict[str, Any],
    manifest: Path,
    vault: Path,
) -> tuple[bool, dict[str, Any]]:
    workflow = task["workflow"]
    if workflow == "search":
        results = cast(
            list[dict[str, Any]],
            _run_cli(
                manifest,
                "search",
                task["query"],
                "--query-mode",
                "natural",
                "--subject",
                task["subject"],
            ),
        )
        quotes = [result["quote"] for result in results]
        return _contains_all(quotes, task["expected_quotes"]), {"quotes": quotes}

    if workflow == "status":
        result = cast(
            dict[str, Any],
            _run_cli(manifest, "status", task["subject"]),
        )
        checks = []
        for key in ("decisions", "blockers", "conflicts"):
            expected = task.get(f"expected_{key}", [])
            actual = [item["summary"] for item in result[key]]
            checks.append(_contains_all(actual, expected))
        expected_connections = task.get("expected_connections", [])
        actual_connections = [item["summary"] for item in result["connected_entities"]]
        checks.append(_contains_all(actual_connections, expected_connections))
        if task.get("expect_evidence_gap"):
            checks.append(bool(result["evidence_gaps"]))
        return all(checks), result

    if workflow == "ambiguous_search":
        results = cast(
            list[dict[str, Any]],
            _run_cli(
                manifest,
                "search",
                task["query"],
                "--query-mode",
                "natural",
            ),
        )
        source_paths = sorted({result["source_path"] for result in results})
        requires_clarification = len(source_paths) > 1
        passed = requires_clarification and _contains_all(
            source_paths,
            task["expected_source_paths"],
        )
        return passed, {
            "source_paths": source_paths,
            "requires_clarification": requires_clarification,
        }

    if workflow == "citation_round_trip":
        results = cast(
            list[dict[str, Any]],
            _run_cli(
                manifest,
                "search",
                task["query"],
                "--query-mode",
                "natural",
                "--subject",
                task["subject"],
            ),
        )
        if not results:
            return False, {"error": "search returned no evidence"}
        selected = results[0]
        evidence = cast(
            dict[str, Any],
            _run_cli(manifest, "evidence", selected["record_id"]),
        )
        source_range = cast(
            dict[str, Any],
            _run_cli(manifest, "source-range", selected["anchor_id"]),
        )
        passed = (
            evidence["anchor_id"] == selected["anchor_id"]
            and evidence["quote"] == selected["quote"]
            and source_range["anchor_id"] == selected["anchor_id"]
            and source_range["quote"] == selected["quote"]
            and source_range["source_revision_id"] == selected["source_revision_id"]
        )
        return passed, {
            "record_id": selected["record_id"],
            "anchor_id": selected["anchor_id"],
            "source_revision_id": selected["source_revision_id"],
        }

    if workflow == "revision_comparison":
        source = vault / task["source_path"]
        original = source.read_text(encoding="utf-8")
        if task["before"] not in original:
            return False, {"error": "revision fixture did not contain expected text"}
        source.write_text(
            original.replace(task["before"], task["after"], 1),
            encoding="utf-8",
        )
        _run_cli(manifest, "ingest")
        comparison = cast(
            dict[str, Any],
            _run_cli(manifest, "compare-revisions", task["source_path"]),
        )
        changes = [
            (change["before"]["quote"], change["after"]["quote"])
            for change in comparison["modified"]
        ]
        return (task["before"], task["after"]) in changes, comparison

    raise ValueError(f"Unsupported agent workflow: {workflow}")


def evaluate(tasks_path: Path) -> dict[str, Any]:
    fixture = json.loads(tasks_path.read_text(encoding="utf-8"))
    repository = Path(__file__).resolve().parents[2]
    source_vault = repository / "corpora" / "fixtures" / "research-vault"
    task_results = []
    with tempfile.TemporaryDirectory(prefix="kg-agent-evaluation-") as temporary:
        workspace = Path(temporary)
        vault = workspace / "vault"
        shutil.copytree(source_vault, vault)
        (vault / "review.md").write_text(
            "# Orbit review\n\n"
            "Orbit has a documented review conflict.\n\n"
            "## Conflicts\n\n"
            "- The evaluation notes disagree about the approved sample size.\n",
            encoding="utf-8",
        )
        manifest = workspace / "corpus.yml"
        manifest.write_text(
            "corpus_id: agent-evaluation\n"
            "display_name: Agent evaluation\n"
            "vault_root: vault\n"
            "database: index.sqlite3\n"
            "include: ['*.md']\n"
            "seed_entities:\n"
            "  - entity_id: orbit\n"
            "    name: Orbit\n"
            "    entity_type: project\n"
            "    aliases: [Orbit study]\n"
            "  - entity_id: signal\n"
            "    name: Signal\n"
            "    entity_type: topic\n"
            "  - entity_id: relay\n"
            "    name: Relay\n"
            "    entity_type: experiment\n"
            "metadata_fields:\n"
            "  event_time: date\n",
            encoding="utf-8",
        )
        _run_cli(manifest, "ingest")
        for task in fixture["tasks"]:
            passed, details = _evaluate_task(task, manifest, vault)
            task_results.append(
                {
                    "id": task["id"],
                    "category": task["category"],
                    "passed": passed,
                    "details": details,
                }
            )

    passed = sum(result["passed"] for result in task_results)
    return {
        "benchmark": "agent-cli-e5",
        "benchmark_version": fixture["benchmark_version"],
        "tasks": len(task_results),
        "passed": passed,
        "pass_rate": passed / len(task_results),
        "task_results": task_results,
    }


def main() -> None:
    benchmark_directory = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Evaluate the stable JSON CLI with agent-oriented workflows."
    )
    parser.add_argument(
        "--tasks",
        type=Path,
        default=benchmark_directory / "tasks.json",
    )
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = evaluate(args.tasks)
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")


if __name__ == "__main__":
    main()
