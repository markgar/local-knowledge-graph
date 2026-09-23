CREATE TABLE store_format (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    format TEXT NOT NULL CHECK (format = 'evidence-store/2'),
    manifest_version TEXT NOT NULL CHECK (manifest_version = 'canonical-sqlite-manifest/1'),
    schema_signature TEXT NOT NULL
);
CREATE TABLE schema_object_manifest (
    name TEXT NOT NULL PRIMARY KEY,
    object_type TEXT NOT NULL CHECK (object_type IN ('table', 'index', 'view', 'trigger')),
    table_name TEXT NOT NULL,
    definition_hash TEXT NOT NULL
);

CREATE TABLE corpus (
    corpus_id TEXT NOT NULL PRIMARY KEY,
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
    CHECK ((grant_name = 'write_documents' AND owner_id != '' AND writer_id != ''
            AND synchronization_scope != '')
        OR (grant_name != 'write_documents' AND owner_id = '' AND writer_id = ''
            AND synchronization_scope = '')),
    FOREIGN KEY (corpus_id, namespace) REFERENCES source_namespace
);
CREATE TABLE document (
    document_id TEXT NOT NULL PRIMARY KEY,
    corpus_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    external_id TEXT NOT NULL,
    owner_id TEXT NOT NULL,
    synchronization_scope TEXT NOT NULL,
    current_state TEXT NOT NULL,
    UNIQUE (corpus_id, namespace, external_id),
    UNIQUE (corpus_id, namespace, document_id),
    UNIQUE (corpus_id, document_id),
    UNIQUE (corpus_id, namespace, owner_id, synchronization_scope, document_id),
    FOREIGN KEY (corpus_id, namespace) REFERENCES source_namespace,
    FOREIGN KEY (document_id, current_state)
        REFERENCES document_state(document_id, state_version) DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE revision (
    revision_id TEXT NOT NULL PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES document,
    sequence INTEGER NOT NULL CHECK (typeof(sequence) = 'integer' AND sequence > 0),
    content_hash TEXT NOT NULL,
    content BLOB NOT NULL CHECK (typeof(content) = 'blob'),
    byte_length INTEGER NOT NULL CHECK (typeof(byte_length) = 'integer' AND byte_length = length(content)),
    created_at TEXT NOT NULL,
    UNIQUE (document_id, content_hash),
    UNIQUE (document_id, sequence),
    UNIQUE (document_id, revision_id)
);
CREATE TABLE anchor_set (
    set_id TEXT NOT NULL PRIMARY KEY,
    revision_id TEXT NOT NULL REFERENCES revision,
    UNIQUE (revision_id, set_id)
);
CREATE TABLE document_state (
    state_version TEXT NOT NULL PRIMARY KEY,
    document_id TEXT NOT NULL REFERENCES document,
    sequence INTEGER NOT NULL CHECK (typeof(sequence) = 'integer' AND sequence > 0),
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
    snapshot_id TEXT NOT NULL PRIMARY KEY,
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
    anchor_id TEXT NOT NULL PRIMARY KEY,
    document_id TEXT NOT NULL,
    revision_id TEXT NOT NULL,
    local_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (typeof(ordinal) = 'integer' AND ordinal > 0),
    start_offset INTEGER NOT NULL CHECK (typeof(start_offset) = 'integer' AND start_offset >= 0),
    end_offset INTEGER NOT NULL CHECK (typeof(end_offset) = 'integer' AND end_offset > start_offset),
    quote TEXT NOT NULL,
    quote_hash TEXT NOT NULL,
    origin_state TEXT NOT NULL,
    origin_kind TEXT NOT NULL CHECK (origin_kind IN ('supplied', 'generated')),
    origin_key TEXT NOT NULL,
    UNIQUE (revision_id, origin_kind, origin_key),
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
    ordinal INTEGER NOT NULL CHECK (typeof(ordinal) = 'integer' AND ordinal > 0),
    PRIMARY KEY (set_id, anchor_id),
    UNIQUE (set_id, ordinal),
    UNIQUE (set_id, local_id),
    FOREIGN KEY (revision_id, set_id) REFERENCES anchor_set(revision_id, set_id),
    FOREIGN KEY (revision_id, anchor_id, local_id) REFERENCES anchor(revision_id, anchor_id, local_id)
);
CREATE TABLE processing_state (
    state_version TEXT NOT NULL PRIMARY KEY REFERENCES document_state,
    indexing TEXT NOT NULL CHECK (indexing IN ('pending', 'ready', 'failed')),
    enrichment TEXT NOT NULL CHECK (enrichment IN ('pending', 'partial', 'complete', 'failed')),
    indexing_reason TEXT NOT NULL CHECK (indexing_reason IN (
        'processor_not_available', 'not_processed', 'unsupported_policy',
        'provider_unavailable', 'invalid_provider_output', 'state_changed',
        'storage_failure', 'superseded_attempt', 'claim_lost', 'inactive', 'ready'
    )),
    enrichment_reason TEXT NOT NULL CHECK (enrichment_reason = 'processor_not_available')
);
CREATE TABLE write_key (
    key_id TEXT NOT NULL PRIMARY KEY,
    corpus_id TEXT NOT NULL REFERENCES corpus,
    writer_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN (
        'put_document', 'remove_document', 'enrich', 'replace_seed_set'
    )),
    key_hash TEXT NOT NULL,
    digest TEXT NOT NULL,
    digest_version TEXT NOT NULL CHECK (
        (operation IN ('put_document', 'remove_document') AND digest_version = 'e1-request-digest/1')
        OR (operation IN ('enrich', 'replace_seed_set') AND digest_version = 'k1-request-digest/1')
    ),
    status TEXT NOT NULL CHECK (status IN ('applied', 'unchanged')),
    committed_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    expired INTEGER NOT NULL DEFAULT 0 CHECK (expired IN (0, 1)),
    UNIQUE (corpus_id, writer_id, operation, key_hash),
    UNIQUE (corpus_id, key_id),
    UNIQUE (corpus_id, key_id, writer_id, operation, key_hash),
    CHECK (expires_at >= committed_at)
);
CREATE TABLE write_response (
    key_id TEXT NOT NULL PRIMARY KEY,
    corpus_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    receipt_json TEXT NOT NULL,
    FOREIGN KEY (corpus_id, key_id) REFERENCES write_key(corpus_id, key_id),
    FOREIGN KEY (corpus_id, document_id) REFERENCES document(corpus_id, document_id)
);
CREATE TABLE write_provenance (
    key_id TEXT NOT NULL PRIMARY KEY,
    corpus_id TEXT NOT NULL,
    document_id TEXT NOT NULL,
    state_version TEXT NOT NULL,
    attribution_json TEXT NOT NULL,
    FOREIGN KEY (corpus_id, key_id) REFERENCES write_key(corpus_id, key_id),
    FOREIGN KEY (document_id, state_version) REFERENCES document_state(document_id, state_version)
);
CREATE TABLE receipt_clock (
    singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
    watermark TEXT NOT NULL
);
CREATE INDEX anchor_revision_idx ON anchor(revision_id, ordinal);
CREATE INDEX state_document_idx ON document_state(document_id, sequence);

CREATE TABLE knowledge_schema (
    corpus_id TEXT NOT NULL PRIMARY KEY REFERENCES corpus,
    schema_version TEXT NOT NULL,
    definition_json TEXT NOT NULL,
    definition_hash TEXT NOT NULL,
    UNIQUE (corpus_id, schema_version)
);
CREATE TABLE knowledge_writer_binding (
    corpus_id TEXT NOT NULL, namespace TEXT NOT NULL, principal_id TEXT NOT NULL,
    owner_id TEXT NOT NULL, writer_id TEXT NOT NULL,
    PRIMARY KEY (corpus_id, namespace, principal_id, owner_id, writer_id),
    FOREIGN KEY (corpus_id, namespace) REFERENCES source_namespace
);
CREATE TABLE entity (
    entity_id TEXT NOT NULL PRIMARY KEY, corpus_id TEXT NOT NULL REFERENCES corpus,
    name TEXT NOT NULL, entity_type TEXT NOT NULL,
    creation_sequence INTEGER NOT NULL
        CHECK (typeof(creation_sequence) = 'integer' AND creation_sequence > 0),
    retired INTEGER NOT NULL DEFAULT 0 CHECK (retired IN (0, 1)),
    UNIQUE (corpus_id, entity_id), UNIQUE (corpus_id, creation_sequence)
);
CREATE TABLE contribution (
    contribution_id TEXT NOT NULL PRIMARY KEY, corpus_id TEXT NOT NULL,
    sequence INTEGER NOT NULL CHECK (typeof(sequence) = 'integer' AND sequence > 0),
    kind TEXT NOT NULL CHECK (kind IN ('entity_support', 'alias', 'identifier', 'mention', 'assertion')),
    key_id TEXT NOT NULL, local_id TEXT NOT NULL,
    owner_id TEXT NOT NULL, writer_id TEXT NOT NULL,
    producer TEXT NOT NULL, producer_version TEXT NOT NULL,
    model_id TEXT, configuration_id TEXT, schema_version TEXT NOT NULL, committed_at TEXT NOT NULL,
    support_kind TEXT NOT NULL CHECK (support_kind IN ('source', 'seed')),
    CHECK (kind NOT IN ('mention', 'assertion') OR support_kind = 'source'),
    UNIQUE (corpus_id, contribution_id), UNIQUE (corpus_id, sequence), UNIQUE (key_id, local_id),
    UNIQUE (corpus_id, contribution_id, kind),
    UNIQUE (corpus_id, contribution_id, support_kind),
    FOREIGN KEY (corpus_id, key_id) REFERENCES write_key(corpus_id, key_id)
        DEFERRABLE INITIALLY DEFERRED,
    FOREIGN KEY (corpus_id, schema_version) REFERENCES knowledge_schema(corpus_id, schema_version)
);
CREATE TABLE entity_support (
    corpus_id TEXT NOT NULL, contribution_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind = 'entity_support'),
    entity_id TEXT NOT NULL, attested_name TEXT NOT NULL, attested_type TEXT NOT NULL,
    PRIMARY KEY (corpus_id, contribution_id),
    FOREIGN KEY (corpus_id, contribution_id, kind)
        REFERENCES contribution(corpus_id, contribution_id, kind),
    FOREIGN KEY (corpus_id, entity_id) REFERENCES entity(corpus_id, entity_id)
);
CREATE TABLE alias (
    corpus_id TEXT NOT NULL, contribution_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind = 'alias'), entity_id TEXT NOT NULL, alias TEXT NOT NULL,
    PRIMARY KEY (corpus_id, contribution_id),
    FOREIGN KEY (corpus_id, contribution_id, kind)
        REFERENCES contribution(corpus_id, contribution_id, kind),
    FOREIGN KEY (corpus_id, entity_id) REFERENCES entity(corpus_id, entity_id)
);
CREATE TABLE identifier (
    corpus_id TEXT NOT NULL, contribution_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind = 'identifier'),
    entity_id TEXT NOT NULL, scheme TEXT NOT NULL, value TEXT NOT NULL,
    PRIMARY KEY (corpus_id, contribution_id),
    FOREIGN KEY (corpus_id, contribution_id, kind)
        REFERENCES contribution(corpus_id, contribution_id, kind),
    FOREIGN KEY (corpus_id, entity_id) REFERENCES entity(corpus_id, entity_id)
);
CREATE TABLE mention (
    corpus_id TEXT NOT NULL, contribution_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind = 'mention'), entity_id TEXT NOT NULL,
    PRIMARY KEY (corpus_id, contribution_id),
    FOREIGN KEY (corpus_id, contribution_id, kind)
        REFERENCES contribution(corpus_id, contribution_id, kind),
    FOREIGN KEY (corpus_id, entity_id) REFERENCES entity(corpus_id, entity_id)
);
CREATE TABLE assertion (
    corpus_id TEXT NOT NULL, contribution_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind = 'assertion'), subject_id TEXT NOT NULL, predicate TEXT NOT NULL,
    interpretation TEXT NOT NULL CHECK (interpretation IN ('explicit', 'inferred')),
    object_kind TEXT NOT NULL CHECK (object_kind IN ('entity', 'string', 'integer', 'boolean', 'timestamp')),
    object_entity_id TEXT, object_string TEXT, object_integer TEXT,
    object_boolean INTEGER CHECK (object_boolean IS NULL OR object_boolean IN (0, 1)),
    object_timestamp TEXT,
    CHECK ((object_kind = 'entity') = (object_entity_id IS NOT NULL)),
    CHECK ((object_kind = 'string') = (object_string IS NOT NULL)),
    CHECK ((object_kind = 'integer') = (object_integer IS NOT NULL)),
    CHECK ((object_kind = 'boolean') = (object_boolean IS NOT NULL)),
    CHECK ((object_kind = 'timestamp') = (object_timestamp IS NOT NULL)),
    PRIMARY KEY (corpus_id, contribution_id),
    FOREIGN KEY (corpus_id, contribution_id, kind)
        REFERENCES contribution(corpus_id, contribution_id, kind),
    FOREIGN KEY (corpus_id, subject_id) REFERENCES entity(corpus_id, entity_id),
    FOREIGN KEY (corpus_id, object_entity_id) REFERENCES entity(corpus_id, entity_id)
);
CREATE TABLE contribution_evidence (
    corpus_id TEXT NOT NULL, contribution_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (typeof(ordinal) = 'integer' AND ordinal BETWEEN 1 AND 200),
    support_kind TEXT NOT NULL CHECK (support_kind = 'source'),
    namespace TEXT NOT NULL, document_id TEXT NOT NULL, revision_id TEXT NOT NULL,
    anchor_id TEXT NOT NULL, state_version TEXT NOT NULL, metadata_snapshot_id TEXT NOT NULL,
    passage_set_id TEXT, passage_id TEXT,
    CHECK ((passage_id IS NULL) = (passage_set_id IS NULL)),
    PRIMARY KEY (contribution_id, ordinal),
    FOREIGN KEY (corpus_id, contribution_id, support_kind)
        REFERENCES contribution(corpus_id, contribution_id, support_kind),
    FOREIGN KEY (corpus_id, namespace, document_id)
        REFERENCES document(corpus_id, namespace, document_id),
    FOREIGN KEY (document_id, revision_id) REFERENCES revision(document_id, revision_id),
    FOREIGN KEY (revision_id, anchor_id) REFERENCES anchor(revision_id, anchor_id),
    FOREIGN KEY (document_id, revision_id, state_version)
        REFERENCES document_state(document_id, revision_id, state_version),
    FOREIGN KEY (document_id, revision_id, state_version, metadata_snapshot_id)
        REFERENCES metadata_snapshot(document_id, revision_id, state_version, snapshot_id),
    FOREIGN KEY (corpus_id, namespace, document_id, revision_id, passage_set_id, passage_id, anchor_id)
        REFERENCES passage(corpus_id, namespace, document_id, revision_id, passage_set_id, passage_id, anchor_id),
    FOREIGN KEY (document_id, state_version, passage_set_id)
        REFERENCES state_passage_set(document_id, state_version, passage_set_id)
);
CREATE TABLE seed_set (
    corpus_id TEXT NOT NULL, namespace TEXT NOT NULL, owner_id TEXT NOT NULL,
    writer_id TEXT NOT NULL, seed_set_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (typeof(generation) = 'integer' AND generation >= 1),
    created_at TEXT NOT NULL,
    PRIMARY KEY (corpus_id, namespace, owner_id, writer_id, seed_set_id),
    FOREIGN KEY (corpus_id, namespace) REFERENCES source_namespace
);
CREATE TABLE seed_slot (
    corpus_id TEXT NOT NULL, namespace TEXT NOT NULL, owner_id TEXT NOT NULL,
    writer_id TEXT NOT NULL, seed_set_id TEXT NOT NULL, seed_key TEXT NOT NULL,
    entity_id TEXT, current_contribution_id TEXT,
    PRIMARY KEY (corpus_id, namespace, owner_id, writer_id, seed_set_id, seed_key),
    FOREIGN KEY (corpus_id, namespace, owner_id, writer_id, seed_set_id) REFERENCES seed_set,
    FOREIGN KEY (corpus_id, entity_id) REFERENCES entity(corpus_id, entity_id),
    FOREIGN KEY (corpus_id, current_contribution_id) REFERENCES contribution(corpus_id, contribution_id)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE contribution_seed (
    corpus_id TEXT NOT NULL, contribution_id TEXT NOT NULL,
    support_kind TEXT NOT NULL CHECK (support_kind = 'seed'),
    namespace TEXT NOT NULL, owner_id TEXT NOT NULL, writer_id TEXT NOT NULL,
    seed_set_id TEXT NOT NULL, seed_key TEXT NOT NULL,
    PRIMARY KEY (corpus_id, contribution_id),
    FOREIGN KEY (corpus_id, contribution_id, support_kind)
        REFERENCES contribution(corpus_id, contribution_id, support_kind),
    FOREIGN KEY (corpus_id, namespace, owner_id, writer_id, seed_set_id, seed_key) REFERENCES seed_slot
);
CREATE TABLE seed_membership_event (
    event_id TEXT NOT NULL PRIMARY KEY, corpus_id TEXT NOT NULL, namespace TEXT NOT NULL,
    owner_id TEXT NOT NULL, writer_id TEXT NOT NULL, seed_set_id TEXT NOT NULL, seed_key TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (typeof(generation) = 'integer' AND generation >= 1),
    sequence INTEGER NOT NULL CHECK (typeof(sequence) = 'integer' AND sequence > 0),
    event_kind TEXT NOT NULL CHECK (event_kind IN ('activated', 'withdrawn')),
    contribution_id TEXT NOT NULL, key_id TEXT NOT NULL, attribution_json TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    UNIQUE (corpus_id, namespace, owner_id, writer_id, seed_set_id, sequence),
    FOREIGN KEY (corpus_id, namespace, owner_id, writer_id, seed_set_id, seed_key) REFERENCES seed_slot,
    FOREIGN KEY (corpus_id, contribution_id) REFERENCES contribution(corpus_id, contribution_id),
    FOREIGN KEY (corpus_id, key_id) REFERENCES write_key(corpus_id, key_id)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE knowledge_write_response (
    key_id TEXT NOT NULL PRIMARY KEY, corpus_id TEXT NOT NULL,
    receipt_kind TEXT NOT NULL CHECK (receipt_kind IN ('enrichment', 'seed_set')),
    receipt_json TEXT NOT NULL, authorization_json TEXT NOT NULL,
    FOREIGN KEY (corpus_id, key_id) REFERENCES write_key(corpus_id, key_id)
);
CREATE TABLE knowledge_write_provenance (
    key_id TEXT NOT NULL PRIMARY KEY, corpus_id TEXT NOT NULL,
    schema_version TEXT NOT NULL, attribution_json TEXT NOT NULL,
    FOREIGN KEY (corpus_id, key_id) REFERENCES write_key(corpus_id, key_id),
    FOREIGN KEY (corpus_id, schema_version) REFERENCES knowledge_schema(corpus_id, schema_version)
);

CREATE TABLE passage_policy_definition (
    policy_token TEXT NOT NULL PRIMARY KEY, definition_hash TEXT NOT NULL, definition_json TEXT NOT NULL,
    UNIQUE (policy_token, definition_hash)
);
CREATE TABLE passage_set (
    passage_set_id TEXT NOT NULL PRIMARY KEY, corpus_id TEXT NOT NULL, namespace TEXT NOT NULL,
    document_id TEXT NOT NULL, revision_id TEXT NOT NULL,
    policy_token TEXT NOT NULL, definition_hash TEXT NOT NULL, manifest_hash TEXT NOT NULL,
    member_count INTEGER NOT NULL CHECK (typeof(member_count) = 'integer' AND member_count >= 0),
    origin_state TEXT NOT NULL,
    UNIQUE (corpus_id, namespace, document_id, revision_id, passage_set_id),
    FOREIGN KEY (corpus_id, namespace, document_id) REFERENCES document(corpus_id, namespace, document_id),
    FOREIGN KEY (document_id, revision_id) REFERENCES revision(document_id, revision_id),
    FOREIGN KEY (policy_token, definition_hash) REFERENCES passage_policy_definition(policy_token, definition_hash),
    FOREIGN KEY (document_id, revision_id, origin_state)
        REFERENCES document_state(document_id, revision_id, state_version)
);
CREATE TABLE passage (
    passage_id TEXT NOT NULL PRIMARY KEY, corpus_id TEXT NOT NULL, namespace TEXT NOT NULL,
    document_id TEXT NOT NULL, revision_id TEXT NOT NULL, passage_set_id TEXT NOT NULL,
    anchor_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (typeof(ordinal) = 'integer' AND ordinal > 0),
    origin_state TEXT NOT NULL,
    UNIQUE (corpus_id, namespace, document_id, revision_id, passage_set_id, passage_id),
    UNIQUE (corpus_id, namespace, document_id, revision_id, passage_set_id, passage_id, anchor_id),
    UNIQUE (passage_set_id, ordinal), UNIQUE (passage_set_id, passage_id, ordinal),
    FOREIGN KEY (corpus_id, namespace, document_id, revision_id, passage_set_id) REFERENCES passage_set(
        corpus_id, namespace, document_id, revision_id, passage_set_id),
    FOREIGN KEY (revision_id, anchor_id) REFERENCES anchor(revision_id, anchor_id),
    FOREIGN KEY (document_id, revision_id, origin_state)
        REFERENCES document_state(document_id, revision_id, state_version)
);
CREATE TABLE passage_set_member (
    passage_set_id TEXT NOT NULL, passage_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (typeof(ordinal) = 'integer' AND ordinal > 0),
    PRIMARY KEY (passage_set_id, passage_id), UNIQUE (passage_set_id, ordinal),
    FOREIGN KEY (passage_set_id, passage_id, ordinal)
        REFERENCES passage(passage_set_id, passage_id, ordinal)
);
CREATE TABLE state_passage_set (
    corpus_id TEXT NOT NULL, namespace TEXT NOT NULL, document_id TEXT NOT NULL,
    revision_id TEXT NOT NULL, state_version TEXT NOT NULL PRIMARY KEY, passage_set_id TEXT NOT NULL,
    UNIQUE (document_id, state_version, passage_set_id),
    FOREIGN KEY (corpus_id, namespace, document_id) REFERENCES document(corpus_id, namespace, document_id),
    FOREIGN KEY (document_id, revision_id, state_version)
        REFERENCES document_state(document_id, revision_id, state_version),
    FOREIGN KEY (corpus_id, namespace, document_id, revision_id, passage_set_id)
        REFERENCES passage_set(corpus_id, namespace, document_id, revision_id, passage_set_id)
);
CREATE TABLE index_configuration (
    configuration_id TEXT NOT NULL PRIMARY KEY, descriptor_json TEXT NOT NULL, descriptor_hash TEXT NOT NULL
);
CREATE TABLE index_work_slot (
    corpus_id TEXT NOT NULL, namespace TEXT NOT NULL, document_id TEXT NOT NULL,
    configuration_id TEXT NOT NULL REFERENCES index_configuration,
    fence INTEGER NOT NULL CHECK (typeof(fence) = 'integer' AND fence >= 0),
    latest_attempt_id TEXT,
    PRIMARY KEY (corpus_id, document_id, configuration_id),
    FOREIGN KEY (corpus_id, namespace, document_id) REFERENCES document(corpus_id, namespace, document_id),
    FOREIGN KEY (corpus_id, document_id, configuration_id, latest_attempt_id)
        REFERENCES index_attempt(corpus_id, document_id, configuration_id, attempt_id)
        DEFERRABLE INITIALLY DEFERRED
);
CREATE TABLE index_attempt (
    attempt_id TEXT NOT NULL PRIMARY KEY, corpus_id TEXT NOT NULL, namespace TEXT NOT NULL,
    document_id TEXT NOT NULL, revision_id TEXT NOT NULL, state_version TEXT NOT NULL,
    configuration_id TEXT NOT NULL,
    fence INTEGER NOT NULL CHECK (typeof(fence) = 'integer' AND fence > 0),
    execution_identity_json TEXT, execution_identity_hash TEXT,
    status TEXT NOT NULL CHECK (status IN (
        'admitted', 'staging', 'published', 'unchanged', 'failed', 'stale', 'superseded'
    )),
    reason TEXT CHECK (reason IN (
        'not_processed', 'unsupported_policy', 'provider_unavailable', 'invalid_provider_output',
        'state_changed', 'storage_failure', 'superseded_attempt', 'claim_lost', 'inactive'
    )),
    diagnostic_id TEXT, created_at TEXT NOT NULL, completed_at TEXT,
    CHECK ((execution_identity_json IS NULL) = (execution_identity_hash IS NULL)),
    CHECK ((status IN ('admitted', 'staging')) = (completed_at IS NULL)),
    UNIQUE (corpus_id, document_id, configuration_id, attempt_id),
    UNIQUE (corpus_id, document_id, configuration_id, fence),
    FOREIGN KEY (corpus_id, namespace, document_id) REFERENCES document(corpus_id, namespace, document_id),
    FOREIGN KEY (document_id, revision_id, state_version)
        REFERENCES document_state(document_id, revision_id, state_version),
    FOREIGN KEY (corpus_id, document_id, configuration_id) REFERENCES index_work_slot
);
CREATE TABLE index_staging_member (
    attempt_id TEXT NOT NULL, passage_id TEXT NOT NULL,
    corpus_id TEXT NOT NULL, namespace TEXT NOT NULL, document_id TEXT NOT NULL,
    revision_id TEXT NOT NULL, passage_set_id TEXT NOT NULL, configuration_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (typeof(ordinal) = 'integer' AND ordinal > 0),
    lexical_input TEXT NOT NULL, representation_hash TEXT NOT NULL,
    vector BLOB NOT NULL CHECK (typeof(vector) = 'blob'),
    dimensions INTEGER NOT NULL CHECK (typeof(dimensions) = 'integer' AND dimensions > 0),
    vector_hash TEXT NOT NULL, CHECK (length(vector) = 4 * dimensions),
    PRIMARY KEY (attempt_id, passage_id), UNIQUE (attempt_id, ordinal),
    FOREIGN KEY (corpus_id, document_id, configuration_id, attempt_id)
        REFERENCES index_attempt(corpus_id, document_id, configuration_id, attempt_id),
    FOREIGN KEY (corpus_id, namespace, document_id, revision_id, passage_set_id, passage_id)
        REFERENCES passage(corpus_id, namespace, document_id, revision_id, passage_set_id, passage_id)
);
CREATE TABLE document_projection (
    projection_id TEXT NOT NULL PRIMARY KEY, corpus_id TEXT NOT NULL, namespace TEXT NOT NULL,
    document_id TEXT NOT NULL, revision_id TEXT NOT NULL, state_version TEXT NOT NULL,
    passage_set_id TEXT NOT NULL, configuration_id TEXT NOT NULL REFERENCES index_configuration,
    execution_identity_json TEXT NOT NULL, execution_identity_hash TEXT NOT NULL,
    attempt_id TEXT NOT NULL,
    lexical_count INTEGER NOT NULL CHECK (typeof(lexical_count) = 'integer' AND lexical_count >= 0),
    vector_count INTEGER NOT NULL CHECK (typeof(vector_count) = 'integer' AND vector_count >= 0),
    manifest_hash TEXT NOT NULL, created_at TEXT NOT NULL,
    CHECK (lexical_count = vector_count),
    UNIQUE (corpus_id, document_id, configuration_id, projection_id),
    UNIQUE (projection_id, corpus_id, namespace, document_id, revision_id, passage_set_id),
    FOREIGN KEY (corpus_id, namespace, document_id) REFERENCES document(corpus_id, namespace, document_id),
    FOREIGN KEY (document_id, revision_id, state_version)
        REFERENCES document_state(document_id, revision_id, state_version),
    FOREIGN KEY (corpus_id, namespace, document_id, revision_id, passage_set_id)
        REFERENCES passage_set(corpus_id, namespace, document_id, revision_id, passage_set_id)
);
CREATE TABLE projection_member (
    projection_id TEXT NOT NULL, passage_id TEXT NOT NULL, corpus_id TEXT NOT NULL,
    namespace TEXT NOT NULL, document_id TEXT NOT NULL, revision_id TEXT NOT NULL,
    passage_set_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (typeof(ordinal) = 'integer' AND ordinal > 0),
    lexical_input TEXT NOT NULL, representation_hash TEXT NOT NULL,
    vector BLOB NOT NULL CHECK (typeof(vector) = 'blob'),
    dimensions INTEGER NOT NULL CHECK (typeof(dimensions) = 'integer' AND dimensions > 0),
    vector_hash TEXT NOT NULL, CHECK (length(vector) = 4 * dimensions),
    PRIMARY KEY (projection_id, passage_id), UNIQUE (projection_id, ordinal),
    FOREIGN KEY (projection_id, corpus_id, namespace, document_id, revision_id, passage_set_id)
        REFERENCES document_projection(projection_id, corpus_id, namespace, document_id, revision_id, passage_set_id),
    FOREIGN KEY (corpus_id, namespace, document_id, revision_id, passage_set_id, passage_id)
        REFERENCES passage(corpus_id, namespace, document_id, revision_id, passage_set_id, passage_id)
);
CREATE TABLE active_document_projection (
    corpus_id TEXT NOT NULL, document_id TEXT NOT NULL, configuration_id TEXT NOT NULL,
    projection_id TEXT NOT NULL,
    PRIMARY KEY (corpus_id, document_id, configuration_id),
    FOREIGN KEY (corpus_id, document_id, configuration_id, projection_id)
        REFERENCES document_projection(corpus_id, document_id, configuration_id, projection_id)
);

CREATE TABLE processing_guard (
    corpus_id TEXT NOT NULL PRIMARY KEY REFERENCES corpus,
    epoch INTEGER NOT NULL CHECK (typeof(epoch) = 'integer' AND epoch >= 0),
    blocked INTEGER NOT NULL CHECK (blocked IN (0, 1))
);
CREATE TABLE processing_plan (
    corpus_id TEXT NOT NULL REFERENCES corpus, plan_id TEXT NOT NULL, plan_version TEXT NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('index', 'enrichment')),
    producer TEXT NOT NULL, producer_version TEXT NOT NULL,
    configuration_id TEXT REFERENCES index_configuration,
    definition_hash TEXT NOT NULL, capability_json TEXT NOT NULL,
    enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
    PRIMARY KEY (corpus_id, plan_id, plan_version)
);
CREATE TABLE processing_worker_binding (
    corpus_id TEXT NOT NULL, plan_id TEXT NOT NULL, plan_version TEXT NOT NULL,
    namespace TEXT NOT NULL, principal_id TEXT NOT NULL, worker_id TEXT NOT NULL,
    owner_id TEXT NOT NULL, writer_id TEXT NOT NULL,
    PRIMARY KEY (corpus_id, plan_id, plan_version, namespace, principal_id, worker_id, owner_id, writer_id),
    FOREIGN KEY (corpus_id, plan_id, plan_version) REFERENCES processing_plan,
    FOREIGN KEY (corpus_id, namespace) REFERENCES source_namespace
);
CREATE TABLE processing_job (
    job_id TEXT NOT NULL PRIMARY KEY, corpus_id TEXT NOT NULL, namespace TEXT NOT NULL,
    owner_id TEXT NOT NULL, writer_id TEXT NOT NULL, principal_id TEXT NOT NULL,
    plan_id TEXT NOT NULL, plan_version TEXT NOT NULL,
    target_kind TEXT NOT NULL CHECK (target_kind IN ('document', 'unit')),
    document_id TEXT, target_state TEXT, batch_id TEXT, unit_id TEXT,
    target_digest TEXT NOT NULL, logical_key TEXT NOT NULL,
    creation_sequence INTEGER NOT NULL
        CHECK (typeof(creation_sequence) = 'integer' AND creation_sequence > 0),
    status TEXT NOT NULL CHECK (status IN (
        'queued', 'running', 'retry_wait', 'succeeded', 'failed', 'superseded', 'blocked', 'cancelled'
    )),
    status_version INTEGER NOT NULL CHECK (typeof(status_version) = 'integer' AND status_version >= 1),
    checkpoint_sequence INTEGER NOT NULL
        CHECK (typeof(checkpoint_sequence) = 'integer' AND checkpoint_sequence >= 0),
    lifetime_attempts INTEGER NOT NULL CHECK (typeof(lifetime_attempts) = 'integer' AND lifetime_attempts >= 0),
    retry_episode INTEGER NOT NULL CHECK (typeof(retry_episode) = 'integer' AND retry_episode >= 1),
    episode_attempts INTEGER NOT NULL CHECK (typeof(episode_attempts) = 'integer' AND episode_attempts BETWEEN 0 AND 10),
    next_due_at TEXT NOT NULL,
    claim_fence INTEGER NOT NULL CHECK (typeof(claim_fence) = 'integer' AND claim_fence >= 0),
    worker_id TEXT, lease_deadline TEXT,
    guard_epoch INTEGER NOT NULL CHECK (typeof(guard_epoch) = 'integer' AND guard_epoch >= 0),
    failure_code TEXT CHECK (failure_code IN (
        'invalid_request', 'forbidden', 'not_found', 'state_conflict', 'retry_conflict',
        'retry_expired', 'unsupported', 'state_changed', 'stale_index', 'budget_exceeded', 'internal_error'
    )),
    diagnostic_id TEXT,
    coordination_reason TEXT CHECK (coordination_reason IN (
        'lease_lost', 'awaiting_input', 'dependency_changed', 'retry_scheduled',
        'retry_exhausted', 'purge_blocked', 'processor_unavailable', 'plan_disabled',
        'authority_unavailable'
    )),
    result_kind TEXT CHECK (result_kind IN ('projection', 'write_key')),
    result_id TEXT,
    result_outcome TEXT CHECK (result_outcome IN ('published', 'verified_unchanged', 'applied', 'unchanged')),
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    CHECK ((target_kind = 'document' AND document_id IS NOT NULL AND target_state IS NOT NULL
            AND batch_id IS NULL AND unit_id IS NULL)
        OR (target_kind = 'unit' AND document_id IS NULL AND target_state IS NULL
            AND batch_id IS NOT NULL AND unit_id IS NOT NULL)),
    CHECK ((status = 'running') = (worker_id IS NOT NULL)),
    CHECK ((status = 'running') = (lease_deadline IS NOT NULL)),
    CHECK ((status = 'succeeded') = (result_kind IS NOT NULL)),
    CHECK ((status = 'succeeded') = (result_id IS NOT NULL)),
    CHECK ((status = 'succeeded') = (result_outcome IS NOT NULL)),
    CHECK ((failure_code IS NULL) = (diagnostic_id IS NULL)),
    CHECK (result_kind IS NULL
        OR (result_kind = 'projection' AND result_outcome IN ('published', 'verified_unchanged'))
        OR (result_kind = 'write_key' AND result_outcome IN ('applied', 'unchanged'))),
    UNIQUE (corpus_id, job_id), UNIQUE (corpus_id, logical_key), UNIQUE (corpus_id, creation_sequence),
    FOREIGN KEY (corpus_id, plan_id, plan_version) REFERENCES processing_plan,
    FOREIGN KEY (corpus_id, namespace) REFERENCES source_namespace,
    FOREIGN KEY (corpus_id, namespace, document_id) REFERENCES document(corpus_id, namespace, document_id),
    FOREIGN KEY (document_id, target_state) REFERENCES document_state(document_id, state_version),
    FOREIGN KEY (corpus_id, batch_id, unit_id) REFERENCES processing_unit(corpus_id, batch_id, unit_id)
);
CREATE TABLE processing_dependency (
    corpus_id TEXT NOT NULL, job_id TEXT NOT NULL, namespace TEXT NOT NULL,
    document_id TEXT NOT NULL, revision_id TEXT NOT NULL, state_version TEXT NOT NULL,
    namespace_token TEXT NOT NULL,
    PRIMARY KEY (job_id, document_id),
    FOREIGN KEY (corpus_id, job_id) REFERENCES processing_job(corpus_id, job_id),
    FOREIGN KEY (corpus_id, namespace, document_id) REFERENCES document(corpus_id, namespace, document_id),
    FOREIGN KEY (document_id, revision_id, state_version)
        REFERENCES document_state(document_id, revision_id, state_version)
);
CREATE TABLE processing_checkpoint (
    corpus_id TEXT NOT NULL, job_id TEXT NOT NULL PRIMARY KEY,
    sequence INTEGER NOT NULL CHECK (typeof(sequence) = 'integer' AND sequence > 0),
    stage TEXT NOT NULL CHECK (stage IN ('passages_published', 'projection_staged', 'owner_committed')),
    artifact_id TEXT NOT NULL, format_version TEXT NOT NULL, configuration_digest TEXT NOT NULL,
    progress_ordinal INTEGER NOT NULL CHECK (typeof(progress_ordinal) = 'integer' AND progress_ordinal >= 0),
    claim_fence INTEGER NOT NULL CHECK (typeof(claim_fence) = 'integer' AND claim_fence > 0),
    FOREIGN KEY (corpus_id, job_id) REFERENCES processing_job(corpus_id, job_id)
);
CREATE TABLE processing_batch (
    batch_id TEXT NOT NULL PRIMARY KEY, corpus_id TEXT NOT NULL REFERENCES corpus,
    principal_id TEXT NOT NULL, owner_id TEXT NOT NULL, writer_id TEXT NOT NULL,
    envelope_digest TEXT NOT NULL,
    item_count INTEGER NOT NULL CHECK (typeof(item_count) = 'integer' AND item_count BETWEEN 1 AND 100),
    created_at TEXT NOT NULL, UNIQUE (corpus_id, batch_id)
);
CREATE TABLE processing_unit (
    unit_id TEXT NOT NULL PRIMARY KEY, corpus_id TEXT NOT NULL, batch_id TEXT NOT NULL,
    ordinal INTEGER NOT NULL CHECK (typeof(ordinal) = 'integer' AND ordinal BETWEEN 0 AND 99),
    operation TEXT NOT NULL CHECK (operation IN ('put_document', 'remove_document', 'enrich')),
    writer_id TEXT NOT NULL, retry_key_hash TEXT NOT NULL, payload_digest TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'running', 'settled')),
    attempt_fence INTEGER NOT NULL CHECK (typeof(attempt_fence) = 'integer' AND attempt_fence >= 0),
    canonical_key_id TEXT,
    failure_code TEXT CHECK (failure_code IN (
        'invalid_request', 'forbidden', 'not_found', 'state_conflict', 'retry_conflict',
        'retry_expired', 'unsupported', 'state_changed', 'stale_index', 'budget_exceeded', 'internal_error'
    )),
    diagnostic_id TEXT, transient INTEGER NOT NULL CHECK (transient IN (0, 1)),
    CHECK ((status = 'settled' AND ((canonical_key_id IS NOT NULL) + (failure_code IS NOT NULL)) = 1)
        OR (status != 'settled' AND canonical_key_id IS NULL AND failure_code IS NULL)),
    CHECK ((failure_code IS NULL) = (diagnostic_id IS NULL)),
    UNIQUE (corpus_id, batch_id, unit_id), UNIQUE (batch_id, ordinal),
    UNIQUE (batch_id, writer_id, operation, retry_key_hash),
    FOREIGN KEY (corpus_id, batch_id) REFERENCES processing_batch(corpus_id, batch_id),
    FOREIGN KEY (corpus_id, canonical_key_id, writer_id, operation, retry_key_hash)
        REFERENCES write_key(corpus_id, key_id, writer_id, operation, key_hash)
);
CREATE TABLE state_intent (
    intent_sequence INTEGER PRIMARY KEY CHECK (intent_sequence > 0),
    corpus_id TEXT NOT NULL, namespace TEXT NOT NULL, document_id TEXT NOT NULL,
    revision_id TEXT NOT NULL, state_version TEXT NOT NULL,
    change_kind TEXT NOT NULL CHECK (change_kind IN ('put', 'remove', 'policy')),
    created_at TEXT NOT NULL, UNIQUE (state_version), UNIQUE (corpus_id, intent_sequence),
    FOREIGN KEY (corpus_id, namespace, document_id) REFERENCES document(corpus_id, namespace, document_id),
    FOREIGN KEY (document_id, revision_id, state_version)
        REFERENCES document_state(document_id, revision_id, state_version)
);
CREATE TABLE processing_scan (
    corpus_id TEXT NOT NULL, plan_id TEXT NOT NULL, plan_version TEXT NOT NULL,
    start_sequence INTEGER NOT NULL CHECK (typeof(start_sequence) = 'integer' AND start_sequence >= 0),
    through_sequence INTEGER NOT NULL CHECK (typeof(through_sequence) = 'integer' AND through_sequence >= 0),
    target_sequence INTEGER NOT NULL CHECK (typeof(target_sequence) = 'integer' AND target_sequence >= 0),
    status TEXT NOT NULL CHECK (status IN ('scanning', 'idle')),
    CHECK (start_sequence <= through_sequence AND through_sequence <= target_sequence),
    PRIMARY KEY (corpus_id, plan_id, plan_version),
    FOREIGN KEY (corpus_id, plan_id, plan_version) REFERENCES processing_plan
);
CREATE TABLE processing_intent_ack (
    corpus_id TEXT NOT NULL, plan_id TEXT NOT NULL, plan_version TEXT NOT NULL,
    intent_sequence INTEGER NOT NULL CHECK (typeof(intent_sequence) = 'integer' AND intent_sequence > 0),
    disposition TEXT NOT NULL CHECK (disposition IN ('scheduled', 'inactive', 'unsupported')),
    job_id TEXT, CHECK ((disposition = 'scheduled') = (job_id IS NOT NULL)),
    PRIMARY KEY (corpus_id, plan_id, plan_version, intent_sequence),
    FOREIGN KEY (corpus_id, plan_id, plan_version) REFERENCES processing_plan,
    FOREIGN KEY (corpus_id, intent_sequence) REFERENCES state_intent(corpus_id, intent_sequence),
    FOREIGN KEY (corpus_id, job_id) REFERENCES processing_job(corpus_id, job_id)
);
CREATE TABLE sync_scope (
    corpus_id TEXT NOT NULL, namespace TEXT NOT NULL, owner_id TEXT NOT NULL, synchronization_scope TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (typeof(generation) = 'integer' AND generation >= 0),
    mutation_epoch INTEGER NOT NULL CHECK (typeof(mutation_epoch) = 'integer' AND mutation_epoch >= 0),
    PRIMARY KEY (corpus_id, namespace, owner_id, synchronization_scope),
    FOREIGN KEY (corpus_id, namespace) REFERENCES source_namespace
);
CREATE TABLE sync_run (
    run_id TEXT NOT NULL PRIMARY KEY, corpus_id TEXT NOT NULL, namespace TEXT NOT NULL,
    owner_id TEXT NOT NULL, synchronization_scope TEXT NOT NULL, writer_id TEXT NOT NULL, principal_id TEXT NOT NULL,
    generation INTEGER NOT NULL CHECK (typeof(generation) = 'integer' AND generation >= 1),
    expected_epoch INTEGER NOT NULL CHECK (typeof(expected_epoch) = 'integer' AND expected_epoch >= 0),
    namespace_token TEXT NOT NULL,
    guard_epoch INTEGER NOT NULL CHECK (typeof(guard_epoch) = 'integer' AND guard_epoch >= 0),
    status TEXT NOT NULL CHECK (status IN ('open', 'complete', 'partial', 'failed', 'superseded', 'cancelled')),
    admitted_pages INTEGER NOT NULL CHECK (typeof(admitted_pages) = 'integer' AND admitted_pages BETWEEN 0 AND 1000),
    admitted_units INTEGER NOT NULL CHECK (typeof(admitted_units) = 'integer' AND admitted_units BETWEEN 0 AND 10000),
    admitted_documents INTEGER NOT NULL CHECK (typeof(admitted_documents) = 'integer' AND admitted_documents BETWEEN 0 AND 10000),
    created_at TEXT NOT NULL, finished_at TEXT,
    CHECK ((status = 'open') = (finished_at IS NULL)),
    UNIQUE (corpus_id, run_id),
    UNIQUE (corpus_id, run_id, namespace, owner_id, synchronization_scope),
    UNIQUE (corpus_id, namespace, owner_id, synchronization_scope, generation),
    FOREIGN KEY (corpus_id, namespace, owner_id, synchronization_scope) REFERENCES sync_scope
);
CREATE TABLE sync_page (
    corpus_id TEXT NOT NULL, run_id TEXT NOT NULL,
    page_ordinal INTEGER NOT NULL CHECK (typeof(page_ordinal) = 'integer' AND page_ordinal BETWEEN 0 AND 999),
    batch_id TEXT NOT NULL, manifest_digest TEXT NOT NULL, page_retry_key_hash TEXT NOT NULL,
    item_count INTEGER NOT NULL CHECK (typeof(item_count) = 'integer' AND item_count BETWEEN 1 AND 100),
    PRIMARY KEY (run_id, page_ordinal), UNIQUE (run_id, page_retry_key_hash),
    UNIQUE (corpus_id, run_id, page_ordinal, batch_id),
    FOREIGN KEY (corpus_id, run_id) REFERENCES sync_run(corpus_id, run_id),
    FOREIGN KEY (corpus_id, batch_id) REFERENCES processing_batch(corpus_id, batch_id)
);
CREATE TABLE sync_identity (
    corpus_id TEXT NOT NULL, run_id TEXT NOT NULL, page_ordinal INTEGER NOT NULL,
    batch_id TEXT NOT NULL, unit_id TEXT NOT NULL, external_identity_hash TEXT NOT NULL,
    PRIMARY KEY (run_id, external_identity_hash), UNIQUE (run_id, batch_id, unit_id),
    FOREIGN KEY (corpus_id, run_id, page_ordinal, batch_id) REFERENCES sync_page(corpus_id, run_id, page_ordinal, batch_id),
    FOREIGN KEY (corpus_id, batch_id, unit_id) REFERENCES processing_unit(corpus_id, batch_id, unit_id)
);
CREATE TABLE sync_seen (
    corpus_id TEXT NOT NULL, run_id TEXT NOT NULL, namespace TEXT NOT NULL, owner_id TEXT NOT NULL,
    synchronization_scope TEXT NOT NULL, document_id TEXT NOT NULL, accepted_state TEXT NOT NULL,
    batch_id TEXT NOT NULL, unit_id TEXT NOT NULL,
    PRIMARY KEY (run_id, document_id), UNIQUE (run_id, batch_id, unit_id),
    FOREIGN KEY (corpus_id, run_id, namespace, owner_id, synchronization_scope)
        REFERENCES sync_run(corpus_id, run_id, namespace, owner_id, synchronization_scope),
    FOREIGN KEY (corpus_id, batch_id, unit_id) REFERENCES processing_unit(corpus_id, batch_id, unit_id),
    FOREIGN KEY (run_id, batch_id, unit_id) REFERENCES sync_identity(run_id, batch_id, unit_id),
    FOREIGN KEY (corpus_id, namespace, owner_id, synchronization_scope, document_id)
        REFERENCES document(corpus_id, namespace, owner_id, synchronization_scope, document_id),
    FOREIGN KEY (document_id, accepted_state) REFERENCES document_state(document_id, state_version)
);
CREATE TABLE sync_removal_provenance (
    corpus_id TEXT NOT NULL, run_id TEXT NOT NULL, namespace TEXT NOT NULL, owner_id TEXT NOT NULL,
    synchronization_scope TEXT NOT NULL, document_id TEXT NOT NULL, state_version TEXT NOT NULL,
    attribution_json TEXT NOT NULL,
    PRIMARY KEY (run_id, document_id),
    FOREIGN KEY (corpus_id, run_id, namespace, owner_id, synchronization_scope)
        REFERENCES sync_run(corpus_id, run_id, namespace, owner_id, synchronization_scope),
    FOREIGN KEY (corpus_id, namespace, owner_id, synchronization_scope, document_id)
        REFERENCES document(corpus_id, namespace, owner_id, synchronization_scope, document_id),
    FOREIGN KEY (document_id, state_version) REFERENCES document_state(document_id, state_version)
);
CREATE TABLE processing_key (
    key_id TEXT NOT NULL PRIMARY KEY, corpus_id TEXT NOT NULL REFERENCES corpus,
    principal_id TEXT NOT NULL, owner_id TEXT NOT NULL, writer_id TEXT NOT NULL,
    operation TEXT NOT NULL CHECK (operation IN (
        'register_batch', 'schedule', 'begin_snapshot', 'observe_page', 'finish_snapshot', 'retry'
    )),
    key_hash TEXT NOT NULL, digest TEXT NOT NULL,
    digest_version TEXT NOT NULL CHECK (digest_version = 'e4-control-digest/1'),
    status TEXT NOT NULL CHECK (status IN ('applied', 'unchanged')),
    committed_at TEXT NOT NULL, expires_at TEXT NOT NULL,
    expired INTEGER NOT NULL CHECK (expired IN (0, 1)),
    CHECK (expires_at >= committed_at),
    UNIQUE (corpus_id, writer_id, operation, key_hash), UNIQUE (corpus_id, key_id)
);
CREATE TABLE processing_response (
    key_id TEXT NOT NULL PRIMARY KEY, corpus_id TEXT NOT NULL,
    result_kind TEXT NOT NULL CHECK (result_kind IN ('batch', 'job', 'run', 'page', 'finish', 'retry')),
    response_json TEXT NOT NULL, authorization_json TEXT NOT NULL,
    FOREIGN KEY (corpus_id, key_id) REFERENCES processing_key(corpus_id, key_id)
);

CREATE INDEX policy_principal_idx ON policy_grant(corpus_id, principal_id, namespace, grant_name);
CREATE INDEX knowledge_binding_principal_idx ON knowledge_writer_binding(corpus_id, principal_id, namespace, owner_id, writer_id);
CREATE INDEX entity_name_idx ON entity(corpus_id, name, entity_id);
CREATE INDEX contribution_owner_idx ON contribution(corpus_id, owner_id, writer_id, kind, sequence);
CREATE INDEX entity_support_entity_idx ON entity_support(corpus_id, entity_id, contribution_id);
CREATE INDEX alias_name_idx ON alias(corpus_id, alias, entity_id, contribution_id);
CREATE INDEX alias_entity_idx ON alias(corpus_id, entity_id, contribution_id);
CREATE INDEX identifier_value_idx ON identifier(corpus_id, scheme, value, entity_id, contribution_id);
CREATE INDEX identifier_entity_idx ON identifier(corpus_id, entity_id, contribution_id);
CREATE INDEX mention_entity_idx ON mention(corpus_id, entity_id, contribution_id);
CREATE INDEX assertion_subject_idx ON assertion(corpus_id, subject_id, contribution_id);
CREATE INDEX assertion_predicate_idx ON assertion(corpus_id, subject_id, predicate, contribution_id);
CREATE INDEX assertion_object_idx ON assertion(corpus_id, object_entity_id, predicate, contribution_id);
CREATE INDEX contribution_evidence_state_idx ON contribution_evidence(document_id, state_version, contribution_id);
CREATE INDEX contribution_seed_slot_idx ON contribution_seed(corpus_id, namespace, owner_id, writer_id, seed_set_id, seed_key, contribution_id);
CREATE INDEX seed_event_slot_idx ON seed_membership_event(corpus_id, namespace, owner_id, writer_id, seed_set_id, seed_key, sequence);
CREATE INDEX passage_anchor_idx ON passage(revision_id, anchor_id, passage_id);
CREATE INDEX projection_state_idx ON document_projection(corpus_id, document_id, state_version, configuration_id, projection_id);
CREATE INDEX projection_reuse_idx ON projection_member(document_id, passage_id, representation_hash, projection_id);
CREATE INDEX processing_claim_idx ON processing_job(corpus_id, plan_id, plan_version, status, next_due_at, creation_sequence, job_id);
CREATE INDEX processing_status_idx ON processing_job(corpus_id, namespace, owner_id, creation_sequence, job_id);
CREATE INDEX processing_dependency_state_idx ON processing_dependency(document_id, state_version, job_id);
CREATE INDEX processing_lease_idx ON processing_job(corpus_id, status, lease_deadline, job_id);
CREATE INDEX intent_corpus_sequence_idx ON state_intent(corpus_id, intent_sequence);
CREATE INDEX sync_seen_document_idx ON sync_seen(document_id, accepted_state, run_id);
CREATE INDEX write_key_expiry_idx ON write_key(expired, expires_at, key_id);
CREATE INDEX processing_key_expiry_idx ON processing_key(expired, expires_at, key_id);
