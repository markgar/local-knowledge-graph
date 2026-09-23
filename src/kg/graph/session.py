"""Serialized trusted-local graph lifecycle and typed application queries."""

from __future__ import annotations

import logging
import math
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from threading import Event, Lock
from typing import TYPE_CHECKING, Literal, Self, TypedDict, Unpack
from uuid import uuid4

from kg._execution_budget import (
    CancelledStop,
    Deadline,
    DeadlineStop,
    PrivateBudget,
    PrivateResourceStop,
    ScratchReservation,
    StepMeter,
    _BulkReadAccounting,
    _BulkReadOperation,
    _graph_build_operation,
)
from kg.diagnostics._bounds import bounded_size
from kg.evidence import EvidenceDatabase, EvidenceService
from kg.evidence._graph_observer import GraphSourceBinding
from kg.evidence._read_context import CanonicalReadContext
from kg.evidence._values import validated
from kg.evidence.errors import EvidenceServiceError
from kg.graph._build import (
    GraphBuildError,
    GraphBuildFailure,
    GraphBuildResources,
    GraphCleanupOutcome,
    GraphCleanupResidue,
    StagedGraph,
    build_graph,
    dispose_graph,
    retry_graph_cleanup,
)
from kg.graph._native import NativeError, NativeGraphReadHandle, NativeRows, NativeValue
from kg.graph._session_owner import _SessionOwner, cleanup_log
from kg.graph._session_types import (
    GraphCode,
    GraphFailure,
    GraphGeneration,
    GraphSessionError,
    GraphState,
    RefreshResult,
    Retry,
    SessionStatus,
    _WarmReadMeter,
)
from kg.knowledge._graph_export import GraphCoverage
from kg.models.evidence import LocalIdentity
from kg.models.foundation import (
    BatchResult,
    Failure,
    Scope,
    Value,
    WriteBatch,
    WriteOutcome,
    WriteRequest,
)

if TYPE_CHECKING:
    from kg.graph._decisions import _GraphDecisionSelection
    from kg.models.graph import (
        GraphCapabilities,
        GraphRelationshipDecisionsRequest,
        GraphRelationshipDecisionsResult,
        GraphTraversalRequest,
        GraphTraversalResult,
    )

LOGGER = logging.getLogger(__name__)
_OUTPUT_BYTES = 8 << 20
_COLD_SECONDS = 300
_WARM_SECONDS = 30
_RETRY: dict[GraphCode, Retry] = {
    "state_changed": "finish_import_then_refresh",
    "deadline_exceeded": "refresh",
    "invalid_projection": "refresh",
    "native_error": "reopen",
    "storage_error": "reopen",
}


def _failure(code: GraphCode, diagnostic_id: str | None = None) -> GraphFailure:
    return GraphFailure(code, _RETRY.get(code, "none"), diagnostic_id or str(uuid4()))


def _translate(error: Exception) -> GraphFailure:
    if isinstance(error, GraphSessionError):
        return error.failure
    if isinstance(error, GraphBuildError):
        return _failure(error.failure.code, error.failure.diagnostic_id)
    if isinstance(error, CancelledStop):
        return _failure("cancelled")
    if isinstance(error, DeadlineStop):
        return _failure("deadline_exceeded")
    if isinstance(error, PrivateResourceStop):
        return _failure("resource_exhausted")
    if isinstance(error, NativeError):
        return _failure(error.code)
    if isinstance(error, EvidenceServiceError):
        code = error.failure.code
        if code in ("forbidden", "state_changed", "invalid_request", "unsupported"):
            return _failure(code, error.failure.diagnostic_id)
        return _failure("storage_error", error.failure.diagnostic_id)
    if isinstance(error, ValueError):
        return _failure("invalid_request")
    cleanup_log(error)
    return _failure("storage_error")


@dataclass(frozen=True)
class GraphArtifactLocation:
    generation_directory: Path | None
    owned_paths: tuple[Path, ...]
    state: str
    native_handles_open: bool
    observer_open: bool


@dataclass(frozen=True)
class _Request:
    budget: PrivateBudget
    meter: StepMeter
    cancel: Event
    bulk: _BulkReadOperation | None = None


class _StatusChanges(TypedDict, total=False):
    last_error: GraphFailure | None
    cleanup_error: GraphFailure | None
    cleanup_pending: bool


def _size(value: object, remaining: int = _OUTPUT_BYTES, depth: int = 0) -> int:
    """Bound immutable output before serialization; reject live/mutable handles."""
    if depth > 30 or remaining < 0:
        raise PrivateResourceStop()
    if value is None or type(value) is bool:
        size = 5
    elif isinstance(value, str):
        size = bounded_size(value, remaining)
    elif type(value) is int:
        size = 2 + value.bit_length()
    elif type(value) is float and math.isfinite(value):
        size = 32
    elif isinstance(value, datetime):
        size = 64
    elif isinstance(value, GraphGeneration):
        size = 32 + _size((value.session_id, value.ordinal), remaining - 32, depth + 1)
    elif isinstance(value, tuple):
        size = 2
        for item in value:
            size += 1 + _size(item, remaining - size, depth + 1)
    elif isinstance(value, Value):
        if type(value).model_config.get("frozen") is not True or value.__pydantic_extra__:
            raise GraphSessionError(_failure("invalid_request"))
        size = 2
        for key, item in value.__dict__.items():
            size += 2
            size += _size(key, remaining - size, depth + 1)
            size += _size(item, remaining - size, depth + 1)
    else:
        raise GraphSessionError(_failure("invalid_request"))
    if size > remaining:
        raise PrivateResourceStop()
    return size


class _BorrowedRows:
    def __init__(self, view: _BorrowedNativeReader, rows: NativeRows) -> None:
        self._view, self._rows = view, rows
        self._closed = False

    def read(self, *, limit: int = 200) -> tuple[tuple[NativeValue, ...], ...]:
        self._view.check()
        if self._closed:
            raise GraphSessionError(_failure("invalid_request"))
        return self._rows.read(limit=limit)

    def close(self) -> None:
        self._view.check()
        self._release()

    def _release(self) -> None:
        self._view._session._owner.check_owner()
        if not self._closed:
            self._rows.close()
            self._closed = True


class _BorrowedNativeReader:
    def __init__(
        self, session: LocalGraphSession, generation: GraphGeneration,
        native: NativeGraphReadHandle, request: _Request,
    ) -> None:
        self._session, self._generation = session, generation
        self._native, self._request = native, request
        self._active = True
        self._rows: list[_BorrowedRows] = []

    def check(self) -> None:
        self._session._owner.check_owner()
        if not self._active or self._generation != self._session._generation:
            raise GraphSessionError(_failure("generation_invalid"))
        self._session._check(self._request)

    def execute(self, template: str, parameters: Mapping[str, NativeValue]) -> _BorrowedRows:
        self.check()
        rows = _BorrowedRows(self, self._native.execute(
            template, parameters, budget=self._request.budget, cancel=self._request.cancel,
            timeout_milliseconds=5000,
        ))
        self._rows.append(rows)
        return rows

    def close(self) -> None:
        self._session._owner.check_owner()
        self._active = False
        for rows in self._rows:
            rows._release()


@dataclass(frozen=True)
class GraphReadContext:
    generation: GraphGeneration
    binding: GraphSourceBinding
    canonical: CanonicalReadContext
    native: _BorrowedNativeReader
    meter: StepMeter
    cancel: Event
    _output: list[ScratchReservation]
    _sizes: list[int]

    def retain[T](self, value: T) -> T:
        """Charge immutable output before appending it to a consumer's result."""
        self.native.check()
        size = _size(value, _OUTPUT_BYTES - self._sizes[0])
        self._output.append(self.meter.reserve_scratch(size + 256, "general"))
        self._sizes[0] += size
        return value


class LocalGraphSession:
    def __init__(
        self, database: EvidenceDatabase, identity: LocalIdentity, scope: Scope, *,
        graph_directory: Path, expected_coverage: GraphCoverage | None = None,
    ) -> None:
        self._database = EvidenceDatabase(database.path.resolve())
        self._identity, self._scope = validated(LocalIdentity, identity), validated(Scope, scope)
        if self._identity.principal_id != self._scope.access.principal_id:
            raise GraphSessionError(_failure("forbidden"))
        self._coverage = (
            None if expected_coverage is None else validated(GraphCoverage, expected_coverage)
        )
        if graph_directory.is_symlink():
            raise GraphSessionError(_failure("invalid_request"))
        self._directory = graph_directory.resolve()
        if self._directory in (
            self._database.path,
            Path(str(self._database.path) + "-wal"),
            Path(str(self._database.path) + "-shm"),
        ) or (self._directory.exists() and not self._directory.is_dir()):
            raise GraphSessionError(_failure("invalid_request"))
        self._lock = Lock()
        self._status = SessionStatus("unbuilt")
        self._session_id = str(uuid4())
        self._ordinal = 0
        self._generation: GraphGeneration | None = None
        self._resources: GraphBuildResources | None = None
        self._stage: StagedGraph | None = None
        self._residue: GraphCleanupResidue | None = None
        self._root: Path | None = None
        self._active: _Request | None = None
        self._evidence: EvidenceService | None = None
        self._accounting: _BulkReadAccounting | None = None
        self._build_failure: GraphBuildFailure | None = None
        self._cleanup_outcome: GraphCleanupOutcome | None = None
        self._owner = _SessionOwner(self._shutdown)

    def status(self) -> SessionStatus:
        with self._lock:
            return self._status

    def capabilities(self) -> GraphCapabilities:
        from kg.graph._native import _engine
        from kg.models.graph import GraphCapabilities

        request = self._admit(build=False, cancel=None)

        def probe() -> GraphCapabilities:
            try:
                _engine()
            except NativeError as error:
                if error.code != "graph_unavailable":
                    raise
                return GraphCapabilities(runtime="unavailable", unavailable_reason=error.code)
            return GraphCapabilities(runtime="available")

        return self._call(request, probe)

    def traverse(
        self, request: GraphTraversalRequest, *, cancel: Event | None = None,
    ) -> GraphTraversalResult:
        from kg.graph._relationships import traverse
        from kg.models.graph import GraphTraversalRequest, GraphTraversalResult

        try:
            request = validated(GraphTraversalRequest, request)
        except EvidenceServiceError as error:
            raise GraphSessionError(_translate(error)) from None
        try:
            return self._run_read(
                request.scope, lambda context: traverse(context, request), cancel=cancel,
            )
        except GraphSessionError as error:
            return GraphTraversalResult(
                request_id=request.request_id, scope=request.scope, outcome="failed",
                generation=None, error=error.failure,
            )

    def relationship_decisions(
        self, request: GraphRelationshipDecisionsRequest, *, cancel: Event | None = None,
    ) -> GraphRelationshipDecisionsResult:
        from kg.graph._decisions import relationship_decisions
        from kg.models.graph import (
            GraphRelationshipDecisionsRequest,
            GraphRelationshipDecisionsResult,
        )

        try:
            request = validated(GraphRelationshipDecisionsRequest, request)
        except EvidenceServiceError as error:
            raise GraphSessionError(_translate(error)) from None
        try:
            return self._run_read(
                request.scope, lambda context: relationship_decisions(context, request),
                cancel=cancel,
            )
        except GraphSessionError as error:
            return GraphRelationshipDecisionsResult(
                request_id=request.request_id, scope=request.scope, outcome="failed",
                generation=None, error=error.failure,
            )

    def _relationship_decision_selection(
        self, request: GraphRelationshipDecisionsRequest, *, cancel: Event | None = None,
    ) -> _GraphDecisionSelection:
        from kg.graph._decisions import _select_relationship_decisions
        from kg.models.graph import GraphRelationshipDecisionsRequest

        try:
            request = validated(GraphRelationshipDecisionsRequest, request)
        except EvidenceServiceError as error:
            raise GraphSessionError(_translate(error)) from None
        return self._run_read(
            request.scope, lambda context: _select_relationship_decisions(context, request),
            cancel=cancel,
        )

    def _set(
        self, *, state: GraphState | None = None, **changes: Unpack[_StatusChanges],
    ) -> None:
        with self._lock:
            self._status = replace(self._status, **changes)
            if state is not None:
                self._status = replace(self._status, state=state)

    def _admit(self, *, build: bool, cancel: Event | None) -> _Request:
        self._owner.check_caller()
        with self._lock:
            if self._status.closed:
                raise GraphSessionError(_failure("closed"))
        event = Event() if cancel is None else cancel
        deadline = Deadline(time.monotonic() + (_COLD_SECONDS if build else _WARM_SECONDS))
        if build:
            try:
                bulk = _graph_build_operation(deadline=deadline, cancel=event)
            except (CancelledStop, DeadlineStop, PrivateResourceStop) as error:
                raise GraphSessionError(_translate(error)) from None
            return _Request(bulk.budget, bulk.meter, event, bulk)
        budget = PrivateBudget(deadline)
        return _Request(budget, _WarmReadMeter(budget, event), event)

    def _check(self, request: _Request) -> None:
        request.meter.check_deadline()
        if self.status().closed:
            raise GraphSessionError(_failure("closed"))
        if request.cancel.is_set():
            raise CancelledStop()

    def _call[T](self, request: _Request, action: Callable[[], T]) -> T:
        def run() -> T:
            with self._lock:
                self._active = request
            try:
                self._check(request)
                return action()
            finally:
                if request.bulk is not None:
                    self._accounting = request.bulk.snapshot()
                with self._lock:
                    self._active = None
        try:
            return self._owner.call(run, request.budget.deadline)
        except Exception as error:
            raise GraphSessionError(_translate(error)) from None

    def _invalidate(self, state: GraphState = "dirty") -> None:
        self._generation = None
        self._ordinal += 1
        self._set(state=state)

    def _dispose(self) -> bool:
        try:
            if self._stage is not None:
                self._residue = self._stage.close()
                self._stage = None
            if self._resources is not None:
                self._residue = dispose_graph(self._resources)
                self._resources = None
            if self._residue is not None:
                self._cleanup_outcome = self._residue.last_outcome
                self._set(cleanup_pending=True, cleanup_error=_failure(
                    "cleanup_failed", self._cleanup_outcome.diagnostic_id,
                ))
                return False
        except Exception as error:
            cleanup_log(error)
            self._cleanup_outcome = GraphCleanupOutcome(False, str(uuid4()))
            self._set(cleanup_pending=True, cleanup_error=_failure(
                "cleanup_failed", self._cleanup_outcome.diagnostic_id,
            ))
            return False
        self._cleanup_outcome = GraphCleanupOutcome(True)
        self._set(cleanup_pending=False, cleanup_error=None)
        return True

    def _retry_cleanup(self) -> bool:
        if self._residue is not None:
            try:
                outcome = retry_graph_cleanup(self._residue)
                self._cleanup_outcome = outcome
                if outcome.complete:
                    self._residue = None
                else:
                    self._set(cleanup_pending=True, cleanup_error=_failure(
                        "cleanup_failed", outcome.diagnostic_id,
                    ))
                    return False
            except Exception as error:
                cleanup_log(error)
                self._set(cleanup_pending=True, cleanup_error=_failure("cleanup_failed"))
                return False
        return self._dispose()

    def _failed(self, error: Exception, request: _Request) -> GraphSessionError:
        if isinstance(error, GraphBuildError):
            residue = error.take_cleanup_residue()
            if residue is not None:
                self._residue = residue
            self._build_failure, self._cleanup_outcome = error.failure, error.cleanup
        failure = _translate(error)
        if request.bulk is not None:
            self._accounting = request.bulk.snapshot()
            if (
                isinstance(error, PrivateResourceStop)
                and self._accounting.stop_reason == "cancelled"
            ):
                failure = _failure("cancelled")
        self._invalidate("dirty" if failure.code == "state_changed" else "error")
        if failure.code != "cleanup_failed" or self.status().last_error is None:
            self._set(last_error=failure)
        if not self.status().cleanup_pending:
            self._dispose()
        return GraphSessionError(failure)

    def _build(self, request: _Request) -> None:
        if request.bulk is None:
            raise GraphSessionError(_failure("state_changed"))
        self._invalidate()
        if not self._retry_cleanup():
            raise GraphSessionError(_failure("cleanup_failed"))
        self._check(request)
        if self._root is None:
            if self._directory.is_symlink():
                raise GraphSessionError(_failure("invalid_request"))
            self._directory.mkdir(parents=True, exist_ok=True)
            self._root = Path(tempfile.mkdtemp(prefix="session-", dir=self._directory))
        self._stage = build_graph(
            self._database, self._identity, self._scope, staging_parent=self._root,
            operation=request.bulk, expected_coverage=self._coverage,
        )
        self._resources = self._stage.transfer(operation=request.bulk)
        self._stage = None
        resource = self._resources
        if (
            resource.binding is not resource.observer.binding
            or resource.manifest.identity != self._identity
            or resource.manifest.scope != self._scope
            or (self._coverage is not None and self._coverage != resource.manifest.coverage)
        ):
            raise GraphSessionError(_failure("invalid_projection"))
        with resource.observer.operation(
            resource.binding, self._identity, self._scope,
            request.budget.deadline, request.budget,
        ) as operation, operation.release_fence():
            self._check(request)
            self._generation = GraphGeneration(self._session_id, self._ordinal)
            self._set(state="ready", last_error=None)
        self._check(request)

    def refresh(self, *, cancel: Event | None = None) -> RefreshResult:
        try:
            request = self._admit(build=True, cancel=cancel)
            def refresh() -> RefreshResult:
                try:
                    self._build(request)
                    return RefreshResult("ready", None)
                except Exception as error:
                    failure = self._failed(error, request).failure
                    return RefreshResult(
                        "dirty" if failure.code == "state_changed" else "error", failure,
                    )
            return self._call(request, refresh)
        except Exception as error:
            return RefreshResult("error", _translate(error))

    def _run_read[T](
        self, scope: Scope, consume: Callable[[GraphReadContext], T], *,
        expected_generation: GraphGeneration | None = None, cancel: Event | None = None,
    ) -> T:
        scope = validated(Scope, scope)
        if scope != self._scope:
            raise GraphSessionError(_failure("forbidden"))
        request = self._admit(
            build=expected_generation is None and self.status().state != "ready", cancel=cancel,
        )
        def read() -> T:
            if expected_generation is not None and (
                expected_generation != self._generation or self.status().state != "ready"
            ):
                raise GraphSessionError(_failure("generation_invalid"))
            output: list[ScratchReservation] = []
            try:
                if self.status().state != "ready":
                    self._build(request)
                resource, generation = self._resources, self._generation
                if resource is None or generation is None:
                    raise GraphSessionError(_failure("generation_invalid"))
                with resource.observer.operation(
                    resource.binding, self._identity, scope,
                    request.budget.deadline, request.budget,
                ) as operation:
                    with operation.read_context(request.meter) as canonical:
                        native = _BorrowedNativeReader(self, generation, resource.native, request)
                        context = GraphReadContext(
                            generation, resource.binding, canonical, native,
                            request.meter, request.cancel, output, [0],
                        )
                        try:
                            result = consume(context)
                            output.append(request.meter.reserve_scratch(
                                max(1, _size(result)), "general",
                            ))
                        except Exception:
                            try:
                                native.close()
                            except Exception as cleanup_error:
                                cleanup_log(cleanup_error)
                            raise
                        else:
                            native.close()
                    with operation.release_fence():
                        self._check(request)
                self._check(request)
                return result
            except Exception as error:
                raise self._failed(error, request) from None
            finally:
                for reservation in output:
                    reservation.release()
        return self._call(request, read)

    def _write_units(self, requests: tuple[WriteRequest, ...], request: _Request) -> tuple[
        WriteOutcome, ...
    ]:
        if self._evidence is None:
            self._evidence = EvidenceService(self._database, self._identity)
        outcomes = []
        for item in requests:
            try:
                self._check(request)
            except (CancelledStop, DeadlineStop, GraphSessionError) as error:
                self._set(last_error=(
                    _failure("closed") if self.status().closed else _translate(error)
                ))
                outcomes.append(WriteOutcome(
                    request_id=item.request_id, status="failed",
                    error=Failure(code="budget_exceeded", diagnostic_id=str(uuid4())),
                ))
                continue
            self._invalidate()
            # Committed receipts are not read results: no post-write cancellation gate.
            outcomes.append(self._evidence._write_budgeted(item, request.budget))
            try:
                self._check(request)
            except (CancelledStop, DeadlineStop, GraphSessionError) as error:
                self._set(last_error=(
                    _failure("closed") if self.status().closed else _translate(error)
                ))
        return tuple(outcomes)

    def write(self, request: WriteRequest, *, cancel: Event | None = None) -> WriteOutcome:
        request = validated(WriteRequest, request)
        if request.scope != self._scope:
            raise GraphSessionError(_failure("forbidden"))
        operation = self._admit(build=False, cancel=cancel)
        return self._call(operation, lambda: self._write_units((request,), operation)[0])

    def write_batch(self, batch: WriteBatch, *, cancel: Event | None = None) -> BatchResult:
        batch = validated(WriteBatch, batch)
        if any(item.scope != self._scope for item in batch.items):
            raise GraphSessionError(_failure("forbidden"))
        operation = self._admit(build=False, cancel=cancel)
        def write() -> BatchResult:
            outcomes = self._write_units(batch.items, operation)
            successes = sum(outcome.receipt is not None for outcome in outcomes)
            status: Literal["complete", "partial", "failed"] = (
                "complete" if successes == len(outcomes) else "partial" if successes else "failed"
            )
            result = BatchResult(
                contract_version="foundation/1", batch_id=batch.batch_id, outcomes=outcomes,
                status=status,
            )
            result.validate_for(batch)
            return result
        return self._call(operation, write)

    def _inventory(self) -> tuple[GraphArtifactLocation, ...]:
        def inventory() -> tuple[GraphArtifactLocation, ...]:
            result = []
            if self._residue is not None:
                residue = self._residue
                result.append(GraphArtifactLocation(
                    residue.directory, residue.owned_paths, "cleanup_pending",
                    residue.native_reader is not None or residue.native_writer is not None,
                    residue.observer is not None,
                ))
            for owner in (self._stage, self._resources):
                if owner is not None:
                    result.append(GraphArtifactLocation(
                        owner.directory, tuple(owner.directory.rglob("*")) + (owner.directory,),
                        "active" if self.status().state == "ready" else "retired", True, True,
                    ))
            return tuple(result)
        return self._owner.call(inventory, Deadline(time.monotonic() + _WARM_SECONDS))

    def _shutdown(self) -> bool:
        self._invalidate()
        if not self._retry_cleanup():
            return False
        try:
            if self._root is not None:
                self._root.rmdir()
                self._root = None
        except OSError as error:
            cleanup_log(error)
            self._set(cleanup_pending=True, cleanup_error=_failure("cleanup_failed"))
            return False
        self._set(cleanup_pending=False, cleanup_error=None)
        return True

    def close(self) -> SessionStatus:
        self._owner.check_caller()
        with self._lock:
            self._status = replace(self._status, closed=True)
            if self._active is not None:
                self._active.cancel.set()
        if not self._owner.close():
            self._set(cleanup_pending=True, cleanup_error=_failure("close_pending"))
        return self.status()

    def __enter__(self) -> Self:
        return self

    def __copy__(self) -> Self:
        raise TypeError("Graph session ownership cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> Self:
        raise TypeError("Graph session ownership cannot be copied")

    def __exit__(self, *args: object) -> None:
        status = self.close()
        if status.cleanup_pending:
            raise GraphSessionError(status.cleanup_error or _failure("close_pending"))
