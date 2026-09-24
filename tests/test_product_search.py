from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from support.providers import Embeddings, Reranker, providers

from kg.config import load_manifest
from kg.db import Database
from kg.ingest import IngestService
from kg.models.manifest import CorpusManifest
from kg.retrieval import (
    ProductSearchExplanation,
    SearchExplanationError,
    SearchService,
    SearchStateChangedError,
)
from kg.retrieval._telemetry import execution_trace
from kg.retrieval.context import CONTEXTUAL_SOURCE_TEXT_VERSION, contextual_passage_text
from kg.retrieval.dense import (
    SOURCE_TEXT_VERSION,
    DenseIndexError,
    DenseRetrievalService,
    EmbeddingProfile,
)
from kg.retrieval.explain import explain_search
from kg.retrieval.rerank import RerankedRetrievalService, RerankerError
from kg.retrieval.service import RetrievalService, SearchQueryError


@pytest.fixture
def atlas(tmp_path: Path) -> CorpusManifest:
    original = load_manifest(Path("corpora/atlas.yml"))
    vault = tmp_path / "vault"
    shutil.copytree(original.vault_root, vault)
    manifest = original.model_copy(update={"vault_root": vault, "database": tmp_path / "kg.db"})
    assert IngestService(Database(manifest.database)).ingest(manifest).failed == 0
    return manifest


def index(
    manifest: CorpusManifest,
    embedding: Embeddings,
    *,
    contextual: bool = False,
) -> DenseRetrievalService:
    dense = DenseRetrievalService(
        Database(manifest.database),
        manifest.corpus_id,
        provider=embedding,
        profile=embedding.profile,
        contextual=contextual,
    )
    dense.build_index()
    return dense


def invoke(service: SearchService, explained: bool, **kwargs: Any) -> Any:
    method = service.explain_search if explained else service.search
    return method("archive signing certificate", **kwargs)


@pytest.mark.service
@pytest.mark.parametrize("profile", list(EmbeddingProfile))
@pytest.mark.parametrize("contextual", [False, True])
def test_actual_pipeline_parity_configuration_and_evidence(
    atlas: CorpusManifest,
    monkeypatch: pytest.MonkeyPatch,
    profile: EmbeddingProfile,
    contextual: bool,
) -> None:
    embedding, reranker = Embeddings(profile), Reranker()
    projection = index(atlas, embedding, contextual=contextual).build_index()
    loads = providers(monkeypatch, embedding, reranker)
    database = Database(atlas.database)
    service = SearchService(
        database, atlas.corpus_id, embedding_profile=profile, contextual=contextual
    )
    component = RerankedRetrievalService(
        database, atlas.corpus_id, embedding_profile=profile, contextual=contextual
    )
    assert loads == []
    options = {"subject": "Atlas", "limit": 7}
    expected = component.search("archive signing certificate", **options)
    assert expected
    actual = service.search("archive signing certificate", **options)
    assert actual == expected
    embedding.queries.clear()
    reranker.calls.clear()
    report = service.explain_search("archive signing certificate", include_quotes=True, **options)
    assert embedding.queries == ["archive signing certificate"]
    assert len(reranker.calls) == 1
    assert [(hit.record_id, hit.rank) for hit in report.hits] == [
        (result.record_id, result.rank) for result in actual
    ]
    assert report.configuration.projection_id == projection.projection_id
    assert report.configuration.projection_path == str(projection.index_path)
    assert report.configuration.embedding_profile == profile.value
    assert report.configuration.contextual is contextual
    assert report.configuration.source_text_version == (
        CONTEXTUAL_SOURCE_TEXT_VERSION if contextual else SOURCE_TEXT_VERSION
    )
    assert report.configuration.embedding_model.model_dump() == {
        "name": embedding.name,
        "revision": embedding.revision,
        "pipeline_version": embedding.pipeline_version,
    }
    assert report.configuration.reranker_model.name == reranker.name
    assert report.configuration.reranker_model.revision == reranker.revision
    assert report.configuration.reranker_model.pipeline_version == reranker.pipeline_version
    retrieval = RetrievalService(database, atlas.corpus_id)
    for result, hit in zip(actual, report.hits, strict=True):
        anchor = retrieval.source_range(result.anchor_id)
        assert result.quote == hit.quote == anchor.quote
        assert result.source_revision_id == hit.source_revision_id == anchor.source_revision_id
        source = (atlas.vault_root / result.source_path).read_text()
        assert source[anchor.start_offset : anchor.end_offset] == result.quote
        assert retrieval.source_context(result.anchor_id).selected == anchor
        assert retrieval.evidence(result.record_id).anchor_id == result.anchor_id
        text = (
            contextual_passage_text(result.quote, result.title, result.heading_path)
            if contextual
            else result.quote
        )
        assert result.rank == Reranker.value(text)
    assert execution_trace.get() is None


@pytest.mark.service
def test_full_union_trace_nulls_truncation_and_single_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vault = tmp_path / "large"
    vault.mkdir()
    for number in range(120):
        text = "keyword" if number < 60 else "meaning"
        if number < 10:
            text += " meaning keyword"
        (vault / f"{number:03}.md").write_text(f"# Document\n\n{text} item {number}\n")
    manifest = load_manifest(Path("corpora/atlas.yml")).model_copy(
        update={
            "vault_root": vault,
            "database": tmp_path / "large.db",
            "seed_entities": [],
        }
    )
    database = Database(manifest.database)
    IngestService(database).ingest(manifest)
    embedding, reranker = Embeddings(), Reranker()
    index(manifest, embedding)
    loads = providers(monkeypatch, embedding, reranker)
    service = SearchService(database, manifest.corpus_id)
    report = service.explain_search("keyword", limit=5, trace_limit=200)
    assert loads == ["embedding", "reranker"]
    assert embedding.queries == ["keyword"]
    assert len(reranker.calls) == 1
    assert report.report_version == "2"
    assert report.lexical_expression == '"keyword"'
    counts = report.stage_counts
    assert counts.lexical_candidates == counts.dense_candidates == 50
    assert 50 < counts.deduplicated_union < 100
    assert counts.fused_shortlist == counts.reranked_candidates == 50
    assert counts.returned_hits == 5
    assert len(report.candidates) == report.total_candidates == counts.deduplicated_union
    assert not report.truncated
    assert report.configuration.lexical_candidate_limit == 50
    assert report.configuration.dense_candidate_limit == 50
    assert report.configuration.reranker_candidate_limit == 50
    assert (report.configuration.lexical_weight, report.configuration.dense_weight) == (1.0, 0.5)
    assert report.configuration.rrf_k == 20
    assert any(c.lexical is None for c in report.candidates)
    assert any(c.dense is None for c in report.candidates)
    assert any(c.lexical and c.dense for c in report.candidates)
    assert any(c.reranker is None for c in report.candidates)
    assert len({candidate.record_id for candidate in report.candidates}) == len(report.candidates)
    for candidate in report.candidates:
        terms = [
            weight / (20 + stage.position) if stage else None
            for weight, stage in [(1.0, candidate.lexical), (0.5, candidate.dense)]
        ]
        assert candidate.fusion.lexical_contribution == terms[0]
        assert candidate.fusion.dense_contribution == terms[1]
        assert candidate.fusion.score == sum(term for term in terms if term is not None)
        assert candidate.fusion.selected_for_reranking == (candidate.fusion.position <= 50)
        assert (candidate.reranker is not None) == candidate.fusion.selected_for_reranking
        if candidate.reranker:
            assert candidate.final_position == (
                candidate.reranker.position if candidate.reranker.position <= 5 else None
            )
    expected_order = sorted(
        report.candidates,
        key=lambda c: (0, c.reranker.position) if c.reranker else (1, c.fusion.position),
    )
    assert report.candidates == expected_order
    assert [c.record_id for c in report.candidates[:5]] == [h.record_id for h in report.hits]
    assert [h.rank for h in report.hits] == sorted([h.rank for h in report.hits], reverse=True)
    assert any(c.reranker and c.reranker.position != c.fusion.position for c in report.candidates)
    payload = json.loads(report.model_dump_json())
    assert ProductSearchExplanation.model_validate(payload) == report
    for candidate, encoded in zip(report.candidates, payload["candidates"], strict=True):
        for stage in ("lexical", "dense", "reranker", "final_position"):
            if getattr(candidate, stage) is None:
                assert encoded[stage] is None
        assert "quote" not in encoded
        for stage in ("lexical", "dense"):
            if getattr(candidate, stage) is None:
                assert encoded["fusion"][f"{stage}_contribution"] is None
    assert all("quote" not in hit for hit in payload["hits"])
    ordinary = service.search("keyword", limit=5)
    for trace_limit in (1, 3, 50, 200):
        for quotes in (False, True):
            bounded = service.explain_search(
                "keyword",
                limit=5,
                trace_limit=trace_limit,
                include_quotes=quotes,
            )
            assert bounded.stage_counts == counts
            assert bounded.configuration == report.configuration
            assert bounded.candidates == report.candidates[:trace_limit]
            assert bounded.displayed_candidates == min(trace_limit, report.total_candidates)
            assert bounded.truncated == (trace_limit < report.total_candidates)
            assert [(h.record_id, h.rank) for h in bounded.hits] == [
                (r.record_id, r.rank) for r in ordinary
            ]
            assert [h.quote for h in bounded.hits] == [
                r.quote if quotes else None for r in ordinary
            ]
            assert len(bounded.hits) == 5
    assert loads == ["embedding", "reranker"]
    expanded = service.explain_search("keyword", limit=75, trace_limit=200)
    assert expanded.configuration.lexical_candidate_limit == 75
    assert expanded.configuration.dense_candidate_limit == 75
    assert expanded.configuration.reranker_candidate_limit == 75
    assert expanded.stage_counts.reranked_candidates == 75
    assert len(expanded.hits) == 75


@pytest.mark.service
@pytest.mark.parametrize("explained", [False, True])
@pytest.mark.parametrize("empty", ["corpus", "source", "subject", "date"])
@pytest.mark.parametrize("failure", ["embedding", "reranker", "none"])
def test_empty_search_requires_both_providers(
    atlas: CorpusManifest,
    monkeypatch: pytest.MonkeyPatch,
    explained: bool,
    empty: str,
    failure: str,
) -> None:
    if empty == "corpus":
        atlas = atlas.model_copy(update={"include": ["missing-*.md"]})
        IngestService(Database(atlas.database)).ingest(atlas)
    embedding, reranker = Embeddings(), Reranker()
    index(atlas, embedding)
    loads = providers(monkeypatch, embedding, reranker)
    attempts: list[str] = []

    def unavailable(*args: Any) -> Any:
        attempts.append(failure)
        error = DenseIndexError if failure == "embedding" else RerankerError
        raise error("model unavailable")

    if failure != "none":
        module, provider = (
            ("dense", "SentenceTransformerEmbeddingProvider")
            if failure == "embedding"
            else ("rerank", "SentenceTransformerCrossEncoderProvider")
        )
        monkeypatch.setattr(f"kg.retrieval.{module}.{provider}", unavailable)
    service = SearchService(Database(atlas.database), atlas.corpus_id)
    filters: dict[str, Any] = {
        "source": {"source_path": "missing.md"},
        "subject": {"subject": "No such subject"},
        "date": {"since": datetime(2099, 1, 1, tzinfo=UTC)},
        "corpus": {},
    }[empty]
    if failure != "none":
        error = DenseIndexError if failure == "embedding" else RerankerError
        for _ in range(2):
            with pytest.raises(error, match="approved"):
                invoke(service, explained, **filters)
        assert attempts == [failure, failure]
        if failure == "reranker":
            assert loads == ["embedding"]
    else:
        result = invoke(service, explained, **filters)
        assert (result.hits if explained else result) == []
        assert loads == ["embedding", "reranker"]
        assert len(embedding.queries) == 1
        if explained:
            assert result.candidates == []
            assert result.total_candidates == result.displayed_candidates == 0
            assert not result.truncated
            assert all(value == 0 for value in result.stage_counts.model_dump().values())
            assert result.configuration.projection_id
    assert reranker.calls == []


@pytest.mark.service
@pytest.mark.parametrize("explained", [False, True])
@pytest.mark.parametrize("empty", [False, True])
@pytest.mark.parametrize("state", ["missing", "stale", "incompatible"])
def test_unavailable_projection_never_degrades(
    atlas: CorpusManifest,
    monkeypatch: pytest.MonkeyPatch,
    explained: bool,
    empty: bool,
    state: str,
) -> None:
    embedding, reranker = Embeddings(), Reranker()
    if state != "missing":
        index(atlas, embedding)
    if state == "stale":
        path = atlas.vault_root / "04-audit-trail.md"
        path.write_text(path.read_text() + "\nNew evidence.\n")
        IngestService(Database(atlas.database)).ingest(atlas)
    elif state == "incompatible":
        embedding.pipeline_version = "incompatible-runtime"
    providers(monkeypatch, embedding, reranker)
    service = SearchService(Database(atlas.database), atlas.corpus_id)
    with pytest.raises(DenseIndexError, match="kg dense-index"):
        invoke(service, explained, **({"source_path": "missing.md"} if empty else {}))
    assert reranker.calls == []


@pytest.mark.service
@pytest.mark.parametrize("explained", [False, True])
@pytest.mark.parametrize("contextual", [False, True])
def test_same_facade_edit_stale_rebuild_recovery(
    atlas: CorpusManifest,
    monkeypatch: pytest.MonkeyPatch,
    explained: bool,
    contextual: bool,
) -> None:
    embedding, reranker = Embeddings(), Reranker()
    dense = index(atlas, embedding, contextual=contextual)
    loads = providers(monkeypatch, embedding, reranker)
    database = Database(atlas.database)
    service = SearchService(database, atlas.corpus_id, contextual=contextual)
    before = invoke(service, explained, source_path="04-audit-trail.md")
    old_hit = (before.hits if explained else before)[0]
    path = atlas.vault_root / "04-audit-trail.md"
    path.write_text(path.read_text() + "\nNew archive signing certificate evidence.\n")
    IngestService(database).ingest(atlas)
    with pytest.raises(DenseIndexError, match="stale"):
        invoke(service, explained)
    with pytest.raises(DenseIndexError, match="stale"):
        invoke(service, not explained, source_path="missing.md")
    dense.build_index()
    after = invoke(service, explained, source_path="04-audit-trail.md")
    after_hits = after.hits if explained else after
    assert after_hits
    assert all(hit.source_revision_id != old_hit.source_revision_id for hit in after_hits)
    assert loads == ["embedding", "reranker"]
    retrieval = RetrievalService(database, atlas.corpus_id)
    assert (
        retrieval.source_range(old_hit.anchor_id).source_revision_id == old_hit.source_revision_id
    )
    comparison = retrieval.compare_revisions("04-audit-trail.md")
    assert comparison
    if explained:
        assert before.configuration.projection_id != after.configuration.projection_id


@pytest.mark.service
@pytest.mark.parametrize("explained", [False, True])
@pytest.mark.parametrize(
    "query,kwargs,error",
    [
        ("", {}, SearchQueryError),
        ("!!!", {}, SearchQueryError),
        ("valid", {"limit": 0}, ValueError),
        ("valid", {"limit": True}, ValueError),
        ("valid", {"limit": 1.5}, ValueError),
        ("valid", {"subject": 42}, SearchQueryError),
        ("valid", {"source_path": 42}, SearchQueryError),
        ("valid", {"since": "yesterday"}, SearchQueryError),
    ],
)
def test_invalid_arguments_precede_provider_initialization(
    atlas: CorpusManifest,
    monkeypatch: pytest.MonkeyPatch,
    explained: bool,
    query: str,
    kwargs: dict[str, Any],
    error: type[Exception],
) -> None:
    loads = providers(monkeypatch, Embeddings(), Reranker())
    service = SearchService(Database(atlas.database), atlas.corpus_id)
    method = service.explain_search if explained else service.search
    with pytest.raises(error):
        method(query, **kwargs)
    assert loads == []


@pytest.mark.service
@pytest.mark.parametrize("bound", [0, -1, 201, True, 1.5, "50"])
def test_invalid_trace_limit_precedes_readiness(
    atlas: CorpusManifest,
    monkeypatch: pytest.MonkeyPatch,
    bound: Any,
) -> None:
    loads = providers(monkeypatch, Embeddings(), Reranker())
    service = SearchService(Database(atlas.database), atlas.corpus_id)
    with pytest.raises(ValueError, match="trace_limit"):
        service.explain_search("certificate", trace_limit=bound)
    assert loads == []


@pytest.mark.service
@pytest.mark.parametrize("explained", [False, True])
@pytest.mark.parametrize("restore", [False, True])
@pytest.mark.parametrize("stage", ["embedding_load", "query", "reranker_load", "score"])
def test_intervening_ingestion_from_readiness_through_reranking_is_rejected(
    atlas: CorpusManifest,
    monkeypatch: pytest.MonkeyPatch,
    explained: bool,
    restore: bool,
    stage: str,
) -> None:
    database = Database(atlas.database)
    embedding, reranker = Embeddings(), Reranker()
    index(atlas, embedding)
    providers(monkeypatch, embedding, reranker)
    service = SearchService(database, atlas.corpus_id)
    retrieval = RetrievalService(database, atlas.corpus_id)
    fingerprint = retrieval.index_fingerprint()

    def mutate() -> None:
        path = atlas.vault_root / "04-audit-trail.md"
        original = path.read_text()
        path.write_text(original + "\nChanged evidence.\n")
        IngestService(database).ingest(atlas)
        if restore:
            path.write_text(original)
            IngestService(database).ingest(atlas)
            assert retrieval.index_fingerprint() == fingerprint

    if stage == "query":
        embedding.on_query = mutate
    elif stage == "score":
        reranker.on_score = mutate
    elif stage == "embedding_load":

        def load_embedding(profile: EmbeddingProfile) -> Embeddings:
            mutate()
            return embedding

        monkeypatch.setattr(
            "kg.retrieval.dense.SentenceTransformerEmbeddingProvider", load_embedding
        )
    else:

        def load_reranker() -> Reranker:
            mutate()
            return reranker

        monkeypatch.setattr(
            "kg.retrieval.rerank.SentenceTransformerCrossEncoderProvider", load_reranker
        )
    error = SearchExplanationError if explained else SearchStateChangedError
    with pytest.raises(error, match="retry") as raised:
        invoke(service, explained)
    assert raised.value.code == "search_state_changed"
    assert execution_trace.get() is None


@pytest.mark.service
def test_subject_scope_filters_corpus_isolation_and_legacy_operations(
    atlas: CorpusManifest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(atlas.database)
    embedding, reranker = Embeddings(), Reranker()
    index(atlas, embedding)
    loads = providers(monkeypatch, embedding, reranker)
    service = SearchService(database, atlas.corpus_id)
    retrieval = RetrievalService(database, atlas.corpus_id)
    assert retrieval.search("certificate")
    assert explain_search(database, atlas.corpus_id, "certificate").report_version == "1"
    assert retrieval.actions("Atlas")
    assert retrieval.status("Atlas")
    assert loads == []
    report = service.explain_search(
        "certificate",
        subject="Atlas",
        source_path="04-audit-trail.md",
        since=datetime(2026, 9, 21, tzinfo=UTC),
        include_quotes=True,
    )
    assert report.hits
    assert all(hit.source_path == "04-audit-trail.md" for hit in report.hits)
    assert all(hit.event_time and hit.event_time >= "2026-09-21" for hit in report.hits)
    audit = next(scope for scope in report.subject_scope if scope.entity_id == "audit-trail")
    assert audit.distance == 2
    assert len(audit.supporting_edge_ids) == len(audit.supporting_anchor_ids) == 2
    for edge_id, anchor_id in zip(
        audit.supporting_edge_ids, audit.supporting_anchor_ids, strict=True
    ):
        assert retrieval.evidence(edge_id).anchor_id == anchor_id
    assert any(
        reason.distance == 2 and reason.supporting_edge_ids == audit.supporting_edge_ids
        for hit in report.hits
        for reason in hit.subject_reasons
    )
    assert report.active_current_revisions_only
    assert not report.supersession_filter_applied
    assert (
        service.search("certificate", subject="Atlas", source_path="05-atlascope-planning.md") == []
    )
    assert (
        service.search(
            "certificate", source_path="04-audit-trail.md", since=datetime(2026, 9, 22, tzinfo=UTC)
        )
        == []
    )
    before = service.search("certificate")
    other = atlas.model_copy(update={"corpus_id": "other-corpus"})
    IngestService(database).ingest(other)
    assert service.search("certificate") == before


@pytest.mark.service
@pytest.mark.parametrize("explained", [False, True])
@pytest.mark.parametrize("stage", ["embedding", "reranker"])
def test_nonempty_execution_failure_has_no_fallback_and_can_recover(
    atlas: CorpusManifest,
    monkeypatch: pytest.MonkeyPatch,
    explained: bool,
    stage: str,
) -> None:
    embedding, reranker = Embeddings(), Reranker()
    index(atlas, embedding)
    loads = providers(monkeypatch, embedding, reranker)
    service = SearchService(Database(atlas.database), atlas.corpus_id)
    error = DenseIndexError if stage == "embedding" else RerankerError

    def fail() -> None:
        raise error("execution failed")

    if stage == "embedding":
        embedding.on_query = fail
    else:
        reranker.on_score = fail
    with pytest.raises(error, match="execution failed"):
        invoke(service, explained)
    assert execution_trace.get() is None
    embedding.on_query = reranker.on_score = lambda: None
    result = invoke(service, explained)
    assert result.hits if explained else result
    assert loads == ["embedding", "reranker"]


@pytest.mark.service
@pytest.mark.parametrize("explained", [False, True])
@pytest.mark.parametrize("stage", ["embedding", "reranker"])
def test_initialization_failure_recovers_on_same_facade(
    atlas: CorpusManifest,
    monkeypatch: pytest.MonkeyPatch,
    explained: bool,
    stage: str,
) -> None:
    embedding, reranker = Embeddings(), Reranker()
    index(atlas, embedding)
    service = SearchService(Database(atlas.database), atlas.corpus_id)
    error = DenseIndexError if stage == "embedding" else RerankerError
    providers(monkeypatch, embedding, reranker)

    def fail(*args: Any) -> Any:
        raise error("initialization failed")

    target = (
        "kg.retrieval.dense.SentenceTransformerEmbeddingProvider"
        if stage == "embedding"
        else "kg.retrieval.rerank.SentenceTransformerCrossEncoderProvider"
    )
    monkeypatch.setattr(target, fail)
    with pytest.raises(error, match="initialization failed"):
        invoke(service, explained, source_path="missing.md")
    loads = providers(monkeypatch, embedding, reranker)
    result = invoke(service, explained, source_path="missing.md")
    assert (result.hits if explained else result) == []
    assert loads == (["embedding", "reranker"] if stage == "embedding" else ["reranker"])


@pytest.mark.service
@pytest.mark.parametrize("explained", [False, True])
@pytest.mark.parametrize("stage", ["embedding", "reranker"])
def test_empty_readiness_commit_and_restore_are_rejected(
    atlas: CorpusManifest,
    monkeypatch: pytest.MonkeyPatch,
    explained: bool,
    stage: str,
) -> None:
    database = Database(atlas.database)
    embedding, reranker = Embeddings(), Reranker()
    index(atlas, embedding)
    providers(monkeypatch, embedding, reranker)
    service = SearchService(database, atlas.corpus_id)
    retrieval = RetrievalService(database, atlas.corpus_id)
    fingerprint = retrieval.index_fingerprint()

    def mutate() -> None:
        with database.transaction() as writer:
            writer.execute("UPDATE source_document SET title = title || ' changed'")
        with database.transaction() as writer:
            writer.execute("UPDATE source_document SET title = substr(title, 1, length(title) - 8)")
        assert retrieval.index_fingerprint() == fingerprint

    if stage == "embedding":

        def load_embedding(profile: EmbeddingProfile) -> Embeddings:
            mutate()
            return embedding

        monkeypatch.setattr(
            "kg.retrieval.dense.SentenceTransformerEmbeddingProvider",
            load_embedding,
        )
    else:

        def load_reranker() -> Reranker:
            mutate()
            return reranker

        monkeypatch.setattr(
            "kg.retrieval.rerank.SentenceTransformerCrossEncoderProvider",
            load_reranker,
        )
    error = SearchExplanationError if explained else SearchStateChangedError
    with pytest.raises(error, match="retry") as raised:
        invoke(service, explained, source_path="missing.md")
    assert raised.value.code == "search_state_changed"
    assert reranker.calls == []


@pytest.mark.service
@pytest.mark.parametrize("assembly", ["attribution", "report"])
def test_commits_during_explanation_assembly_are_rejected(
    atlas: CorpusManifest,
    monkeypatch: pytest.MonkeyPatch,
    assembly: str,
) -> None:
    from kg.retrieval import search as search_module

    database = Database(atlas.database)
    embedding, reranker = Embeddings(), Reranker()
    index(atlas, embedding)
    providers(monkeypatch, embedding, reranker)
    service = SearchService(database, atlas.corpus_id)

    def mutate() -> None:
        with database.transaction() as writer:
            writer.execute("UPDATE source_document SET title = title || ' changed'")
        with database.transaction() as writer:
            writer.execute("UPDATE source_document SET title = substr(title, 1, length(title) - 8)")

    if assembly == "attribution":
        original = search_module._scope_paths

        def scope(*args: Any) -> Any:
            result = original(*args)
            mutate()
            return result

        monkeypatch.setattr(search_module, "_scope_paths", scope)
    else:
        configuration = service._configuration

        def configure(*args: Any) -> Any:
            result = configuration(*args)
            mutate()
            return result

        monkeypatch.setattr(service, "_configuration", configure)
    with pytest.raises(SearchExplanationError, match="retry") as raised:
        service.explain_search("certificate", subject="Atlas")
    assert raised.value.code == "search_state_changed"
    assert execution_trace.get() is None


@pytest.mark.service
def test_component_scores_and_ties_are_recorded_without_rerunning(
    atlas: CorpusManifest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(atlas.database)
    embedding, reranker = Embeddings(), Reranker()
    dense = index(atlas, embedding)
    providers(monkeypatch, embedding, reranker)
    monkeypatch.setattr(reranker, "value", lambda text: 1.0)
    service = SearchService(database, atlas.corpus_id)
    report = service.explain_search("certificate", subject="Atlas", limit=10, trace_limit=200)
    assert embedding.queries == ["certificate"]
    assert len(reranker.calls) == 1
    lexical = RetrievalService(database, atlas.corpus_id).search(
        "certificate",
        subject="Atlas",
        limit=50,
        query_mode="natural",
    )
    semantic = dense.search("certificate", subject="Atlas", limit=50)
    entries = {entry.record_id: entry for entry in report.candidates}
    for stage, results in (("lexical", lexical), ("dense", semantic)):
        assert len(results) == sum(getattr(entry, stage) is not None for entry in entries.values())
        for position, result in enumerate(results, start=1):
            assert getattr(entries[result.record_id], stage).model_dump() == {
                "position": position,
                "score": result.rank,
            }
    assert [hit.record_id for hit in report.hits] == sorted(hit.record_id for hit in report.hits)
    assert all(hit.rank == 1.0 for hit in report.hits)
    assert report.tie_breakers == {
        "lexical": ["source_path", "start_offset", "passage_id"],
        "dense": ["passage_id"],
        "fusion": ["record_id"],
        "reranker": ["record_id"],
    }
    assert "L2" in report.score_semantics["dense"]
    assert "not confidence" in report.score_semantics["reranker"]


@pytest.mark.service
def test_configuration_and_quote_arguments_fail_without_models(
    atlas: CorpusManifest,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loads = providers(monkeypatch, Embeddings(), Reranker())
    database = Database(atlas.database)
    with pytest.raises(ValueError):
        SearchService(database, atlas.corpus_id, embedding_profile="invalid")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="contextual"):
        SearchService(database, atlas.corpus_id, contextual="yes")  # type: ignore[arg-type]
    service = SearchService(database, atlas.corpus_id)
    with pytest.raises(ValueError, match="include_quotes"):
        service.explain_search("certificate", include_quotes="yes")  # type: ignore[arg-type]
    with pytest.raises(SearchQueryError, match="string"):
        service.search(None)  # type: ignore[arg-type]
    assert loads == []
