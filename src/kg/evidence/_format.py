"""Exact admission for the complete canonical schema, independent of service capabilities."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections import defaultdict
from contextlib import nullcontext
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

from kg._sqlite import EVIDENCE_APPLICATION_ID, execute_schema, has_user_schema, read_snapshot
from kg.evidence._sql import AccountedConnection
from kg.evidence.errors import EvidenceServiceError

FORMAT = "evidence-store/3"
USER_VERSION = 3
MANIFEST_VERSION = "canonical-sqlite-manifest/1"
FORMAT_GUIDANCE = (
    "Unsupported canonical database format. This build requires evidence-store/3. "
    "Keep the existing file unchanged; initialize a new empty database and reload "
    "source documents, policy/schema and explicit knowledge. "
    "Automatic upgrade or reset is not supported."
)


def _encoded(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _hash(value: object) -> str:
    return hashlib.sha256(MANIFEST_VERSION.encode() + b"\0" + _encoded(value)).hexdigest()


def schema_sql() -> str:
    return resources.files("kg.evidence").joinpath("schema.sql").read_text(encoding="utf-8")


def _catalog(connection: sqlite3.Connection) -> tuple[tuple[str, str, str, str], ...]:
    return tuple(
        (str(row[0]), str(row[1]), str(row[2]), str(row[3]))
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema "
            "WHERE name NOT GLOB 'sqlite_*' AND sql IS NOT NULL "
            "ORDER BY type COLLATE BINARY,name COLLATE BINARY,tbl_name COLLATE BINARY"
        )
    )


@dataclass(frozen=True)
class Manifest:
    signature: str
    catalog: tuple[tuple[str, str, str, str], ...]
    objects: tuple[tuple[str, str, str, str], ...]


def _grouped(
    connection: sqlite3.Connection, sql: str,
) -> dict[str, list[tuple[object, ...]]]:
    groups: dict[str, list[tuple[object, ...]]] = defaultdict(list)
    progress = (
        connection._precise_progress()
        if isinstance(connection, AccountedConnection) else nullcontext()
    )
    with progress:
        for row in connection.execute(sql):
            groups[str(row[0])].append(tuple(row)[1:])
    return groups


def _manifest(
    connection: sqlite3.Connection, catalog: tuple[tuple[str, str, str, str], ...],
) -> Manifest:
    # Batch fresh PRAGMA reads; retain the manifest's original per-object row order.
    tables = " FROM sqlite_schema AS s"
    table_filter = " WHERE s.type='table' AND s.name NOT GLOB 'sqlite_*' AND s.sql IS NOT NULL"
    columns = _grouped(
        connection,
        "SELECT s.name,p.*" + tables + " CROSS JOIN pragma_table_xinfo(s.name) AS p"
        + table_filter + " ORDER BY s.name COLLATE BINARY,p.cid",
    )
    foreign_keys = _grouped(
        connection,
        "SELECT s.name,p.*" + tables + " CROSS JOIN pragma_foreign_key_list(s.name) AS p"
        + table_filter + " ORDER BY s.name COLLATE BINARY,p.id,p.seq",
    )
    indexes = _grouped(
        connection,
        "SELECT s.name,p.*" + tables + " CROSS JOIN pragma_index_list(s.name) AS p"
        + table_filter,
    )
    index_columns = _grouped(
        connection,
        "SELECT i.name,p.*" + tables + " CROSS JOIN pragma_index_list(s.name) AS i"
        " CROSS JOIN pragma_index_xinfo(i.name) AS p"
        + table_filter + " ORDER BY i.name COLLATE BINARY,p.seqno",
    )
    definitions: list[dict[str, object]] = []
    objects: list[tuple[str, str, str, str]] = []
    for kind, name, table, sql in catalog:
        definition: dict[str, object] = {
            "type": kind,
            "name": name,
            "table": table,
            "sql": sql,
        }
        if kind == "table":
            definition["columns"] = columns.get(name, [])
            definition["foreign_keys"] = foreign_keys.get(name, [])
            table_indexes = []
            for index in indexes.get(name, []):
                table_indexes.append(
                    {
                        "name": index[1] if index[3] == "c" else None,
                        "unique": index[2],
                        "origin": index[3],
                        "partial": index[4],
                        "columns": index_columns.get(str(index[1]), []),
                    }
                )
            definition["indexes"] = sorted(table_indexes, key=_encoded)
        elif kind == "index":
            definition["columns"] = index_columns.get(name, [])
        definitions.append(definition)
        objects.append((name, kind, table, _hash(definition)))
    return Manifest(_hash(definitions), catalog, tuple(sorted(objects)))


@lru_cache(maxsize=1)
def expected_manifest() -> Manifest:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("BEGIN")
        execute_schema(connection, schema_sql())
        return _manifest(connection, _catalog(connection))
    finally:
        connection.close()


def install_manifest(connection: sqlite3.Connection) -> None:
    if not connection.in_transaction:
        raise RuntimeError("Manifest installation requires the schema transaction")
    expected = expected_manifest()
    connection.executemany(
        "INSERT INTO schema_object_manifest(name,object_type,table_name,definition_hash) "
        "VALUES (?,?,?,?)",
        expected.objects,
    )
    connection.execute(
        "INSERT INTO store_format(singleton,format,manifest_version,schema_signature) "
        "VALUES (1,?,?,?)",
        (FORMAT, MANIFEST_VERSION, expected.signature),
    )


def check(connection: sqlite3.Connection, *, allow_empty: bool = False) -> bool:
    with read_snapshot(connection):
        application = connection.execute("PRAGMA application_id").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if allow_empty and application == 0 and version == 0 and not has_user_schema(connection):
            return False
        if application != EVIDENCE_APPLICATION_ID or version != USER_VERSION:
            raise EvidenceServiceError("unsupported", explanation=FORMAT_GUIDANCE)
        expected = expected_manifest()
        catalog = _catalog(connection)
        if catalog != expected.catalog:
            raise EvidenceServiceError("unsupported", explanation=FORMAT_GUIDANCE)
        actual = _manifest(connection, catalog)
        marker = [
            tuple(row)
            for row in connection.execute(
                "SELECT singleton,format,manifest_version,schema_signature FROM store_format"
            )
        ]
        objects = tuple(
            tuple(row)
            for row in connection.execute(
                "SELECT name,object_type,table_name,definition_hash "
                "FROM schema_object_manifest ORDER BY name COLLATE BINARY"
            )
        )
        if (
            actual.signature != expected.signature
            or marker != [(1, FORMAT, MANIFEST_VERSION, expected.signature)]
            or objects != expected.objects
        ):
            raise EvidenceServiceError("unsupported", explanation=FORMAT_GUIDANCE)
        return True
