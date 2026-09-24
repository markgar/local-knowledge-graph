#!/usr/bin/env python3
"""Invoke the historical demo CLI and render a small cited status report."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, cast


def load_status(
    manifest: Path,
    subject: str,
    since: str,
    executable: str | None = None,
) -> dict[str, Any]:
    prefix = [sys.executable, "-m", "kg.legacy_cli"] if executable is None else [executable]
    process = subprocess.run(
        [
            *prefix,
            "status",
            subject,
            "--manifest",
            str(manifest),
            "--since",
            since,
            "--format",
            "json",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode:
        try:
            error = json.loads(process.stderr)
            message = error["message"]
        except (json.JSONDecodeError, KeyError):
            message = process.stderr.strip() or "kg failed without an error message"
        raise RuntimeError(message)
    payload: object = json.loads(process.stdout)
    if not isinstance(payload, dict) or not all(
        isinstance(key, str) for key in payload
    ):
        raise RuntimeError("kg returned an invalid status payload")
    return cast(dict[str, Any], payload)


def render_status(status: dict[str, Any]) -> str:
    sections = [
        ("Decisions", status["decisions"]),
        ("Open actions", status["open_actions"]),
        ("Completed actions", status["completed_actions"]),
        ("Blockers", status["blockers"]),
        ("Conflicts", status["conflicts"]),
    ]
    lines = [f"# Status: {status['subject']}"]
    for title, records in sections:
        if not records:
            continue
        lines.extend(["", f"## {title}"])
        for record in records:
            summary = record["summary"] or record["quote"]
            heading = " / ".join(record["heading_path"])
            lines.append(
                f"- {summary} "
                f"({record['source_path']} @ {heading}; "
                f"revision {record['source_revision_id'][:12]})"
            )
    for gap in status["evidence_gaps"]:
        lines.extend(["", f"Insufficient evidence: {gap}"])
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("manifest", type=Path)
    parser.add_argument("subject")
    parser.add_argument("--since", default="30d")
    arguments = parser.parse_args()
    try:
        status = load_status(arguments.manifest, arguments.subject, arguments.since)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    print(render_status(status))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
