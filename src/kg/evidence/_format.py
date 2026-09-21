"""Exact admission for the complete canonical schema, independent of service capabilities."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources

from kg._sqlite import EVIDENCE_APPLICATION_ID, execute_schema, has_user_schema, read_snapshot
from kg.evidence.errors import EvidenceServiceError

FORMAT = "evidence-store/2"
USER_VERSION = 2
MANIFEST_VERSION = "canonical-sqlite-manifest/1"


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


def _manifest(connection: sqlite3.Connection) -> Manifest:
    catalog = _catalog(connection)
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
            definition["columns"] = [
                tuple(row)
                for row in connection.execute("SELECT * FROM pragma_table_xinfo(?)", (name,))
            ]
            definition["foreign_keys"] = [
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM pragma_foreign_key_list(?) ORDER BY id,seq", (name,)
                )
            ]
            indexes = []
            for index in connection.execute("SELECT * FROM pragma_index_list(?)", (name,)):
                indexes.append(
                    {
                        "name": index[1] if index[3] == "c" else None,
                        "unique": index[2],
                        "origin": index[3],
                        "partial": index[4],
                        "columns": [
                            tuple(row)
                            for row in connection.execute(
                                "SELECT * FROM pragma_index_xinfo(?) ORDER BY seqno", (index[1],)
                            )
                        ],
                    }
                )
            definition["indexes"] = sorted(indexes, key=_encoded)
        elif kind == "index":
            definition["columns"] = [
                tuple(row)
                for row in connection.execute(
                    "SELECT * FROM pragma_index_xinfo(?) ORDER BY seqno", (name,)
                )
            ]
        definitions.append(definition)
        objects.append((name, kind, table, _hash(definition)))
    return Manifest(_hash(definitions), catalog, tuple(sorted(objects)))


@lru_cache(maxsize=1)
def expected_manifest() -> Manifest:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("BEGIN")
        execute_schema(connection, schema_sql())
        return _manifest(connection)
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
            raise EvidenceServiceError("unsupported")
        expected = expected_manifest()
        if _catalog(connection) != expected.catalog:
            raise EvidenceServiceError("unsupported")
        actual = _manifest(connection)
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
            raise EvidenceServiceError("unsupported")
        return True
