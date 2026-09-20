# Shared contract reference

Contract version: `foundation/1`.
Implementation: validation and serialization only; service enforcement is planned.

## How to use this reference

This is the single reference for shared evidence, ownership, write and query
semantics. It is not a delivery plan. Use [ROADMAP.md](ROADMAP.md) for work
packages and their issues, [SPEC.md](SPEC.md) for current product behavior, and
[acceptance recipes](corpora/foundation/README.md) for integration cases and owners.

Executable values live in `kg.models.foundation`; they validate and serialize data but do not write,
authorize, synchronize, execute queries, or guarantee persistence. Existing
product interface version 2 and report versions are independent and unchanged.
No access to live sources or hosted models is authorized by this foundation.

Passing contract tests proves validation behavior, not storage, concurrency, authorization,
or recovery. Later packages must supply real integration evidence, not skipped
tests or mocked passing substitutes. Numeric performance targets and their
measurement protocol live in [benchmarks/foundation](benchmarks/foundation/README.md),
not in this reference.

## Shared contracts

Exact field names/types and discriminators are in
[`src/kg/models/foundation.py`](src/kg/models/foundation.py). Each top-level request,
result, snapshot, and capability value requires `contract_version: "foundation/1"`.
Nested values inherit that version. Unknown fields, versions and operations fail;
strict types reject boolean-as-integer and string-as-number coercion. JSON arrays
become immutable tuples; Python constructors use tuples. Use `model_validate_json`,
`model_dump_json`, and `model_json_schema`. `_prepared.py` and `_writer.py` remain
private, unrelated implementation boundaries.

The following sections combine implemented value validation with the service
obligations those values describe. Commit-time, persistence, authorization,
processing and execution guarantees remain unimplemented unless explicitly
identified as current behavior in [SPEC.md](SPEC.md).

### 1. Scope, identity, and ownership

Every operation carries a corpus scope and an effective access context.
Caller-supplied writer/plugin labels provide attribution, not authorization.
The trusted local invocation boundary establishes which source namespaces and
operations that caller may use; possession of an object ID grants no access.
An access context is not a claim that a local CLI protects against an operating
system user who can directly read the database files.

| Concept | Initial semantics |
| --- | --- |
| Source namespace | Stable connector-instance identity within a corpus, independent of plugin version, process, synchronization run, and display name. Two accounts using one plugin are different namespaces. |
| External document identity | `(corpus_id, source_namespace, external_id)` identifies a supplied document. Source path/URL is a location, not the universal key. Similar content across namespaces does not merge documents. |
| Internal identities | Document, revision, anchor, passage, entity, and assertion IDs are opaque handles. Preserve existing Markdown IDs; do not require clients to reproduce hash algorithms. |
| Content revision | Immutable identity for the exact supplied content of a document. Restoring the same content may reuse its revision, consistent with current history. |
| Document state version | Separate token that changes on relevant activation, metadata, access, or lifecycle changes, including removal/restoration of the same revision. It protects against stale writes that content hashes alone cannot detect. |
| Evidence reference | Corpus, document, revision, and anchor identity, with passage identity when applicable. Resolve and validate the entire relationship, not merely each ID's existence. |
| Contribution owner | Stable owner of a seed, alias, identifier, assertion, or other contribution. Attribution also records the responsible plugin/agent and available model/configuration identity. Shared entities do not make all contributions jointly writable. |

Seed synchronization replaces only contributions owned by that manifest's
configured writer. It must not delete another writer's aliases or deactivate an
entity solely because the entity is absent from the seed list. Entity lifecycle
must distinguish removal of one contribution from explicit entity retirement.
An entity is currently eligible when not explicitly retired and at least one
creation/seed contribution is active and visible to the caller. Alias, identifier
and assertion visibility never activates an otherwise unsupported entity.
Seed creations/aliases/identifiers carry `SeedSupport` and require the additional
`seed` grant; seed authority does not invent source evidence. Assertions and
mentions always require source support.

Document synchronization is similarly confined to an explicitly declared source
namespace and synchronization scope. A complete snapshot may deactivate absent
documents only after successful completion. A partial page, failed enumeration,
or interrupted run is not proof of absence. Incremental synchronization uses
explicit changes/removals; absence from a batch never implies deletion.
Concurrent synchronization runs need a generation/precondition check before an
older run can deactivate documents discovered by a newer run.

The current Markdown importer deactivates unselected documents across a corpus,
deletes corpus-wide aliases, and deletes/rebuilds mentions and relationships on
re-extraction/config changes. **Before any new writer can coexist**, E1/K1 must
either owner-scope all these mutations or explicitly reject mixed-writer storage.
E2 must then implement scoped coexistence, not merely add owner labels. If legacy
documents cannot be assigned an unambiguous owner, require explicit migration mapping
rather than claiming or deactivating them.

### 2. Canonical content and evidence

For generic text intake, accept valid Unicode text, retain it without newline or
Unicode normalization, and define hashes over its exact UTF-8 encoding. Reject
invalid encodings rather than silently replacing characters. Existing Markdown
byte hashes and exact citation identities must remain compatible.

Anchor ranges use zero-based, half-open Unicode code-point offsets in the full
retained text, not UTF-8 bytes or JavaScript UTF-16 units. Supplied ranges must be
within bounds and their quotes must equal the exact text slice. Clients using
other offset conventions must convert before submission. Cover CRLF, combining
characters, and non-BMP characters in contract examples.

`SourceMetadata` supplies title/location, timezone-aware event time, and up to 100
unique named string/integer/boolean attributes (strings at most 4096 code points).
`MetadataSnapshot` identifies an immutable metadata snapshot separately from the
content revision and state version. Every changed metadata/access/activation state
gets a fresh opaque state version even when restoring old content. Citations retain
the metadata snapshot at creation; current reads select the current snapshot.
E1 must store this relationship, not overwrite titles/locations on old citations.
No-op writes preserve state. Access policy history is not returned as metadata.

New revisions retain complete supplied text. Legacy revisions without full text
return an explicit content-unavailable state while retaining their valid anchors,
quotes, and history. Do not concatenate anchors into invented full content.
Backfill is permitted only from verified matching source content.

Passage processing identifies its policy/version separately from the immutable
source revision. Reprocessing may replace the active passage projection, but
must not repoint old evidence IDs to different text. E3 owns the processing
implementation; F0 establishes evidence identity and reference validity.

### 3. Writes, retries, and transaction boundaries

| Field or rule | Initial semantics |
| --- | --- |
| Envelope | Explicit contract version, operation, request/item identity, corpus/source scope, attribution, and typed payload. Unknown versions/operations and invalid fields fail before mutation. |
| Creation | Explicit create-only precondition; do not treat an omitted revision as permission to overwrite an existing document. |
| Update/removal | Expected document state version checked atomically with mutation. Stale submissions return conflict without applying their writes. |
| Enrichment | Names evidence revisions and expected state versions for every supporting document; validates all evidence, current eligibility, scope, and ownership at commit. Historical knowledge is readable, not silently accepted as current enrichment. |
| Retry key | Scoped to corpus, writer, and operation. An identical retry returns the committed identity/outcome without duplicate writes; a different payload using that key conflicts. |
| Atomicity | One document write or bounded enrichment change set is atomic, including its provenance and retry record. A batch consists of independent units; there is no implied batch-wide rollback or cross-item dependency. |
| Batch result | One correlated outcome per input in input order, with explicit applied/unchanged/rejected/conflict/failed status. Batch-level partial success is explicit; malformed envelopes fail before any items run. |
| Transaction owner | The public write service owns validation-to-commit atomicity. Internal writers participate in that transaction; external callers do not supply live SQLite connections. |

Idempotency is a persistence guarantee, not merely request-model validation.
Resolve authorized retries before reapplying stale preconditions, but never replay
retained content to a caller whose access was revoked. Purge must invalidate or
sanitize retry responses; a retry must not resurrect purged content.
The selected starting policy is a 30-day durable result window from commit, then
`retry_expired` for the same key rather than re-execution. E4 retains a non-content
key tombstone for the managed corpus lifetime; keys are not reusable. The digest
covers operation payload and owner/producer/model/configuration attribution, not
request correlation or refreshed access context. Reauthorize on every replay.
Purge removes response content and prevents replay/resurrection; tombstones retain
only opaque key/digest/status data, with concrete live retention approval left to X1.
Identical successful retries return the same receipt/IDs and committed status with
the current request's correlation ID.

Failures expose typed codes and bounded diagnostics, not source text or secrets
in operational logs. Invalid evidence or an unsupported operation must not become
a successful empty result. Size and batch limits are defined in the
[limits and compatibility section](#limits-and-compatibility)
and validated by the models.

#### Bounded enrichment change sets

The selected contract describes one atomic change set that can
create entities and add related aliases, mentions, and supported assertions.
It may cite multiple documents in one corpus, including different authorized
source namespaces. Single-entity and single-document submissions are valid
smaller instances of the same contract.

New entities have unique request-local references usable only within that change
set. Existing entities use stored IDs. Validate every reference and all supporting
document state versions within the same commit boundary. A stale, inaccessible,
or invalid dependency rejects the entire set; no new entities, aliases, or
assertions from it remain committed.

For example, an agent submits one retry-keyed change set that creates Sam and
Atlas, adds an alias for Sam, and asserts that Sam owns Atlas using anchors from
an email and a meeting note. The assertion refers to the two new entities by
request-local handles. Success returns their stored IDs and the assertion ID.
If either document's expected state changed before commit, nothing in this set
is applied. An authorized identical retry after success returns the original
committed result and IDs, not a second set of entities.

This is not a general transaction scripting API: no arbitrary SQL, remote calls,
document ingestion, indexing, or cross-corpus writes inside the set. Limits are
100 changes and 200 total evidence-reference occurrences per set,
including repeated support across different contributions. Each change has a unique
`local_id`; only entity-creation IDs may be used as local entity references.
`dependencies` contains exactly one namespace/document/revision/state entry per
supporting document, no unused entries. Receipts map **every change** local ID to
its durable stored ID. `BatchResult.validate_for(request)` checks complete mapping,
receipt type and input-order correlation.

Multiple supports within a contribution are **conjunctive**. All must remain
active/current and visible for current use; loss of any support makes that
contribution ineligible, not partially supported. Independently sufficient evidence
requires separate contributions. Historical reads may retain outdated contributions
but still require access to every support. Source restoration does not revive stale
enrichment merely because its content hash matches: revalidation/re-enrichment is
required against current state tokens. K4 implements lifecycle; X1 implements
authorization. Models verify only internal reference shape and declared scope.

This starting direction avoids making agents orchestrate partial entity/assertion
writes. Revisit it if executable examples show excessive complexity. Before a
public release it can be revised with coordinated contract changes; after clients
depend on it, preserve compatibility or introduce an explicit contract version.

### 4. Knowledge contributions

The initial value model distinguishes entities, external identifiers, aliases,
passage mentions, and typed assertions/relationships. Co-mention does not create
a factual relationship. Similar names or candidate scores do not merge entities.

An assertion records its subject, predicate/type, typed object/value, evidence
references, explicit-source versus inferred interpretation, contribution owner,
and attribution. Objects are entity references, strings, integers, booleans or
timezone-aware timestamps, discriminated by `kind`; floating point/JSON objects
are intentionally excluded in v1. Predicate, entity-type and identifier-scheme
names use `[a-z][a-z0-9_.:-]{0,127}`. K1 must resolve predicates against a
corpus-registered schema (allowed subject/object types), not silently accept a
syntactically valid unknown predicate. Registration is trusted configuration,
not an operation inside a change set. Owner and attribution belong to the whole
set and to each persisted contribution it creates; different owners require
independent sets. Identity similarity/identifier collision never implies a merge.

Evidence may support competing assertions. Preserve both without choosing truth
by write order. Historical assertions, retractions, and currently supported
knowledge must remain distinguishable. K4 implements correction semantics; K1's
initial contract must leave room for them without rewriting evidence history.
Identity linking/merge corrections belong to K3, not automatic alias insertion.

### 5. Query and lifecycle boundaries

The executor accepts a typed, acyclic plan with named steps, validated dependency
references, exact constraints, and bounded operation budgets. F0 defines the
initial operation/result vocabulary and rejects unsupported operations and
invalid dependencies. Q1 implements execution using current services; Q2 extends
it with typed graph and exact structured operations.

Results distinguish ranked evidence, records, paths, and aggregates. Outcomes
distinguish complete, empty, ambiguous, unsupported, partial, stale-index, and
failed execution. Report effective scope, operation counts, truncation and
supporting evidence. Counts operate over the complete eligible indexed set, not
ranked candidates; interrupted or budget-limited counts are not labeled exact.
`QueryRequest` uses topologically ordered steps: `search`, `resolve`, `records`,
`count`, `paths`, `evidence`. Records/paths depend on an earlier resolve; count
depends on an exhaustive records selection, never a ranked/search result. The
resolve selector is exactly one of `name` or `entity_id`; exact ID lookup remains
scoped and cannot return a different entity or ambiguity. Named resolution may
be ambiguous only on the selected output's dependency chain; a completed resolve
has exactly one candidate. Returned records match the selected record type and
paths cannot exceed the requested hop limit. The
initial vocabulary deliberately does not support arbitrary graph-to-record joins;
Q2 must extend/version the contract before advertising them. `output_step` names
the result. Ambiguous resolution stops dependent execution without guessing.
The search operator always means existing full-pipeline product search.

**One dependent execution observes a coherent read state or fails with
`state_changed` and no data**, including before any pagination exists. Q1 owns
the snapshot/change-detection mechanism across service calls; access revocation
must be checked before returning data and cannot be overridden by a snapshot.
`read_state_id` identifies that state, not merely a content fingerprint.
`QueryResult.validate_for(request)` checks correlation, exact effective scope,
budgets, output kind and aggregate-support step; models cannot prove actual
snapshot isolation, authorization, exhaustive selection, or time enforcement.
Aggregate support is inspectable via its records step in the retained result set;
aggregate results therefore require a non-null `result_set_id`.
Q1/Q2 must supply this mechanism, not just return an unresolvable label.

Source activation, indexing readiness, and enrichment progress are separate
states. An accepted write does not claim that semantic indexing or enrichment
has finished. Stale work cannot mark a newer state ready. Graph completion is
not a prerequisite for indexing. Existing product search retains its mandatory
full-pipeline/readiness behavior.

Access restrictions apply to historical reads, entity lookup, graph expansion,
aggregate counts, explanations, projections, and continuation as well as hits.
Derived visibility must not broaden the scope of its supporting evidence.
F0 fixes the access-context propagation contract; X1 completes enforcement and
source-policy integration. Synthetic access fixtures do not authorize real ACLs.

Reserve result-set/snapshot identity and continuation metadata in the result
contract. Q4 decides the concrete cursor mechanism, expiry, and data-change
policy. Permission revocation and purge must invalidate affected continuations
or otherwise prevent disclosure; snapshot stability cannot override access.
Candidate-pool exhaustion is not corpus exhaustion.

Deactivation retains history; purge removes retained content and affected derived
data within the managed storage boundary. X1 must enumerate canonical stores,
indexes, job/retry payloads, diagnostics, and snapshots, and define failure/recovery
behavior. The eventual policy must state how backups and SQLite storage remnants
are handled; deleting a row is not a promise of physical secure erasure.

## Limits and compatibility

`FoundationCapabilities` advertises **validation_only**, separately from existing
`kg capabilities`. No new service is advertised there. Intake is at most 5,000,000
UTF-8 text bytes, 1,000 supplied anchors, 8,000,000 canonical serialized JSON bytes
per request, 100 independent requests and 16,000,000 serialized bytes per batch.
Canonical size means `model_dump_json()` including defaults/nulls, not raw wire
whitespace. A future transport must also bound raw input before parsing.
All malformed batch envelopes fail before processing any unit; valid units may
then independently fail commit-time checks. No cross-item local references.
Diagnostics contain typed codes and opaque correlation IDs only, never source
text. Do not log Pydantic exceptions with their untrusted input values at a future
service boundary.

Query defaults: 16 steps, 10,000 examined records, 5,000 ms (configurable to at most
30,000), search limit 20 (at most 100), graph depth 2 (at most 3). A records
selection is not limited by its displayed result page (at most 1,000 records).
These modest limits bound local transactional and query work; capability growth
requires reviewed contract/budget updates, not silent coercion. Structural limits
are validated here; time, database integrity and authorization are service duties.

| Existing surface | Compatibility/migration decision (E1/E2/K1/Q1) |
| --- | --- |
| Markdown document/revision/anchor/passage IDs | Preserve verbatim, including hash inputs and move/restore identities; opaque to new clients. No normalization or rehashing existing evidence. |
| Corpus and manifest | Register a stable namespace, synchronization scope and owner by explicit configuration mapping; no inference from plugin version or display label. A namespace is unique per corpus; each document has one owner/sync scope, changes require explicit migration. Trusted registration binds allowed owners/writers/predicates to the access policy. |
| Legacy ambiguous provenance | Stop migration pending explicit mapping; no corpus-wide claiming or deletion. Preserve old DB/history before migrating. |
| Legacy full text | `ContentResult(state="unavailable", text=null)` until verified matching original bytes are backfilled. Empty retained text is `available`, not unavailable. |
| Source title/location/event time | Preserve historical citation context; initial metadata snapshot can record known fields only. Never invent missing historical metadata/state transitions. |
| Manifest seeds/aliases | One manifest-owned contribution per seed/alias; preserve entity IDs; absence removes only that owner's contribution. Agent aliases/identifiers never become manifest-owned. |
| Mentions/wikilinks | Preserve record/evidence IDs and explicit provenance, map wikilinks to `legacy:wikilink` entity-valued assertions, not `work:owns` or inferred facts. Owner-scoped rebuild or mixed-writer rejection is mandatory first. |
| Tasks/decisions/blockers/conflicts | Keep current record IDs/types, fields, exact support and task/decision supersession rules through compatibility records. Do not reinterpret prose, infer edges or coerce owner text into entity identity. K1 can add typed assertions separately, with a reviewed migration, not replace legacy records implicitly. |
| Current CLI/Python results | Unchanged models, commands, JSON/report versions and ordering; Q1 adapters wrap rather than replace them. No new version fields added to legacy results. |

Pre-release changes can revise `foundation/1` only with coordinated producer,
consumer, example and test changes. Once external clients depend on this version,
incompatible changes require a new explicit version and migration path. Expanding
the supported predicate registry is configuration; adding object kinds or plan
dependencies is a contract change. Physical purge, concurrent commit validation,
retry storage and mixed-writer preservation remain later-package acceptance gates.
