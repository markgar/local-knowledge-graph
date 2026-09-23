from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml
from support.modules import module
from support.providers import Embeddings, Reranker, providers
from typer.testing import CliRunner

from kg.cli import app
from kg.config import load_manifest
from kg.db import Database
from kg.models.contracts import SearchResult
from kg.retrieval import ProductSearchExplanation, SearchExplanationError
from kg.retrieval.dense import DenseIndexError, EmbeddingProfile
from kg.retrieval.rerank import RerankerError
from kg.retrieval.service import SearchQueryError

RUNNER = CliRunner()


@pytest.fixture
def manifest_path(tmp_path: Path) -> Path:
    original = load_manifest(Path("corpora/atlas.yml"))
    vault = tmp_path / "vault"
    shutil.copytree(original.vault_root, vault)
    data = yaml.safe_load(Path("corpora/atlas.yml").read_text())
    data.update(vault_root=str(vault), database=str(tmp_path / "index.sqlite3"))
    path = tmp_path / "corpus.yml"
    path.write_text(yaml.safe_dump(data))
    invoke(path, "ingest")
    return path


def invoke(path: Path, command: str, *arguments: str) -> Any:
    result = RUNNER.invoke(app, [command, *arguments, "--manifest", str(path), "--format", "json"])
    assert result.exit_code == 0, result.output or result.exception
    return json.loads(result.stdout)


@pytest.mark.parametrize("profile", list(EmbeddingProfile))
@pytest.mark.parametrize("contextual", [False, True])
def test_unqualified_pipeline_filters_graph_isolation_and_evidence(
    manifest_path: Path, monkeypatch: pytest.MonkeyPatch,
    profile: EmbeddingProfile, contextual: bool,
) -> None:
    embedding, reranker = Embeddings(profile), Reranker()
    loads = providers(monkeypatch, embedding, reranker)
    flags = ["--embedding-profile", profile.value, *(["--contextual"] if contextual else [])]
    projection = invoke(manifest_path, "dense-index", *flags)
    hits = invoke(
        manifest_path, "search", "archive signing certificate",
        "--subject", "Atlas", "--limit", "100", *flags,
    )
    assert loads == ["embedding", "embedding", "reranker"]
    assert embedding.queries == ["archive signing certificate"]
    assert len(reranker.calls) == 1
    assert hits
    assert all(set(hit) == set(SearchResult.model_fields) for hit in hits)
    assert [hit["rank"] for hit in hits] == sorted([hit["rank"] for hit in hits], reverse=True)
    assert all(hit["source_path"] != "05-atlascope-planning.md" for hit in hits)
    report = invoke(
        manifest_path, "search", "archive signing certificate", "--subject", "Atlas",
        "--explain", "--include-quotes", "--limit", "100", *flags,
    )
    ProductSearchExplanation.model_validate(report)
    assert report["configuration"]["projection_id"] == projection["projection_id"]
    assert report["configuration"]["embedding_profile"] == profile.value
    assert report["configuration"]["contextual"] is contextual
    assert [(h["record_id"], h["rank"], h["quote"]) for h in report["hits"]] == [
        (h["record_id"], h["rank"], h["quote"]) for h in hits
    ]
    certificate = next(
        hit for hit in report["hits"]
        if hit["quote"] == "- The archive signing certificate is unavailable."
    )
    assert any(
        reason["distance"] == 2 and len(reason["supporting_edge_ids"]) == 2
        for reason in certificate["subject_reasons"]
    )
    evidence = invoke(manifest_path, "evidence", certificate["record_id"])
    source = invoke(manifest_path, "source-range", certificate["anchor_id"])
    context = invoke(manifest_path, "source-context", certificate["anchor_id"])
    assert (
        evidence["quote"] == source["quote"] == context["selected"]["quote"] == certificate["quote"]
    )
    corpus = load_manifest(manifest_path)
    text = (corpus.vault_root / source["source_path"]).read_text()
    assert text[source["start_offset"]:source["end_offset"]] == source["quote"]
    filtered = invoke(
        manifest_path, "search", "archive signing certificate", "--subject", "Atlas",
        "--source", source["source_path"], "--limit", "1", *flags,
    )
    assert len(filtered) == 1 and filtered[0]["source_path"] == source["source_path"]
    monkeypatch.setattr("kg.cli._parse_since", lambda value: datetime(2099, 1, 1, tzinfo=UTC))
    assert invoke(manifest_path, "search", "certificate", "--since", "1h", *flags) == []
    other = yaml.safe_load(manifest_path.read_text())
    other["corpus_id"] = "other"
    other_path = manifest_path.with_name("other.yml")
    other_path.write_text(yaml.safe_dump(other))
    invoke(other_path, "ingest")
    after = invoke(
        manifest_path, "search", "archive signing certificate",
        "--subject", "Atlas", "--limit", "100", *flags,
    )
    assert after == hits
    missing = RUNNER.invoke(app, [
        "evidence", certificate["record_id"], "--manifest", str(other_path), "--format", "json",
    ])
    assert missing.exit_code == 2
    assert json.loads(missing.stderr)["error"] == "record_not_found"


def test_full_trace_json_nulls_quotes_truncation_and_authoritative_order(
    manifest_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    corpus = load_manifest(manifest_path)
    for number in range(120):
        text = "keyword" if number < 60 else "meaning"
        if number < 10:
            text += " meaning keyword"
        (corpus.vault_root / f"{number:03}.md").write_text(f"# Document\n\n{text} item {number}\n")
    invoke(manifest_path, "ingest")
    embedding, reranker = Embeddings(), Reranker()
    providers(monkeypatch, embedding, reranker)
    invoke(manifest_path, "dense-index")
    hits = invoke(manifest_path, "search", "keyword", "--limit", "5")
    assert [h["rank"] for h in hits] == sorted([h["rank"] for h in hits], reverse=True)
    report = invoke(
        manifest_path, "search", "keyword", "--limit", "5", "--explain", "--explain-limit", "200"
    )
    assert len(reranker.calls) == 2
    assert embedding.queries == ["keyword", "keyword"]
    assert report["report_version"] == "2"
    assert report["stage_counts"]["lexical_candidates"] == 50
    assert report["stage_counts"]["dense_candidates"] == 50
    assert report["stage_counts"]["reranked_candidates"] == 50
    assert report["total_candidates"] > 50
    assert len({c["record_id"] for c in report["candidates"]}) == report["total_candidates"]
    assert any(c["lexical"] and c["dense"] for c in report["candidates"])
    for stage in ("lexical", "dense", "reranker", "final_position"):
        assert any(c[stage] is None for c in report["candidates"])
    for stage in ("lexical", "dense"):
        assert any(c["fusion"][f"{stage}_contribution"] is None for c in report["candidates"])
    assert any(
        c["reranker"] and c["reranker"]["position"] != c["fusion"]["position"]
        for c in report["candidates"]
    )
    assert all("quote" not in h for h in report["hits"])
    assert all("quote" not in c for c in report["candidates"])
    assert [(h["record_id"], h["rank"]) for h in report["hits"]] == [
        (h["record_id"], h["rank"]) for h in hits
    ]
    for limit in (1, 50, 200):
        bounded = invoke(
            manifest_path, "search", "keyword", "--limit", "5", "--explain",
            "--explain-limit", str(limit), "--include-quotes",
        )
        assert bounded["stage_counts"] == report["stage_counts"]
        assert bounded["candidates"] == report["candidates"][:limit]
        assert bounded["displayed_candidates"] == min(limit, report["total_candidates"])
        assert bounded["truncated"] is (limit < report["total_candidates"])
        assert [(h["record_id"], h["rank"], h["quote"]) for h in bounded["hits"]] == [
            (h["record_id"], h["rank"], h["quote"]) for h in hits
        ]
    default = invoke(manifest_path, "search", "keyword", "--explain")
    assert default["trace_limit"] == 50
    for include in (False, True):
        text = RUNNER.invoke(app, [
            "search", "keyword", "--manifest", str(manifest_path), "--explain",
            *(["--include-quotes"] if include else []),
        ])
        assert text.exit_code == 0
        assert (hits[0]["quote"] in text.stdout) is include
        assert "not confidence" in text.stdout


@pytest.mark.parametrize(
    "mode", ["strict", "natural", "dense", "hybrid", "reranked", "unknown", ""]
)
@pytest.mark.parametrize("equals", [False, True])
@pytest.mark.parametrize("format_", ["text", "json"])
def test_every_obsolete_mode_fails_with_migration_guidance(
    tmp_path: Path, mode: str, equals: bool, format_: str,
) -> None:
    flags = [f"--query-mode={mode}"] if equals else ["--query-mode", *([mode] if mode else [])]
    result = RUNNER.invoke(app, [
        "search", "query", "--manifest", str(tmp_path / "missing.yml"),
        *flags, "--format", format_,
    ])
    assert result.exit_code == 2
    assert not result.stdout
    assert "has been removed" in result.stderr
    assert "Omit the flag" in result.stderr
    assert "dense-index" in result.stderr
    if format_ == "json":
        assert json.loads(result.stderr)["error"] == "invalid_query"


@pytest.mark.parametrize("flags", [
    ["--include-quotes"], ["--explain-limit", "50"],
    ["--explain", "--explain-limit", "0"], ["--explain", "--explain-limit", "201"],
    ["--explain", "--explain-limit", "bad"], ["--limit", "0"], ["--limit", "101"],
    ["--since", "bad"], ["--embedding-profile", "unknown"],
])
def test_invalid_arguments_precede_readiness(
    manifest_path: Path, monkeypatch: pytest.MonkeyPatch, flags: list[str],
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("invalid arguments must not initialize models")

    monkeypatch.setattr("kg.retrieval.dense.SentenceTransformerEmbeddingProvider", forbidden)
    monkeypatch.setattr("kg.retrieval.rerank.SentenceTransformerCrossEncoderProvider", forbidden)
    result = RUNNER.invoke(app, [
        "search", "query", "--manifest", str(manifest_path), "--format", "json", *flags,
    ])
    assert result.exit_code == 2
    assert not result.stdout


@pytest.mark.parametrize("explained", [False, True])
@pytest.mark.parametrize("empty", ["none", "corpus", "source"])
@pytest.mark.parametrize("failure", ["embedding", "reranker", "none"])
def test_readiness_is_mandatory_even_without_candidates(
    manifest_path: Path, monkeypatch: pytest.MonkeyPatch,
    explained: bool, empty: str, failure: str,
) -> None:
    if empty == "corpus":
        data = yaml.safe_load(manifest_path.read_text())
        data["include"] = ["missing-*.md"]
        manifest_path.write_text(yaml.safe_dump(data))
        invoke(manifest_path, "ingest")
    embedding, reranker = Embeddings(), Reranker()
    loads = providers(monkeypatch, embedding, reranker)
    invoke(manifest_path, "dense-index")
    loads.clear()

    def unavailable(*args: Any) -> Any:
        raise (DenseIndexError if failure == "embedding" else RerankerError)("model unavailable")

    if failure != "none":
        factory = (
            "dense.SentenceTransformerEmbeddingProvider" if failure == "embedding"
            else "rerank.SentenceTransformerCrossEncoderProvider"
        )
        monkeypatch.setattr(f"kg.retrieval.{factory}", unavailable)
    result = RUNNER.invoke(app, [
        "search", "certificate", "--manifest", str(manifest_path), "--format", "json",
        *(["--explain"] if explained else []),
        *(["--source", "missing.md"] if empty == "source" else []),
    ])
    if failure == "none":
        assert result.exit_code == 0, result.output
        assert loads == ["embedding", "reranker"]
        payload = json.loads(result.stdout)
        if empty != "none":
            assert (payload["hits"] if explained else payload) == []
            assert reranker.calls == []
        else:
            assert reranker.calls
    else:
        assert result.exit_code == 2
        assert not result.stdout
        error = json.loads(result.stderr)
        assert error["error"] == (
            "dense_index_unavailable" if failure == "embedding" else "reranker_unavailable"
        )
        assert "approved" in error["message"]


@pytest.mark.parametrize("explained", [False, True])
@pytest.mark.parametrize("provider", ["embedding", "reranker"])
def test_provider_execution_failures_are_not_successful_search(
    manifest_path: Path, monkeypatch: pytest.MonkeyPatch, explained: bool, provider: str,
) -> None:
    embedding, reranker = Embeddings(), Reranker()
    providers(monkeypatch, embedding, reranker)
    invoke(manifest_path, "dense-index")

    def fail() -> None:
        raise (DenseIndexError if provider == "embedding" else RerankerError)("inference failed")

    if provider == "embedding":
        embedding.on_query = fail
    else:
        reranker.on_score = fail
    result = RUNNER.invoke(app, [
        "search", "certificate", "--manifest", str(manifest_path), "--format", "json",
        *(["--explain"] if explained else []),
    ])
    assert result.exit_code == 2
    assert not result.stdout
    error = json.loads(result.stderr)
    assert error["error"] == (
        "dense_index_unavailable" if provider == "embedding" else "reranker_unavailable"
    )
    assert "inference failed" in error["message"]


@pytest.mark.parametrize("mode", ["strict", "natural"])
def test_legacy_lexical_contextual_rejection_is_preserved(
    manifest_path: Path, mode: str,
) -> None:
    worker = module("benchmarks/_lexical_search.py")
    for explain in ([], ["--explain"]):
        with pytest.raises(SearchQueryError, match="contextual requires"):
            worker.evaluate([
                "approval", "--manifest", str(manifest_path),
                "--query-mode", mode, "--contextual", *explain,
            ])


@pytest.mark.parametrize("query", ["", " ", "!!!"])
@pytest.mark.parametrize("explained", [False, True])
def test_invalid_query_precedes_provider_initialization(
    manifest_path: Path, monkeypatch: pytest.MonkeyPatch, query: str, explained: bool,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Invalid query must not load semantic models")
    monkeypatch.setattr("kg.retrieval.dense.SentenceTransformerEmbeddingProvider", forbidden)
    monkeypatch.setattr("kg.retrieval.rerank.SentenceTransformerCrossEncoderProvider", forbidden)
    result = RUNNER.invoke(app, [
        "search", query, "--manifest", str(manifest_path), "--format", "json",
        *(["--explain"] if explained else []),
    ])
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"] == "invalid_query"


@pytest.mark.parametrize("explained", [False, True])
@pytest.mark.parametrize("state", ["missing", "stale", "incompatible"])
def test_index_failures_and_matching_rebuild_recovery(
    manifest_path: Path, monkeypatch: pytest.MonkeyPatch, explained: bool, state: str,
) -> None:
    embedding, reranker = Embeddings(), Reranker()
    providers(monkeypatch, embedding, reranker)
    if state != "missing":
        invoke(manifest_path, "dense-index")
    if state == "stale":
        corpus = load_manifest(manifest_path)
        with (corpus.vault_root / "01-planning-meeting.md").open("a") as source:
            source.write("\nAdditional evidence.\n")
        invoke(manifest_path, "ingest")
    if state == "incompatible":
        embedding.pipeline_version = "incompatible-v2"
    flags = ["--explain"] if explained else []
    result = RUNNER.invoke(app, [
        "search", "certificate", "--manifest", str(manifest_path), "--format", "json", *flags,
    ])
    assert result.exit_code == 2
    assert not result.stdout
    error = json.loads(result.stderr)
    assert error["error"] == "dense_index_unavailable"
    assert "dense-index" in error["message"]
    invoke(manifest_path, "dense-index")
    assert invoke(manifest_path, "search", "certificate", *flags)


@pytest.mark.parametrize("explained", [False, True])
@pytest.mark.parametrize("during", ["readiness", "scoring"])
def test_intervening_edit_restore_returns_retry_error(
    manifest_path: Path, monkeypatch: pytest.MonkeyPatch, explained: bool, during: str,
) -> None:
    embedding, reranker = Embeddings(), Reranker()
    providers(monkeypatch, embedding, reranker)
    invoke(manifest_path, "dense-index")
    database = Database(load_manifest(manifest_path).database)

    def change_restore() -> None:
        with database.transaction() as connection:
            connection.execute("UPDATE source_document SET title = title || ' changed'")
        with database.transaction() as connection:
            connection.execute(
                "UPDATE source_document SET title = substr(title, 1, length(title)-8)"
            )

    if during == "readiness":
        def load(profile: EmbeddingProfile) -> Embeddings:
            change_restore()
            return embedding
        monkeypatch.setattr("kg.retrieval.dense.SentenceTransformerEmbeddingProvider", load)
    else:
        reranker.on_score = change_restore
    result = RUNNER.invoke(app, [
        "search", "certificate", "--manifest", str(manifest_path), "--format", "json",
        *(["--explain"] if explained else []),
    ])
    assert result.exit_code == 2
    assert not result.stdout
    error = json.loads(result.stderr)
    assert error["error"] == "search_state_changed"
    assert "retry" in error["message"]


def test_other_explanation_error_codes_are_preserved(
    manifest_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise SearchExplanationError("incomplete_search_trace", "Trace unavailable")
    monkeypatch.setattr("kg.cli.SearchService.explain_search", unavailable)
    result = RUNNER.invoke(app, [
        "search", "certificate", "--manifest", str(manifest_path), "--format", "json", "--explain",
    ])
    assert result.exit_code == 2
    assert json.loads(result.stderr) == {
        "error": "incomplete_search_trace", "message": "Trace unavailable",
    }


def test_structured_operations_do_not_initialize_semantic_models(
    manifest_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("Structured operations must remain independent of semantic models")
    monkeypatch.setattr("kg.retrieval.dense.SentenceTransformerEmbeddingProvider", forbidden)
    monkeypatch.setattr("kg.retrieval.rerank.SentenceTransformerCrossEncoderProvider", forbidden)
    status = invoke(manifest_path, "status", "Atlas")
    action = invoke(manifest_path, "actions", "Atlas")[0]
    assert status["open_actions"]
    invoke(manifest_path, "record-state")
    invoke(manifest_path, "evidence", action["record_id"])
    invoke(manifest_path, "source-range", action["anchor_id"])
    invoke(manifest_path, "source-context", action["anchor_id"])
    invoke(manifest_path, "revisions", action["source_path"])
    corpus = load_manifest(manifest_path)
    with (corpus.vault_root / action["source_path"]).open("a") as source:
        source.write("\nNew revision evidence.\n")
    invoke(manifest_path, "ingest", "--explain")
    comparison = invoke(manifest_path, "compare-revisions", action["source_path"])
    assert comparison["added"]
    assert invoke(manifest_path, "evidence", action["record_id"])["quote"] == action["quote"]
    assert not invoke(manifest_path, "source-range", action["anchor_id"])["is_current"]
    capabilities = RUNNER.invoke(app, ["capabilities", "--format", "json"])
    assert capabilities.exit_code == 0
    payload = json.loads(capabilities.stdout)
    assert payload["interface_version"] == "2"
    search = next(tool for tool in payload["tools"] if tool["command"] == "search")
    assert search["explanation"]["report_version"] == "2"
    assert search["explanation"]["trace_limit"] == {
        "flag": "--explain-limit", "default": 50, "min": 1, "max": 200,
    }
    assert "removed" in search["migration"]
    help_ = RUNNER.invoke(app, ["search", "--help"])
    assert help_.exit_code == 0
    assert "reranking" in help_.stdout
    assert "removed" in help_.stdout
