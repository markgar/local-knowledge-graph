from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import pytest
from typer.testing import CliRunner

from kg.config import load_manifest
from kg.db import Database
from kg.ingest import IngestService
from kg.legacy_cli import app
from kg.models.contracts import SearchResult
from kg.models.manifest import CorpusManifest
from kg.retrieval import explain as explanation_module
from kg.retrieval.explain import (
    SearchExplanation,
    SearchExplanationError,
    explain_search,
    render_search_explanation,
)
from kg.retrieval.service import RetrievalService, SearchQueryError

RUNNER = CliRunner()
PRIVATE_QUOTE = "Cobalt evidence contains the private marshmallow payload."


@pytest.fixture
def corpus(tmp_path: Path) -> CorpusManifest:
    vault = tmp_path / "vault"
    vault.mkdir()
    documents = {
        "root.md": "# Cobalt\n\nCobalt connects to [[Junction]].\n",
        "bridge.md": "# Junction\n\nJunction connects to [[Ledger]].\n",
        "ledger.md": "# Ledger\n\nLedger connects to [[Archive]].\n\nDistant evidence.\n",
        "archive.md": "# Archive\n\nBeyondscope evidence.\n",
        "direct.md": f"# Notes\n\n{PRIVATE_QUOTE}\n\nInherited evidence.\n",
        "similar.md": "# Cobaltic\n\nUnrelated evidence.\n",
        "unknown.md": "# Unlisted\n\nFallback evidence.\n",
        "heading.md": "# Neutral\n\n## Unlisted heading\n\nHeading evidence.\n",
    }
    for name, content in documents.items():
        (vault / name).write_text(content, encoding="utf-8")
    manifest_path = tmp_path / "corpus.yml"
    manifest_path.write_text(
        "corpus_id: explanation\n"
        "display_name: Explanation fixture\n"
        "vault_root: vault\n"
        "database: index.sqlite3\n"
        "include: ['*.md']\n"
        "seed_entities:\n"
        "  - {entity_id: cobalt, name: Cobalt, entity_type: project, aliases: [Blue]}\n"
        "  - {entity_id: junction, name: Junction, entity_type: component}\n"
        "  - {entity_id: ledger, name: Ledger, entity_type: component}\n"
        "  - {entity_id: archive, name: Archive, entity_type: component}\n",
        encoding="utf-8",
    )
    manifest = load_manifest(manifest_path)
    result = IngestService(Database(manifest.database)).ingest(manifest)
    assert result.failed == 0
    return manifest


def test_direct_and_inherited_subject_reasons(corpus: CorpusManifest) -> None:
    database = Database(corpus.database)
    direct = explain_search(database, corpus.corpus_id, "private", subject="Cobalt")
    assert len(direct.hits) == 1
    reason = next(r for r in direct.hits[0].subject_reasons if r.kind == "exact_mention")
    assert (reason.entity_id, reason.distance, reason.scope) == ("cobalt", 0, "direct")
    assert reason.anchor_id == direct.hits[0].anchor_id
    assert reason.supporting_edge_ids == []
    assert reason.mention_id

    inherited = explain_search(database, corpus.corpus_id, "Inherited", subject="Blue")
    assert len(inherited.hits) == 1
    reasons = inherited.hits[0].subject_reasons
    assert len(reasons) == 1
    assert reasons[0].kind == "document_inheritance"
    assert reasons[0].anchor_id == direct.hits[0].anchor_id
    assert reasons[0].distance == 0
    assert reasons[0].mention_id == reason.mention_id


def test_two_hop_scope_has_real_supporting_edges(corpus: CorpusManifest) -> None:
    database = Database(corpus.database)
    report = explain_search(database, corpus.corpus_id, "Distant", subject="Cobalt")
    assert [(e.entity_id, e.distance) for e in report.subject_scope] == [
        ("cobalt", 0), ("junction", 1), ("ledger", 2)
    ]
    assert len(report.hits) == 1
    reason = next(r for r in report.hits[0].subject_reasons if r.entity_id == "ledger")
    assert reason.kind == "document_inheritance"
    assert reason.scope == "graph_expanded"
    assert reason.distance == 2
    assert len(reason.supporting_edge_ids) == 2
    with database.connection() as connection:
        keys = {
            row["entity_id"]: row["entity_key"]
            for row in connection.execute("SELECT entity_id, entity_key FROM entity")
        }
        edges = {
            row["relationship_id"]: row
            for row in connection.execute("SELECT * FROM relationship")
        }
    first, second = [edges[key] for key in reason.supporting_edge_ids]
    assert {first["source_entity_key"], first["target_entity_key"]} == {
        keys["cobalt"], keys["junction"]
    }
    assert {second["source_entity_key"], second["target_entity_key"]} == {
        keys["junction"], keys["ledger"]
    }
    ledger = report.subject_scope[2]
    assert ledger.supporting_anchor_ids == [first["anchor_id"], second["anchor_id"]]
    assert explain_search(database, corpus.corpus_id, "Beyondscope", subject="Cobalt").hits == []


def test_expanded_exact_mentions_and_metadata_match(corpus: CorpusManifest) -> None:
    database = Database(corpus.database)
    report = explain_search(
        database, corpus.corpus_id, "connects", subject="Cobalt", source_path="root.md"
    )
    reasons = report.hits[0].subject_reasons
    assert any(r.kind == "metadata_name_match" and r.metadata_field == "title" for r in reasons)
    expanded = next(r for r in reasons if r.kind == "exact_mention" and r.entity_id == "junction")
    assert expanded.distance == 1
    assert expanded.scope == "graph_expanded"
    assert len(expanded.supporting_edge_ids) == 1


def test_similar_name_and_unknown_subject_metadata_fallback(corpus: CorpusManifest) -> None:
    database = Database(corpus.database)
    assert explain_search(database, corpus.corpus_id, "Unrelated", subject="Cobalt").hits == []
    report = explain_search(database, corpus.corpus_id, "Fallback", subject="Unlisted")
    assert report.subject_scope == []
    assert {r.metadata_field for r in report.hits[0].subject_reasons} == {"title", "heading_path"}
    heading = explain_search(database, corpus.corpus_id, "evidence", subject="Unlisted heading")
    assert len(heading.hits) == 1
    assert [r.metadata_field for r in heading.hits[0].subject_reasons] == ["heading_path"]
    assert explain_search(database, corpus.corpus_id, "evidence", subject="Unlist").hits == []


@pytest.mark.parametrize("mode", ["strict", "natural"])
@pytest.mark.parametrize("subject", [None, "Cobalt", "Blue", "Unlisted", "missing"])
def test_explained_results_equal_ordinary_search(
    corpus: CorpusManifest, mode: Literal["strict", "natural"], subject: str | None
) -> None:
    database = Database(corpus.database)
    service = RetrievalService(database, corpus.corpus_id)
    query = "EVIDENCE evidence"
    ordinary = service.search(query, subject=subject, limit=3, query_mode=mode)
    report = explain_search(
        database, corpus.corpus_id, query, subject=subject, limit=3,
        query_mode=mode, include_quotes=True,
    )
    for hit, expected in zip(report.hits, ordinary, strict=True):
        values = expected.model_dump()
        values.pop("summary")
        values.pop("status")
        assert hit.model_dump(exclude={"position", "subject_reasons"}) == values
    assert [hit.position for hit in report.hits] == list(range(1, len(ordinary) + 1))
    assert report.lexical_expression == (
        '"EVIDENCE" AND "evidence"' if mode == "strict" else '"evidence"'
    )
    assert "not confidence" in report.rank_semantics
    assert report.model_dump()["supersession_filter_applied"] is False
    assert "Supersession filter: not applied" in render_search_explanation(report)
    assert SearchExplanation.model_validate_json(report.model_dump_json()) == report


def test_filters_empty_corpus_and_empty_results(corpus: CorpusManifest, tmp_path: Path) -> None:
    database = Database(corpus.database)
    cutoff = datetime(9999, 1, 1, tzinfo=UTC)
    report = explain_search(
        database, corpus.corpus_id, "evidence", since=cutoff, source_path="direct.md", limit=1
    )
    assert report.hits == []
    assert report.filters.since == cutoff
    assert report.filters.source_path == "direct.md"
    assert report.filters.limit == 1
    assert explain_search(database, corpus.corpus_id, "unfindable").hits == []
    empty = explain_search(Database(tmp_path / "empty.sqlite3"), "empty", "evidence")
    assert empty.hits == []
    assert empty.subject_scope == []
    assert "Hits: 0" in render_search_explanation(empty)


def test_corpus_scope_isolated(corpus: CorpusManifest) -> None:
    database = Database(corpus.database)
    before = explain_search(database, corpus.corpus_id, "evidence", subject="Cobalt")
    other = corpus.model_copy(update={"corpus_id": "other"})
    IngestService(database).ingest(other)
    after = explain_search(database, corpus.corpus_id, "evidence", subject="Cobalt")
    assert after == before


def test_graph_paths_ignore_inactive_and_historical_edges(corpus: CorpusManifest) -> None:
    database = Database(corpus.database)
    before = explain_search(database, corpus.corpus_id, "Distant", subject="Cobalt")
    assert before.hits
    bridge = corpus.vault_root / "bridge.md"
    original = bridge.read_text(encoding="utf-8")
    bridge.write_text("# Junction\n\nNo explicit relationship.\n", encoding="utf-8")
    IngestService(database).ingest(corpus)
    after_edit = explain_search(database, corpus.corpus_id, "Distant", subject="Cobalt")
    assert after_edit.hits == []
    assert [(e.entity_id, e.distance) for e in after_edit.subject_scope] == [
        ("cobalt", 0), ("junction", 1)
    ]
    bridge.write_text(original, encoding="utf-8")
    IngestService(database).ingest(corpus)
    assert explain_search(database, corpus.corpus_id, "Distant", subject="Cobalt").hits
    bridge.unlink()
    IngestService(database).ingest(corpus)
    assert explain_search(database, corpus.corpus_id, "Distant", subject="Cobalt").hits == []


def test_graph_paths_are_undirected(corpus: CorpusManifest) -> None:
    report = explain_search(
        Database(corpus.database), corpus.corpus_id, "private", subject="Ledger"
    )
    assert len(report.hits) == 1
    reason = next(r for r in report.hits[0].subject_reasons if r.kind == "exact_mention")
    assert reason.entity_id == "cobalt"
    assert reason.distance == 2
    assert len(reason.supporting_edge_ids) == 2


@pytest.mark.parametrize("format_", ["json", "text"])
def test_lexical_quotes_are_separately_opted_in(corpus: CorpusManifest, format_: str) -> None:
    def render(include_quotes: bool) -> str:
        report = explain_search(
            Database(corpus.database), corpus.corpus_id, "private", subject="Cobalt",
            include_quotes=include_quotes,
        )
        return (
            report.model_dump_json(exclude_none=True)
            if format_ == "json" else render_search_explanation(report)
        )

    redacted = render(False)
    assert PRIVATE_QUOTE not in redacted
    assert "Cobalt" in redacted
    if format_ == "json":
        assert "quote" not in json.loads(redacted)["hits"][0]
    included = render(True)
    assert PRIVATE_QUOTE in included
    if format_ == "json":
        assert json.loads(included)["hits"][0]["quote"] == PRIVATE_QUOTE
    else:
        assert "FTS expression:" in redacted
        assert "not confidence" in redacted
        assert "exact_mention" in redacted


def test_lexical_search_shape_unchanged(corpus: CorpusManifest) -> None:
    results = RetrievalService(Database(corpus.database), corpus.corpus_id).search("private")
    payload = [result.model_dump(mode="json") for result in results]
    assert isinstance(payload, list)
    assert payload[0]["quote"] == PRIVATE_QUOTE
    assert set(payload[0]) == set(SearchResult.model_fields)


@pytest.mark.parametrize(
    ("flags", "error"),
    [
        (["--include-quotes"], "invalid_query"),
        (["--explain-limit", "50"], "invalid_query"),
        (["--explain", "--since", "not-a-date"], "invalid_query"),
        (["--explain", "--query-mode", "dense"], "invalid_query"),
        (["--explain", "--query-mode", "hybrid"], "invalid_query"),
        (["--explain", "--query-mode", "reranked"], "invalid_query"),
    ],
)
def test_invalid_flags_fail_before_model_work(
    corpus: CorpusManifest, monkeypatch: pytest.MonkeyPatch, flags: list[str], error: str
) -> None:
    def forbidden(*args: Any, **kwargs: Any) -> None:
        pytest.fail("Models must not be loaded for invalid arguments")

    monkeypatch.setattr("kg.retrieval.dense.SentenceTransformerEmbeddingProvider", forbidden)
    monkeypatch.setattr("kg.retrieval.rerank.SentenceTransformerCrossEncoderProvider", forbidden)
    result = RUNNER.invoke(app, [
        "search", "evidence", "--manifest", str(corpus.manifest_path), "--format", "json", *flags
    ])
    assert result.exit_code == 2
    assert json.loads(result.stderr)["error"] == error


def test_invalid_query_and_stale_index_are_not_explained(corpus: CorpusManifest) -> None:
    database = Database(corpus.database)
    with pytest.raises(SearchQueryError):
        explain_search(database, corpus.corpus_id, "!!!")
    with database.transaction() as connection:
        connection.execute("UPDATE lexical_projection SET version = 'obsolete'")
    with pytest.raises(SearchQueryError, match="missing or stale"):
        explain_search(database, corpus.corpus_id, "evidence")


def test_concurrent_commit_and_restore_are_rejected(
    corpus: CorpusManifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(corpus.database)
    original = explanation_module._scope_paths

    def mutate(
        service: RetrievalService, connection: sqlite3.Connection, subject: str
    ) -> dict[str, explanation_module.ScopeEntity]:
        paths = original(service, connection, subject)
        with database.transaction() as writer:
            writer.execute("UPDATE source_document SET title = title || ' changed'")
        with database.transaction() as writer:
            writer.execute("UPDATE source_document SET title = substr(title, 1, length(title) - 8)")
        return paths

    monkeypatch.setattr(explanation_module, "_scope_paths", mutate)
    with pytest.raises(SearchExplanationError) as error:
        explain_search(database, corpus.corpus_id, "evidence", subject="Cobalt")
    assert error.value.code == "search_state_changed"


def test_changes_between_search_and_attribution_are_rejected(
    corpus: CorpusManifest, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(corpus.database)
    original = RetrievalService.search

    def mutate(service: RetrievalService, *args: Any, **kwargs: Any) -> list[SearchResult]:
        results = original(service, *args, **kwargs)
        with database.transaction() as writer:
            writer.execute("UPDATE source_document SET is_active = 0")
        return results

    monkeypatch.setattr(RetrievalService, "search", mutate)
    with pytest.raises(SearchExplanationError) as error:
        explain_search(database, corpus.corpus_id, "evidence", subject="Cobalt")
    assert error.value.code == "search_state_changed"
