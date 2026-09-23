"""Standalone lifecycle. Models run outside write locks; every mutation is fenced."""

from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from typing import Literal

from kg._execution_budget import DeadlineStop, PrivateBudget, PrivateResourceStop
from kg.diagnostics._collector import Capture, CaptureUnavailable
from kg.diagnostics._targets import ReportTargets, WriterTarget
from kg.evidence._transactions import CanonicalWriteContext, writing
from kg.evidence._values import now, sha, timestamp, token
from kg.evidence.database import EvidenceDatabase
from kg.evidence.errors import EvidenceServiceError
from kg.ids import digest
from kg.indexing import _configuration, _inspection, _passages, _storage, _vectors
from kg.models.evidence import LocalIdentity
from kg.models.foundation import Attribution, ExternalDocument, Scope
from kg.models.indexing import (
    DEFAULT_CONFIGURATION,
    ExecutionIdentity,
    IndexConfiguration,
    IndexReason,
    ProcessResult,
)
from kg.models.indexing_events import IndexPhase
from kg.retrieval.dense import DenseIndexError, EmbeddingProfile, EmbeddingProvider

CaptureType = Capture | CaptureUnavailable
ProviderFactory = Callable[[EmbeddingProfile], EmbeddingProvider]


class ProcessFailure(Exception):
    def __init__(self, reason: IndexReason) -> None:
        self.reason = reason


class Process:
    def __init__(
        self,
        database: EvidenceDatabase,
        identity: LocalIdentity,
        scope: Scope,
        attribution: Attribution,
        document_id: str,
        expected_state: str,
        configuration: IndexConfiguration,
        mode: Literal["incremental", "rebuild"],
        provider_factory: ProviderFactory,
        budget: PrivateBudget,
        capture: CaptureType,
    ) -> None:
        self.database, self.identity, self.scope = database, identity, scope
        self.attribution, self.document_id, self.expected_state = (
            attribution,
            document_id,
            expected_state,
        )
        self.configuration, self.mode, self.provider_factory = configuration, mode, provider_factory
        self.budget, self.capture = budget, capture
        self.config_id = _configuration.configuration_id(configuration)
        self.attempt: _storage.Attempt | None = None
        self.context: CanonicalWriteContext | None = None
        self.source_bytes = 0
        self.phase: Literal["admission", "passage", "vector", "publication"] = "admission"

    def event(self, phase: IndexPhase) -> None:
        with self.capture.guard():
            self.capture.append(phase)

    @contextmanager
    def transaction(self, *, admission: bool = False) -> Iterator[CanonicalWriteContext]:
        with writing(self.database, self.identity, budget=self.budget) as context:
            self.context = context
            if not admission:
                assert self.attempt is not None
                _storage.guard(
                    context.connection,
                    self.identity,
                    self.scope,
                    self.attribution,
                    self.attempt,
                )
            yield context

    def run(self) -> ProcessResult:
        try:
            with self.transaction(admission=True) as context:
                self.attempt = _storage.admit(
                    context.connection,
                    self.identity,
                    self.scope,
                    self.attribution,
                    self.document_id,
                    self.expected_state,
                    self.configuration,
                )
                size = context.connection.execute(
                    "SELECT r.byte_length,CASE WHEN s.passage_policy='supplied-anchors/1' "
                    "THEN coalesce((SELECT sum(length(CAST(a.quote AS BLOB))) "
                    "FROM anchor_set_member m JOIN anchor a ON a.anchor_id=m.anchor_id "
                    "WHERE m.set_id=s.set_id),0) ELSE 0 END "
                    "FROM document_state s JOIN revision r ON r.revision_id=s.revision_id "
                    "WHERE s.state_version=?",
                    (self.expected_state,),
                ).fetchone()
                if size is None:
                    raise EvidenceServiceError("internal_error")
                self.source_bytes = size[0] + size[1]
                doc = _storage.owner(
                    context.connection,
                    self.identity,
                    self.scope,
                    self.attribution,
                    self.document_id,
                )
                with self.capture.guard():
                    self.capture.retain(
                        ReportTargets(
                            values=(
                                WriterTarget(
                                    document=ExternalDocument(
                                        source_namespace=doc["namespace"],
                                        synchronization_scope=doc["synchronization_scope"],
                                        external_id=doc["external_id"],
                                    ),
                                    owner_id=self.attribution.owner_id,
                                    writer_id=self.attribution.writer_id,
                                ),
                            )
                        )
                    )
            with self.capture.guard():
                self.event(
                    IndexPhase(
                        phase="admission",
                        status="complete",
                        logical_configuration_id=self.config_id,
                    )
                )
            self.drain()
            return self.produce()
        except _passages.PassagePolicyError:
            return self.fail("unsupported_policy")
        except ProcessFailure as error:
            return self.fail(error.reason)
        except EvidenceServiceError as error:
            if (
                error.failure.code == "internal_error"
                and self.attempt is not None
                and self.context is not None
                and self.context.commit_outcome == "confirmed_rolled_back"
            ):
                return self.fail("storage_failure")
            if error.failure.code != "state_conflict":
                raise
            return self.stale(
                "superseded_attempt" if isinstance(error, _storage.Superseded) else "state_changed"
            )

    def stale(self, reason: IndexReason) -> ProcessResult:
        self.capture.group.redact(reason)
        return ProcessResult(
            document_id=self.document_id,
            state_version=self.expected_state,
            configuration_id=self.config_id,
            attempt_id=self.attempt.attempt_id if self.attempt else None,
            outcome="stale",
            reason=reason,
        )

    def drain(self) -> None:
        while True:
            with self.transaction() as context:
                result = _storage.cleanup(
                    context.connection,
                    self.scope,
                    self.document_id,
                    self.config_id,
                    1000,
                    preserve_previous=True,
                )
            if not result.has_more:
                return
            if result.removed == 0:
                raise EvidenceServiceError("internal_error")

    def fail(self, reason: IndexReason) -> ProcessResult:
        assert self.attempt is not None
        diagnostic_id = token()
        try:
            with self.transaction() as context:
                context.connection.execute(
                    "UPDATE index_attempt SET status='failed',reason=?,diagnostic_id=?,"
                    "completed_at=? "
                    "WHERE attempt_id=?",
                    (reason, diagnostic_id, timestamp(now()), self.attempt.attempt_id),
                )
                if self.configuration == DEFAULT_CONFIGURATION:
                    status = _inspection.status(
                        context.connection,
                        self.identity,
                        self.scope,
                        self.document_id,
                        self.configuration,
                        self.budget,
                    )
                    context.connection.execute(
                        "UPDATE processing_state SET indexing=?,indexing_reason=? "
                        "WHERE state_version=?",
                        (
                            "ready" if status.status == "ready" else "failed",
                            "ready" if status.status == "ready" else reason,
                            self.expected_state,
                        ),
                    )
                cleanup_pending = _storage.obsolete(
                    context.connection,
                    self.scope,
                    self.document_id,
                    self.config_id,
                )
        except EvidenceServiceError as error:
            if error.failure.code != "state_conflict":
                raise
            return self.stale(
                "superseded_attempt" if isinstance(error, _storage.Superseded) else "state_changed"
            )
        with self.capture.guard():
            self.event(
                IndexPhase(
                    phase=self.phase,
                    status="failed",
                    reason=reason,
                )
            )
        return ProcessResult(
            document_id=self.document_id,
            state_version=self.expected_state,
            configuration_id=self.config_id,
            attempt_id=self.attempt.attempt_id,
            outcome="failed",
            reason=reason,
            cleanup_pending=cleanup_pending,
            diagnostic_id=diagnostic_id,
        )

    def produce(self) -> ProcessResult:
        assert self.attempt is not None
        self.phase = "passage"
        # Hold ownership for exact source, Unicode slices, hashes and the prepared boundaries.
        with (
            self.budget.reserve_scratch(
                max(4096, self.source_bytes * 16 + 1_000_000),
                "general",
            ),
            ExitStack() as buffers,
        ):
            prepared = _passages.prepare(
                self.database,
                self.identity,
                self.scope,
                self.attribution,
                self.document_id,
                self.expected_state,
                budget=self.budget,
            )
            with self.transaction() as context:
                published = _passages.publish(context, self.scope, self.attribution, prepared)
            with self.capture.guard():
                self.event(
                    IndexPhase(
                        phase="passage",
                        status=published.outcome,
                        passage_set_id=published.passage_set_id,
                    )
                )
            self.phase = "vector"
            try:
                self.budget.check_deadline()
                provider = self.provider_factory(
                    EmbeddingProfile(self.configuration.embedding_profile)
                )
                self.budget.check_deadline()
                identity = _configuration.execution_identity(self.configuration, provider)
            except DenseIndexError:
                raise ProcessFailure("provider_unavailable") from None
            except (TypeError, ValueError, AttributeError):
                raise ProcessFailure("invalid_provider_output") from None
            except EvidenceServiceError as error:
                if error.failure.code != "unsupported":
                    raise
                raise ProcessFailure("invalid_provider_output") from None
            with self.transaction() as context:
                context.connection.execute(
                    "UPDATE index_attempt SET status='staging',execution_identity_json=?,"
                    "execution_identity_hash=? WHERE attempt_id=?",
                    (
                        _configuration.identity_json(identity),
                        _configuration.identity_hash(identity),
                        self.attempt.attempt_id,
                    ),
                )
                previous = _storage.active(
                    context.connection,
                    self.scope,
                    self.document_id,
                    self.config_id,
                )
                previous_identity = (
                    _inspection.verify(
                        context.connection,
                        previous,
                        self.configuration,
                        self.budget,
                    )
                    if previous is not None and self.mode == "incremental"
                    else None
                )
                title = (
                    buffers.enter_context(
                        _inspection.title(
                            context.connection,
                            self.document_id,
                            self.expected_state,
                            self.budget,
                        )
                    )
                    if self.configuration.contextual
                    else ""
                )
                if (
                    self.mode == "incremental"
                    and previous is not None
                    and previous["state_version"] == self.expected_state
                    and previous_identity == identity
                ):
                    projection_id = previous["projection_id"]
                    self.finish(context, "unchanged")
                else:
                    projection_id = None
            if projection_id is not None:
                with self.capture.guard():
                    self.event(IndexPhase(phase="publication", status="unchanged"))
                return self.result(
                    prepared,
                    published,
                    projection_id,
                    0,
                    len(prepared.boundaries),
                    self.cleanup_after_commit(),
                )
            reuse_projection = (
                previous["projection_id"]
                if self.mode == "incremental"
                and previous is not None
                and previous_identity == identity
                else None
            )
            produced, reused = self.stage(prepared, provider, identity, title, reuse_projection)
            self.phase = "publication"
            projection_id = self.publish(prepared, identity)
            with self.capture.guard():
                self.event(
                    IndexPhase(
                        phase="publication",
                        status="published",
                        observed_configuration_id=_configuration.identity_hash(identity),
                    )
                )
            return self.result(
                prepared,
                published,
                projection_id,
                produced,
                reused,
                self.cleanup_after_commit(),
            )

    def cleanup_after_commit(self) -> bool:
        # Separate ordinary authority after settlement; never reuse a terminal attempt guard.
        try:
            with writing(self.database, self.identity, budget=self.budget) as context:
                _storage.owner(
                    context.connection,
                    self.identity,
                    self.scope,
                    self.attribution,
                    self.document_id,
                )
                cleanup = _storage.cleanup(
                    context.connection,
                    self.scope,
                    self.document_id,
                    self.config_id,
                    1000,
                )
            return cleanup.has_more
        except (DeadlineStop, PrivateResourceStop):
            return True

    def result(
        self,
        prepared: _passages.PreparedPassages,
        publication: _passages.PassagePublication,
        projection_id: str,
        produced: int,
        reused: int,
        cleanup_pending: bool = False,
    ) -> ProcessResult:
        assert self.attempt is not None
        return ProcessResult(
            document_id=self.document_id,
            state_version=self.expected_state,
            configuration_id=self.config_id,
            attempt_id=self.attempt.attempt_id,
            passage_set_id=prepared.passage_set_id,
            projection_id=projection_id,
            outcome="unchanged" if produced == 0 and self._unchanged else "ready",
            produced_passages=publication.member_count if publication.outcome == "produced" else 0,
            reused_passages=publication.member_count if publication.outcome == "reused" else 0,
            produced_vectors=produced,
            reused_vectors=reused,
            cleanup_pending=cleanup_pending,
        )

    _unchanged = False

    def finish(
        self, context: CanonicalWriteContext, outcome: Literal["published", "unchanged"]
    ) -> None:
        assert self.attempt is not None
        context.connection.execute(
            "UPDATE index_attempt SET status=?,completed_at=? WHERE attempt_id=?",
            (outcome, timestamp(now()), self.attempt.attempt_id),
        )
        if self.configuration == DEFAULT_CONFIGURATION:
            context.connection.execute(
                "UPDATE processing_state SET indexing='ready',indexing_reason='ready' "
                "WHERE state_version=?",
                (self.expected_state,),
            )
        self._unchanged = outcome == "unchanged"

    def stage(
        self,
        prepared: _passages.PreparedPassages,
        provider: EmbeddingProvider,
        identity: ExecutionIdentity,
        title: str,
        previous: str | None,
    ) -> tuple[int, int]:
        assert self.attempt is not None
        produced = reused = 0
        index = 0
        while index < len(prepared.boundaries):
            batch: list[str] = []
            maximum = 0
            while index + len(batch) < len(prepared.boundaries) and len(batch) < 8:
                boundary = prepared.boundaries[index + len(batch)]
                text = _inspection.representation(boundary.quote, title, self.configuration)
                size = len(text.encode()) + 16
                if size > 8192:
                    raise PrivateResourceStop()
                if max(maximum, size) * (len(batch) + 1) > 8192:
                    break
                maximum = max(maximum, size)
                batch.append(text)
            with self.budget.reserve_provider(
                maximum * len(batch) * 16 + len(batch) * identity.dimensions * 64,
                unit="vector",
                sequences=len(batch),
                padded_token_positions=maximum * len(batch),
            ):
                vectors: list[bytes | None] = []
                with self.transaction() as context:
                    for offset, text in enumerate(batch):
                        ordinal = index + offset + 1
                        passage_id = digest(
                            "e3-passage/1",
                            prepared.passage_set_id,
                            str(ordinal),
                            prepared.boundaries[ordinal - 1].anchor_id,
                        )
                        old = (
                            context.connection.execute(
                                "SELECT vector,representation_hash,vector_hash,dimensions "
                                "FROM projection_member WHERE projection_id=? AND passage_id=?",
                                (previous, passage_id),
                            ).fetchone()
                            if previous is not None
                            else None
                        )
                        if old is not None and old["representation_hash"] == sha(text.encode()):
                            if (
                                old["dimensions"] != identity.dimensions
                                or sha(old["vector"]) != old["vector_hash"]
                            ):
                                raise EvidenceServiceError("internal_error")
                            try:
                                _vectors.validate(old["vector"], identity.dimensions)
                            except _vectors.InvalidVector:
                                raise EvidenceServiceError("internal_error") from None
                            vectors.append(old["vector"])
                        else:
                            vectors.append(None)
                needed = [
                    text for text, vector in zip(batch, vectors, strict=True) if vector is None
                ]
                if needed:
                    try:
                        self.budget.check_deadline()
                        output = provider.encode_documents(needed, batch_size=len(needed))
                        self.budget.check_deadline()
                    except DenseIndexError:
                        raise ProcessFailure("provider_unavailable") from None
                    if not isinstance(output, list) or len(output) != len(needed):
                        raise ProcessFailure("invalid_provider_output")
                    try:
                        encoded = iter(_vectors.encode(v, identity.dimensions) for v in output)
                        vectors = [next(encoded) if v is None else v for v in vectors]
                    except (_vectors.InvalidVector, TypeError, ValueError, OverflowError):
                        raise ProcessFailure("invalid_provider_output") from None
                    try:
                        actual = _configuration.execution_identity(self.configuration, provider)
                    except (EvidenceServiceError, ValueError, TypeError, AttributeError):
                        raise ProcessFailure("invalid_provider_output") from None
                    if actual != identity:
                        raise ProcessFailure("invalid_provider_output")
                with self.transaction() as context:
                    for offset, (text, vector) in enumerate(zip(batch, vectors, strict=True)):
                        assert vector is not None
                        ordinal = index + offset + 1
                        boundary = prepared.boundaries[ordinal - 1]
                        passage_id = digest(
                            "e3-passage/1",
                            prepared.passage_set_id,
                            str(ordinal),
                            boundary.anchor_id,
                        )
                        context.connection.execute(
                            "INSERT INTO index_staging_member VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (
                                self.attempt.attempt_id,
                                passage_id,
                                self.scope.corpus_id,
                                prepared.namespace,
                                self.document_id,
                                prepared.revision_id,
                                prepared.passage_set_id,
                                self.config_id,
                                ordinal,
                                boundary.quote,
                                sha(text.encode()),
                                vector,
                                identity.dimensions,
                                sha(vector),
                            ),
                        )
                produced += len(needed)
                reused += len(batch) - len(needed)
                with self.capture.guard():
                    self.event(
                        IndexPhase(
                            phase="vector",
                            status="rebuilt"
                            if self.mode == "rebuild"
                            else "produced"
                            if needed
                            else "reused",
                        )
                    )
                index += len(batch)
        return produced, reused

    def publish(self, prepared: _passages.PreparedPassages, identity: ExecutionIdentity) -> str:
        assert self.attempt is not None
        projection_id = token()
        with self.transaction() as context:
            connection = context.connection
            hashes = [
                _inspection.member_hash(row)
                for row in connection.execute(
                    "SELECT * FROM index_staging_member WHERE attempt_id=? ORDER BY ordinal",
                    (self.attempt.attempt_id,),
                )
            ]
            if len(hashes) != len(prepared.boundaries):
                raise EvidenceServiceError("internal_error")
            connection.execute(
                "INSERT INTO document_projection VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    projection_id,
                    self.scope.corpus_id,
                    prepared.namespace,
                    self.document_id,
                    prepared.revision_id,
                    self.expected_state,
                    prepared.passage_set_id,
                    self.config_id,
                    _configuration.identity_json(identity),
                    _configuration.identity_hash(identity),
                    self.attempt.attempt_id,
                    len(hashes),
                    len(hashes),
                    digest("e3-projection/1", *hashes),
                    timestamp(now()),
                ),
            )
            connection.execute(
                "INSERT INTO projection_member SELECT ?,passage_id,corpus_id,namespace,document_id,"
                "revision_id,passage_set_id,ordinal,lexical_input,representation_hash,vector,"
                "dimensions,vector_hash FROM index_staging_member WHERE attempt_id=?",
                (projection_id, self.attempt.attempt_id),
            )
            projection = connection.execute(
                "SELECT * FROM document_projection WHERE projection_id=?",
                (projection_id,),
            ).fetchone()
            assert projection is not None
            _inspection.verify(connection, projection, self.configuration, self.budget)
            connection.execute(
                "INSERT INTO active_document_projection VALUES (?,?,?,?) "
                "ON CONFLICT(corpus_id,document_id,configuration_id) DO UPDATE "
                "SET projection_id=excluded.projection_id",
                (self.scope.corpus_id, self.document_id, self.config_id, projection_id),
            )
            connection.execute(
                "DELETE FROM index_staging_member WHERE attempt_id=?", (self.attempt.attempt_id,)
            )
            self.finish(context, "published")
        return projection_id
