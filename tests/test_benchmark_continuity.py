from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from support.modules import module

from kg.config import load_manifest
from kg.db import Database
from kg.ingest import IngestService
from kg.retrieval import RetrievalService


@pytest.fixture
def lexical_manifest(tmp_path: Path) -> Path:
    (tmp_path / "note.md").write_text("# Atlas\n\narchive signing certificate\n")
    path = tmp_path / "corpus.json"
    path.write_text(json.dumps({
        "corpus_id": "continuity", "display_name": "Continuity", "vault_root": ".",
        "database": "index.sqlite3", "include": ["*.md"],
    }))
    manifest = load_manifest(path)
    IngestService(Database(manifest.database)).ingest(manifest)
    return path


@pytest.mark.service
@pytest.mark.parametrize("strategy", ["strict", "natural"])
def test_private_worker_uses_actual_lexical_backend_without_models(
    lexical_manifest: Path, monkeypatch: pytest.MonkeyPatch, strategy: str,
) -> None:
    worker = module("benchmarks/_lexical_search.py")

    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Historical lexical search must not initialize semantic models")

    monkeypatch.setattr("kg.retrieval.dense.SentenceTransformerEmbeddingProvider", forbidden)
    monkeypatch.setattr("kg.retrieval.rerank.SentenceTransformerCrossEncoderProvider", forbidden)
    results = worker.evaluate([
        "certificate absentword", "--manifest", str(lexical_manifest), "--query-mode", strategy,
    ])
    assert bool(results) is (strategy == "natural")
    manifest = load_manifest(lexical_manifest)
    expected = RetrievalService(Database(manifest.database), manifest.corpus_id).search(
        "certificate absentword", query_mode=strategy,
    )
    assert results == [r.model_dump(mode="json") for r in expected]


@pytest.mark.functional
def test_private_worker_preserves_quote_opt_in_and_errors(lexical_manifest: Path) -> None:
    worker = module("benchmarks/_lexical_search.py")
    args = ["certificate", "--manifest", str(lexical_manifest), "--query-mode", "strict"]
    report = worker.evaluate([*args, "--explain"])
    assert report["report_version"] == "1" and report["hits"]
    assert all("quote" not in hit for hit in report["hits"])
    quoted = worker.evaluate([*args, "--explain", "--include-quotes"])
    assert quoted["hits"][0]["quote"] == "archive signing certificate"
    for extra in (["--include-quotes"], ["--contextual"], ["--limit", "0"],
                  ["--since", "bad"], ["--query-mode", "reranked"]):
        process = subprocess.run(
            [sys.executable, "benchmarks/_lexical_search.py", *args, *extra],
            capture_output=True, text=True, check=False,
        )
        assert process.returncode != 0 and not process.stdout
        assert json.loads(process.stderr)["error"] == "invalid_query"


@pytest.mark.functional
@pytest.mark.parametrize("arm", ["index", "kg"])
@pytest.mark.parametrize("mode_args,has_hits", [
    ([], False), (["--query-mode", "strict"], False), (["--query-mode=natural"], True),
])
def test_work_memory_pins_actual_backend_and_retains_journal(
    tmp_path: Path, arm: str, mode_args: list[str], has_hits: bool,
) -> None:
    evaluator = module("benchmarks/work_memory/evaluate.py")
    run = tmp_path / "run"
    evaluator.prepare(run)
    result, success = evaluator.tool(run, arm, ["search", "certificate absentword", *mode_args])
    assert success and bool(result) is has_hits
    event = json.loads((run / f"tools-{arm}.jsonl").read_text().splitlines()[-1])
    assert event["execution"]["backend"] == "legacy_lexical"
    assert event["execution"]["argv"][1].endswith("/benchmarks/_lexical_search.py")
    assert event["execution"]["exit_code"] == 0
    assert any("--query-mode" in arg for arg in event["execution"]["argv"])
    assert not list(run.glob("*.dense*.sqlite3"))


@pytest.mark.service
def test_agent_search_is_natural_not_public_default(lexical_manifest: Path) -> None:
    evaluator = module("benchmarks/agent/evaluate.py")
    passed, details = evaluator._evaluate_task({
        "workflow": "search", "query": "certificate absentword", "subject": "Atlas",
        "expected_quotes": ["archive signing certificate"],
    }, lexical_manifest, lexical_manifest.parent)
    assert passed
    assert details["quotes"] == ["archive signing certificate"]


@pytest.mark.service
@pytest.mark.parametrize("strategy", ["strict", "natural", "dense", "hybrid", "reranked"])
def test_qasper_keeps_explicit_component_selection(
    lexical_manifest: Path, monkeypatch: pytest.MonkeyPatch, strategy: str,
) -> None:
    evaluator = module("benchmarks/qasper/evaluate.py")
    calls: list[tuple[str, dict[str, Any]]] = []

    def service(name: str) -> type:
        class Component:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                calls.append((name, kwargs))

            def warmup(self) -> None:
                calls.append(("warmup", {}))

            def search(self, query: str, **kwargs: Any) -> list[Any]:
                calls.append((f"{name}.search", kwargs))
                return []
        return Component

    for name in ("RetrievalService", "DenseRetrievalService", "HybridRetrievalService",
                 "RerankedRetrievalService"):
        monkeypatch.setattr(evaluator, name, service(name))
    gold = lexical_manifest.parent / "gold.json"
    gold.write_text(json.dumps({
        "dataset": "controlled", "dataset_version": "1", "split": "test", "papers": 1,
        "questions": [{
        "question_id": "q", "paper_id": "p", "paper_title": "Atlas",
        "question": "certificate", "annotations": [{"evidence": ["certificate"]}],
    }]}))
    evaluator.evaluate(lexical_manifest, gold, strategy=strategy)
    backend = {
        "strict": "RetrievalService", "natural": "RetrievalService",
        "dense": "DenseRetrievalService", "hybrid": "HybridRetrievalService",
        "reranked": "RerankedRetrievalService",
    }[strategy]
    searches = [(name, options) for name, options in calls if name.endswith(".search")]
    assert len(searches) == 1 and searches[0][0] == f"{backend}.search"
    assert searches[0][1]["subject"] == "Atlas"
    if strategy in {"strict", "natural"}:
        assert searches[0][1]["query_mode"] == strategy
        assert calls[0] == ("RetrievalService", {})
        assert not any(name == "warmup" for name, _ in calls)
