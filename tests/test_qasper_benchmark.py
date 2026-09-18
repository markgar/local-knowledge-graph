from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

from kg.config import load_manifest
from kg.db import Database
from kg.ingest import IngestService
from kg.models.contracts import SearchResult


def _load_benchmark_module(name: str) -> ModuleType:
    path = Path(__file__).parents[1] / "benchmarks" / "qasper" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"qasper_{name}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _paper() -> dict[str, object]:
    return {
        "title": "Example Retrieval Paper",
        "abstract": "A deterministic benchmark.",
        "full_text": [
            {
                "section_name": "Method",
                "paragraphs": ["The system uses SQLite for local retrieval."],
            }
        ],
        "qas": [
            {
                "question_id": "q1",
                "question": "What does the system use for retrieval?",
                "answers": [
                    {
                        "answer": {
                            "unanswerable": False,
                            "extractive_spans": ["SQLite"],
                            "yes_no": None,
                            "free_form_answer": "",
                            "evidence": [
                                "The system uses SQLite for local retrieval."
                            ],
                        }
                    }
                ],
            }
        ],
        "figures_and_tables": [
            {
                "file": "table.png",
                "caption": "Table 1: Exact benchmark values.",
            }
        ],
    }


def test_qasper_fixture_prepares_and_evaluates_exact_evidence(tmp_path: Path) -> None:
    prepare = _load_benchmark_module("prepare")
    evaluate = _load_benchmark_module("evaluate")
    assert "FLOAT SELECTED: Table 1: Exact benchmark values." in prepare.render_paper(
        "1234.56789",
        _paper(),
    )
    counts = prepare.prepare_corpus(
        {"1234.56789": _paper()},
        ["1234.56789"],
        tmp_path,
    )
    manifest = load_manifest(tmp_path / "corpus.yml")
    IngestService(Database(manifest.database)).ingest(manifest)

    result = evaluate.evaluate(
        tmp_path / "corpus.yml",
        tmp_path / "gold.json",
        strategy="natural",
    )

    assert counts == {"papers": 1, "questions": 1}
    assert result["metrics"]["evidence_recall_at_1"] == 1.0
    assert result["metrics"]["anchor_integrity"] == 1.0


def test_qasper_fixture_evaluates_hybrid_strategy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare = _load_benchmark_module("prepare")
    evaluate = _load_benchmark_module("evaluate")
    prepare.prepare_corpus(
        {"1234.56789": _paper()},
        ["1234.56789"],
        tmp_path,
    )
    manifest = load_manifest(tmp_path / "corpus.yml")
    IngestService(Database(manifest.database)).ingest(manifest)

    class StubHybridRetrievalService:
        def __init__(self, database: Database, corpus_id: str) -> None:
            self.retrieval = evaluate.RetrievalService(database, corpus_id)

        def warmup(self) -> None:
            pass

        def search(self, query: str, **kwargs: object) -> list[SearchResult]:
            return self.retrieval.search(query, query_mode="natural", **kwargs)

    monkeypatch.setattr(
        evaluate,
        "HybridRetrievalService",
        StubHybridRetrievalService,
    )
    result = evaluate.evaluate(
        tmp_path / "corpus.yml",
        tmp_path / "gold.json",
        strategy="hybrid",
    )

    assert result["query_strategy"] == "hybrid"
    assert result["metrics"]["evidence_recall_at_1"] == 1.0
    assert result["metrics"]["anchor_integrity"] == 1.0


def test_qasper_fixture_evaluates_reranked_strategy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare = _load_benchmark_module("prepare")
    evaluate = _load_benchmark_module("evaluate")
    prepare.prepare_corpus(
        {"1234.56789": _paper()},
        ["1234.56789"],
        tmp_path,
    )
    manifest = load_manifest(tmp_path / "corpus.yml")
    IngestService(Database(manifest.database)).ingest(manifest)

    class StubRerankedRetrievalService:
        def __init__(self, database: Database, corpus_id: str) -> None:
            self.retrieval = evaluate.RetrievalService(database, corpus_id)

        def warmup(self) -> None:
            pass

        def search(self, query: str, **kwargs: object) -> list[SearchResult]:
            return self.retrieval.search(query, query_mode="natural", **kwargs)

    monkeypatch.setattr(
        evaluate,
        "RerankedRetrievalService",
        StubRerankedRetrievalService,
    )
    result = evaluate.evaluate(
        tmp_path / "corpus.yml",
        tmp_path / "gold.json",
        strategy="reranked",
    )

    assert result["query_strategy"] == "reranked"
    assert result["metrics"]["evidence_recall_at_1"] == 1.0
    assert result["metrics"]["anchor_integrity"] == 1.0


def test_qasper_fixture_refuses_unowned_markdown(tmp_path: Path) -> None:
    prepare = _load_benchmark_module("prepare")
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "personal.md").write_text("# Personal\n", encoding="utf-8")

    try:
        prepare.prepare_corpus(
            {"1234.56789": _paper()},
            ["1234.56789"],
            tmp_path,
        )
    except ValueError as exc:
        assert "Refusing to modify unowned Markdown" in str(exc)
    else:
        raise AssertionError("Expected unowned Markdown protection")
