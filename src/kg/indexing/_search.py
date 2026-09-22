"""Snapshot-local full retrieval. No canonical writes, fallback, or owning release."""

from __future__ import annotations

import heapq
import math
import re
import sqlite3
import struct
import sys
import unicodedata
from dataclasses import dataclass, field
from typing import Literal

from kg._execution_budget import PrivateResourceStop
from kg.diagnostics._targets import DocumentTarget as ReportDocument
from kg.diagnostics._targets import EvidenceTarget, ReportTargets
from kg.evidence import _reporting
from kg.evidence._coordination import DocumentTarget
from kg.evidence._read_context import CanonicalReadContext
from kg.evidence._reads import evidence_view
from kg.evidence.errors import EvidenceServiceError
from kg.indexing import _configuration, _inspection, _vectors
from kg.indexing._process import CaptureType, ProviderFactory
from kg.indexing._selection import ProjectionHandle, ProjectionMember, RankedSelection
from kg.models.foundation import EvidenceRef, RankedEvidence, RankedResult
from kg.models.indexing import (
    CanonicalSearchHit,
    CanonicalSearchResult,
    ExecutionIdentity,
    IndexConfiguration,
)
from kg.models.indexing_events import IndexCandidate, IndexPhase
from kg.retrieval.dense import DenseIndexError, EmbeddingProfile
from kg.retrieval.rerank import (
    MODEL_NAME,
    MODEL_REVISION,
    SCORING_PIPELINE_VERSION,
    RerankerError,
    RerankerProvider,
    _coerce_score,
)


@dataclass(frozen=True)
class SearchSelection:
    """Provisional output; the caller still owns snapshot lifetime and final release."""

    ranked: RankedSelection
    response: CanonicalSearchResult


@dataclass
class Candidate:
    passage_id: str
    projection_id: str
    lexical_rank: int | None = None
    lexical_score: float | None = None
    dense_rank: int | None = None
    dense_score: float | None = None
    fused_score: float = 0
    fused_rank: int = 0
    rerank_score: float | None = None
    rerank_rank: int | None = None
    reference: EvidenceRef | None = None


@dataclass
class DenseCandidate:
    score: float
    passage_id: str
    projection_id: str

    def __lt__(self, other: DenseCandidate) -> bool:
        return (self.score < other.score or (
            self.score == other.score and self.passage_id > other.passage_id
        ))


@dataclass
class Pipeline:
    context: CanonicalReadContext
    query: str
    configuration: IndexConfiguration
    limit: int
    provider_factory: ProviderFactory
    reranker: RerankerProvider
    capture: CaptureType
    members: dict[str, ProjectionMember] = field(default_factory=dict)
    populated: int = 0
    lexical_truncated: bool = False
    dense_truncated: bool = False

    @property
    def pool(self) -> int:
        return max(50, self.limit)

    def phase(
        self, phase: Literal["admission", "readiness", "lexical", "dense", "fusion", "rerank"],
        status: Literal["started", "complete", "ready"], *,
        logical_configuration_id: str | None = None,
        identity: ExecutionIdentity | None = None,
    ) -> None:
        with self.capture.guard():
            self.capture.append(IndexPhase(
                phase=phase, status=status, logical_configuration_id=logical_configuration_id,
                observed_configuration_id=(
                    _configuration.identity_hash(identity) if identity is not None else None
                ),
            ))

    def run(self) -> SearchSelection:
        context = self.context
        context.check_active()
        budget = context.meter.private_budget
        context._hold_scratch(64 * len(self.query) + 65536, "general")
        config_id = _configuration.configuration_id(self.configuration)
        with self.capture.guard():
            self.capture.configure(config_id, ReportTargets())
        self.phase("admission", "started", logical_configuration_id=config_id)
        try:
            provider = self.provider_factory(EmbeddingProfile(self.configuration.embedding_profile))
            context.check_active()
            identity = _configuration.execution_identity(self.configuration, provider)
            if (
                self.reranker.name != MODEL_NAME or self.reranker.revision != MODEL_REVISION
                or not self.reranker.pipeline_version.startswith(SCORING_PIPELINE_VERSION + "|")
                or len(self.reranker.pipeline_version) > 2048
            ):
                raise EvidenceServiceError("unsupported")
        except (TypeError, ValueError, AttributeError):
            raise EvidenceServiceError("internal_error") from None
        self.phase("admission", "complete", logical_configuration_id=config_id, identity=identity)
        connection = context.connection
        connection.execute(
            "CREATE VIRTUAL TABLE temp.e3_search USING fts5("
            "quote, passage_id UNINDEXED, projection_id UNINDEXED,"
            "tokenize='unicode61 remove_diacritics 0')"
        )
        connection.execute(
            "CREATE VIRTUAL TABLE temp.e3_probe USING fts5("
            "quote, tokenize='unicode61 remove_diacritics 0')"
        )
        expression = self.expression()
        # UTF-8 bytes bound tokenizer positions, including the configured query prompt.
        positions = len(self.query.encode()) + len(identity.query_encoding.encode()) + 32
        context._hold_scratch(identity.dimensions * 64, "vector")
        with budget.reserve_provider(
            positions * 32 + identity.dimensions * 64, unit="vector",
            sequences=1, padded_token_positions=positions,
        ):
            try:
                query_blob = _vectors.encode(provider.encode_query(self.query), identity.dimensions)
            except (TypeError, ValueError, AttributeError):
                raise EvidenceServiceError("internal_error") from None
            context.check_active()
            vector = struct.unpack(f"<{identity.dimensions}f", query_blob)
        self.populate(identity, config_id)
        lexical = self.lexical(expression)
        dense = self.dense(vector, identity.dimensions)
        candidates = self.fuse(lexical, dense)
        selected = self.rerank(candidates)
        hits = []
        for item in selected[:self.limit]:
            assert item.reference is not None and item.rerank_score is not None
            member = self.members[item.projection_id]
            view = evidence_view(
                connection, context.scope, item.reference, member.document.state_version,
                context=context, stage="search_final_evidence",
            )
            hits.append(CanonicalSearchHit(evidence=view, score=item.rerank_score))
            with self.capture.guard():
                _reporting.retain_result(self.capture, view)
        with self.capture.guard():
            self.report_candidates(candidates, selected)
        projection = ProjectionHandle(
            context.session_id, context.scope, config_id, tuple(self.members.values()),
        )
        projection.check(context)
        response = CanonicalSearchResult(
            scope=context.scope, hits=tuple(hits), configuration_id=config_id,
            execution_identity=identity, reranker_pipeline=self.reranker.pipeline_version,
            read_state_id=context.session_id.value,
            projection_ids=tuple(self.members),
            candidate_limit=self.pool, lexical_truncated=self.lexical_truncated,
            dense_truncated=self.dense_truncated, fusion_truncated=len(candidates) > self.pool,
            sqlite_version=sqlite3.sqlite_version, unicode_version=unicodedata.unidata_version,
            python_version=sys.version.split()[0],
        )
        connection.execute("DROP TABLE temp.e3_probe")
        connection.execute("DROP TABLE temp.e3_search")
        context.check_active()
        return SearchSelection(
            RankedSelection(RankedResult(kind="ranked", hits=tuple(
                RankedEvidence(evidence=hit.evidence.reference, score=hit.score) for hit in hits
            )), projection),
            response,
        )

    def expression(self) -> str:
        tokens = list(dict.fromkeys(token.casefold() for token in re.findall(r"\w+", self.query)))
        accepted = []
        for token in tokens:
            self.context.connection.execute("DELETE FROM temp.e3_probe")
            self.context.connection.execute("INSERT INTO temp.e3_probe VALUES (?)", (token,))
            phrase = '"' + token.replace('"', '""') + '"'
            if self.context.connection.execute(
                "SELECT 1 FROM temp.e3_probe WHERE e3_probe MATCH ?", (phrase,),
            ).fetchone() is not None:
                accepted.append(phrase)
        if not accepted:
            raise EvidenceServiceError("invalid_request")
        return " OR ".join(accepted)

    def populate(self, identity: ExecutionIdentity, config_id: str) -> None:
        context = self.context
        connection, budget = context.connection, context.meter.private_budget
        self.phase("readiness", "started")
        for namespace in context.scope.access.namespaces:
            rows = connection.execute(
                "SELECT d.document_id,d.namespace,s.revision_id,s.state_version,"
                "s.namespace_token,n.policy_token FROM document d "
                "JOIN document_state s ON s.state_version=d.current_state "
                "JOIN source_namespace n ON n.corpus_id=d.corpus_id AND n.namespace=d.namespace "
                "WHERE d.corpus_id=? AND d.namespace=? AND s.source='active' "
                "ORDER BY d.document_id", (context.scope.corpus_id, namespace),
            )
            for doc in rows:
                if doc["namespace_token"] != doc["policy_token"]:
                    raise EvidenceServiceError("stale_index")
                projection = connection.execute(
                    "SELECT p.* FROM active_document_projection a JOIN document_projection p "
                    "ON p.projection_id=a.projection_id WHERE a.document_id=? "
                    "AND a.configuration_id=? AND p.state_version=?",
                    (doc["document_id"], config_id, doc["state_version"]),
                ).fetchone()
                if projection is None:
                    raise EvidenceServiceError("stale_index")
                # Cover the verifier's bounded manifest list as well as its per-row reservations.
                with budget.reserve_scratch(1 << 20, "general"):
                    observed = _inspection.verify(
                        connection, projection, self.configuration, budget,
                    )
                if observed != identity:
                    raise EvidenceServiceError("stale_index")
                context._hold_scratch(4096, "general")
                member = ProjectionMember(
                    document=DocumentTarget(
                        corpus_id=context.scope.corpus_id, namespace=namespace,
                        document_id=doc["document_id"], revision_id=doc["revision_id"],
                        state_version=doc["state_version"], namespace_token=doc["namespace_token"],
                    ),
                    projection_id=projection["projection_id"],
                    passage_set_id=projection["passage_set_id"],
                )
                self.members[member.projection_id] = member
                with self.capture.guard():
                    self.capture.retain(ReportTargets(values=(ReportDocument(
                        document_id=doc["document_id"], revision_id=doc["revision_id"],
                        state_version=doc["state_version"],
                    ),)))
                rows_to_insert = connection.execute(
                    "SELECT passage_id,length(CAST(lexical_input AS BLOB)) "
                    "FROM projection_member WHERE projection_id=? ORDER BY ordinal",
                    (member.projection_id,),
                )
                for passage_id, text_bytes in rows_to_insert:
                    context.meter.reserve_public("search_temp")
                    with budget.reserve_scratch(max(1, text_bytes * 16), "text"):
                        row = connection.execute(
                            "SELECT lexical_input FROM projection_member "
                            "WHERE projection_id=? AND passage_id=?",
                            (member.projection_id, passage_id),
                        ).fetchone()
                        if row is None:
                            raise EvidenceServiceError("internal_error")
                        connection.execute(
                            "INSERT INTO temp.e3_search(quote,passage_id,projection_id) "
                            "VALUES (?,?,?)", (row[0], passage_id, member.projection_id),
                        )
                    self.populated += 1
        self.phase("readiness", "ready")

    def lexical(self, expression: str) -> list[Candidate]:
        self.phase("lexical", "started")
        self.context._hold_scratch(self.pool * 2048, "general")
        result = []
        rows = self.context.connection.execute(
            "SELECT passage_id,projection_id,bm25(e3_search,1.0) AS score "
            "FROM temp.e3_search WHERE e3_search MATCH ? ORDER BY score,passage_id LIMIT ?",
            (expression, self.pool + 1),
        )
        for rank, row in enumerate(rows, 1):
            if rank > self.pool:
                self.lexical_truncated = True
                break
            self.context.meter.reserve_public("search_lexical")
            score = float(row["score"])
            if not math.isfinite(score):
                raise EvidenceServiceError("internal_error")
            result.append(Candidate(row["passage_id"], row["projection_id"], rank, score))
        self.phase("lexical", "complete")
        return result

    def dense(self, vector: tuple[float, ...], dimensions: int) -> list[DenseCandidate]:
        self.phase("dense", "started")
        context = self.context
        context._hold_scratch(self.pool * 2048, "general")
        heap: list[DenseCandidate] = []
        for projection_id in self.members:
            rows = context.connection.execute(
                "SELECT passage_id,length(vector),dimensions FROM projection_member "
                "WHERE projection_id=? ORDER BY ordinal", (projection_id,),
            )
            for passage_id, size, stored_dimensions in rows:
                if stored_dimensions != dimensions or size != dimensions * 4:
                    raise EvidenceServiceError("internal_error")
                context.meter.reserve_public("search_vector")
                with context.meter.reserve_scratch(dimensions * 96, "vector"):
                    row = context.connection.execute(
                        "SELECT vector FROM projection_member "
                        "WHERE projection_id=? AND passage_id=?",
                        (projection_id, passage_id),
                    ).fetchone()
                    if row is None:
                        raise EvidenceServiceError("internal_error")
                    values = struct.unpack(f"<{dimensions}f", row[0])
                    score = math.fsum(a * b for a, b in zip(vector, values, strict=True))
                    if not math.isfinite(score):
                        raise EvidenceServiceError("internal_error")
                    item = DenseCandidate(score, passage_id, projection_id)
                    if len(heap) < self.pool:
                        heapq.heappush(heap, item)
                    elif heap[0] < item:
                        heapq.heapreplace(heap, item)
        self.dense_truncated = self.populated > self.pool
        self.phase("dense", "complete")
        return sorted(heap, key=lambda item: (-item.score, item.passage_id))

    def fuse(self, lexical: list[Candidate], dense: list[DenseCandidate]) -> list[Candidate]:
        self.phase("fusion", "started")
        self.context._hold_scratch(self.pool * 8192, "general")
        union = {item.passage_id: item for item in lexical}
        for rank, dense_item in enumerate(dense, 1):
            item = union.setdefault(
                dense_item.passage_id, Candidate(dense_item.passage_id, dense_item.projection_id),
            )
            item.dense_score, item.dense_rank = dense_item.score, rank
        for item in union.values():
            item.fused_score = (
                (1 / (20 + item.lexical_rank) if item.lexical_rank is not None else 0)
                + (0.5 / (20 + item.dense_rank) if item.dense_rank is not None else 0)
            )
            member = self.members[item.projection_id]
            row = self.context.connection.execute(
                "SELECT anchor_id FROM passage WHERE passage_id=? AND passage_set_id=?",
                (item.passage_id, member.passage_set_id),
            ).fetchone()
            if row is None:
                raise EvidenceServiceError("internal_error")
            item.reference = EvidenceRef(
                corpus_id=self.context.scope.corpus_id,
                source_namespace=member.document.namespace, document_id=member.document.document_id,
                revision_id=member.document.revision_id,
                anchor_id=row[0], passage_id=item.passage_id,
            )
        result = sorted(union.values(), key=lambda item: (-item.fused_score, item.passage_id))
        for rank, item in enumerate(result, 1):
            item.fused_rank = rank
        self.phase("fusion", "complete")
        return result

    def rerank(self, candidates: list[Candidate]) -> list[Candidate]:
        self.phase("rerank", "started")
        context, budget = self.context, self.context.meter.private_budget
        # One sequence per call preserves pinned native encoding without cross-batch padding.
        for item in candidates[:self.pool]:
            member = self.members[item.projection_id]
            size_row = context.connection.execute(
                "SELECT length(CAST(lexical_input AS BLOB)) FROM projection_member "
                "WHERE projection_id=? AND passage_id=?", (item.projection_id, item.passage_id),
            ).fetchone()
            if size_row is None:
                raise EvidenceServiceError("internal_error")
            with budget.reserve_scratch(max(1, size_row[0] * 16), "text"):
                row = context.connection.execute(
                    "SELECT lexical_input FROM projection_member WHERE projection_id=? "
                    "AND passage_id=?", (item.projection_id, item.passage_id),
                ).fetchone()
                if row is None:
                    raise EvidenceServiceError("internal_error")
                with _inspection.title(
                    context.connection, member.document.document_id,
                    member.document.state_version, budget,
                ) as title:
                    positions = (
                        size_row[0]
                        + (len(title.encode()) + 2 if self.configuration.contextual else 0)
                        + len(self.query.encode()) + 32
                    )
                    with budget.reserve_provider(
                        positions * 32, unit="reranker", sequences=1,
                        padded_token_positions=positions,
                    ):
                        text = _inspection.representation(row[0], title, self.configuration)
                        context.meter.reserve_public("search_rerank")
                        scores = self.reranker.score(self.query, [text], batch_size=1)
                        context.check_active()
                        try:
                            if len(scores) != 1:
                                raise EvidenceServiceError("internal_error")
                            item.rerank_score = _coerce_score(scores[0])
                        except (TypeError, ValueError, RerankerError):
                            raise EvidenceServiceError("internal_error") from None
        selected = sorted(
            candidates[:self.pool],
            key=lambda item: (-float(item.rerank_score or 0), item.passage_id),
        )
        for rank, item in enumerate(selected, 1):
            item.rerank_rank = rank
        self.phase("rerank", "complete")
        return selected

    def report_candidates(self, candidates: list[Candidate], selected: list[Candidate]) -> None:
        for item in selected + candidates[self.pool:]:
            assert item.reference is not None
            with self.capture.guard():
                self.capture.append(IndexCandidate(
                    reference=item.reference,
                    lexical_member=item.lexical_rank is not None,
                    lexical_rank=item.lexical_rank, lexical_score=item.lexical_score,
                    dense_member=item.dense_rank is not None,
                    dense_rank=item.dense_rank, dense_score=item.dense_score,
                    lexical_contribution=(
                        1 / (20 + item.lexical_rank) if item.lexical_rank is not None else None
                    ),
                    dense_contribution=(
                        0.5 / (20 + item.dense_rank) if item.dense_rank is not None else None
                    ),
                    fused_rank=item.fused_rank, fused_score=item.fused_score,
                    shortlisted=item.rerank_rank is not None,
                    rerank_member=item.rerank_rank is not None,
                    rerank_rank=item.rerank_rank, rerank_score=item.rerank_score,
                    final_rank=(
                        item.rerank_rank if item.rerank_rank is not None
                        and item.rerank_rank <= self.limit else None
                    ),
                    exclusion=(
                        "not_selected_for_reranking" if item.rerank_rank is None else
                        "below_return_limit" if item.rerank_rank > self.limit else None
                    ),
                ), ReportTargets(values=(EvidenceTarget(reference=item.reference),)))


def execute(pipeline: Pipeline) -> SearchSelection:
    try:
        return pipeline.run()
    except (DenseIndexError, RerankerError):
        raise EvidenceServiceError("unsupported") from None
    except _vectors.InvalidVector:
        raise EvidenceServiceError("internal_error") from None
    except (MemoryError, OverflowError):
        raise PrivateResourceStop() from None
