from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


def _collector() -> ModuleType:
    path = Path("benchmarks/work_memory/host_telemetry.py")
    spec = importlib.util.spec_from_file_location("host_telemetry", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _start(call_id: str, command: str) -> dict[str, object]:
    return {
        "type": "tool.execution_start", "id": f"{call_id}-start",
        "timestamp": "2026-01-01T00:00:00Z",
        "data": {
            "toolCallId": call_id, "toolName": "bash", "model": "recorded-model",
            "parentToolCallId": "agent-call", "arguments": {"command": command},
        },
    }


def _complete(call_id: str, content: str) -> dict[str, object]:
    return {
        "type": "tool.execution_complete", "id": f"{call_id}-end",
        "timestamp": "2026-01-01T00:00:01Z",
        "data": {
            "toolCallId": call_id, "success": True, "result": {"content": content},
        },
    }


def test_host_success_is_not_process_or_delivery_success(tmp_path: Path) -> None:
    collector = _collector()
    run = tmp_path / "run"
    prefix = f"uv run python benchmarks/work_memory/evaluate.py tool --run {run} --arm kg --"
    events = [
        _start("spill", f"{prefix} status Project && {prefix} actions Project"),
        _complete(
            "spill",
            "Output too large to read at once (23.6 KB). Saved to: /tmp/tool-output.txt\n"
            "Preview: ...\n<shellId: 1 completed with exit code 0>",
        ),
        _start("failed", f"{prefix} status Project | python -c 'print(1)'"),
        _complete(
            "failed", "/bin/bash: python: command not found\nBrokenPipeError\n"
            "<shellId: 2 completed with exit code 127>",
        ),
        _start("normal", f"{prefix} search missing"),
        _complete("normal", "[]\n<shellId: 3 completed with exit code 0>"),
        _start("pending", f"{prefix} status Project"),
        {"type": "assistant.message", "data": {"content": f"Do not export {prefix}"}},
    ]
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    result = collector.collect(path, run)
    calls = {call["call_id"]: call for call in result["calls"]}
    assert len(calls) == 4
    assert calls["spill"]["host_tool_success"] is True
    assert calls["spill"]["process_exit_code"] == 0
    assert calls["spill"]["delivery_observation"] == "spill_notice_and_preview"
    assert len(calls["spill"]["invocations"]) == 2
    assert calls["failed"]["host_tool_success"] is True
    assert calls["failed"]["process_exit_code"] == 127
    assert "command not found" in calls["failed"]["process_diagnostic"]
    assert calls["normal"]["process_exit_code"] == 0
    assert calls["normal"]["delivery_observation"] == "inline_response"
    assert calls["pending"]["process_exit_code"] is None
    assert calls["pending"]["host_tool_success"] is None
    assert calls["pending"]["delivery_observation"] == "not_observed"
    assert result["summary"]["output_spills"] == 1
    assert result["summary"]["nonzero_process_exits"] == 1
    assert result["summary"]["unobserved_completions"] == 1
    assert "Do not export" not in json.dumps(result)


def test_only_selected_run_is_exported_and_paths_with_spaces_work(tmp_path: Path) -> None:
    collector = _collector()
    run = tmp_path / "run with spaces"
    command = (
        "uv run python benchmarks/work_memory/evaluate.py tool "
        f"--run '{run}' --arm markdown -- read note.md"
    )
    events = [
        _start("selected", command),
        _start("other", command.replace(str(run), "/unrelated-run")),
        _start("code", f"uv run python -c {json.dumps(command)}"),
        _complete("selected", '{"documents": []}\n<shellId: 1 completed with exit code 0>'),
    ]
    path = tmp_path / "events.jsonl"
    path.write_text("".join(json.dumps(event) + "\n" for event in events))
    result = collector.collect(path, run)
    assert [call["call_id"] for call in result["calls"]] == ["selected"]
    assert result["calls"][0]["invocations"][0]["arguments"] == ["read", "note.md"]


def test_incomplete_host_log_is_explicit_not_an_empty_success(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text('{"type":')
    with pytest.raises(ValueError, match="incomplete host event"):
        _collector().collect(path, tmp_path / "run")
