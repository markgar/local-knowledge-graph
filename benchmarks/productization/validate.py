"""Isolated product/component parity, not a relevance benchmark or public API."""

from __future__ import annotations

import argparse
import gc
import hashlib
import importlib.metadata
import json
import math
import os
import platform
import shutil
import subprocess
import sys
import time
import traceback
from collections.abc import Callable
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

from kg.config import load_manifest, select_sources
from kg.db import Database
from kg.ingest import IngestService
from kg.retrieval import RerankedRetrievalService, RetrievalService, SearchService
from kg.retrieval.dense import (
    EMBEDDING_PROFILES,
    DenseIndexError,
    DenseRetrievalService,
    EmbeddingProfile,
    EmbeddingProvider,
    SentenceTransformerEmbeddingProvider,
)
from kg.retrieval.rerank import (
    MODEL_NAME,
    MODEL_REVISION,
    RerankerError,
    RerankerProvider,
    SentenceTransformerCrossEncoderProvider,
)

REPOSITORY = Path(__file__).resolve().parents[2]
MANIFESTS = ("corpora/atlas.yml", "corpora/atlas-state.yml")
QUERY = "archive signing certificate"
NO_SOURCE = "__productization_no_matching_source__.md"
REL_TOL = 1e-5
ABS_TOL = 1e-6


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def git(*arguments: str) -> str:
    return subprocess.check_output(
        ["git", *arguments], cwd=REPOSITORY, text=True,
    ).strip()


def identity() -> dict[str, Any]:
    files = sorted((REPOSITORY / "src/kg").rglob("*.py"))
    files += [REPOSITORY / "src/kg/schema.sql", Path(__file__).resolve()]
    return {
        "code_revision": git("rev-parse", "HEAD"),
        "branch": git("branch", "--show-current"),
        "worktree_status": git("status", "--short"),
        "corpora_tree": git("rev-parse", "HEAD:corpora"),
        "code_sha256": {str(p.relative_to(REPOSITORY)): sha256(p) for p in files},
        "python": sys.version,
        "dependencies": {
            distribution.metadata["Name"]: distribution.version
            for distribution in importlib.metadata.distributions()
        },
        "hardware": {
            "platform": platform.platform(), "machine": platform.machine(),
            "processor": platform.processor(), "logical_cpus": os.cpu_count(),
            "mac_model_and_memory_bytes": subprocess.check_output(
                ["sysctl", "-n", "hw.model", "hw.memsize"], text=True,
            ).splitlines() if sys.platform == "darwin" else None,
        },
        "device": "cpu",
        "torch_threads": 2,
        "command": [sys.executable, *sys.argv],
    }


def real_embedding(profile: EmbeddingProfile) -> EmbeddingProvider:
    import torch
    from sentence_transformers import SentenceTransformer

    torch.set_num_threads(2)
    # Constrain the existing providers to CPU without changing their model pins
    # or encoding behavior. Only one combination is alive at a time.
    def cpu_model(*args: Any, **kwargs: Any) -> Any:
        return SentenceTransformer(*args, **kwargs, device="cpu")

    with patch("sentence_transformers.SentenceTransformer", cpu_model):
        return SentenceTransformerEmbeddingProvider(profile)


def real_reranker() -> RerankerProvider:
    from sentence_transformers import CrossEncoder

    def cpu_model(*args: Any, **kwargs: Any) -> Any:
        return CrossEncoder(*args, **kwargs, device="cpu")

    with patch("sentence_transformers.CrossEncoder", cpu_model):
        return SentenceTransformerCrossEncoderProvider()


def compare(
    expected: list[dict[str, Any]], actual: list[dict[str, Any]], *, exact: bool,
) -> None:
    if [item["record_id"] for item in actual] != [item["record_id"] for item in expected]:
        raise AssertionError("Ordered record IDs differ")
    for left, right in zip(expected, actual, strict=True):
        for key, value in left.items():
            if key == "rank":
                if not math.isfinite(value) or not math.isfinite(right[key]):
                    raise AssertionError("Non-finite score")
                matches = (
                    value == right[key] if exact else
                    math.isclose(value, right[key], rel_tol=REL_TOL, abs_tol=ABS_TOL)
                )
                if not matches:
                    raise AssertionError(f"Score mismatch for {left['record_id']}: "
                                         f"{value!r} != {right[key]!r}")
            elif key not in right or right[key] != value:
                raise AssertionError(f"{key} mismatch for {left['record_id']}")


def validate_combination(
    manifest_path: Path,
    profile: EmbeddingProfile,
    contextual: bool,
    workspace: Path,
    result: dict[str, Any],
    *,
    embedding_factory: Callable[[EmbeddingProfile], EmbeddingProvider] = real_embedding,
    reranker_factory: Callable[[], RerankerProvider] = real_reranker,
    exact: bool = False,
) -> None:
    result["stage"] = "fixture_copy"
    original = load_manifest(manifest_path)
    selection = select_sources(original)
    if selection.missing:
        raise ValueError(f"Missing fixture selections: {selection.missing}")
    vault = workspace / "vault"
    vault.mkdir()
    sources = {}
    for source in selection.paths:
        relative = source.relative_to(original.vault_root)
        destination = vault / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        sources[relative.as_posix()] = sha256(source)
        if sha256(destination) != sources[relative.as_posix()]:
            raise AssertionError(f"Source copy changed: {relative}")
    if NO_SOURCE in sources:
        raise AssertionError("Empty-case source unexpectedly exists")
    isolated = original.model_copy(update={
        "vault_root": vault.resolve(), "database": (workspace / "index.sqlite3").resolve(),
    })
    write_json(workspace / "corpus.json", isolated.model_dump(mode="json"))
    result["fixture"] = {
        "manifest_sha256": sha256(manifest_path), "sources_sha256": sources,
        "isolated_manifest": str(workspace / "corpus.json"),
    }
    result["stage"] = "ingest"
    database = Database(isolated.database)
    ingestion = IngestService(database).ingest(isolated)
    result["ingestion"] = ingestion.model_dump(mode="json")
    if ingestion.failed:
        raise AssertionError("Fixture ingestion failed")
    retrieval = RetrievalService(database, isolated.corpus_id)
    result["corpus_fingerprint"] = retrieval.index_fingerprint()
    embedding: EmbeddingProvider | None = None
    reranker: RerankerProvider | None = None
    loads = {"embedding": 0, "reranker": 0}
    score_calls = 0

    def load_embedding(selected: EmbeddingProfile) -> EmbeddingProvider:
        nonlocal embedding
        if selected != profile:
            raise AssertionError("Embedding profile changed")
        if embedding is None:
            embedding = embedding_factory(selected)
        loads["embedding"] += 1
        return embedding

    def load_reranker() -> RerankerProvider:
        nonlocal reranker
        if reranker is None:
            reranker = reranker_factory()
            original_score = reranker.score

            def score(*args: Any, **kwargs: Any) -> list[float]:
                nonlocal score_calls
                score_calls += 1
                return original_score(*args, **kwargs)

            # Restored by ExitStack below, including on a failed combination.
            stack.enter_context(patch.object(reranker, "score", score))
        loads["reranker"] += 1
        return reranker

    with ExitStack() as stack:
        stack.enter_context(patch(
            "kg.retrieval.dense.SentenceTransformerEmbeddingProvider", load_embedding,
        ))
        stack.enter_context(patch(
            "kg.retrieval.rerank.SentenceTransformerCrossEncoderProvider", load_reranker,
        ))
        result["stage"] = "dense_index"
        projection = DenseRetrievalService(
            database, isolated.corpus_id, profile=profile, contextual=contextual,
        ).build_index(batch_size=8)
        result["projection"] = projection.model_dump(mode="json")
        options = {"embedding_profile": profile, "contextual": contextual}
        result["empty_readiness"] = {}
        for method_name in ("search", "explain_search"):
            result["stage"] = f"empty_readiness_{method_name}"
            before, scores_before = dict(loads), score_calls
            fresh = SearchService(database, isolated.corpus_id, **options)
            response = getattr(fresh, method_name)(QUERY, source_path=NO_SOURCE)
            hits = response if method_name == "search" else response.hits
            if hits:
                raise AssertionError("Excluding source returned hits")
            if any(loads[key] <= before[key] for key in loads):
                raise AssertionError("Empty product search did not initialize both providers")
            if score_calls != scores_before:
                raise AssertionError("Empty product search scored candidates")
            result["empty_readiness"][method_name] = {
                "status": "passed", "ready_provider_requests": {
                    key: loads[key] - before[key] for key in loads
                }, "score_calls": score_calls - scores_before,
            }
        component = RerankedRetrievalService(database, isolated.corpus_id, **options)
        product = SearchService(database, isolated.corpus_id, **options)
        result["cases"] = []
        for label, filters in (
            ("unscoped", {}), ("subject_Atlas", {"subject": "Atlas"}),
            ("excluded_source", {"source_path": NO_SOURCE}),
        ):
            case: dict[str, Any] = {"label": label, "filters": filters, "status": "incomplete"}
            result["cases"].append(case)
            result["stage"] = f"parity_{label}"
            expected = [r.model_dump(mode="json") for r in component.search(QUERY, **filters)]
            actual = [r.model_dump(mode="json") for r in product.search(QUERY, **filters)]
            explanation = product.explain_search(QUERY, include_quotes=True, **filters)
            explained = [hit.model_dump(mode="json") for hit in explanation.hits]
            case.update({
                "component": expected, "product": actual,
                "explanation": explanation.model_dump(mode="json"),
            })
            if label != "excluded_source" and not expected:
                raise AssertionError("Representative query unexpectedly returned no candidates")
            compare(expected, actual, exact=exact)
            # Explanation hits intentionally omit summary/status, not citation fields.
            compare(
                [{k: v for k, v in item.items() if k not in {"summary", "status"}}
                 for item in expected],
                explained, exact=exact,
            )
            case.update({
                "status": "passed",
                "citations": [
                    retrieval.source_range(item["anchor_id"]).model_dump(mode="json")
                    for item in actual
                ],
            })
        result["stage"] = "fixture_integrity"
        if sources != {
            source.relative_to(original.vault_root).as_posix(): sha256(source)
            for source in select_sources(original).paths
        }:
            raise AssertionError("Authored fixtures changed during validation")
        result["status"] = "passed"
        result["stage"] = "complete"


def run_matrix(
    output: Path, *, controlled: bool = False,
    embedding_factory: Callable[[EmbeddingProfile], EmbeddingProvider] = real_embedding,
    reranker_factory: Callable[[], RerankerProvider] = real_reranker,
) -> dict[str, Any]:
    output.mkdir(parents=True, exist_ok=False)
    report: dict[str, Any] = {
        "benchmark": "productization-parity-v1", "status": "incomplete",
        "validation": "controlled" if controlled else "real-model",
        "started_at": datetime.now(UTC).isoformat(), "identity": identity(),
        "query": QUERY, "limit": 20, "trace_limit": 50,
        "tolerances": {"relative": 0 if controlled else REL_TOL,
                       "absolute": 0 if controlled else ABS_TOL},
        "reranker": {"name": MODEL_NAME, "revision": MODEL_REVISION},
        "combinations": [],
    }
    for manifest in MANIFESTS:
        for profile in EmbeddingProfile:
            for contextual in (False, True):
                config = EMBEDDING_PROFILES[profile]
                report["combinations"].append({
                    "id": (
                        f"{Path(manifest).stem}-{profile.value}-contextual-{str(contextual).lower()}"
                    ),
                    "manifest": manifest, "embedding_profile": profile.value,
                    "embedding_model": config.model_name,
                    "embedding_revision": config.model_revision,
                    "contextual": contextual, "status": "incomplete", "stage": "not_started",
                })
    write_json(output / "report.json", report)
    for result in report["combinations"]:
        workspace = output / result["id"]
        workspace.mkdir()
        started = time.perf_counter()
        result["stage"] = "starting"
        write_json(output / "report.json", report)
        try:
            validate_combination(
                REPOSITORY / result["manifest"], EmbeddingProfile(result["embedding_profile"]),
                result["contextual"], workspace, result,
                embedding_factory=embedding_factory, reranker_factory=reranker_factory,
                exact=controlled,
            )
        except Exception as exc:
            # The batch boundary must persist failures, never turn them into a
            # successful empty result or omit the remaining required combinations.
            result["status"] = (
                "blocked" if isinstance(exc, (DenseIndexError, RerankerError, OSError, ImportError))
                else "failed"
            )
            result["error"] = {"type": type(exc).__name__, "message": str(exc),
                               "traceback": traceback.format_exc()}
        result["elapsed_seconds"] = time.perf_counter() - started
        write_json(output / "report.json", report)
        print(f"{result['id']}: {result['status']} ({result['stage']})", flush=True)
        gc.collect()
    report["status"] = (
        "passed" if all(r["status"] == "passed" for r in report["combinations"]) else "incomplete"
    )
    report["finished_at"] = datetime.now(UTC).isoformat()
    write_json(output / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="New isolated run directory.")
    args = parser.parse_args()
    report = run_matrix(args.output.resolve())
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())
