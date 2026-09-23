from __future__ import annotations

import hashlib
import json
import sqlite3

LEXICAL_INDEX_VERSION = "corpus-current-bm25-v1"


def corpus_fingerprint(connection: sqlite3.Connection, corpus_id: str) -> str:
    rows = connection.execute(
        """
        SELECT sd.document_id, sd.current_revision_id,
               sr.indexed_parser_version, sr.indexed_config_hash
        FROM source_document sd
        JOIN source_revision sr ON sr.revision_id = sd.current_revision_id
        WHERE sd.corpus_id = ? AND sd.is_active = 1
        ORDER BY sd.document_id
        """,
        (corpus_id,),
    ).fetchall()
    return hashlib.sha256(
        json.dumps([dict(row) for row in rows], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def lexical_table(corpus_id: str) -> str:
    return f"current_fts_{hashlib.sha256(corpus_id.encode()).hexdigest()}"


def refresh_lexical_index(connection: sqlite3.Connection, corpus_id: str) -> None:
    fingerprint = corpus_fingerprint(connection, corpus_id)
    existing = connection.execute(
        "SELECT source_fingerprint, version FROM lexical_projection WHERE corpus_id = ?",
        (corpus_id,),
    ).fetchone()
    if existing and (existing["source_fingerprint"], existing["version"]) == (
        fingerprint, LEXICAL_INDEX_VERSION
    ):
        return
    table = lexical_table(corpus_id)
    connection.execute(
        f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING fts5(
            passage_id UNINDEXED, title, heading_text, passage_text, aliases,
            tokenize = 'unicode61'
        )
        """
    )
    connection.execute(f"DELETE FROM {table}")
    connection.execute(
        f"""
        INSERT INTO {table} (passage_id, title, heading_text, passage_text, aliases)
        SELECT f.passage_id, f.title, f.heading_text, f.passage_text, f.aliases
        FROM passage_fts f
        JOIN passage p ON p.passage_id = f.passage_id
        JOIN source_anchor sa ON sa.anchor_id = p.anchor_id
        JOIN source_document sd ON sd.document_id = p.document_id
        WHERE sd.corpus_id = ? AND sd.is_active = 1
          AND sa.revision_id = sd.current_revision_id
        ORDER BY sd.source_path, sa.start_offset, p.passage_id
        """,
        (corpus_id,),
    )
    connection.execute(
        """
        INSERT INTO lexical_projection (corpus_id, source_fingerprint, version)
        VALUES (?, ?, ?)
        ON CONFLICT(corpus_id) DO UPDATE SET
            source_fingerprint = excluded.source_fingerprint,
            version = excluded.version
        """,
        (corpus_id, fingerprint, LEXICAL_INDEX_VERSION),
    )
