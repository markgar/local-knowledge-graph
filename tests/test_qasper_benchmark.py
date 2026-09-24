from __future__ import annotations

import hashlib
import importlib.util
import io
from datetime import datetime
from pathlib import Path
from types import ModuleType

import pytest

from kg.config import load_manifest
from kg.db import Database
from kg.ingest import IngestService
from kg.models.contracts import SearchResult
from kg.retrieval import RetrievalService
from kg.retrieval.dense import EmbeddingProfile


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


@pytest.mark.service
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


@pytest.mark.service
@pytest.mark.parametrize("use_context", [False, True])
def test_qasper_fixture_evaluates_hybrid_strategy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_context: bool,
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
        def __init__(
            self,
            database: Database,
            corpus_id: str,
            *,
            embedding_profile: EmbeddingProfile,
            contextual: bool,
        ) -> None:
            assert embedding_profile is EmbeddingProfile.qwen3_embedding_06b
            assert contextual is use_context
            self.retrieval = RetrievalService(database, corpus_id)

        def warmup(self) -> None:
            pass

        def search(
            self,
            query: str,
            subject: str | None = None,
            limit: int = 20,
            since: datetime | None = None,
            source_path: str | None = None,
        ) -> list[SearchResult]:
            return self.retrieval.search(
                query,
                subject=subject,
                limit=limit,
                since=since,
                source_path=source_path,
                query_mode="natural",
            )

    monkeypatch.setattr(
        evaluate,
        "HybridRetrievalService",
        StubHybridRetrievalService,
    )
    result = evaluate.evaluate(
        tmp_path / "corpus.yml",
        tmp_path / "gold.json",
        strategy="hybrid",
        embedding_profile=EmbeddingProfile.qwen3_embedding_06b,
        contextual=use_context,
    )

    assert result["query_strategy"] == "hybrid"
    assert result["contextual"] is use_context
    assert result["metrics"]["evidence_recall_at_1"] == 1.0
    assert result["metrics"]["anchor_integrity"] == 1.0


@pytest.mark.service
@pytest.mark.parametrize("use_context", [False, True])
def test_qasper_fixture_evaluates_reranked_strategy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    use_context: bool,
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
        def __init__(
            self,
            database: Database,
            corpus_id: str,
            *,
            embedding_profile: EmbeddingProfile,
            contextual: bool,
        ) -> None:
            assert embedding_profile is EmbeddingProfile.qwen3_embedding_06b
            assert contextual is use_context
            self.retrieval = RetrievalService(database, corpus_id)

        def warmup(self) -> None:
            pass

        def search(
            self,
            query: str,
            subject: str | None = None,
            limit: int = 20,
            since: datetime | None = None,
            source_path: str | None = None,
        ) -> list[SearchResult]:
            return self.retrieval.search(
                query,
                subject=subject,
                limit=limit,
                since=since,
                source_path=source_path,
                query_mode="natural",
            )

    monkeypatch.setattr(
        evaluate,
        "RerankedRetrievalService",
        StubRerankedRetrievalService,
    )
    result = evaluate.evaluate(
        tmp_path / "corpus.yml",
        tmp_path / "gold.json",
        strategy="reranked",
        embedding_profile=EmbeddingProfile.qwen3_embedding_06b,
        contextual=use_context,
    )

    assert result["query_strategy"] == "reranked"
    assert result["contextual"] is use_context
    assert result["embedding_profile"] == "qwen3-embedding-0.6b"
    assert result["metrics"]["evidence_recall_at_1"] == 1.0
    assert result["metrics"]["anchor_integrity"] == 1.0
    assert result["question_results"][0]["top_score"] is not None
    assert result["question_results"][0]["recall_at_1"] == 1.0
    assert result["question_results"][0]["reciprocal_rank"] == 1.0


@pytest.mark.service
def test_qasper_e4_calibrates_paper_grouped_answerability() -> None:
    calibrate = _load_benchmark_module("calibrate")
    questions = []
    for index in range(20):
        answerable = index % 2 == 0
        questions.append(
            {
                "paper_id": f"paper-{index}",
                "question_id": f"question-{index}",
                "answerable": answerable,
                "returned": 10,
                "record_ids": [f"record-{index}"],
                "top_score": 0.9 if answerable else 0.1,
                "recall_at_1": 1.0 if answerable else 0.0,
                "recall_at_5": 1.0 if answerable else 0.0,
                "recall_at_10": 1.0 if answerable else 0.0,
                "reciprocal_rank": 1.0 if answerable else 0.0,
                "evidence_f1_at_10": 1.0 if answerable else 0.0,
            }
        )

    result = calibrate.calibrate(
        {
            "dataset": "QASPER",
            "dataset_version": "0.3",
            "query_strategy": "reranked",
            "embedding_profile": "gte-modernbert",
            "question_results": questions,
        },
        folds=2,
    )

    assert result["experiment"] == "E4"
    assert result["metrics"]["answerability_balanced_accuracy"] == 1.0
    assert result["metrics"]["answerable_coverage"] == 1.0
    assert result["metrics"]["unanswerable_false_evidence_rate"] == 0.0
    assert result["metrics"]["evidence_recall_at_10"] == 1.0
    assert {item["calibration_fold"] for item in result["question_results"]} == {
        0,
        1,
    }


@pytest.mark.service
def test_qasper_e4_requires_unmodified_e3_result_fields() -> None:
    calibrate = _load_benchmark_module("calibrate")

    with pytest.raises(ValueError, match="requires E3 reranked results"):
        calibrate.calibrate(
            {
                "query_strategy": "hybrid",
                "question_results": [{"question_id": "question"}],
            }
        )


@pytest.mark.service
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


@pytest.mark.service
def test_qasper_fixture_refuses_unexpected_markdown_on_rerun(tmp_path: Path) -> None:
    prepare = _load_benchmark_module("prepare")
    prepare.prepare_corpus(
        {"1234.56789": _paper()},
        ["1234.56789"],
        tmp_path,
    )
    (tmp_path / "vault" / "contamination.md").write_text(
        "# Unexpected\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unexpected Markdown"):
        prepare.prepare_corpus(
            {"1234.56789": _paper()},
            ["1234.56789"],
            tmp_path,
        )


@pytest.mark.service
@pytest.mark.parametrize(
    "relative_path",
    [
        Path("vault/1234-56789.md"),
        Path("corpus.yml"),
        Path("gold.json"),
        Path(".qasper-fixture.json"),
    ],
)
def test_qasper_fixture_refuses_symlinked_outputs(
    tmp_path: Path,
    relative_path: Path,
) -> None:
    prepare = _load_benchmark_module("prepare")
    prepare.prepare_corpus(
        {"1234.56789": _paper()},
        ["1234.56789"],
        tmp_path,
    )
    victim = tmp_path / "victim.txt"
    victim.write_text("unchanged", encoding="utf-8")
    output = tmp_path / relative_path
    output.unlink()
    output.symlink_to(victim)

    with pytest.raises(ValueError, match="non-regular file"):
        prepare.prepare_corpus(
            {"1234.56789": _paper()},
            ["1234.56789"],
            tmp_path,
        )

    assert victim.read_text(encoding="utf-8") == "unchanged"


@pytest.mark.service
def test_qasper_fixture_refuses_symlinked_vault(tmp_path: Path) -> None:
    prepare = _load_benchmark_module("prepare")
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    (tmp_path / "vault").symlink_to(redirected, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinked directory"):
        prepare.prepare_corpus(
            {"1234.56789": _paper()},
            ["1234.56789"],
            tmp_path,
        )


@pytest.mark.service
def test_qasper_fixture_refuses_symlinked_output_directory(tmp_path: Path) -> None:
    prepare = _load_benchmark_module("prepare")
    redirected = tmp_path / "redirected"
    redirected.mkdir()
    output = tmp_path / "output"
    output.symlink_to(redirected, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinked directory"):
        prepare.prepare_corpus(
            {"1234.56789": _paper()},
            ["1234.56789"],
            output,
        )

    assert list(redirected.iterdir()) == []


@pytest.mark.service
def test_qasper_download_uses_private_temporary_and_atomic_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare = _load_benchmark_module("prepare")
    payload = b"pinned archive"
    monkeypatch.setattr(prepare, "ARCHIVE_SHA256", hashlib.sha256(payload).hexdigest())
    monkeypatch.setattr(
        prepare.urllib.request,
        "urlopen",
        lambda url: io.BytesIO(payload),
    )
    destination = tmp_path / "qasper.tgz"
    predictable_temporary = destination.with_suffix(".tmp")
    victim = tmp_path / "victim.txt"
    victim.write_text("unchanged", encoding="utf-8")
    predictable_temporary.symlink_to(victim)

    assert prepare.download_archive(destination) == destination

    assert destination.read_bytes() == payload
    assert predictable_temporary.is_symlink()
    assert victim.read_text(encoding="utf-8") == "unchanged"
    assert not list(tmp_path.glob(".qasper.tgz.*.tmp"))


@pytest.mark.service
def test_qasper_download_refuses_symlinked_destination(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prepare = _load_benchmark_module("prepare")
    victim = tmp_path / "victim.txt"
    victim.write_text("unchanged", encoding="utf-8")
    destination = tmp_path / "qasper.tgz"
    destination.symlink_to(victim)
    monkeypatch.setattr(
        prepare.urllib.request,
        "urlopen",
        lambda url: pytest.fail("download should not start"),
    )

    with pytest.raises(ValueError, match="non-regular file"):
        prepare.download_archive(destination)

    assert victim.read_text(encoding="utf-8") == "unchanged"
