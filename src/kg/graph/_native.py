"""Application-owned Ladybug mapping and bounded, explicitly owned native reads."""

from __future__ import annotations

import importlib
import json
import logging
import platform
import sys
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from typing import Literal, Protocol, Self, cast

from kg._execution_budget import CancelledStop, PrivateBudget, ScratchReservation
from kg.diagnostics._bounds import bounded_size
from kg.evidence._values import sha
from kg.knowledge._graph_export import GraphEntity, GraphEvidence, GraphExportItem
from kg.knowledge._selection import EntityWitness, SeedWitness

type NativeValue = None | bool | int | float | str | list[NativeValue]
BUFFER_POOL_BYTES = 256 << 20
COMMITTED_CHECKPOINT_FAILURE = (
    "Transaction committed successfully, but the post-commit checkpoint failed."
)
LOGGER = logging.getLogger(__name__)


class NativeError(Exception):
    def __init__(
        self, code: Literal["graph_unavailable", "native_error", "invalid_projection"],
    ) -> None:
        self.code = code
        super().__init__(code)


class _Result(Protocol):
    def has_next(self) -> bool: ...
    def get_next(self) -> object: ...
    def close(self) -> None: ...


class _Database(Protocol):
    def close(self) -> None: ...


class _Connection(Protocol):
    def execute(self, query: str, parameters: dict[str, NativeValue]) -> _Result: ...
    def set_query_timeout(self, timeout_in_ms: int) -> None: ...
    def close(self) -> None: ...


class _Module(Protocol):
    __version__: str

    def Database(
        self, database_path: str, *, buffer_pool_size: int, max_num_threads: int,
        read_only: bool, lazy_init: bool,
    ) -> _Database: ...
    def Connection(self, database: _Database, num_threads: int) -> _Connection: ...


def _engine() -> _Module:
    if (
        sys.platform != "darwin" or platform.machine() != "arm64"
        or sys.version_info[:2] != (3, 12) or int(platform.mac_ver()[0].split(".")[0]) < 15
    ):
        raise NativeError("graph_unavailable")
    try:
        module = cast(_Module, importlib.import_module("ladybug"))
    except (ImportError, OSError):
        raise NativeError("graph_unavailable") from None
    if module.__version__ != "0.20.4":
        raise NativeError("graph_unavailable")
    return module


def _check(budget: PrivateBudget, cancel: Event) -> None:
    budget.check_deadline()
    if cancel.is_set():
        raise CancelledStop()


def _value(value: object) -> NativeValue:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, list):
        return [_value(item) for item in value]
    raise NativeError("invalid_projection")


class NativeRows:
    def __init__(
        self, owner: NativeGraphReadHandle, result: _Result, budget: PrivateBudget, cancel: Event,
    ) -> None:
        self._owner, self._result = owner, result
        self._budget: PrivateBudget | None = budget
        self._cancel: Event | None = cancel
        self._scratch: list[ScratchReservation] = []
        self._indeterminate = False
        self._closed = False

    def __copy__(self) -> Self:
        raise TypeError("Native rows cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> Self:
        raise TypeError("Native rows cannot be copied")

    def read(self, *, limit: int = 200) -> tuple[tuple[NativeValue, ...], ...]:
        """Borrow one page until the next read/close; retaining copies needs caller accounting."""
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("Invalid native page limit")
        budget, cancel = self._budget, self._cancel
        if budget is None or cancel is None or not self._owner._usable:
            raise ValueError("Closed native rows")
        _check(budget, cancel)
        for reservation in self._scratch:
            reservation.release()
        self._scratch.clear()
        rows: list[tuple[NativeValue, ...]] = []
        total = 0
        while len(rows) < limit and self._result.has_next():
            _check(budget, cancel)
            # Native results are engine-owned; this charge covers the returned Python copy.
            row = self._result.get_next()
            if not isinstance(row, list):
                raise NativeError("invalid_projection")
            size = 4096 + bounded_size(row, 8 << 20) * 4
            total += size
            if total > 8 << 20:
                budget.reserve_scratch(65 << 20, "general")
            self._scratch.append(budget.reserve_scratch(size, "general"))
            rows.append(tuple(_value(value) for value in row))
        _check(budget, cancel)
        return tuple(rows)

    def close(self) -> None:
        if self._closed:
            return
        if self._indeterminate:
            raise NativeError("native_error")
        try:
            self._result.close()
        except Exception:
            self._indeterminate = True
            raise NativeError("native_error") from None
        finally:
            for reservation in self._scratch:
                reservation.release()
            self._scratch.clear()
            self._budget = self._cancel = None
        self._closed = True
        self._owner._rows.remove(self)


@contextmanager
def _closing_rows(rows: NativeRows) -> Iterator[NativeRows]:
    try:
        yield rows
    except BaseException:
        try:
            rows.close()
        except Exception as cleanup_error:
            LOGGER.error("Native row cleanup failed class=%s", type(cleanup_error).__name__)
        raise
    else:
        rows.close()


def _same_row(actual: tuple[NativeValue, ...], expected: tuple[NativeValue, ...]) -> bool:
    return len(actual) == len(expected) and all(
        type(value) is type(wanted) and value == wanted
        for value, wanted in zip(actual, expected, strict=True)
    )


class NativeGraphReadHandle:
    """Construct empty, establish external custody, then perform fallible open."""

    def __init__(self) -> None:
        self._database: _Database | None = None
        self._connection: _Connection | None = None
        self._rows: list[NativeRows] = []
        self._usable = False
        self._indeterminate = False

    def __copy__(self) -> Self:
        raise TypeError("Native ownership cannot be copied")

    def __deepcopy__(self, memo: dict[int, object]) -> Self:
        raise TypeError("Native ownership cannot be copied")

    def open(self, path: Path, *, read_only: bool) -> None:
        module = _engine()
        if self._database is not None or self._connection is not None:
            raise ValueError("Native handle already opened")
        self._database = module.Database(
            str(path), buffer_pool_size=BUFFER_POOL_BYTES, max_num_threads=2,
            read_only=read_only, lazy_init=True,
        )
        self._connection = module.Connection(self._database, 2)
        self._usable = True

    def execute(
        self, template: str, parameters: Mapping[str, NativeValue], *,
        budget: PrivateBudget, cancel: Event,
    ) -> NativeRows:
        if not self._usable or self._connection is None or self._rows:
            raise ValueError("Closed or busy native owner")
        if ";" in template:
            raise ValueError("Only single application-owned statements are supported")
        _check(budget, cancel)
        self._connection.set_query_timeout(max(1, int(budget.deadline.remaining() * 1000)))
        try:
            result = self._connection.execute(template, dict(parameters))
        except RuntimeError as error:
            self._execute_failed(template, error)
            _check(budget, cancel)
            raise NativeError("native_error") from None
        self._executed(template)
        rows = NativeRows(self, result, budget, cancel)
        self._rows.append(rows)
        _check(budget, cancel)
        return rows

    def _executed(self, template: str) -> None:
        pass

    def _execute_failed(self, template: str, error: RuntimeError) -> None:
        pass

    def close(self) -> None:
        self._usable = False
        if self._indeterminate:
            raise NativeError("native_error")
        for rows in tuple(self._rows):
            rows.close()
        for attribute in ("_connection", "_database"):
            handle = getattr(self, attribute)
            if handle is not None:
                try:
                    handle.close()
                except Exception:
                    self._indeterminate = True
                    raise NativeError("native_error") from None
                setattr(self, attribute, None)


NODES = {
    "Entity": ("entity_id", "name STRING, entity_type STRING, creation_sequence INT64"),
    "Assertion": ("assertion_id",
                  "contribution_sequence INT64, schema_version STRING, predicate STRING, "
                  "interpretation STRING, object_kind STRING, decision_text STRING, "
                  "attribution_json STRING, committed_at STRING, dependency_json STRING"),
    "EntityProof": ("proof_id", "entity_id STRING, contribution_id STRING, "
                    "contribution_sequence INT64, basis_kind STRING, witness_json STRING"),
    "Evidence": ("evidence_id", "corpus_id STRING, namespace STRING, document_id STRING, "
                 "revision_id STRING, anchor_id STRING, passage_id STRING, passage_set_id STRING, "
                 "state_version STRING, metadata_snapshot_id STRING, namespace_token STRING"),
    "SeedProof": ("proof_id", "namespace STRING, owner_id STRING, writer_id STRING, "
                  "seed_set_id STRING, seed_key STRING, contribution_id STRING, "
                  "membership_event_id STRING, generation INT64"),
}
EDGES = {
    "SUBJECT": ("Entity", "Assertion"), "OBJECT": ("Assertion", "Entity"),
    "SELECTED": ("Entity", "EntityProof"), "SUBJECT_PROOF": ("Assertion", "EntityProof"),
    "OBJECT_PROOF": ("Assertion", "EntityProof"),
    "ASSERTION_SOURCE": ("Assertion", "Evidence"), "ENTITY_SOURCE": ("EntityProof", "Evidence"),
    "ENTITY_SEED": ("EntityProof", "SeedProof"),
}


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def proof_id(witness: EntityWitness) -> str:
    return sha(("entity-proof/1\0" + _json(witness.model_dump(mode="json"))).encode())


def evidence_id(evidence: GraphEvidence) -> str:
    return sha(("graph-evidence/1\0" + _json(evidence.model_dump(mode="json"))).encode())


class NativeGraphWriter(NativeGraphReadHandle):
    def __init__(self) -> None:
        super().__init__()
        self._transaction = False
        self._cleanup_result: _Result | None = None

    def _executed(self, template: str) -> None:
        if template == "BEGIN TRANSACTION":
            self._transaction = True
        elif template == "COMMIT":
            self._transaction = False

    def _execute_failed(self, template: str, error: RuntimeError) -> None:
        if template == "COMMIT" and str(error).startswith(COMMITTED_CHECKPOINT_FAILURE):
            self._transaction = False

    def run(
        self, query: str, params: Mapping[str, NativeValue],
        budget: PrivateBudget, cancel: Event,
    ) -> tuple[tuple[NativeValue, ...], ...]:
        rows = self.execute(query, params, budget=budget, cancel=cancel)
        with _closing_rows(rows):
            values = rows.read()
            if rows._result.has_next():
                raise NativeError("invalid_projection")
            return values

    def close(self) -> None:
        self._usable = False
        if self._transaction and self._connection is not None and not self._indeterminate:
            for rows in tuple(self._rows):
                rows.close()
            # Cleanup is permitted after expiry; never reset the business budget.
            try:
                self._cleanup_result = self._connection.execute("ROLLBACK", {})
                self._transaction = False
                self._cleanup_result.close()
            except Exception:
                self._indeterminate = True
                raise NativeError("native_error") from None
            self._cleanup_result = None
        super().close()

    def schema(self, budget: PrivateBudget, cancel: Event) -> None:
        for table, (key, columns) in NODES.items():
            self.run(
                f"CREATE NODE TABLE {table}({key} STRING, {columns}, "
                f"PRIMARY KEY({key}))", {}, budget, cancel,
            )
        for table, (source, target) in EDGES.items():
            self.run(
                f"CREATE REL TABLE {table}(FROM {source} TO {target}, ordinal INT64)",
                {}, budget, cancel,
            )

    def _node(
        self, table: str, values: dict[str, NativeValue], budget: PrivateBudget, cancel: Event,
    ) -> None:
        key = NODES[table][0]
        assignments = ", ".join(f"n.{name}=${name}" for name in values if name != key)
        returned = ", ".join(f"n.{name}" for name in values)
        result = self.run(
            f"MERGE (n:{table} {{{key}: ${key}}}) ON CREATE SET {assignments} "
            f"RETURN {returned}", values, budget, cancel,
        )
        expected = tuple(values.values())
        if len(result) != 1 or not _same_row(result[0], expected):
            raise NativeError("invalid_projection")

    def _edge(
        self, table: str, source_id: str, target_id: str,
        budget: PrivateBudget, cancel: Event, ordinal: int = 0,
    ) -> None:
        source, target = EDGES[table]
        result = self.run(
            f"MATCH (s:{source} {{{NODES[source][0]}:$s}}), "
            f"(t:{target} {{{NODES[target][0]}:$t}}) "
            f"CREATE (s)-[r:{table} {{ordinal:$ordinal}}]->(t) "
            f"RETURN s.{NODES[source][0]}, t.{NODES[target][0]}, r.ordinal",
            {"s": source_id, "t": target_id, "ordinal": ordinal}, budget, cancel,
        )
        if len(result) != 1 or not _same_row(result[0], (source_id, target_id, ordinal)):
            raise NativeError("invalid_projection")

    def _evidence(self, evidence: GraphEvidence, budget: PrivateBudget, cancel: Event) -> str:
        proof = evidence.captured
        ref = proof.reference
        identifier = evidence_id(evidence)
        self._node("Evidence", {
            "evidence_id": identifier, "corpus_id": ref.corpus_id,
            "namespace": ref.source_namespace, "document_id": ref.document_id,
            "revision_id": ref.revision_id, "anchor_id": ref.anchor_id,
            "passage_id": ref.passage_id, "passage_set_id": evidence.passage_set_id,
            "state_version": proof.dependency.state_version,
            "metadata_snapshot_id": proof.metadata_snapshot_id,
            "namespace_token": proof.namespace_token,
        }, budget, cancel)
        return identifier

    def insert(self, item: GraphExportItem, budget: PrivateBudget, cancel: Event) -> None:
        if isinstance(item, GraphEntity):
            self._node("Entity", {
                "entity_id": item.entity_id, "name": item.name, "entity_type": item.entity_type,
                "creation_sequence": item.creation_sequence,
            }, budget, cancel)
            witness = item.witness
            pid = proof_id(witness)
            self._node("EntityProof", {
                "proof_id": pid, "entity_id": item.entity_id,
                "contribution_id": witness.contribution_id,
                "contribution_sequence": witness.contribution_sequence,
                "basis_kind": witness.basis.kind, "witness_json": witness.model_dump_json(),
            }, budget, cancel)
            self._edge("SELECTED", item.entity_id, pid, budget, cancel)
            if isinstance(witness.basis, SeedWitness):
                seed = witness.basis
                self._node("SeedProof", {
                    "proof_id": pid, "namespace": seed.namespace, "owner_id": seed.owner_id,
                    "writer_id": seed.writer_id, "seed_set_id": seed.seed_set_id,
                    "seed_key": seed.seed_key, "contribution_id": seed.contribution_id,
                    "membership_event_id": seed.membership_event_id, "generation": seed.generation,
                }, budget, cancel)
                self._edge("ENTITY_SEED", pid, pid, budget, cancel)
            for ordinal, evidence in enumerate(item.witness_evidence, 1):
                eid = self._evidence(evidence, budget, cancel)
                self._edge("ENTITY_SOURCE", pid, eid, budget, cancel, ordinal)
        else:
            self._node("Assertion", {
                "assertion_id": item.assertion_id,
                "contribution_sequence": item.contribution_sequence,
                "schema_version": item.schema_version, "predicate": item.predicate,
                "interpretation": item.interpretation,
                "object_kind": "decision" if item.decision_member is not None else "entity",
                "decision_text": item.decision_text,
                "attribution_json": item.attribution.model_dump_json(),
                "committed_at": item.committed_at, "dependency_json": item.model_dump_json(),
            }, budget, cancel)
            self._edge("SUBJECT", item.subject_id, item.assertion_id, budget, cancel)
            self._edge("SUBJECT_PROOF", item.assertion_id, proof_id(item.subject_witness),
                       budget, cancel)
            if item.object_entity_id is not None and item.object_witness is not None:
                self._edge("OBJECT", item.assertion_id, item.object_entity_id, budget, cancel)
                self._edge("OBJECT_PROOF", item.assertion_id, proof_id(item.object_witness),
                           budget, cancel)
            for ordinal, evidence in enumerate(item.support, 1):
                eid = self._evidence(evidence, budget, cancel)
                self._edge("ASSERTION_SOURCE", item.assertion_id, eid, budget, cancel, ordinal)


def scalar(
    native: NativeGraphReadHandle, query: str, budget: PrivateBudget, cancel: Event,
) -> int:
    rows = native.execute(query, {}, budget=budget, cancel=cancel)
    with _closing_rows(rows):
        result = rows.read(limit=2)
        if len(result) != 1 or len(result[0]) != 1 or type(result[0][0]) is not int:
            raise NativeError("invalid_projection")
        value = result[0][0]
        assert isinstance(value, int)
        return value
