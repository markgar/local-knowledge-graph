"""Run portable PR checks under the public launcher's timed process supervisor."""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.metadata
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import time
import tomllib
import uuid
from contextlib import suppress
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from support.gates import (  # noqa: E402
    AREAS,
    LAYERS,
    RELEASE_OBLIGATIONS,
    plan,
    write_json,
)

MOVED = {
    f"tests/{name}.py": f"tests/unit/{name}.py"
    for name in (
        "test_foundation_contracts", "test_execution_models", "test_knowledge_contracts",
        "test_manifest", "test_markdown_parser", "test_query_meter",
    )
}
NEW_TOOLING = {
    ".github/scripts/check_pr.py", ".github/scripts/check_pr.sh", "tests/conftest.py",
    "tests/support/gates.py", "tests/test_test_gates.py", "tests/unit/test_gate_values.py",
}
SOURCE_AREAS = {
    "_execution_budget.py": {
        "evidence", "schema", "knowledge", "indexing", "query", "processing", "graph", "cli",
    },
    "evidence": {"evidence"},
    "knowledge": {"knowledge", "schema"},
    "indexing": {"indexing"},
    "query": {"query"},
    "processing": {"processing"},
    "graph": {"graph"},
    "client": {"cli"},
    "diagnostics": {"evidence", "query", "graph"},
    "ingest": {"demo"},
    "markdown": {"demo"},
    "retrieval": {"demo"},
}
MODEL_AREAS = {
    "foundation": {"evidence", "knowledge", "query"},
    "evidence": {"evidence"},
    "knowledge": {"knowledge", "schema"},
    "schema": {"schema"},
    "graph": {"graph"},
    "query": {"query"},
    "processing": {"processing"},
    "execution": {"evidence", "query", "graph"},
    "contracts": {"demo"},
    "manifest": {"demo"},
    "indexing": {"indexing"},
    "indexing_events": {"indexing"},
}
BUILD_FILES = {
    "uv.lock", "uv.toml", ".python-version", "MANIFEST.in", "setup.py", "setup.cfg",
    "requirements.txt",
}


def git(*args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=ROOT, stderr=subprocess.PIPE)


def before(base: str, path: str) -> str | None:
    result = subprocess.run(
        ["git", "show", f"{base}:{path}"], cwd=ROOT, capture_output=True, check=False
    )
    if result.returncode == 0:
        return result.stdout.decode()
    if subprocess.run(
        ["git", "cat-file", "-e", f"{base}^{{commit}}"], cwd=ROOT,
        capture_output=True, check=False,
    ).returncode:
        raise ValueError(f"Cannot read baseline commit: {base}")
    return None


def marker(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Attribute)
        and isinstance(node.value.value, ast.Name)
        and node.value.value.id == "pytest" and node.value.attr == "mark"
        and node.attr in LAYERS | {"requires_native"}
    )


class MetadataOnly(ast.NodeTransformer):
    def visit_FunctionDef(self, node):
        node.decorator_list = [d for d in node.decorator_list if not marker(d)]
        return self.generic_visit(node)

    def visit_Import(self, node):
        node.names = [name for name in node.names if name.name != "pytest"]
        return node if node.names else None

    def visit_Call(self, node):
        node = self.generic_visit(node)
        if (
            isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "pytest" and node.func.attr == "param"
            and len(node.args) == 1 and len(node.keywords) == 1
            and node.keywords[0].arg == "marks" and marker(node.keywords[0].value)
        ):
            return node.args[0]
        return node


def metadata_only(old: str | None, new: str, *, foundation_move: bool = False) -> bool:
    if old is None:
        return False
    if foundation_move:
        new = new.replace(
            'Path(__file__).resolve().parents[2] / "corpora" / "foundation"',
            'Path(__file__).resolve().parents[1] / "corpora" / "foundation"',
        )
    return ast.dump(MetadataOnly().visit(ast.parse(old))) == ast.dump(
        MetadataOnly().visit(ast.parse(new))
    )


def native_boundary_only(old: str | None, new: str) -> bool:
    new = new.replace("from support.gates import NATIVE_CONTEXT\n", "")
    new = new.replace(
        '    if os.environ.get(NATIVE_CONTEXT) != "1":\n'
        '        pytest.fail('
        '"Native test must declare requires_native before probing the runtime")\n',
        "",
    )
    return metadata_only(old, new)


def source_areas(path: str) -> set[str]:
    parts = Path(path).parts
    if parts[:2] != ("src", "kg"):
        return set()
    name = parts[2]
    if name == "models":
        key = Path(path).stem
        if key not in MODEL_AREAS:
            raise ValueError(f"New/shared model needs reviewed impact policy: {path}")
        return MODEL_AREAS[key]
    if name in SOURCE_AREAS:
        result = SOURCE_AREAS[name].copy()
        if path == "src/kg/evidence/schema.sql":
            result |= {"schema", "knowledge", "indexing", "query", "processing", "graph"}
        return result
    if name in {"cli.py", "__main__.py"}:
        return {"cli"}
    if name in {
        "legacy_cli.py", "config.py", "db.py", "aliases.py", "ids.py", "lexical.py",
        "record_state.py", "schema.sql",
    }:
        return {"demo"}
    raise ValueError(f"Shared/new runtime surface needs reviewed impact policy: {path}")


def changed_scope(base_ref: str, areas: set[str]) -> dict:
    base = git("merge-base", "HEAD", base_ref).decode().strip()
    changed = {
        path.decode()
        for path in git("diff", "--name-only", "--no-renames", "-z", base).split(b"\0")
        if path
    }
    changed |= {
        path.decode()
        for path in git("ls-files", "--others", "--exclude-standard", "-z").split(b"\0")
        if path
    }
    rows = []
    for name in sorted(changed):
        required: set[str] = set()
        reason = "documentation"
        path = ROOT / name
        if name in BUILD_FILES:
            raise ValueError(f"Dependency/build/runtime-version scope needs policy review: {name}")
        if name == "pyproject.toml":
            original = before(base, name)
            if original is None:
                raise ValueError("Missing baseline pyproject.toml")
            old, new = tomllib.loads(original), tomllib.loads(path.read_text())
            for value in (old, new):
                value.get("tool", {}).get("pytest", {}).pop("ini_options", None)
            if old != new:
                raise ValueError("Non-pytest pyproject change needs dependency/build policy review")
            required, reason = {"validation"}, "pytest-only organization"
        elif name in NEW_TOOLING or name in {
            ".github/workflows/ci.yml", ".github/scripts/ci_scope.py",
        }:
            required, reason = {"validation"}, "validation tooling"
        elif name == "tests/support/graph.py":
            boundary_only = native_boundary_only(before(base, name), path.read_text())
            required = {"validation"} if boundary_only else {"validation", "graph"}
            reason = "native test-support boundary" if boundary_only else "changed graph fixture"
        elif name.startswith("tests/"):
            old_name = next((old for old, new in MOVED.items() if new == name), name)
            current = ROOT / MOVED.get(name, name)
            old = before(base, old_name)
            if not current.is_file():
                raise ValueError(f"Deleted test requires an explicit reviewed policy: {name}")
            new = current.read_text()
            if name.endswith(".py") and metadata_only(
                old, new, foundation_move=old_name == "tests/test_foundation_contracts.py"
            ):
                required, reason = {"validation"}, "test metadata/move only"
            elif name == "tests/test_ci_scope.py":
                required, reason = {"validation"}, "full-caller regression"
            else:
                required, reason = {"validation"}, "behavior tests: reviewed product area required"
                if not areas - {"validation"}:
                    raise ValueError(f"Test behavior/helper change needs a product area: {name}")
        elif name.startswith("src/"):
            required, reason = source_areas(name), "runtime ownership minimum; review dependencies"
        elif name.startswith("corpora/") and name != "corpora/foundation/README.md":
            required, reason = {"evaluation"}, "authored corpus input, including Markdown"
        elif name.startswith("benchmarks/") and path.suffix != ".md":
            required, reason = {"evaluation"}, "authored fixture/evaluation behavior"
        elif name.startswith("examples/"):
            if not areas - {"validation", "evaluation"}:
                raise ValueError(f"Example change needs reviewed product area: {name}")
            reason = "example: declared product impact"
        elif path.suffix == ".md" or name in {".gitignore", "LICENSE"}:
            pass
        else:
            raise ValueError(f"Unsupported changed surface needs policy extension: {name}")
        missing = required - areas
        if missing:
            raise ValueError(
                f"{name} requires areas {sorted(missing)} in addition to impact review"
            )
        rows.append({"path": name, "minimum_areas": sorted(required), "reason": reason})
    return {
        "base": base, "head": git("rev-parse", "HEAD").decode().strip(),
        "dirty": bool(git("status", "--porcelain")),
        "changed_paths": rows,
    }


def build_preparation(path: Path) -> dict:
    from packaging.requirements import InvalidRequirement, Requirement
    from packaging.version import InvalidVersion, Version

    build = tomllib.loads(path.read_text()).get("build-system", {})
    result = {
        "mode": "prepared-no-isolation", "interpreter": sys.executable,
        "backend": build.get("build-backend"), "requirements": [], "errors": [],
        "status": "ready",
    }
    if result["backend"] != "setuptools.build_meta" or "backend-path" in build:
        result["errors"].append("Unsupported build backend/path; reviewed policy required")
    requirements = build.get("requires")
    if not isinstance(requirements, list) or not requirements:
        result["errors"].append("build-system.requires must be a nonempty requirement list")
    else:
        for text in requirements:
            row = {"requirement": text, "status": "failed"}
            result["requirements"].append(row)
            try:
                if not isinstance(text, str):
                    raise ValueError("Build requirements must be strings")
                requirement = Requirement(text)
                if requirement.url or requirement.extras:
                    raise ValueError("URL/extra build requirements need reviewed policy")
                row["name"] = requirement.name
                if requirement.marker and not requirement.marker.evaluate():
                    row["status"] = "not_applicable"
                    continue
                installed = importlib.metadata.version(requirement.name)
                row["version"] = installed
                if not installed or not requirement.specifier.contains(
                    Version(installed), prereleases=True,
                ):
                    raise ValueError(f"Installed {installed!r} does not satisfy {text}")
                row["status"] = "satisfied"
            except (
                InvalidRequirement, InvalidVersion, ValueError,
                importlib.metadata.PackageNotFoundError,
            ) as error:
                row["error"] = f"{type(error).__name__}: {error}"
                result["errors"].append(f"{text}: {row['error']}")
    if result["errors"]:
        result["status"] = "failed"
    return result


def run_child(command: list[str], env: dict[str, str]) -> int:
    # All checks remain in the launcher's one owned group, including grandchildren.
    process = subprocess.Popen(command, cwd=ROOT, env=env)
    try:
        return process.wait()
    except BaseException:
        with suppress(ProcessLookupError):
            process.terminate()
        raise


def finalize_record(record: dict, elapsed: float, failure: str | None = None) -> int:
    report_path = Path(record["report"])
    payload = report_path.read_bytes()
    if hashlib.sha256(payload).hexdigest() != record["sha256"]:
        raise ValueError("Gate report changed before timing finalization")
    report = json.loads(payload)
    if report["timed_gate"] != "pending":
        raise ValueError("Report already finalized")
    if not math.isfinite(elapsed) or elapsed < 0:
        raise ValueError("Missing/invalid outer elapsed time")
    if failure:
        if report["correctness"] == "passed":
            report["correctness"] = "incomplete"
        report["supervisor_error"] = failure
    passed = report["correctness"] == "passed" and elapsed <= 119
    report.update(
        outer_uv_seconds=elapsed, outer_uv_limit_seconds=119,
        reporting_allowance_seconds=1, public_call_target_seconds=120,
        timed_gate="passed" if passed else "failed",
    )
    write_json(report_path, report, replace=True)
    print(f"PR gate {report['timed_gate']}: outer uv {elapsed:.3f}s (limit 119s + reporting)")
    return 0 if passed else 1


def finalize(timing: Path, receipt: Path) -> int:
    return finalize_record(json.loads(receipt.read_text()), float(timing.read_text().strip()))


def group_alive(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return False
    return True


def finish_group(process: subprocess.Popen, *, cancelled: bool) -> bool:
    """Return whether cleanup had to terminate lingering owned processes."""
    alive = group_alive(process.pid)
    if alive:
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        until = time.monotonic() + 2
        while time.monotonic() < until:
            process.poll()  # Reap the leader, independently of descendant liveness.
            if not group_alive(process.pid):
                break
            time.sleep(0.02)
        if group_alive(process.pid):
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
    process.wait()
    until = time.monotonic() + 2
    while group_alive(process.pid) and time.monotonic() < until:
        time.sleep(0.02)
    if group_alive(process.pid):
        raise OSError("Owned process group did not disappear after shutdown")
    return alive or cancelled


def interrupted(signum, frame):
    raise KeyboardInterrupt


def mark_interrupted(path: Path, run_id: str, message: str) -> None:
    if path.is_file():
        report = json.loads(path.read_text())
        if report.get("run_id") != run_id:
            raise ValueError("Refusing to overwrite a different gate's report")
        report.update(correctness="incomplete", timed_gate="incomplete", error=message)
        write_json(path, report, replace=True)
    else:
        write_json(path, {
            "version": "pr-gate/1", "run_id": run_id, "correctness": "incomplete",
            "timed_gate": "incomplete", "error": message, "commands": [],
            "release_obligations": RELEASE_OBLIGATIONS,
        })


def launch(arguments: list[str], report_path: Path) -> int:
    """Supervise one owned group; time uv startup through all check/group cleanup."""
    run_id = uuid.uuid4().hex
    process = None
    record = None
    cancelled = False
    cleanup_needed = False
    status = 1
    signals = {signal.SIGINT, signal.SIGTERM}
    previous = {sig: signal.signal(sig, interrupted) for sig in signals}

    def note_cancellation(signum, frame):
        nonlocal cancelled
        cancelled = True

    def cleanup_after_failure() -> str:
        if process is not None:
            try:
                finish_group(process, cancelled=True)
            except OSError as cleanup_error:
                return f"; owned-group cleanup also failed: {cleanup_error}"
        return ""

    clock = time.monotonic()
    try:
        with tempfile.TemporaryDirectory(prefix="kg-pr-clock-") as temporary:
            receipt = Path(temporary) / "receipt"
            env = dict(
                os.environ, KG_GATE_RECEIPT=str(receipt), KG_GATE_RUN_ID=run_id,
                UV_OFFLINE="1", HF_HUB_OFFLINE="1", UV_PYTHON_DOWNLOADS="never",
            )
            mask = signal.pthread_sigmask(signal.SIG_BLOCK, signals)
            try:
                process = subprocess.Popen(
                    ["uv", "run", "--no-sync", "python", ".github/scripts/check_pr.py", *arguments],
                    cwd=ROOT, env=env, start_new_session=True,
                    preexec_fn=lambda: signal.pthread_sigmask(signal.SIG_SETMASK, mask),
                )
            finally:
                signal.pthread_sigmask(signal.SIG_SETMASK, mask)
            try:
                status = process.wait()
            except KeyboardInterrupt:
                cancelled = True
            finally:
                # Repeated cancellation cannot interrupt bounded group shutdown.
                for sig in signals:
                    signal.signal(sig, note_cancellation)
                cleanup_needed = finish_group(process, cancelled=cancelled)
            if receipt.is_file():
                record = json.loads(receipt.read_text())
        elapsed = time.monotonic() - clock
        for sig in signals:
            signal.signal(sig, interrupted)
        if cancelled or record is None:
            mark_interrupted(report_path, run_id, "Interrupted or missing runner receipt")
            return 130 if cancelled else 1
        failure = (
            "Outer invocation failed or owned child cleanup was required"
            if status != 0 or cleanup_needed else None
        )
        return finalize_record(record, elapsed, failure)
    except KeyboardInterrupt:
        for sig in signals:
            signal.signal(sig, signal.SIG_IGN)
        message = "Interrupted during startup or finalization" + cleanup_after_failure()
        print(f"Gate incomplete: {message}", file=sys.stderr)
        mark_interrupted(report_path, run_id, message)
        return 130
    except (OSError, ValueError) as error:
        for sig in signals:
            signal.signal(sig, signal.SIG_IGN)
        message = (
            ("Interrupted; " if cancelled else "")
            + f"Supervisor failure: {error}" + cleanup_after_failure()
        )
        print(f"Gate incomplete: {message}", file=sys.stderr)
        mark_interrupted(report_path, run_id, message)
        return 1
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


def main() -> int:
    started = time.monotonic()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--area", action="append", choices=sorted(AREAS))
    parser.add_argument("--case", action="append", default=[])
    parser.add_argument("--reason")
    parser.add_argument("--report", type=Path)
    parser.add_argument("--base", default="origin/main")
    parser.add_argument("--finalize", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--launch", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.finalize:
        if not args.receipt:
            parser.error("--finalize requires --receipt")
        return finalize(args.finalize, args.receipt)
    if Path.cwd().resolve() != ROOT:
        parser.error("Run the gate from the repository root")
    if not args.area or not args.reason or not args.reason.strip() or args.report is None:
        parser.error("--area, nonblank --reason and fresh external --report are required")
    report_path = args.report.resolve()
    if report_path.is_relative_to(ROOT):
        parser.error("Report must be outside the repository")
    if report_path.exists():
        parser.error("Report already exists; never overwrite validation evidence")
    if args.launch:
        return launch([arg for arg in sys.argv[1:] if arg != "--launch"], report_path)
    receipt = os.environ.get("KG_GATE_RECEIPT")
    if not receipt:
        parser.error("Use bash .github/scripts/check_pr.sh; inner runner cannot certify total time")
    report = {
        "version": "pr-gate/1", "areas": args.area, "reason": args.reason,
        "correctness": "incomplete", "timed_gate": "pending",
        "commands": [], "release_obligations": RELEASE_OBLIGATIONS,
        "python": sys.version,
        "run_id": os.environ.get("KG_GATE_RUN_ID"),
    }
    code = 1
    try:
        roots, expected = plan(args.area, args.case)
        report["scope"] = changed_scope(args.base, set(args.area))
        report["selectors"] = roots
        env = dict(os.environ, UV_OFFLINE="1", HF_HUB_OFFLINE="1", UV_PYTHON_DOWNLOADS="never")
        # A local pytest default must not silently deselect mandatory gate cases.
        env.pop("PYTEST_ADDOPTS", None)
        cache_options = []
        if cache_root := env.get("KG_GATE_CACHE_DIR"):
            cache = Path(cache_root).resolve()
            cache.mkdir(parents=True, exist_ok=True)
            cache_options = ["-o", f"cache_dir={cache / 'pytest'}"]
            env["MYPY_CACHE_DIR"] = str(cache / "mypy")
            env["RUFF_CACHE_DIR"] = str(cache / "ruff")
        with tempfile.TemporaryDirectory(prefix="kg-pr-") as temporary:
            directory = Path(temporary)
            expected_path = directory / "plan.json"
            pytest_report = directory / "pytest.json"
            write_json(expected_path, expected)
            commands = [
                [sys.executable, "-m", "pytest", *roots, *cache_options,
                 "-o", "addopts=-q --strict-markers",
                 "--pr-gate",
                 "--basetemp", str(directory / "pytest-tmp"),
                 "--pr-plan", str(expected_path), "--gate-report", str(pytest_report),
                 "--durations=20", "--tb=short"],
                [sys.executable, "-m", "ruff", "check", "."],
                [sys.executable, "-m", "mypy"],
                ["uv", "build", "--offline", "--no-build-isolation",
                 "--python", sys.executable, "--out-dir", str(directory / "dist")],
            ]
            report["commands"] = [
                {"argv": command, "status": "not_run"} for command in commands
            ]
            report["build"] = build_preparation(ROOT / "pyproject.toml")
            if report["build"]["status"] != "ready":
              raise ValueError(f"Build preparation failed: {report['build']['errors']}")
            for index, command in enumerate(commands):
                entry = report["commands"][index]
                entry["status"] = "incomplete"
                clock = time.monotonic()
                status = run_child(command, env)
                entry.update(
                    elapsed_seconds=time.monotonic() - clock,
                    exit_code=status, status="passed" if status == 0 else "failed",
                )
                if index == 0:
                    if not pytest_report.is_file():
                        raise ValueError("Pytest did not persist mandatory case results")
                    report["pytest"] = json.loads(pytest_report.read_text())
                    if report["pytest"]["status"] != "passed":
                        status = 1
                if status:
                    report["correctness"] = "failed"
                    break
            else:
                code = 0
        if code == 0:
            report["correctness"] = "passed"
    except (ValueError, OSError, subprocess.CalledProcessError) as error:
        report["correctness"] = "incomplete"
        code = 1
        report["error"] = f"{type(error).__name__}: {error}"
        print(report["error"], file=sys.stderr)
    except KeyboardInterrupt:
        report["correctness"] = "incomplete"
        report["error"] = "Interrupted; no complete validation result"
        code = 130
    finally:
        report["inner_seconds"] = time.monotonic() - started
        write_json(report_path, report)
        write_json(Path(receipt), {
            "report": str(report_path),
            "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
        })
    return code


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
