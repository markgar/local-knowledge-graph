"""Reproducible controlled-provider Q1 composition measurements, not model quality."""

import argparse
import hashlib
import json
import math
import multiprocessing
import os
import platform
import resource
import shutil
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from threading import Event, Thread

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "tests"))

from support.evidence import environment  # noqa: E402
from support.index_search import prepared  # noqa: E402
from support.query_knowledge import plan, produce, setup  # noqa: E402
from support.query_search import controlled_worker, heartbeat_action, request  # noqa: E402

from kg.models.foundation import AggregateResult, RankedResult  # noqa: E402
from kg.models.indexing import DEFAULT_CONFIGURATION  # noqa: E402
from kg.models.query import SupportInspectionRequest  # noqa: E402
from kg.query import QueryService, _worker  # noqa: E402

TEXT = "alpha\r\nCafe\u0301 \U0001f680"


def command(*args):
    return subprocess.check_output(args, text=True).strip()


class Memory:
    def __init__(self):
        self.stop = Event()
        self.samples = []
        self.errors = []
        self.thread = Thread(target=self.sample)

    def sample(self):
        while not self.stop.is_set():
            pids = [os.getpid(), *(p.pid for p in multiprocessing.active_children() if p.pid)]
            try:
                values = command("ps", "-o", "rss=", "-p", ",".join(map(str, pids)))
                self.samples.append(sum(int(v) * 1024 for v in values.split()))
            except (OSError, subprocess.CalledProcessError, ValueError) as error:
                self.errors.append(type(error).__name__)
            self.stop.wait(0.05)

    def finish(self):
        self.stop.set()
        self.thread.join()
        scale = 1 if sys.platform == "darwin" else 1024
        parent = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss * scale
        child = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss * scale
        return {
            "sampling_interval_ms": 50, "samples": len(self.samples),
            "sampled_parent_plus_live_children_peak_bytes": max(self.samples, default=0),
            "parent_os_high_water_bytes": parent,
            "reaped_child_os_high_water_bytes": child,
            "conservative_peak_bytes": max(max(self.samples, default=0), parent + child),
            "high_water_note": "Parent + max reaped child is conservative, not simultaneous RSS.",
            "scope": "Whole harness including real setup/index/count production; no model weights.",
            "sampling_errors": self.errors,
        }


def measured(service, value, scenario):
    start = time.perf_counter()
    outcome = service.execute(value)
    result = outcome.result
    data = result.data
    return outcome, {
        "scenario": scenario, "latency_ms": (time.perf_counter() - start) * 1000,
        "outcome": result.outcome, "stop_reason": outcome.stop_reason,
        "work_accounting": outcome.work_accounting,
        "semantic_reservations": result.records_examined if data is not None else None,
        "operations": result.operations_executed if data is not None else None,
        "hits": len(data.hits) if isinstance(data, RankedResult) else None,
        "count": data.count if isinstance(data, AggregateResult) else None,
        "exact": data.exact if isinstance(data, AggregateResult) else None,
        "partial": result.outcome == "partial",
        "timeout": outcome.stop_reason == "time_budget",
        "invalidated": result.outcome == "state_changed",
    }


def disk(directory):
    return {
        path.name: path.stat().st_size
        for path in sorted(directory.iterdir())
        if path.is_file() and (path.name.endswith(".db") or path.name.endswith(("-wal", "-shm")))
    }


def run(directory, samples):
    if directory.exists():
        raise SystemExit("Use a fresh measurement directory.")
    directory.mkdir(parents=True)
    memory = Memory()
    memory.thread.start()
    rows = []
    original = _worker.run
    try:
        env = environment(directory / "search.db")
        _, _, provider, reranker, value, saved = prepared(env, text=TEXT)
        from kg.indexing._configuration import execution_identity

        observed_embedding = execution_identity(DEFAULT_CONFIGURATION, provider).model_dump()
        observed_reranker = {
            "name": reranker.name, "revision": reranker.revision,
            "pipeline": reranker.pipeline_version,
        }
        heartbeat = heartbeat_action(env, value, saved)
        _worker.run = controlled_worker
        with QueryService(env.database, env.service.identity) as service:
            for _ in range(samples):
                outcome, row = measured(service, request(env, records=5), "ranked_success")
                row["exact_quote"] = (
                    isinstance(outcome.result.data, RankedResult)
                    and len(outcome.result.data.hits) == 1
                    and env.service.evidence(
                        env.scope, outcome.result.data.hits[0].evidence,
                    ).quote == TEXT
                )
                rows.append(row)
            for limit in range(1, 5):
                _, row = measured(service, request(env, records=limit), f"public_limit_{limit}")
                rows.append(row)
            original_run = service._run_worker

            def invalidate(*args):
                elapsed = original_run(*args)
                heartbeat()
                return elapsed

            service._run_worker = invalidate
            _, row = measured(service, request(env), "heartbeat_invalidation")
            rows.append(row)
            service._run_worker = original_run
            context = multiprocessing.get_context("spawn")
            ready, resume = context.Event(), context.Event()
            _worker.run = partial(
                controlled_worker, mode="block_model", ready=ready, resume=resume,
            )
            with ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(
                    measured, service, request(env, milliseconds=3000), "injected_model_timeout",
                )
                reached = ready.wait(10)
                _, row = result.result(timeout=10)
                row["model_barrier_reached"] = reached
                rows.append(row)
            _worker.run = controlled_worker
        for count in (25, 1001):
            env = setup(directory / f"count-{count}.db")
            subject, expected = produce(env, count)
            with QueryService(env.database, env.service.identity) as service:
                value = plan(env, subject)
                outcome, row = measured(service, value, f"decision_count_{count}")
                observed = set()
                start = time.perf_counter()
                pages = []
                if outcome.result.result_set_id is not None:
                    ordinal = 0
                    while True:
                        page = service.inspect_support(SupportInspectionRequest(
                            request_id=f"inspect-{ordinal}", scope=env.scope,
                            result_set_id=outcome.result.result_set_id,
                            records_step_id="decisions", start_ordinal=ordinal, budget=value.budget,
                        ))
                        pages.append({
                            "outcome": page.outcome, "stop_reason": page.stop_reason,
                            "members": len(page.records) if page.error is None else None,
                        })
                        if page.error is not None:
                            break
                        observed.update(record.record_id for record in page.records)
                        if page.next_ordinal is None:
                            break
                        ordinal = page.next_ordinal
                row.update({
                    "inspection_latency_ms": (time.perf_counter() - start) * 1000,
                    "inspection_pages": pages, "inspected_members": len(observed),
                    "membership_matches_producer_ids": observed == expected,
                })
                rows.append(row)
        before_checkpoint = disk(directory)
        for path in directory.glob("*.db"):
            with sqlite3.connect(path) as connection:
                connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        after_checkpoint = disk(directory)
    finally:
        _worker.run = original
        memory_result = memory.finish()
    groups = {}
    for scenario in sorted({row["scenario"] for row in rows}):
        subset = [row for row in rows if row["scenario"] == scenario]
        latencies = sorted(row["latency_ms"] for row in subset)
        groups[scenario] = {
            "samples": len(subset), "p95_ms": latencies[math.ceil(len(subset) * 0.95) - 1],
            "outcomes": dict(Counter(row["outcome"] for row in subset)),
            **{f"{flag}_rate": sum(row[flag] for row in subset) / len(subset)
               for flag in ("timeout", "partial", "invalidated")},
        }
    sources = sorted({
        *ROOT.glob("src/kg/query/*.py"), *ROOT.glob("src/kg/indexing/*.py"),
        ROOT / "src/kg/models/query.py", ROOT / "tests/support/query_search.py",
        ROOT / "tests/support/index_search.py", ROOT / "tests/support/indexing.py",
        ROOT / "tests/support/query_knowledge.py", Path(__file__).resolve(),
    })
    return {
        "record_version": "q1-controlled-composition/1",
        "recorded_at_utc": datetime.now(UTC).isoformat(),
        "git_head": command("git", "rev-parse", "HEAD"),
        "git_worktree_dirty": bool(command("git", "status", "--porcelain")),
        "source_sha256": {
            str(path.relative_to(ROOT)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sources
        },
        "environment": {
            "os": platform.platform(), "machine": platform.machine(),
            "cpu": command("sysctl", "-n", "machdep.cpu.brand_string")
            if sys.platform == "darwin" else platform.processor(),
            "logical_cpus": os.cpu_count(),
            "ram_bytes": int(command("sysctl", "-n", "hw.memsize"))
            if sys.platform == "darwin" else (
                os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
            ),
            "python": platform.python_version(), "sqlite": sqlite3.sqlite_version,
            "volume_capacity_bytes": shutil.disk_usage(directory).total,
        },
        "workload": {
            "label": "one-passage-composed-search-and-25-1001-decisions/1",
            "text_utf8_sha256": hashlib.sha256(TEXT.encode()).hexdigest(),
            "text_bytes": len(TEXT.encode()), "search_documents": 1, "search_passages": 1,
            "foundation_1000_document_workload_executed": False,
        },
        "providers": {
            "embedding_profile": "gte-modernbert", "contextual": False,
            "runtime": "controlled-test", "real_models_loaded": False,
            "worker_status": "fresh spawned process/providers for every query",
            "warm_model_measurement": False, "filesystem_cache_state": "not controlled",
            "configuration": DEFAULT_CONFIGURATION.model_dump(),
            "observed_embedding": observed_embedding, "observed_reranker": observed_reranker,
        },
        "memory": memory_result,
        "disk": {
            "before_checkpoint": before_checkpoint, "after_checkpoint": after_checkpoint,
            "layout": "Canonical and vector rows share SQLite files; no invented per-kind split.",
            "model_cache_bytes_created": 0,
        },
        "samples": rows, "by_scenario": groups,
        "gates": {
            "real_model_quality_and_cold_warm_targets": "not evaluated; #73",
            "e4_checkpoint_invalidation": "not available; #72",
            "v1_reference_workload_targets": "not evaluated by this smaller controlled workload",
        },
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=20)
    args = parser.parse_args()
    if not 1 <= args.samples <= 1000:
        parser.error("--samples must be in 1..1000")
    print(json.dumps(run(args.directory, args.samples), indent=2, sort_keys=True))
