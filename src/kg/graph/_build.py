"""Single-owner staged graph builds; publication belongs to the local controller."""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import tempfile
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Self
from uuid import uuid4

from pydantic import Field, ValidationError

from kg._execution_budget import (
    CancelledStop,
    DeadlineStop,
    PrivateResourceStop,
    _BulkReadAccounting,
    _BulkReadOperation,
)
from kg.evidence._graph_observer import (
    GraphSourceBinding,
    GraphSourceCleanupError,
    GraphSourceObserver,
    open_graph_source,
)
from kg.evidence._values import validated
from kg.evidence.database import EvidenceDatabase
from kg.evidence.errors import EvidenceServiceError
from kg.graph._native import (
    NativeError,
    NativeGraphReadHandle,
    NativeGraphWriter,
    _engine,
    scalar,
)
from kg.knowledge._graph_export import (
    GraphCoverage,
    GraphEntity,
    GraphExportError,
    graph_export,
)
from kg.knowledge._selection import SeedWitness
from kg.models.evidence import LocalIdentity
from kg.models.foundation import Failure, Scope, Token, Value

LOGGER = logging.getLogger(__name__)
STAGING_BYTES = 1 << 30
type GraphBuildCode = Literal[
    "graph_unavailable", "unsupported_mapping", "unsupported_support", "invalid_request",
    "forbidden", "state_changed", "cancelled", "deadline_exceeded", "resource_exhausted",
    "invalid_projection", "native_error", "storage_error", "cleanup_failed",
]
type GraphBuildPhase = Literal[
    "admission", "export", "native_write", "verification", "completion", "transfer", "cleanup",
]


class GraphBuildManifest(Value):
    format: Literal["ladybug-projection/1"] = "ladybug-projection/1"
    coverage: GraphCoverage
    identity: LocalIdentity
    scope: Scope
    build_id: Token
    entity_count: int = Field(ge=0)
    relationship_count: int = Field(ge=0)
    decision_count: int = Field(ge=0)
    assertion_support_occurrences: int = Field(ge=0)
    entity_source_support_occurrences: int = Field(ge=0)
    seed_witness_count: int = Field(ge=0)
    unique_evidence_count: int = Field(ge=0)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    engine_version: Literal["0.20.4"] = "0.20.4"
    complete: Literal[True] = True


@dataclass(frozen=True)
class GraphBuildFailure:
    code: GraphBuildCode
    phase: GraphBuildPhase
    diagnostic_id: str
    canonical_failure: Failure | None = None


@dataclass(frozen=True)
class GraphCleanupOutcome:
    complete: bool
    diagnostic_id: str | None = None


class _Owner:
    def __copy__(self) -> Self:
        raise TypeError("Graph ownership cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> Self:
        raise TypeError("Graph ownership cannot be copied")


class GraphCleanupResidue(_Owner):
    def __init__(self) -> None:
        self.directory: Path | None = None
        self.owned_paths: tuple[Path, ...] = ()
        self.native_writer: NativeGraphWriter | None = None
        self.native_reader: NativeGraphReadHandle | None = None
        self.observer: GraphSourceObserver | None = None
        self.last_outcome = GraphCleanupOutcome(False)

    def inventory(self) -> None:
        if self.directory is None:
            return
        if self.directory.is_symlink():
            raise OSError("Owned directory replaced by symlink")
        self.owned_paths = tuple(self.directory.rglob("*")) + (self.directory,)


def retry_graph_cleanup(residue: GraphCleanupResidue) -> GraphCleanupOutcome:
    failed = False
    for attribute in ("native_writer", "native_reader", "observer"):
        owner = getattr(residue, attribute)
        if owner is not None:
            try:
                owner.close()
            except Exception as error:
                failed = True
                LOGGER.error("Graph cleanup class=%s", type(error).__name__)
            else:
                setattr(residue, attribute, None)
    if residue.native_writer is None and residue.native_reader is None:
        try:
            residue.inventory()
            for path in sorted(residue.owned_paths, key=lambda p: len(p.parts), reverse=True):
                if path.is_symlink() or path.is_file():
                    path.unlink()
                elif path.exists():
                    path.rmdir()
            residue.owned_paths = ()
            residue.directory = None
        except OSError as error:
            failed = True
            LOGGER.error("Graph cleanup class=%s", type(error).__name__)
    complete = (
        not failed and residue.native_writer is None and residue.native_reader is None
        and residue.observer is None and residue.directory is None
    )
    residue.last_outcome = GraphCleanupOutcome(complete, None if complete else str(uuid4()))
    return residue.last_outcome


class GraphBuildError(Exception):
    def __init__(
        self, failure: GraphBuildFailure, accounting: _BulkReadAccounting,
        cleanup: GraphCleanupOutcome, residue: GraphCleanupResidue | None = None,
    ) -> None:
        self.failure, self.accounting, self.cleanup = failure, accounting, cleanup
        self._residue = residue
        super().__init__(f"{failure.code} ({failure.diagnostic_id})")

    def take_cleanup_residue(self) -> GraphCleanupResidue | None:
        residue, self._residue = self._residue, None
        return residue


@dataclass(frozen=True)
class GraphBuildResources(_Owner):
    binding: GraphSourceBinding
    manifest: GraphBuildManifest
    directory: Path
    native: NativeGraphReadHandle
    observer: GraphSourceObserver
    accounting: _BulkReadAccounting
    _custody: list[GraphCleanupResidue]


def dispose_graph(resources: GraphBuildResources) -> GraphCleanupResidue | None:
    if not resources._custody:
        return None
    residue = resources._custody.pop()
    return None if retry_graph_cleanup(residue).complete else residue


class StagedGraph(_Owner):
    def __init__(
        self, resources: GraphBuildResources, operation: _BulkReadOperation,
    ) -> None:
        self._resources: GraphBuildResources | None = resources
        self._pending_operation: _BulkReadOperation | None = operation
        self._view = resources

    @property
    def binding(self) -> GraphSourceBinding:
        return self._view.binding

    @property
    def manifest(self) -> GraphBuildManifest:
        return self._view.manifest

    @property
    def directory(self) -> Path:
        return self._view.directory

    @property
    def native(self) -> NativeGraphReadHandle:
        return self._view.native

    @property
    def observer(self) -> GraphSourceObserver:
        return self._view.observer

    @property
    def accounting(self) -> _BulkReadAccounting:
        return self._view.accounting

    @property
    def pending_operation(self) -> _BulkReadOperation | None:
        return self._pending_operation

    def transfer(self, *, operation: _BulkReadOperation) -> GraphBuildResources:
        try:
            if operation is not self._pending_operation or self._resources is None:
                raise EvidenceServiceError("invalid_request")
            operation.budget.check_deadline()
        except (EvidenceServiceError, DeadlineStop, PrivateResourceStop) as error:
            raise GraphBuildError(
                _failure(error, "transfer"),
                (self._pending_operation.snapshot() if self._pending_operation is not None
                 else self.accounting), GraphCleanupOutcome(True),
            ) from None
        result, self._resources = self._resources, None
        self._pending_operation = None
        return result

    def close(self) -> GraphCleanupResidue | None:
        resources, self._resources = self._resources, None
        self._pending_operation = None
        return None if resources is None else dispose_graph(resources)


def _failure(error: Exception, phase: GraphBuildPhase) -> GraphBuildFailure:
    canonical = None
    code: GraphBuildCode
    if isinstance(error, CancelledStop):
        code = "cancelled"
    elif isinstance(error, DeadlineStop):
        code = "deadline_exceeded"
    elif isinstance(error, PrivateResourceStop):
        code = "resource_exhausted"
    elif isinstance(error, (NativeError, GraphExportError)):
        code = error.code
    elif isinstance(error, EvidenceServiceError):
        canonical = error.failure
        if canonical.code in ("forbidden", "state_changed", "invalid_request"):
            code = canonical.code
        elif canonical.code == "unsupported":
            code = "unsupported_mapping"
        elif canonical.code == "not_found":
            code = "invalid_projection"
        else:
            code = "storage_error"
    elif isinstance(error, (ValueError, ValidationError)):
        code = "invalid_request" if phase == "admission" else "invalid_projection"
    elif isinstance(error, RuntimeError):
        code = "native_error"
    else:
        code = "storage_error"
    failure = GraphBuildFailure(code, phase, str(uuid4()), canonical)
    LOGGER.error(
        "Graph failure diagnostic=%s phase=%s code=%s class=%s",
        failure.diagnostic_id, phase, code, type(error).__name__,
    )
    return failure


def _disk(residue: GraphCleanupResidue, operation: _BulkReadOperation) -> None:
    operation.budget.check_deadline()
    residue.inventory()
    total = sum(path.stat().st_size for path in residue.owned_paths if path.is_file())
    if total > STAGING_BYTES:
        raise PrivateResourceStop()


def _verify(
    native: NativeGraphReadHandle, counts: dict[str, int], operation: _BulkReadOperation,
) -> int:
    expected = {
        "Entity": counts["entity_count"],
        "EntityProof": counts["entity_count"],
        "Assertion": counts["relationship_count"] + counts["decision_count"],
        "SeedProof": counts["seed_witness_count"],
    }
    for table, count in expected.items():
        if scalar(native, f"MATCH (n:{table}) RETURN count(n)",
                  operation.budget, operation.cancel) != count:
            raise NativeError("invalid_projection")
    expected_edges = {
        "SELECTED": counts["entity_count"], "SUBJECT": expected["Assertion"],
        "SUBJECT_PROOF": expected["Assertion"], "OBJECT": counts["relationship_count"],
        "OBJECT_PROOF": counts["relationship_count"],
        "ASSERTION_SOURCE": counts["assertion_support_occurrences"],
        "ENTITY_SOURCE": counts["entity_source_support_occurrences"],
        "ENTITY_SEED": counts["seed_witness_count"],
    }
    for table, count in expected_edges.items():
        if scalar(native, f"MATCH ()-[r:{table}]->() RETURN count(r)",
                  operation.budget, operation.cancel) != count:
            raise NativeError("invalid_projection")
    return scalar(native, "MATCH (e:Evidence) RETURN count(e)", operation.budget, operation.cancel)


def build_graph(
    database: EvidenceDatabase, identity: LocalIdentity, scope: Scope, *,
    staging_parent: Path, operation: _BulkReadOperation,
    expected_coverage: GraphCoverage | None = None,
) -> StagedGraph:
    if not isinstance(operation, _BulkReadOperation):
        raise TypeError("Expected the admitted bulk operation")
    residue = GraphCleanupResidue()
    phase: GraphBuildPhase = "admission"
    try:
        if (
            operation.budget.resource_profile != "graph-build/1"
            or operation.meter.private_budget is not operation.budget
            or operation.cancel is not operation.budget._cancel
        ):
            raise EvidenceServiceError("invalid_request")
        operation.budget.check_deadline()
        identity, scope = validated(LocalIdentity, identity), validated(Scope, scope)
        if expected_coverage is not None:
            expected_coverage = validated(GraphCoverage, expected_coverage)
        _engine()
        if staging_parent.is_symlink() or not staging_parent.is_dir():
            raise EvidenceServiceError("invalid_request")
        residue.directory = Path(tempfile.mkdtemp(prefix="graph-", dir=staging_parent.resolve()))
        directory = residue.directory
        residue.observer = open_graph_source(
            database, identity, scope, operation.budget.deadline, operation.budget,
        )
        observer = residue.observer
        with observer.operation(
            observer.binding, identity, scope, operation.budget.deadline, operation.budget,
        ) as source:
            phase = "export"
            counts = dict.fromkeys((
                "entity_count", "relationship_count", "decision_count",
                "assertion_support_occurrences", "entity_source_support_occurrences",
                "seed_witness_count",
            ), 0)
            digest = hashlib.sha256()
            with source.read_context(operation.meter) as context, closing(graph_export(
                context, expected_coverage=expected_coverage,
            )) as exporter:
                coverage = exporter.coverage
                residue.native_writer = writer = NativeGraphWriter()
                phase = "native_write"
                writer.open(directory / "graph.lbug", read_only=False)
                writer.schema(operation.budget, operation.cancel)
                while True:
                    phase = "export"
                    with closing(exporter.read()) as page:
                        phase = "native_write"
                        writer.run("BEGIN TRANSACTION", {}, operation.budget, operation.cancel)
                        for item in page.items:
                            writer.insert(item, operation.budget, operation.cancel)
                            digest.update(item.model_dump_json().encode() + b"\n")
                            if isinstance(item, GraphEntity):
                                counts["entity_count"] += 1
                                counts["entity_source_support_occurrences"] += len(
                                    item.witness_evidence,
                                )
                                counts["seed_witness_count"] += isinstance(
                                    item.witness.basis, SeedWitness,
                                )
                            else:
                                counts["decision_count" if item.decision_member is not None
                                       else "relationship_count"] += 1
                                counts["assertion_support_occurrences"] += len(item.support)
                        writer.run("COMMIT", {}, operation.budget, operation.cancel)
                        _disk(residue, operation)
                        if page.eof:
                            break
            phase = "verification"
            _verify(writer, counts, operation)
            writer.run("CHECKPOINT", {}, operation.budget, operation.cancel)
            writer.close()
            residue.native_writer = None
            residue.native_reader = reader = NativeGraphReadHandle()
            reader.open(directory / "graph.lbug", read_only=True)
            evidence_count = _verify(reader, counts, operation)
            manifest = GraphBuildManifest(
                coverage=coverage, identity=identity, scope=scope, build_id=str(uuid4()),
                entity_count=counts["entity_count"],
                relationship_count=counts["relationship_count"],
                decision_count=counts["decision_count"],
                assertion_support_occurrences=counts["assertion_support_occurrences"],
                entity_source_support_occurrences=counts["entity_source_support_occurrences"],
                seed_witness_count=counts["seed_witness_count"],
                unique_evidence_count=evidence_count, content_sha256=digest.hexdigest(),
            )
            (directory / "manifest.json").write_text(manifest.model_dump_json(), encoding="utf-8")
            _disk(residue, operation)
            phase = "completion"
            with source.release_fence():
                operation.budget.check_deadline()
        return StagedGraph(GraphBuildResources(
            observer.binding, manifest, directory, reader, observer,
            operation.snapshot(), [residue],
        ), operation)
    except GraphSourceCleanupError as error:
        residue.observer = error.source
        failure = _failure(error.original_failure, phase)
    except (EvidenceServiceError, DeadlineStop, PrivateResourceStop, GraphExportError,
            NativeError, ValueError, OSError, sqlite3.Error, RuntimeError) as error:
        failure = _failure(error, phase)
    except BaseException:
        outcome = retry_graph_cleanup(residue)
        if not outcome.complete:
            raise GraphBuildError(
                GraphBuildFailure("cleanup_failed", phase, str(uuid4())),
                operation.snapshot(), outcome, residue,
            ) from None
        raise
    outcome = retry_graph_cleanup(residue)
    raise GraphBuildError(
        failure, operation.snapshot(), outcome, None if outcome.complete else residue,
    ) from None
