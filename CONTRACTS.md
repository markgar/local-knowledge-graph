# Foundation value reference

`kg.models.foundation` implements **validation and serialization only**, using
Pydantic models with contract version `foundation/1`. It does not persist data,
enforce ACLs or ownership, execute writes or queries, synchronize sources, process
content, implement retries, or retain snapshots/continuations. Declared grants,
state tokens, receipts and result-set IDs are values, not proof of those services.

The separate `kg.evidence` service now executes document writes and evidence reads
using these values. That does not turn foundation enrichment/query/synchronization
descriptions into implemented operations.

See [the models](src/kg/models/foundation.py) for exact fields and defaults,
[focused tests](tests/test_foundation_contracts.py) for executable checks, and
[SPEC.md](SPEC.md) for implemented product behavior. Future service obligations
and delivery planning are tracked in
[issue #28](https://github.com/markgar/local-knowledge-graph/issues/28), not here.

## Evidence service API

`kg.evidence` exports `EvidenceDatabase(Path)`, `EvidenceAdministration(database,
LocalAdminAuthority)`, `EvidenceService(database, LocalIdentity)` and
`EvidenceServiceError`. Administrative/read values live in `kg.models.evidence`;
top-level service values carry `interface_version="evidence/1"`. Existing
`foundation/1` write/result shapes are unchanged.

| API | Result / behavior |
| --- | --- |
| `database.initialize()` | Initialize empty or verify E1 format; incompatible targets raise `unsupported`. Use a fresh file and resupply sources. |
| `admin.register(CorpusRegistration)` | Register namespaces, writer bindings and explicit `LocalPolicy`; identical original registration is unchanged and returns the **current** policy version, without restoring old grants. Conflicting registration fails. |
| `admin.replace_policy(LocalPolicy, expected_policy_version)` | Atomic policy/state rotation; returns version, affected namespaces and changed-document count. |
| `service.write(WriteRequest)` | `put_document` / `remove_document` -> `WriteOutcome`. `enrich` is rejected as unsupported. |
| `service.write_batch(WriteBatch)` | Ordered `BatchResult`; complete envelope validation precedes independent unit transactions. |
| `current(scope, ExternalDocument)` / `document(scope, document_id)` | Current `DocumentView`, including inactive sources and explicit pending processing reason. |
| `state(scope, document_id, state_version)` | Immutable historical state/metadata context plus latest-state flag. |
| `history(scope, document_id, *, after_sequence=0, limit=100)` | Ascending `StatePage`, including predecessor, change kind, snapshot and activity. |
| `revisions(scope, document_id, *, after_sequence=0, limit=100)` | Insertion-ordered `RevisionPage` with exact byte length/hash; restores do not duplicate revisions. |
| `content(scope, document_id, revision_id)` | `ContentResult(available, text)`; exact empty text is available, corruption is an error. |
| `anchors(scope, document_id, state_version, *, after_ordinal=0, limit=100)` | `AnchorPage` for that state's immutable set. |
| `revision_anchors(scope, document_id, revision_id, *, after_ordinal=0, limit=100)` | `RevisionAnchorPage` including former members, complete references and origin citations. |
| `evidence(scope, EvidenceRef, *, state_version=None)` | Exact quote/range/hash and metadata. Default context is the anchor's creation state; explicit state must contain it. |
| `citation(scope, StoredCitation)` | Round-trip the exact required reference/state/metadata-snapshot relationship. |
| `capabilities()` | `EvidenceCapabilities`, not `FoundationCapabilities`: implemented operations, unsupported features and limits. |

All reads need current trusted `read` authority. Writes need `write_documents`
and an owner/writer/synchronization binding; they do not confer full-text access.
Attribution is not a credential. Namespace policy changes rotate affected document
states independently of corpus-wide access-context freshness.

Public inputs are defensively revalidated, including constructed/copied models.
Invalid envelopes raise `EvidenceServiceError` before mutation; valid failed write
units return typed outcomes. Read/admin errors raise the same exception, whose
`.failure` has a code and opaque diagnostic ID, never input-bearing exception text.
Unexpected exceptions propagate after rollback; they never become success.

Pages accept limits 1..200 and nonnegative cursors. Later appends may appear on
later pages. Inventory existence is not current support: flags separately report
current revision, active document and current anchor membership. `is_current_support`
describes the anchor's eligibility **now**, not validity of an old dependency state.
K1 must use the private same-transaction validator rather than that informational flag.

See [SPEC.md](SPEC.md#generic-evidence-store) for byte identity, transactions,
30-day retry/tombstone behavior and clean rebuild policy, and the executable
[Python example](examples/evidence_intake.py). There is no sync completion, passage,
enrichment, indexing/search or purge API in this service.

## Validation and serialization APIs

- Models forbid extra fields, use strict types and are frozen. Integer fields
  reject booleans and numeric strings; discriminated unions reject unknown
  operations or kinds.
- `WriteRequest`, `WriteBatch`, `BatchResult`, `QueryRequest`, `QueryResult`,
  `SynchronizationSnapshot`, `MetadataSnapshot`, `ContentResult` and
  `FoundationCapabilities` require `contract_version: "foundation/1"`.
  Nested `WriteRequest` items in a batch also require it.
- Use `model_validate_json()` for JSON input, `model_dump_json()` for JSON output,
  and `model_json_schema()` for schema generation. JSON arrays become tuples;
  Python constructors and `model_validate()` use tuples for tuple fields and
  timezone-aware `datetime` objects for timestamp fields.
- Parsing checks one value's internal consistency. Separately call
  `BatchResult.validate_for(write_batch)` or
  `QueryResult.validate_for(query_request)` to check request/result correlation.
  These methods return `None` or raise `ValueError`; model validation raises
  Pydantic `ValidationError`. JSON Schema alone does not express all custom checks.

## Field vocabulary and bounds

| Value | Implemented shape |
| --- | --- |
| `Token` | Nonblank, UTF-8-encodable string, 1–256 code points; used for opaque IDs, versions and policy labels. |
| `Label` | Nonblank, UTF-8-encodable string, 1–4,096 code points. |
| `Text` | UTF-8-encodable string; empty text is allowed. |
| `Name` | Matches `^[a-z][a-z0-9_.:-]{0,127}$`; used for metadata keys, entity types, predicates, identifier schemes and result record types. |
| `Scope` | `corpus_id` plus `AccessContext`. |
| `AccessContext` | `principal_id`, `policy_version`, 1–100 unique namespaces and 1–4 unique grants from `read`, `write_documents`, `write_knowledge`, `seed`. |
| `Attribution` | `owner_id`, `writer_id`, `producer`, `producer_version`, optional `model_id` and `configuration_id`. |
| `ExternalDocument` | `source_namespace`, `synchronization_scope`, `external_id`. |
| `EvidenceRef` | `corpus_id`, `source_namespace`, `document_id`, `revision_id`, `anchor_id`, optional `passage_id`. |
| `DocumentDependency` | `source_namespace`, `document_id`, `revision_id`, `state_version`. |

Nonblank checks do not trim or normalize strings. Names are syntax-checked, not
looked up in a predicate/type registry. IDs and state versions are not resolved
against storage.

| Limit | Value |
| --- | --- |
| `SuppliedContent.text` | 5,000,000 UTF-8 bytes |
| Supplied anchors | 0–1,000 |
| `WriteRequest` serialized size | 8,000,000 UTF-8 bytes |
| `WriteBatch` | 1–100 items; 16,000,000 serialized UTF-8 bytes |
| Changes per `ChangeSet` | 1–100 |
| Evidence references per source support | 1–200, without duplicates |
| Total evidence-reference occurrences per change set | 200, including reuse across changes |
| Document dependencies per change set | 0–200 |
| Query steps / `max_operations` | 1–16; budget defaults to 16 |
| Query `max_records` | 1–10,000; defaults to 10,000 |
| Query `max_milliseconds` | 1–30,000; defaults to 5,000 |
| Search limit | 1–100; defaults to 20 |
| Path `max_hops` | 1–3; defaults to 2 |
| Result collections | At most 100 ranked hits; 1,000 records, paths or entity candidates |

Write byte limits use `model_dump_json()` with defaults/nulls included, not the
raw input's whitespace or encoding choices. They do not impose a raw-input
pre-parse limit or a general size limit on every model. Query budgets validate
numbers and declared counts, not elapsed time or actual work.

`FoundationCapabilities` defaults to the write/query operation names listed below
and fixed numeric write limits, with `implementation: "validation_only"`. Its
operation-name tuples are string fields, not execution or registration checks.
This value is separate from the product's `kg capabilities` command.

## Content, metadata and snapshots

`SuppliedContent` contains `text`, optional `anchors` and a required
`passage_policy` token. It retains text without newline or Unicode normalization.
Its `content_hash` property is SHA-256 over exact UTF-8 text bytes; the property
is not a serialized model field.

Each `SuppliedAnchor` has a unique `local_id` within the content, nonnegative
`start`, `end > start`, and a `quote`. Offsets are zero-based, half-open Python
Unicode code-point offsets. The end must be within the text and the quote must
equal `text[start:end]`. Anchors need not be ordered or disjoint.

`SourceMetadata` requires `title` and `location` labels, allows an optional
timezone-aware `event_time`, and accepts at most 100 uniquely named attributes.
Attribute values are strict strings, integers or booleans; strings must be
UTF-8-encodable and at most 4,096 code points. `MetadataSnapshot` combines this
metadata with document, revision, state-version and metadata-snapshot IDs; it
does not create or retain a snapshot.

`ContentResult` requires `text` to be non-null exactly when `state` is `available`.
An empty string is available content; `unavailable` requires null.

`SynchronizationSnapshot` carries scope, attribution, namespace, synchronization
scope, run ID, expected generation and `enumeration` (`partial`, `failed`,
`complete`). Validation requires the namespace in the declared access context
and a `write_documents` grant. No enumeration or deletion occurs.

## Write requests and enrichment

`WriteRequest` requires a request ID, retry key, scope, attribution and a payload
discriminated by `operation`:

| Operation | Payload and checks |
| --- | --- |
| `put_document` | External document, content, metadata and an explicit precondition: `{"kind": "create"}` or `{"kind": "match", "state_version": "..."}`. |
| `remove_document` | External document and a `match` precondition only. |
| `enrich` | A `ChangeSet` of changes and supporting document dependencies. |

Document operations require the declared `write_documents` grant and a document
namespace included in `scope.access.namespaces`. Enrichment requires
`write_knowledge`; every source evidence reference must match the request corpus
and a declared namespace. Any seed-supported change additionally requires `seed`.
These are checks of caller-supplied values, not authorization.

Enrichment change kinds are `entity`, `alias`, `identifier`, `mention` and
`assertion`. Each has a unique change-set `local_id`. Entity references use
`{"kind": "local", "local_id": "..."}` or
`{"kind": "stored", "entity_id": "..."}`. Local references must name an
entity-creation change in the same set; forward references are valid.

- Entities, aliases and identifiers accept either `SourceSupport` (an evidence
  tuple) or `SeedSupport` (a seed key). Mentions and assertions require source
  support. Every mention evidence reference must have a non-null passage ID.
- Assertions have a subject, syntactically validated predicate, `explicit` or
  `inferred` interpretation and a typed object. Object kinds are `entity`,
  `string`, `integer`, `boolean`, `timestamp`; strings use `Label` and timestamps
  require a timezone. Floating-point and arbitrary JSON objects are not variants.
- Source support rejects duplicate full evidence references. The change set
  counts every evidence occurrence across changes toward the 200-reference cap.
- Dependencies are unique by `(source_namespace, document_id)`. Every evidence
  reference needs a dependency with that key and the same revision ID; no unused
  dependencies are allowed. Each dependency also carries a state-version token,
  but its freshness is not checked.

Attribution belongs to the request, not each change. Validation does not check
stored entity existence, evidence relationships in a database, ownership,
semantic support, precondition freshness or retry-key reuse.

## Write outcomes, errors and correlation

`WriteBatch` requires unique request IDs. `WriteOutcome` statuses are `applied`,
`unchanged`, `rejected`, `conflict`, `failed`. Success (`applied`/`unchanged`)
requires a receipt and no error; other statuses require an error and no receipt.
`conflict` status is required exactly for error codes `state_conflict` and
`retry_conflict`.

- A `DocumentReceipt` carries document, revision and metadata-snapshot IDs plus
  `ProcessingState`: state version, source (`active`/`inactive`), indexing
  (`pending`/`ready`/`failed`) and enrichment
  (`pending`/`partial`/`complete`/`failed`). These state fields have no cross-state
  transition checks.
- A `ChangeSetReceipt` carries 1–100 local-to-stored ID mappings with unique
  local IDs. Stored IDs are not required to be unique.
- `BatchResult` contains 1–100 outcomes with unique request IDs. Its status must
  be `complete` when all have receipts, `partial` when some do, or `failed` when
  none do.
- `BatchResult.validate_for()` checks batch identity and one outcome per request
  in input order. Successful document operations need document receipts;
  enrichment receipts must map every change local ID, with no extras.

`Failure` contains only a code and `diagnostic_id` token. Codes are
`invalid_request`, `forbidden`, `not_found`, `state_conflict`, `retry_conflict`,
`retry_expired`, `unsupported`, `state_changed`, `stale_index`, `budget_exceeded`,
`internal_error`. There is no free-text message field. This does not sanitize
caller-supplied tokens or Pydantic exceptions, which can include input values.

## Query plans and results

`QueryRequest` carries request identity, scope, budget, steps and `output_step`.
It requires a declared `read` grant, unique step IDs, an existing output step
and no more steps than `budget.max_operations`.

| Operation | Value-level constraints | Output data kind |
| --- | --- | --- |
| `search` | Text label and bounded limit. | `ranked` |
| `resolve` | Exactly one of name or entity ID. | `entities` |
| `records` | Earlier resolve step; record type `action`, `decision`, `blocker` or `conflict`; optional `open`/`completed` status only for actions. | `records` |
| `count` | Earlier records step, not a search result. | `aggregate` |
| `paths` | Earlier resolve step, predicate name and bounded hop limit. | `paths` |
| `evidence` | Evidence reference matching the declared corpus and namespace. | `records` |

Dependencies must precede their consumers, preventing cycles. No steps execute.
In particular, a records dependency's shape does not prove exhaustive selection.

`QueryResult` carries scope and request identity; required nullable
`read_state_id` and `result_set_id`; optional continuation; operation/record
counts; truncation; and exhaustion (`none`, `candidate_pool`, `eligible_set`).
Its local checks are:

- `unsupported`, `stale_index`, `state_changed` and `failed` require an error and
  no data or continuation. Except for generic `failed`, error code must equal
  the outcome. Other outcomes require data, a non-null read-state ID and no error.
- Continuation requires a result-set ID. Every aggregate also requires one.
  An exact aggregate requires `complete` or `empty`, no truncation and
  `eligible_set` exhaustion.
- `ambiguous` requires multiple unique entity candidates. `empty` requires an
  empty collection or an exact zero aggregate, and cannot be truncated.
- Ranked scores must be finite. Records and paths carry source support; all
  returned evidence must match the declared corpus and namespaces.
- A path has 2–4 entity IDs and 1–3 assertion IDs, with exactly one assertion ID
  per edge. This checks shape, not graph connectivity. Aggregate counts are
  nonnegative; entity candidates are unique.

`QueryResult.validate_for()` additionally checks exact request ID and scope
equality, operations executed no greater than plan length, records examined
within the request budget, and the output kind from the table. It also checks:

- Ambiguity belongs to a name-based resolve on the selected output's dependency
  chain; an exact-ID resolve cannot be ambiguous.
- A completed resolve returns exactly one entity; exact-ID output contains only
  the requested ID.
- Returned records match the selected record type, paths respect the requested
  hop limit and ranked hits respect the search limit.
- Aggregate support names the count step's records selection, and a completed
  or empty count is exact.

These checks do not verify ranking, action status, path predicates, actual counts,
evidence resolution, coherent database reads, retained result sets or access.

## Executable examples and evaluation inputs

- [Enrichment request](corpora/foundation/enrichment.json)
- [Exact-ID count plan](corpora/foundation/query.json)
- [Name-resolution plan](corpora/foundation/query-ambiguous.json)
- [Fixture guide](corpora/foundation/README.md)
- [Synthetic workload generator](benchmarks/foundation/workload.py) and
  [benchmark input guide](benchmarks/foundation/README.md)

These are synthetic values and evaluation inputs, not evidence of implemented
write/query services or permission to access live sources.
