"""Collection metadata and fast-gate boundaries; imports no product code."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from support.gates import (
    AREAS,
    CORE,
    LAYERS,
    NATIVE_CONTEXT,
    RuntimeGuard,
    validate_selection,
    write_json,
)


def pytest_addoption(parser):
    parser.addoption("--pr-gate", action="store_true", help="Enforce portable mandatory PR checks")
    parser.addoption("--pr-plan", help="Expected selector metadata from the gate runner")
    parser.addoption("--test-inventory", help="Write exact collected node/layer inventory")
    parser.addoption("--gate-report", help="Write selected case and phase outcomes")


def pytest_configure(config):
    config._gate_guard = RuntimeGuard()
    config._gate_guard.install()
    config._gate_items = []
    config._gate_phases = []
    config._gate_problem = False


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    seen = set()
    for item in items:
        if item.nodeid in seen:
            raise pytest.UsageError(f"Duplicate collected case: {item.nodeid}")
        seen.add(item.nodeid)
        layers = [mark.name for mark in item.iter_markers() if mark.name in LAYERS]
        in_unit = item.nodeid.startswith("tests/unit/")
        if in_unit and not layers:
            item.add_marker(pytest.mark.unit)
            layers = ["unit"]
        native = item.get_closest_marker("requires_native") is not None
        if (
            len(layers) != 1
            or (in_unit and layers != ["unit"])
            or (layers == ["unit"] and native)
            or (layers == ["native"] and not native)
        ):
            raise pytest.UsageError(
                f"{item.nodeid}: expected exactly one of {sorted(LAYERS)}; "
                f"observed {layers}, requires_native={native}, in_unit={in_unit}"
            )
        if config.getoption("--pr-gate") and (native or layers[0] in {"native", "acceptance"}):
            raise pytest.UsageError(f"Release-only case cannot enter PR gate: {item.nodeid}")
        config._gate_items.append(
            {"nodeid": item.nodeid, "primary": layers[0], "requires_native": native}
        )
    if config.getoption("--pr-gate") and not items:
        raise pytest.UsageError("Mandatory PR selection is empty")
    plan_path = config.getoption("--pr-plan")
    if config.getoption("--pr-gate") and not plan_path:
        raise pytest.UsageError("--pr-gate requires the runner's explicit --pr-plan")
    try:
        if plan_path:
            validate_selection(config._gate_items, json.loads(Path(plan_path).read_text()))
        if config.getoption("--test-inventory") and config.args == ["tests"]:
            expected = dict(CORE)
            for area in AREAS.values():
                expected.update(area)
            validate_selection(config._gate_items, expected)
    except ValueError as error:
        raise pytest.UsageError(str(error)) from error
    # Real runtimes loaded later must not contaminate a unit's import boundary.
    items.sort(key=lambda item: item.get_closest_marker("unit") is None)


def pytest_collection_finish(session):
    config = session.config
    if path := config.getoption("--test-inventory"):
        write_json(Path(path), config._gate_items)
    if not config.getoption("--pr-gate"):
        config._gate_guard.remove()
    if config._gate_items and all(
        item["nodeid"].startswith("tests/unit/") for item in config._gate_items
    ):
        reporter = config.pluginmanager.getplugin("terminalreporter")
        if reporter:
            reporter.write_line(
                "Small unit selection only; use check_pr.sh for PR checks or pytest tests for full."
            )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item, nextitem):
    config = item.config
    previous = os.environ.get(NATIVE_CONTEXT)
    os.environ[NATIVE_CONTEXT] = (
        "1" if item.get_closest_marker("requires_native") is not None else "0"
    )
    guarded = config.getoption("--pr-gate") or item.get_closest_marker("unit") is not None
    try:
        if guarded:
            config._gate_guard.install()
        yield
    finally:
        if previous is None:
            os.environ.pop(NATIVE_CONTEXT, None)
        else:
            os.environ[NATIVE_CONTEXT] = previous
        if not config.getoption("--pr-gate"):
            config._gate_guard.remove()


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    report = (yield).get_result()
    config = item.config
    config._gate_phases.append(
        {"nodeid": report.nodeid, "phase": report.when, "outcome": report.outcome,
         "duration": report.duration, "xfail": bool(getattr(report, "wasxfail", False))}
    )
    if report.failed or report.skipped or getattr(report, "wasxfail", False):
        config._gate_problem = True


def pytest_sessionfinish(session, exitstatus):
    config = session.config
    config._gate_guard.remove()
    if config.getoption("--pr-gate"):
        selected = {item["nodeid"] for item in config._gate_items}
        expected_phases = {
            (node, phase) for node in selected for phase in ("setup", "call", "teardown")
        }
        successful = [
            (phase["nodeid"], phase["phase"]) for phase in config._gate_phases
            if phase["outcome"] == "passed" and not phase["xfail"]
        ]
        if (
            not selected or set(successful) != expected_phases
            or len(successful) != len(expected_phases)
        ):
            config._gate_problem = True
    if config.getoption("--pr-gate") and config._gate_problem and exitstatus == 0:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    if path := config.getoption("--gate-report"):
        write_json(Path(path), {
            "selected": config._gate_items,
            "phases": config._gate_phases,
            "exitstatus": int(session.exitstatus),
            "status": (
                "passed" if session.exitstatus == 0 and not config._gate_problem else "failed"
            ),
        })
