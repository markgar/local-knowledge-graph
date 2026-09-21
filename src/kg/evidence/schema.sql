CREATE TABLE store_format (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    format TEXT NOT NULL CHECK (format = 'evidence-store/1')
);
INSERT INTO store_format VALUES (1, 'evidence-store/1');

CREATE TABLE corpus (
    corpus_id TEXT PRIMARY KEY,
    policy_version TEXT NOT NULL,
    registration_json TEXT NOT NULL
);
CREATE TABLE source_namespace (
    corpus_id TEXT NOT NULL REFERENCES corpus,
    namespace TEXT NOT NULL,
    policy_token TEXT NOT NULL,
    PRIMARY KEY (corpus_id, namespace)
);
CREATE TABLE writer_binding (
    corpus_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    writer_id TEXT NOT NULL,
    synchronization_scope TEXT NOT NULL,
    PRIMARY KEY (corpus_id, namespace, owner_id, writer_id, synchronization_scope),
    FOREIGN KEY (corpus_id, namespace) REFERENCES source_namespace
);
CREATE TABLE policy_grant (
    corpus_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    principal_id TEXT NOT NULL,
    grant_name TEXT NOT NULL
        CHECK (grant_name IN ('read', 'write_documents', 'write_knowledge', 'seed')),
    owner_id TEXT NOT NULL,
    writer_id TEXT NOT NULL,
    synchronization_scope TEXT NOT NULL,
    PRIMARY KEY (corpus_id, namespace, principal_id, grant_name,
                 owner_id, writer_id, synchronization_scope),
    FOREIGN KEY (corpus_id, namespace) REFERENCES source_namespace
);
CREATE TABLE document (
    document_id TEXT PRIMARY KEY,
    corpus_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    external_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    synchronization_scope TEXT NOT NULL,
    current_state TEXT NOT NULL,
    UNIQUE (corpus_id, namespace, external_id),
    FOREIGN KEY (corpus_id, namespace) REFERENCES source_namespace,
    FOREIGN KEY (document_id, current_state)
        REFERENCES document_state(document_id, state_version) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE revision (
    revision_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES document,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    content_hash TEXT NOT NULL,
    content BLOB NOT NULL CHECK (typeof(content) = 'blob'),
    byte_length INTEGER NOT NULL CHECK (byte_length = length(content)),
    created_at TEXT NOT NULL,
    UNIQUE (document_id, content_hash),
    UNIQUE (document_id, sequence),
    UNIQUE (document_id, revision_id)
);
CREATE TABLE anchor_set (
    set_id TEXT PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES revision,
    UNIQUE (revision_id, set_id)
);
CREATE TABLE document_state (
    state_version TEXT PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES document,
    sequence INTEGER NOT NULL CHECK (sequence > 0),
    previous_state TEXT,
    revision_id TEXT NOT NULL,
    set_id TEXT NOT NULL,
    metadata_snapshot_id TEXT NOT NULL,
    source TEXT NOT NULL CHECK (source IN ('active', 'inactive')),
    namespace_token TEXT NOT NULL,
    passage_policy TEXT NOT NULL,
    change_kind TEXT NOT NULL CHECK (change_kind IN ('put', 'remove', 'policy')),
    committed_at TEXT NOT NULL,
    UNIQUE (document_id, sequence),
    UNIQUE (document_id, state_version),
    UNIQUE (document_id, revision_id, state_version),
    FOREIGN KEY (document_id, previous_state) REFERENCES document_state(document_id, state_version),
    FOREIGN KEY (document_id, revision_id) REFERENCES revision(document_id, revision_id),
    FOREIGN KEY (revision_id, set_id) REFERENCES anchor_set(revision_id, set_id),
    FOREIGN KEY (document_id, revision_id, state_version, metadata_snapshot_id)
        REFERENCES metadata_snapshot(document_id, revision_id, state_version, snapshot_id)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE metadata_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    state_version TEXT NOT NULL,
    metadata_json TEXT NOT NULL,
    UNIQUE (document_id, revision_id, state_version, snapshot_id),
    FOREIGN KEY (document_id, revision_id, state_version)
        REFERENCES document_state(document_id, revision_id, state_version)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE anchor (
    anchor_id TEXT PRIMARY KEY,
    document_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    local_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    start_offset INTEGER NOT NULL CHECK (start_offset >= 0),
    end_offset INTEGER NOT NULL CHECK (end_offset > start_offset),
    quote TEXT NOT NULL,
    quote_hash TEXT NOT NULL,
    origin_state TEXT NOT NULL,
    UNIQUE (revision_id, ordinal),
    UNIQUE (revision_id, anchor_id),
    UNIQUE (revision_id, anchor_id, local_id),
    FOREIGN KEY (document_id, revision_id) REFERENCES revision(document_id, revision_id),
    FOREIGN KEY (document_id, revision_id, origin_state)
        REFERENCES document_state(document_id, revision_id, state_version)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE anchor_set_member (
    set_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    anchor_id TEXT NOT NULL,
    local_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (ordinal > 0),
    PRIMARY KEY (set_id, anchor_id),
    UNIQUE (set_id, ordinal),
    UNIQUE (set_id, local_id),
    FOREIGN KEY (revision_id, set_id) REFERENCES anchor_set(revision_id, set_id),
    FOREIGN KEY (revision_id, anchor_id, local_id) REFERENCES anchor(revision_id, anchor_id, local_id)
);
CREATE TABLE processing_state (
    state_version TEXT PRIMARY KEY REFERENCES document_state,
    indexing TEXT NOT NULL CHECK (indexing IN ('pending', 'ready', 'failed')),
    enrichment TEXT NOT NULL CHECK (enrichment IN ('pending', 'partial', 'complete', 'failed')),
    reason TEXT NOT NULL CHECK (reason = 'processor_not_available')
);
CREATE TABLE write_key (
    key_id TEXT PRIMARY KEY,
    corpus_id TEXT NOT NULL REFERENCES corpus,
    writer_id TEXT NOT NULL,
    operation TEXT NOT NULL,
    key_hash TEXT NOT NULL,
    digest TEXT NOT NULL,
    digest_version TEXT NOT NULL CHECK (digest_version = 'e1-request-digest/1'),
    status TEXT NOT NULL CHECK (status IN ('applied', 'unchanged')),
    committed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    expired INTEGER NOT NULL DEFAULT 0 CHECK (expired IN (0, 1)),
    UNIQUE (corpus_id, writer_id, operation, key_hash)
);
CREATE TABLE write_response (
    key_id TEXT PRIMARY KEY REFERENCES write_key,
    document_id TEXT NOT NULL REFERENCES document,
    receipt_json TEXT NOT NULL
);
CREATE TABLE write_provenance (
    key_id TEXT PRIMARY KEY REFERENCES write_key,
    state_version TEXT NOT NULL REFERENCES document_state,
    attribution_json TEXT NOT NULL
);
CREATE TABLE receipt_clock (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    watermark TEXT NOT NULL
);
CREATE INDEX anchor_revision_idx ON anchor(revision_id, ordinal);
CREATE INDEX state_document_idx ON document_state(document_id, sequence);
