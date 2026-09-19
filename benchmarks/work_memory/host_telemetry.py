from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
from datetime import datetime
from pathlib import Path
from typing import Any

EXIT_RE = re.compile(r"<shellId: (?P<shell>[^ ]+) completed with exit code (?P<code>-?\d+)>")
SPILL_RE = re.compile(
    r"\AOutput too large to read at once \((?P<size>[^)]+)\)\. Saved to: (?P<path>[^\n]+)"
)


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value)
    except ValueError:
        return None
    return result if result.tzinfo is not None else None


def _correlate(
    calls: list[dict[str, Any]], run: Path,
) -> None:
    journals = {}
    for arm in ("kg", "markdown"):
        path = run / f"tools-{arm}.jsonl"
        if path.is_file():
            journals[arm] = [
                (number, json.loads(line))
                for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
            ]
    for call in calls:
        started = _timestamp(call["started_at"])
        completed = _timestamp(call["completed_at"])
        for invocation in call["invocations"]:
            matches = []
            if started is not None and completed is not None:
                for number, entry in journals.get(invocation["arm"], []):
                    entry_start = _timestamp(entry.get("started_at"))
                    entry_end = _timestamp(entry.get("completed_at"))
                    if (
                        entry_start is not None and entry_end is not None
                        and started <= entry_start <= entry_end <= completed
                        and entry.get("arguments") == invocation["arguments"]
                    ):
                        execution = entry.get("execution", {})
                        matches.append({
                            "journal": f"tools-{invocation['arm']}.jsonl",
                            "line": number,
                            "request_id": entry.get("request_id"),
                            "run_id": entry.get("run_id"),
                            "wrapper_success": entry.get("success"),
                            "operation_success": entry.get("operation_success"),
                            "delivery_success": entry.get("delivery_success"),
                            "delivery_error": entry.get("delivery_error"),
                            "native_cli_exit_code": execution.get("exit_code"),
                            "serialized_response_bytes": entry.get("returned_utf8_bytes"),
                            "response_shape": entry.get("response_shape"),
                        })
            invocation["journal_matches"] = matches
            invocation["correlation"] = (
                "timestamp_unavailable" if started is None or completed is None
                else "matched" if len(matches) == 1
                else "ambiguous" if matches else "not_observed"
            )


def _invocations(command: str, run: Path) -> list[dict[str, Any]]:
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>")
    lexer.whitespace_split = True
    tokens = list(lexer)
    invocations = []
    for index, token in enumerate(tokens):
        if not token.endswith("benchmarks/work_memory/evaluate.py"):
            continue
        if tokens[index + 1:index + 2] != ["tool"]:
            continue
        if index == 0 or not Path(tokens[index - 1]).name.startswith("python"):
            continue
        end = index + 2
        while end < len(tokens) and tokens[end] not in {";", "&&", "||", "|", ">", ">>", "<"}:
            end += 1
        args = tokens[index + 2:end]
        options = {}
        for position, arg in enumerate(args):
            for name in ("run", "arm"):
                if arg == f"--{name}" and position + 1 < len(args):
                    options[name] = args[position + 1]
                elif arg.startswith(f"--{name}="):
                    options[name] = arg.split("=", 1)[1]
        if options.get("run") != str(run) or options.get("arm") not in {"kg", "markdown"}:
            continue
        separator = args.index("--") if "--" in args else None
        invocations.append({
            "arm": options["arm"],
            "arguments": args[separator + 1:] if separator is not None else None,
        })
    return invocations


def collect(events_path: Path, run: Path) -> dict[str, Any]:
    starts = {}
    completes = {}
    warnings = []
    repository = str(Path(__file__).resolve().parents[2])
    with events_path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            try:
                event = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(
                    f"Invalid or incomplete host event at line {number}; retry"
                ) from exc
            data = event.get("data", {})
            if not isinstance(data, dict):
                continue
            if event.get("type") == "tool.execution_start" and data.get("toolName") == "bash":
                arguments = data.get("arguments", {})
                command = arguments.get("command", "") if isinstance(arguments, dict) else ""
                if "benchmarks/work_memory/evaluate.py" not in command:
                    continue
                try:
                    invocations = _invocations(command, run)
                except ValueError:
                    if str(run) in command:
                        warnings.append(
                            f"Could not parse command at event line {number}; coverage is unknown"
                        )
                    continue
                if invocations:
                    starts[data["toolCallId"]] = (event, data, command, invocations, number)
            elif event.get("type") == "tool.execution_complete":
                completes[data.get("toolCallId")] = (event, data, number)
    calls = []
    for call_id, (start, request, command, invocations, line_number) in starts.items():
        complete, outcome, complete_line = completes.get(call_id, ({}, {}, None))
        result = outcome.get("result", {})
        content = result.get("content") if isinstance(result, dict) else None
        exit_match = EXIT_RE.search(content) if isinstance(content, str) else None
        spill_match = SPILL_RE.match(content) if isinstance(content, str) else None
        spill = None
        if spill_match:
            spill = {
                "reported_size": spill_match["size"],
                "filename": Path(spill_match["path"]).name,
                "full_content_delivered_inline": False,
            }
        calls.append({
            "call_id": call_id,
            "parent_call_id": request.get("parentToolCallId"),
            "start_event_id": start.get("id"),
            "complete_event_id": complete.get("id"),
            "start_event_line": line_number,
            "complete_event_line": complete_line,
            "started_at": start.get("timestamp"),
            "completed_at": complete.get("timestamp"),
            "host_recorded_model": request.get("model"),
            "invocations": invocations,
            "command": command.replace(str(run), "<RUN>").replace(repository, "<REPO>"),
            "host_tool_success": outcome.get("success"),
            "process_exit_code": int(exit_match["code"]) if exit_match else None,
            "shell_id": exit_match["shell"] if exit_match else None,
            "host_content_utf8_bytes": len(content.encode("utf-8"))
            if isinstance(content, str) else None,
            "host_content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest()
            if isinstance(content, str) else None,
            "host_output_spill": spill,
            "delivery_observation": (
                "spill_notice_and_preview" if spill else "inline_response"
                if isinstance(content, str) else "not_observed"
            ),
            "process_diagnostic": (
                content[:1500].replace(str(run), "<RUN>").replace(repository, "<REPO>")
                if exit_match and int(exit_match["code"]) != 0 else None
            ),
        })
    _correlate(calls, run)
    return {
        "version": 1,
        "source": "Actual tool.execution_start/tool.execution_complete host events",
        "calls": calls,
        "collection_warnings": warnings,
        "summary": {
            "host_calls": len(calls),
            "wrapper_invocations": sum(len(call["invocations"]) for call in calls),
            "nonzero_process_exits": sum(
                call["process_exit_code"] not in (0, None) for call in calls
            ),
            "output_spills": sum(call["host_output_spill"] is not None for call in calls),
            "unobserved_completions": sum(call["complete_event_id"] is None for call in calls),
            "correlated_invocations": sum(
                invocation["correlation"] == "matched"
                for call in calls for invocation in call["invocations"]
            ),
            "host_recorded_models": {
                arm: sorted({
                    call["host_recorded_model"] for call in calls
                    if any(item["arm"] == arm for item in call["invocations"])
                    and isinstance(call["host_recorded_model"], str)
                })
                for arm in ("kg", "markdown")
            },
        },
        "limitations": [
            "Host-tool success is not process success or successful evidence delivery.",
            "An absent process exit code is unknown, not success.",
            "Host-returned content is observable; later model context assembly "
            "and token use are not.",
            "Only matching wrapper invocations with a literal run path are collected.",
            "Journal correlation requires exact arguments, the same arm, and timestamps "
            "inside the host call; missing or ambiguous matches remain explicit.",
            "No assistant reasoning, prompts, or unrelated session events are exported.",
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Collect actual host telemetry for a tool run.")
    parser.add_argument("--events", type=Path, required=True)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = collect(args.events, args.run)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["summary"], indent=2))


if __name__ == "__main__":
    main()
