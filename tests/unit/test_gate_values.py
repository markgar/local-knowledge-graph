from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import signal
import sys
import types
from pathlib import Path

import pytest
from support import gates
from support.modules import module


@pytest.fixture
def runner():
    return module(".github/scripts/check_pr.py")


def test_plan_keeps_core_and_normalizes_additions():
    roots, expected = gates.plan(["query", "query"], [
        "tests/unit/test_manifest.py::test_manifest_rejects_path_traversal"
    ])
    assert roots[0] == "tests/unit"
    assert len(roots) == len(set(roots))
    assert set(gates.CORE) <= expected.keys()
    assert gates.AREAS["query"].items() <= expected.items()
    assert not any("::test_manifest" in root for root in roots)
    assert "tests/unit/test_manifest.py::test_manifest_rejects_path_traversal" in expected
    for areas in ([], ["new-area"]):
        with pytest.raises(ValueError, match="supported"):
            gates.plan(areas, [])
    for case in ("../tests/x.py::test_x", "tests/test_query.py", "/tmp/x.py::test_x"):
        with pytest.raises(ValueError, match="exact repo-relative"):
            gates.plan(["query"], [case])


def test_every_declared_selector_has_a_portable_pinned_layer():
    for mapping in (gates.CORE, *gates.AREAS.values()):
        assert mapping
        assert set(mapping.values()) <= {"unit", "service", "functional", "process"}
        for selector in mapping:
            assert Path(selector.split("::")[0]).is_file()


def test_selection_rejects_missing_layer_drift_and_native_requirement():
    expected = {"tests/a.py::test_x": "service"}
    with pytest.raises(ValueError, match="no cases"):
        gates.validate_selection([], expected)
    item = {"nodeid": "tests/a.py::test_x[1]", "primary": "service", "requires_native": False}
    gates.validate_selection([item], expected)
    for mutation in ({"primary": "acceptance"}, {"requires_native": True}):
        with pytest.raises(ValueError, match="metadata mismatch"):
            gates.validate_selection([item | mutation], expected)
    assert not gates.selected_by("tests/a.py::test_xyz", "tests/a.py::test_x")


def test_runtime_guard_refuses_real_import_and_preload_but_allows_controlled_stub(monkeypatch):
    guard = gates.RuntimeGuard()
    with pytest.raises(RuntimeError, match="forbids"):
        guard.find_spec("torch.nn")
    stub = types.ModuleType("sentence_transformers")
    monkeypatch.setitem(sys.modules, "sentence_transformers", stub)
    guard.install()
    guard.remove()
    stub.__spec__ = importlib.util.spec_from_loader("sentence_transformers", loader=object())
    with pytest.raises(RuntimeError, match="preloaded"):
        guard.install()


def test_reports_are_fresh_and_failed_timing_cannot_pass(tmp_path, runner):
    report = tmp_path / "report.json"
    gates.write_json(report, {"timed_gate": "pending", "correctness": "passed"})
    with pytest.raises(FileExistsError):
        gates.write_json(report, {"correctness": "passed"})
    receipt = tmp_path / "receipt.json"
    gates.write_json(receipt, {
        "report": str(report), "sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
    })
    timing = tmp_path / "elapsed"
    timing.write_text("119.001")
    assert runner.finalize(timing, receipt) == 1
    assert json.loads(report.read_text())["timed_gate"] == "failed"
    with pytest.raises(ValueError, match="changed"):
        runner.finalize(timing, receipt)


@pytest.mark.parametrize("elapsed", [85.014630625, 118.999, 119, 119.001])
@pytest.mark.parametrize("correctness", ["passed", "failed", "incomplete"])
def test_two_minute_policy_boundary_and_report(tmp_path, runner, capsys, elapsed, correctness):
    report = tmp_path / "report.json"
    gates.write_json(report, {"timed_gate": "pending", "correctness": correctness})
    record = {"report": str(report), "sha256": hashlib.sha256(report.read_bytes()).hexdigest()}
    passed = correctness == "passed" and elapsed <= 119
    assert runner.finalize_record(record, elapsed) == (0 if passed else 1)
    assert json.loads(report.read_text()) == {
        "correctness": correctness, "timed_gate": "passed" if passed else "failed",
        "outer_uv_seconds": elapsed, "outer_uv_limit_seconds": 119,
        "reporting_allowance_seconds": 1, "public_call_target_seconds": 120,
    }
    assert "limit 119s + reporting" in capsys.readouterr().out


@pytest.mark.parametrize("elapsed", ["NaN", "inf", "-1"])
def test_nonfinite_or_negative_timing_is_not_success(tmp_path, runner, elapsed):
    report, receipt, timing = (tmp_path / name for name in ("report", "receipt", "timing"))
    gates.write_json(report, {"timed_gate": "pending", "correctness": "passed"})
    gates.write_json(receipt, {
        "report": str(report), "sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
    })
    timing.write_text(elapsed)
    with pytest.raises(ValueError, match="elapsed"):
        runner.finalize(timing, receipt)
    assert json.loads(report.read_text())["timed_gate"] == "pending"


def test_metadata_only_does_not_hide_assertion_or_parameter_changes(runner):
    old = "import pytest\n@pytest.mark.parametrize('n', [1, 2])\ndef test_x(n):\n assert n > 0\n"
    new = old.replace(
        "[1, 2]", "[pytest.param(1, marks=pytest.mark.process), "
        "pytest.param(2, marks=pytest.mark.acceptance)]"
    )
    assert runner.metadata_only(old, new)
    assert not runner.metadata_only(old, new.replace("n > 0", "n >= 0"))
    assert not runner.metadata_only(old, new.replace("param(2,", "param(3,"))


def test_new_runtime_and_model_ownership_fail_closed(runner):
    for path in ("src/kg/new_area/a.py", "src/kg/models/new_kind.py", "src/kg/__init__.py"):
        with pytest.raises(ValueError, match="policy"):
            runner.source_areas(path)
    assert {"schema", "query", "graph"} <= runner.source_areas("src/kg/evidence/schema.sql")


def test_shared_execution_budget_requires_all_canonical_downstream_areas(runner, monkeypatch):
    expected = {
        "evidence", "schema", "knowledge", "indexing", "query", "processing", "graph", "cli",
    }
    assert runner.source_areas("src/kg/_execution_budget.py") == expected
    monkeypatch.setattr(
        runner, "git",
        lambda *args: b"src/kg/_execution_budget.py\0" if args[0] == "diff"
        else b"base" if args[0] == "merge-base" else b"",
    )
    for missing in expected:
        with pytest.raises(ValueError, match="requires areas"):
            runner.changed_scope("origin/main", expected - {missing})
    changed = runner.changed_scope("origin/main", expected)["changed_paths"]
    assert changed[0]["minimum_areas"] == sorted(expected)
    with pytest.raises(ValueError, match="policy"):
        runner.source_areas("src/kg/_unreviewed_helper.py")


def test_collection_rejects_conflicting_missing_and_illegal_layers():
    hooks = module("tests/conftest.py")

    class Config:
        _gate_items = []

        def getoption(self, name):
            return False

    class Item:
        nodeid = "tests/isolated.py::test_bad"

        def __init__(self, names):
            self.names = names

        def iter_markers(self):
            return [types.SimpleNamespace(name=name) for name in self.names]

        def get_closest_marker(self, name):
            return name if name in self.names else None

    for names in ([], ["unit", "service"], ["native"], ["unit", "requires_native"]):
        with pytest.raises(pytest.UsageError, match="expected exactly one"):
            hooks.pytest_collection_modifyitems(Config(), [Item(names)])


def test_native_helper_rejects_missing_metadata_before_engine(monkeypatch):
    from support import graph

    def forbidden():
        raise AssertionError("must not probe engine before checking test metadata")

    monkeypatch.setattr(graph, "_engine", forbidden)
    monkeypatch.setenv(gates.NATIVE_CONTEXT, "0")
    with pytest.raises(pytest.fail.Exception, match="must declare requires_native"):
        graph.require_native()


def test_corpus_markdown_is_not_documentation_scope(tmp_path, runner, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)

    def git(*args):
        if args[0] == "merge-base":
            return b"base"
        if args[0] == "diff":
            return b"corpora/fixtures/note.md\0"
        return b""

    monkeypatch.setattr(runner, "git", git)
    with pytest.raises(ValueError, match="requires areas.*evaluation"):
        runner.changed_scope("origin/main", {"validation"})
    result = runner.changed_scope("origin/main", {"evaluation"})
    assert result["changed_paths"][0]["minimum_areas"] == ["evaluation"]


def test_only_pytest_pyproject_edits_are_organization(tmp_path, runner, monkeypatch):
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    old = '[project]\ndependencies=["original"]\n[tool.pytest.ini_options]\naddopts="-q"\n'
    monkeypatch.setattr(runner, "before", lambda *args: old)
    monkeypatch.setattr(
        runner, "git",
        lambda *args: b"pyproject.toml\0" if args[0] == "diff"
        else b"base" if args[0] == "merge-base" else b"",
    )
    path = tmp_path / "pyproject.toml"
    path.write_text(old.replace('"-q"', '"-q --strict-markers"'))
    assert runner.changed_scope("origin/main", {"validation"})["changed_paths"]
    path.write_text(old.replace("original", "new"))
    with pytest.raises(ValueError, match="Non-pytest"):
        runner.changed_scope("origin/main", {"validation"})


def test_runner_stops_after_failure_and_reports_remaining_checks_not_run(
    tmp_path, runner, monkeypatch
):
    report, receipt = tmp_path / "gate.json", tmp_path / "receipt"
    monkeypatch.setattr(runner, "changed_scope", lambda *args: {"head": "test"})
    monkeypatch.setattr(runner, "plan", lambda *args: (["tests/unit"], {}))
    monkeypatch.setenv("KG_GATE_RECEIPT", str(receipt))
    monkeypatch.setattr(sys, "argv", [
        "check_pr.py", "--area", "validation", "--reason", "Controlled tooling test",
        "--report", str(report),
    ])
    calls = []

    def fail(command, env):
        calls.append(command)
        path = Path(command[command.index("--gate-report") + 1])
        gates.write_json(path, {"status": "failed", "selected": [], "phases": []})
        return 1

    monkeypatch.setattr(runner, "run_child", fail)
    assert runner.main() == 1
    result = json.loads(report.read_text())
    assert len(calls) == 1
    assert result["correctness"] == "failed" and result["timed_gate"] == "pending"
    assert [entry["status"] for entry in result["commands"]] == [
        "failed", "not_run", "not_run", "not_run",
    ]
    assert all(entry.startswith("not_run:") for entry in result["release_obligations"])


@pytest.mark.parametrize("error", [OSError("cleanup failed"), KeyboardInterrupt()])
def test_runner_cleanup_failure_never_retains_success(tmp_path, runner, monkeypatch, error):
    report = tmp_path / "gate.json"
    monkeypatch.setattr(runner, "changed_scope", lambda *args: {})
    monkeypatch.setattr(runner, "plan", lambda *args: (["tests/unit"], {}))
    monkeypatch.setenv("KG_GATE_RECEIPT", str(tmp_path / "receipt"))
    monkeypatch.setattr(sys, "argv", [
        "check_pr.py", "--area", "validation", "--reason", "Cleanup regression",
        "--report", str(report),
    ])
    directory = tmp_path / "checks"
    directory.mkdir()

    class BrokenCleanup:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return str(directory)

        def __exit__(self, *args):
            raise error

    def passed(command, env):
        if "--gate-report" in command:
            path = Path(command[command.index("--gate-report") + 1])
            gates.write_json(path, {"status": "passed"})
        return 0

    monkeypatch.setattr(runner.tempfile, "TemporaryDirectory", BrokenCleanup)
    monkeypatch.setattr(runner, "run_child", passed)
    assert runner.main() != 0
    result = json.loads(report.read_text())
    assert result["correctness"] == "incomplete"
    assert result["timed_gate"] == "pending"


@pytest.mark.parametrize("missing", ["test_does_not_exist", "test_manifest[unknown]"])
def test_covered_explicit_selectors_must_still_resolve(missing):
    selector = f"tests/unit/test_manifest.py::{missing}"
    roots, expected = gates.plan(["validation"], [selector])
    assert selector not in roots and selector in expected
    with pytest.raises(ValueError, match="no cases"):
        gates.validate_selection(
            [{"nodeid": "tests/unit/test_manifest.py::test_manifest[known]",
              "primary": "unit", "requires_native": False}],
            {selector: expected[selector]},
        )


@pytest.mark.parametrize("phase", ["startup", "finalization-before", "finalization-after"])
def test_supervisor_signals_cannot_certify_success(tmp_path, runner, monkeypatch, phase):
    report = tmp_path / "report.json"
    monkeypatch.setattr(runner, "group_alive", lambda pid: False)

    class Process:
        pid = 123456789

        def wait(self):
            return 0

    def start(*args, **kwargs):
        env = kwargs["env"]
        gates.write_json(report, {
            "correctness": "passed", "timed_gate": "pending", "run_id": env["KG_GATE_RUN_ID"],
        })
        gates.write_json(Path(env["KG_GATE_RECEIPT"]), {
            "report": str(report), "sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
        })
        if phase == "startup":
            os.kill(os.getpid(), signal.SIGTERM)
        return Process()

    finalize = runner.finalize_record

    def cancelled_finalize(*args):
        if phase == "finalization-after":
            finalize(*args)
        signal.raise_signal(signal.SIGTERM)

    monkeypatch.setattr(runner.subprocess, "Popen", start)
    monkeypatch.setattr(runner, "finalize_record", cancelled_finalize)
    assert runner.launch([], report) == 130
    result = json.loads(report.read_text())
    assert result["correctness"] == result["timed_gate"] == "incomplete"


@pytest.mark.parametrize("phase", ["error", "cancellation", "wait-cancellation"])
@pytest.mark.parametrize("persistence_failure", [False, True])
def test_secondary_cleanup_failure_preserves_incomplete_report(
    tmp_path, runner, monkeypatch, capsys, phase, persistence_failure,
):
    report = tmp_path / "report.json"
    calls = []

    class Process:
        pid = 123456789

        def wait(self):
            if phase == "wait-cancellation":
                raise KeyboardInterrupt()
            return 0

    def start(*args, **kwargs):
        env = kwargs["env"]
        gates.write_json(report, {
            "correctness": "passed", "timed_gate": "pending", "run_id": env["KG_GATE_RUN_ID"],
        })
        gates.write_json(Path(env["KG_GATE_RECEIPT"]), {
            "report": str(report), "sha256": hashlib.sha256(report.read_bytes()).hexdigest(),
        })
        return Process()

    def cleanup(process, *, cancelled):
        assert isinstance(process, Process)
        calls.append(cancelled)
        if len(calls) == 1:
            if phase in {"error", "wait-cancellation"}:
                raise PermissionError("primary cleanup EPERM")
            return False
        raise PermissionError("secondary cleanup EPERM")

    def interrupt(*args):
        raise KeyboardInterrupt()

    def cannot_persist(*args):
        raise OSError("cannot persist incomplete report")

    monkeypatch.setattr(runner.subprocess, "Popen", start)
    monkeypatch.setattr(runner, "finish_group", cleanup)
    if phase == "cancellation":
        monkeypatch.setattr(runner, "finalize_record", interrupt)
    if persistence_failure:
        monkeypatch.setattr(runner, "mark_interrupted", cannot_persist)
        with pytest.raises(OSError, match="cannot persist incomplete report"):
            runner.launch([], report)
        assert "secondary cleanup EPERM" in capsys.readouterr().err
    else:
        assert runner.launch([], report) != 0
        value = json.loads(report.read_text())
        assert value["correctness"] == value["timed_gate"] == "incomplete"
        assert "secondary cleanup EPERM" in value["error"]
        assert value["error"] in capsys.readouterr().err
        if phase in {"error", "wait-cancellation"}:
            assert "primary cleanup EPERM" in value["error"]
        if phase in {"cancellation", "wait-cancellation"}:
            assert "Interrupted" in value["error"]
    assert len(calls) == 2 and calls[-1] is True


@pytest.mark.parametrize("sequence", [
    ("setup", "call", "teardown"), ("setup", "call"),
    ("setup", "call", "teardown", "teardown"),
])
def test_mandatory_outcomes_require_exactly_one_complete_successful_protocol(sequence):
    hooks = module("tests/conftest.py")
    config = types.SimpleNamespace(
        _gate_guard=gates.RuntimeGuard(), _gate_problem=False,
        _gate_items=[{"nodeid": "selected"}],
        _gate_phases=[
            {"nodeid": "selected", "phase": phase, "outcome": "passed", "xfail": False}
            for phase in sequence
        ],
        getoption=lambda name: name == "--pr-gate",
    )
    session = types.SimpleNamespace(config=config, exitstatus=0)
    hooks.pytest_sessionfinish(session, 0)
    assert (session.exitstatus == 0) == (sequence == ("setup", "call", "teardown"))


@pytest.mark.parametrize(("requirement", "installed", "status"), [
    ("setuptools>=68", "84.0.0", "satisfied"),
    ("setuptools>=68", "67.0.0", "failed"),
    ("setuptools>=68", None, "failed"),
    ("setuptools>=68", "", "failed"),
    ("setuptools>=68", "not-a-version", "failed"),
    ("setuptools>=68; python_version>='3.12'", "84.0.0", "satisfied"),
    ("setuptools>=68; python_version<'0'", None, "not_applicable"),
    ("invalid !!!", "84.0.0", "failed"),
    ("setuptools[extra]>=68", "84.0.0", "failed"),
    ("setuptools @ https://example.invalid/setuptools.whl", "84.0.0", "failed"),
])
def test_build_requirements_are_version_checked_and_reported(
    tmp_path, runner, monkeypatch, requirement, installed, status,
):
    path = tmp_path / "pyproject.toml"
    path.write_text(
        '[build-system]\nbuild-backend="setuptools.build_meta"\nrequires=['
        + json.dumps(requirement) + "]\n"
    )
    calls = []

    def version(name):
        calls.append(name)
        if installed is None:
            raise runner.importlib.metadata.PackageNotFoundError(name)
        return installed

    monkeypatch.setattr(runner.importlib.metadata, "version", version)
    result = runner.build_preparation(path)
    assert result["mode"] == "prepared-no-isolation"
    assert result["interpreter"] == sys.executable
    assert result["backend"] == "setuptools.build_meta"
    assert result["requirements"][0]["status"] == status
    assert (result["status"] == "ready") == (status != "failed")
    if status == "satisfied":
        assert result["requirements"][0]["version"] == installed
    elif status == "not_applicable":
        assert calls == []


@pytest.mark.parametrize("declaration", [
    "", '[build-system]\nrequires=[]\nbuild-backend="setuptools.build_meta"',
    '[build-system]\nrequires=["setuptools>=68"]\nbuild-backend="other.backend"',
    '[build-system]\nrequires=["setuptools>=68"]\n'
    'build-backend="setuptools.build_meta"\nbackend-path=["backend"]',
])
def test_unsupported_build_configuration_fails_closed(tmp_path, runner, declaration):
    path = tmp_path / "pyproject.toml"
    path.write_text(declaration)
    assert runner.build_preparation(path)["status"] == "failed"


def test_build_preparation_failure_leaves_every_check_not_run(tmp_path, runner, monkeypatch):
    report = tmp_path / "gate.json"
    monkeypatch.setattr(runner, "changed_scope", lambda *args: {})
    monkeypatch.setattr(runner, "plan", lambda *args: (["tests/unit"], {}))
    monkeypatch.setenv("KG_GATE_RECEIPT", str(tmp_path / "receipt"))
    monkeypatch.setattr(sys, "argv", [
        "check_pr.py", "--area", "validation", "--reason", "Missing backend regression",
        "--report", str(report),
    ])

    def missing(name):
        raise runner.importlib.metadata.PackageNotFoundError(name)

    def forbidden(*args):
        raise AssertionError("No checks may start before complete build readiness")

    monkeypatch.setattr(runner.importlib.metadata, "version", missing)
    monkeypatch.setattr(runner, "run_child", forbidden)
    assert runner.main() == 1
    result = json.loads(report.read_text())
    assert result["correctness"] == "incomplete"
    assert result["build"]["status"] == "failed"
    assert result["build"]["requirements"][0]["requirement"] == "setuptools>=68"
    assert all(entry["status"] == "not_run" for entry in result["commands"])
    build_command = result["commands"][-1]["argv"]
    assert "--offline" in build_command and "--no-build-isolation" in build_command
    assert build_command[build_command.index("--python") + 1] == sys.executable
    assert "not_run: isolated sdist+wheel packaging validation" in result["release_obligations"]
