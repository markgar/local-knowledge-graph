"""Bounded synthetic sessions exercise guards, reporting and process ownership."""

import json
import os
import shutil
import signal
import subprocess
import sys
import time
from contextlib import suppress

import pytest
from support.gates import ROOT, write_json

pytestmark = pytest.mark.functional


def project(tmp_path, code):
    tests = tmp_path / "tests"
    tests.mkdir()
    shutil.copyfile(ROOT / "tests/conftest.py", tests / "conftest.py")
    (tests / "test_sample.py").write_text(code)
    (tmp_path / "pytest.ini").write_text(
        "[pytest]\naddopts = --strict-markers\nmarkers =\n"
        " unit\n service\n functional\n process\n native\n acceptance\n requires_native\n"
    )
    return tests


def child(tmp_path, *args):
    env = dict(os.environ, PYTHONPATH=str(ROOT / "tests"), HF_HUB_OFFLINE="1", UV_OFFLINE="1")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q",
         "--basetemp", str(tmp_path / "pytest-tmp"), *args],
        cwd=tmp_path, env=env, capture_output=True, text=True, check=False,
    )


def test_mixed_parameter_metadata_and_synthetic_provider_are_portable(tmp_path):
    project(tmp_path, """
import sys, types
import pytest
@pytest.mark.parametrize('n', [
    pytest.param(1, marks=pytest.mark.unit),
    pytest.param(2, marks=pytest.mark.service),
])
def test_values(n, monkeypatch):
    stub = types.ModuleType('sentence_transformers')
    monkeypatch.setitem(sys.modules, 'sentence_transformers', stub)
    assert n in (1, 2)
@pytest.mark.native
@pytest.mark.requires_native
def test_native_first_in_file():
    fake = types.ModuleType('ladybug')
    fake.__file__ = '/synthetic/real_runtime.py'
    sys.modules['ladybug'] = fake
@pytest.mark.unit
def test_unit_last_in_file():
    assert 'ladybug' not in sys.modules
""")
    result = child(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 passed" in result.stdout


def test_collection_cannot_initialize_optional_runtime(tmp_path):
    project(tmp_path, "import torch\n")
    result = child(tmp_path, "--collect-only")
    assert result.returncode != 0
    assert "Test boundary forbids real runtime import: torch" in result.stdout + result.stderr


def test_unit_fixture_cannot_initialize_runtime(tmp_path):
    project(tmp_path, """
import pytest
@pytest.fixture
def forbidden():
    import sentence_transformers
@pytest.mark.unit
def test_fixture(forbidden):
    raise AssertionError('must never execute')
""")
    result = child(tmp_path)
    assert result.returncode != 0
    assert "Test boundary forbids real runtime import" in result.stdout + result.stderr


def test_skipped_mandatory_case_is_failed_gate_not_passed(tmp_path):
    project(tmp_path, """
import pytest
@pytest.mark.service
def test_skipped():
    pytest.skip('missing required setup')
@pytest.fixture
def incomplete():
    yield
    pytest.exit('missing teardown outcome', returncode=0)
@pytest.mark.service
def test_incomplete(incomplete):
    pass
""")
    plan = tmp_path / "plan.json"
    report = tmp_path / "report.json"
    write_json(plan, {
        "tests/test_sample.py::test_skipped": "service",
        "tests/test_sample.py::test_incomplete": "service",
    })
    result = child(tmp_path, "--pr-gate", "--pr-plan", str(plan), "--gate-report", str(report))
    assert result.returncode != 0
    value = json.loads(report.read_text())
    assert value["status"] == "failed"
    assert any(phase["outcome"] == "skipped" for phase in value["phases"])
    assert [
        phase["phase"] for phase in value["phases"]
        if phase["nodeid"].endswith("::test_incomplete")
    ] == ["setup", "call"]


@pytest.mark.parametrize("mode", ["normal", "cancel", "leak"])
def test_public_launcher_owns_and_cleans_resistant_descendants(tmp_path, mode):
    repo = tmp_path / "repo"
    scripts = repo / ".github/scripts"
    scripts.mkdir(parents=True)
    for name in ("check_pr.py", "check_pr.sh"):
        shutil.copyfile(ROOT / ".github/scripts" / name, scripts / name)
    support = repo / "tests/support"
    support.mkdir(parents=True)
    shutil.copyfile(ROOT / "tests/support/gates.py", support / "gates.py")
    python = repo / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.symlink_to(sys.executable)
    binaries = tmp_path / "bin"
    binaries.mkdir()
    fake_uv = binaries / "uv"
    fake_uv.write_text(f"#!{sys.executable}\n" + """
import hashlib, json, os, subprocess, sys, time
from pathlib import Path
mode = os.environ['SYNTHETIC_MODE']
report = Path(sys.argv[sys.argv.index('--report') + 1])
if mode != 'normal':
    child = subprocess.Popen([sys.executable, '-c',
        "import os, signal, time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path(os.environ['SYNTHETIC_PID']).write_text(str(os.getpid())); time.sleep(30)"])
    while not Path(os.environ['SYNTHETIC_PID']).exists():
        time.sleep(.01)
if mode == 'cancel':
    time.sleep(30)
report.write_text(json.dumps({
    'correctness': 'passed', 'timed_gate': 'pending', 'run_id': os.environ['KG_GATE_RUN_ID']}))
Path(os.environ['KG_GATE_RECEIPT']).write_text(json.dumps({
    'report': str(report), 'sha256': hashlib.sha256(report.read_bytes()).hexdigest()}))
""")
    fake_uv.chmod(0o755)
    report, pidfile = tmp_path / "report", tmp_path / "pid"
    process = subprocess.Popen(
        ["bash", ".github/scripts/check_pr.sh", "--area", "validation",
         "--reason", "Synthetic supervisor regression", "--report", str(report)],
        cwd=repo, env=dict(os.environ, PATH=f"{binaries}:{os.environ['PATH']}",
                          SYNTHETIC_MODE=mode, SYNTHETIC_PID=str(pidfile)),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    try:
        if mode == "cancel":
            deadline = time.monotonic() + 5
            while not pidfile.exists() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(.01)
            assert pidfile.exists(), "Synthetic descendant never started"
            process.terminate()  # Signal only the public launcher, not its group.
        stdout, stderr = process.communicate(timeout=10)
        value = json.loads(report.read_text())
        diagnostic = {
            "launcher_pid": process.pid,
            "child_pid": pidfile.read_text() if pidfile.exists() else None,
            "returncode": process.returncode, "report": value, "stdout": stdout, "stderr": stderr,
        }
        if mode == "normal":
            assert process.returncode == 0, stdout + stderr
            assert value["correctness"] == value["timed_gate"] == "passed"
        else:
            assert process.returncode != 0, diagnostic
            assert (
                value["correctness"] == "incomplete" and value["timed_gate"] != "passed"
            ), diagnostic
            with pytest.raises(ProcessLookupError):
                os.kill(int(pidfile.read_text()), 0)
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        if pidfile.exists():
            with suppress(ProcessLookupError):
                os.kill(int(pidfile.read_text()), signal.SIGKILL)
