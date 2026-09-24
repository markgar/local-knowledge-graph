from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from support.providers import Embeddings, Reranker

from kg.retrieval.dense import DenseIndexError, EmbeddingProfile
from kg.retrieval.rerank import RerankerError


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    path = Path("benchmarks/productization/validate.py")
    spec = importlib.util.spec_from_file_location("productization_validate", path)
    assert spec and spec.loader
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    monkeypatch.setattr(result, "identity", lambda: {"code_revision": "controlled-test"})
    return result


@pytest.mark.acceptance
def test_controlled_matrix_uses_all_eight_exact_combinations(
    runner: ModuleType, tmp_path: Path,
) -> None:
    report = runner.run_matrix(
        tmp_path / "matrix", controlled=True,
        embedding_factory=Embeddings, reranker_factory=Reranker,
    )
    assert report["status"] == "passed"
    assert report["validation"] == "controlled"
    assert report["tolerances"] == {"relative": 0, "absolute": 0}
    combinations = report["combinations"]
    assert len(combinations) == 8
    assert {(r["manifest"], r["embedding_profile"], r["contextual"]) for r in combinations} == {
        (manifest, profile.value, contextual)
        for manifest in runner.MANIFESTS for profile in EmbeddingProfile
        for contextual in (False, True)
    }
    for result in combinations:
        assert result["status"] == "passed"
        assert len(result["fixture"]["sources_sha256"]) in (10, 12)
        assert [case["label"] for case in result["cases"]] == [
            "unscoped", "subject_Atlas", "excluded_source",
        ]
        for readiness in result["empty_readiness"].values():
            assert readiness["ready_provider_requests"] == {"embedding": 1, "reranker": 1}
            assert readiness["score_calls"] == 0
        for case in result["cases"]:
            assert case["component"] == case["product"]
            assert case["explanation"]["report_version"] == "2"
            for candidate in case["explanation"]["candidates"]:
                assert {"lexical", "dense", "reranker", "final_position"} <= candidate.keys()
    assert json.loads((tmp_path / "matrix/report.json").read_text()) == report


@pytest.mark.acceptance
@pytest.mark.parametrize("dependency", ["embedding", "reranker"])
def test_blocked_matrix_persists_every_combination_and_exact_error(
    runner: ModuleType, tmp_path: Path, dependency: str,
) -> None:
    def unavailable(*args: Any) -> Any:
        error = DenseIndexError if dependency == "embedding" else RerankerError
        raise error("Blocked host: huggingface.co (test)")

    report = runner.run_matrix(
        tmp_path / "blocked", controlled=True,
        embedding_factory=unavailable if dependency == "embedding" else Embeddings,
        reranker_factory=unavailable if dependency == "reranker" else Reranker,
    )
    assert report["status"] == "incomplete"
    assert len(report["combinations"]) == 8
    for result in report["combinations"]:
        assert result["status"] == "blocked"
        assert result["stage"] == (
            "dense_index" if dependency == "embedding" else "empty_readiness_search"
        )
        expected = "Blocked host: huggingface.co (test)"
        if dependency == "reranker":
            expected += (
                " Ensure the pinned reranker model is available from an approved local cache "
                "or download source, then retry search."
            )
        assert result["error"]["message"] == expected
        assert result["error"]["traceback"]
        assert "cases" not in result
    assert json.loads((tmp_path / "blocked/report.json").read_text()) == report


@pytest.mark.service
@pytest.mark.parametrize("method", ["search", "explain_search"])
def test_runner_detects_empty_readiness_bypass(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str,
) -> None:
    original = getattr(runner.SearchService, method)

    def bypass(self: Any, query: str, **kwargs: Any) -> Any:
        if method == "search":
            return []
        # An empty-shaped explanation must not count as readiness success.
        return type("Empty", (), {"hits": []})()

    monkeypatch.setattr(runner.SearchService, method, bypass)
    result: dict[str, Any] = {}
    with pytest.raises(AssertionError, match="initialize both providers"):
        runner.validate_combination(
            Path("corpora/atlas.yml"), EmbeddingProfile.gte_modernbert, False, tmp_path, result,
            embedding_factory=Embeddings, reranker_factory=Reranker, exact=True,
        )
    assert result["stage"] == f"empty_readiness_{method}"
    monkeypatch.setattr(runner.SearchService, method, original)


@pytest.mark.unit
@pytest.mark.parametrize("field,value", [
    ("record_id", "wrong"), ("quote", "wrong"), ("anchor_id", "wrong"),
    ("source_revision_id", "wrong"), ("rank", 1.1), ("rank", float("nan")),
])
def test_parity_mismatches_fail(runner: ModuleType, field: str, value: Any) -> None:
    expected = {"record_id": "r", "quote": "q", "anchor_id": "a",
                "source_revision_id": "v", "rank": 1.0}
    with pytest.raises(AssertionError):
        runner.compare([expected], [{**expected, field: value}], exact=False)


@pytest.mark.unit
def test_score_tolerance_is_real_only(runner: ModuleType) -> None:
    expected = [{"record_id": "r", "rank": 1.0}]
    actual = [{"record_id": "r", "rank": 1.000001}]
    runner.compare(expected, actual, exact=False)
    with pytest.raises(AssertionError):
        runner.compare(expected, actual, exact=True)


@pytest.mark.service
def test_runner_refuses_overwrite(runner: ModuleType, tmp_path: Path) -> None:
    with pytest.raises(FileExistsError):
        runner.run_matrix(tmp_path)


@pytest.mark.service
def test_main_returns_failure_for_incomplete_matrix(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(runner, "run_matrix", lambda output: {"status": "incomplete"})
    monkeypatch.setattr(runner.sys, "argv", ["validate.py", "--output", str(tmp_path / "run")])
    assert runner.main() == 1


@pytest.mark.acceptance
def test_failed_comparison_is_durable_and_remaining_combinations_run(
    runner: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    count = 0

    def validate(*args: Any, **kwargs: Any) -> None:
        nonlocal count
        count += 1
        result = args[4]
        result["stage"] = "parity_unscoped"
        if count == 1:
            raise AssertionError("Ordered record IDs differ")
        result["status"] = "passed"

    monkeypatch.setattr(runner, "validate_combination", validate)
    report = runner.run_matrix(tmp_path / "mismatch", controlled=True)
    assert count == 8 and report["status"] == "incomplete"
    assert report["combinations"][0]["status"] == "failed"
    assert report["combinations"][0]["error"]["message"] == "Ordered record IDs differ"
    assert all(r["status"] == "passed" for r in report["combinations"][1:])
    assert json.loads((tmp_path / "mismatch/report.json").read_text()) == report
