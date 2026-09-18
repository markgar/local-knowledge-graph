CREATE TABLE IF NOT EXISTS source_document (
    document_id TEXT PRIMARY KEY,
    corpus_id TEXT NOT NULL,
    source_path TEXT NOT NULL,
    title TEXT NOT NULL,
    current_revision_id TEXT,
    is_active INTEGER NOT NULL DEFAULT 1 CHECK (is_active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (corpus_id, source_path)
);

CREATE TABLE IF NOT EXISTS source_revision (
    revision_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES source_document(document_id),
    content_hash TEXT NOT NULL,
    observed_mtime TEXT,
    ingested_at TEXT NOT NULL,
    frontmatter_json TEXT NOT NULL,
    indexed_parser_version TEXT,
    indexed_config_hash TEXT,
    UNIQUE (document_id, content_hash)
);

CREATE TABLE IF NOT EXISTS source_anchor (
    anchor_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES source_revision(revision_id),
    structural_path TEXT NOT NULL,
    heading_path_json TEXT NOT NULL,
    anchor_kind TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL,
    quote TEXT NOT NULL,
    quote_hash TEXT NOT NULL,
    UNIQUE (revision_id, structural_path, start_offset, end_offset)
);

CREATE TABLE IF NOT EXISTS ingest_run (
    run_id TEXT PRIMARY KEY,
    corpus_id TEXT NOT NULL,
    parser_version TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    started_at TEXT NOT NULL,
    completed_at TEXT,
    status TEXT NOT NULL,
    counts_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS entity (
    entity_key TEXT PRIMARY KEY,
    corpus_id TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    entity_type TEXT NOT NULL,
    canonical_name TEXT NOT NULL,
    UNIQUE (corpus_id, entity_id),
    UNIQUE (corpus_id, entity_type, canonical_name)
);

CREATE TABLE IF NOT EXISTS entity_alias (
    entity_key TEXT NOT NULL REFERENCES entity(entity_key),
    alias TEXT NOT NULL,
    normalized_alias TEXT NOT NULL,
    PRIMARY KEY (entity_key, normalized_alias)
);

CREATE TABLE IF NOT EXISTS mention (
    mention_id TEXT PRIMARY KEY,
    entity_key TEXT NOT NULL REFERENCES entity(entity_key),
    anchor_id TEXT NOT NULL REFERENCES source_anchor(anchor_id),
    matched_text TEXT NOT NULL,
    start_offset INTEGER NOT NULL,
    end_offset INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS relationship (
    relationship_id TEXT PRIMARY KEY,
    source_entity_key TEXT NOT NULL REFERENCES entity(entity_key),
    target_entity_key TEXT NOT NULL REFERENCES entity(entity_key),
    relationship_type TEXT NOT NULL,
    anchor_id TEXT NOT NULL REFERENCES source_anchor(anchor_id)
);

CREATE TABLE IF NOT EXISTS action_item (
    record_id TEXT PRIMARY KEY,
    anchor_id TEXT NOT NULL REFERENCES source_anchor(anchor_id),
    text TEXT NOT NULL,
    status TEXT NOT NULL,
    owner TEXT,
    due_date TEXT
);

CREATE TABLE IF NOT EXISTS decision (
    record_id TEXT PRIMARY KEY,
    anchor_id TEXT NOT NULL REFERENCES source_anchor(anchor_id),
    text TEXT NOT NULL,
    event_time TEXT
);

CREATE TABLE IF NOT EXISTS blocker (
    record_id TEXT PRIMARY KEY,
    anchor_id TEXT NOT NULL REFERENCES source_anchor(anchor_id),
    text TEXT NOT NULL,
    event_time TEXT
);

CREATE TABLE IF NOT EXISTS conflict (
    record_id TEXT PRIMARY KEY,
    anchor_id TEXT NOT NULL REFERENCES source_anchor(anchor_id),
    text TEXT NOT NULL,
    event_time TEXT
);

CREATE TABLE IF NOT EXISTS passage (
    passage_id TEXT PRIMARY KEY,
    anchor_id TEXT NOT NULL REFERENCES source_anchor(anchor_id),
    document_id TEXT NOT NULL REFERENCES source_document(document_id),
    title TEXT NOT NULL,
    heading_text TEXT NOT NULL,
    passage_text TEXT NOT NULL,
    event_time TEXT
);

CREATE VIRTUAL TABLE IF NOT EXISTS passage_fts USING fts5(
    passage_id UNINDEXED,
    title,
    heading_text,
    passage_text,
    aliases,
    tokenize = 'unicode61'
);

CREATE INDEX IF NOT EXISTS source_revision_document_idx
ON source_revision(document_id);

CREATE INDEX IF NOT EXISTS source_anchor_revision_idx
ON source_anchor(revision_id);

CREATE INDEX IF NOT EXISTS action_item_anchor_idx
ON action_item(anchor_id);

CREATE INDEX IF NOT EXISTS decision_anchor_idx
ON decision(anchor_id);

CREATE INDEX IF NOT EXISTS blocker_anchor_idx
ON blocker(anchor_id);

CREATE INDEX IF NOT EXISTS conflict_anchor_idx
ON conflict(anchor_id);

CREATE INDEX IF NOT EXISTS passage_document_idx
ON passage(document_id);
