from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType


def _load_evaluator() -> ModuleType:
    path = Path("benchmarks/agent/evaluate.py")
    spec = importlib.util.spec_from_file_location("agent_evaluate", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_agent_cli_workflows_pass() -> None:
    evaluator = _load_evaluator()

    result = evaluator.evaluate(Path("benchmarks/agent/tasks.json"))

    assert result["tasks"] == 7
    assert result["passed"] == 7
    assert result["pass_rate"] == 1.0
