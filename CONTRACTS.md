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
top-level service values carry `interface_version="evidence/2"`. Existing
`foundation/1` document write/result shapes are unchanged. The pre-release
enrichment values include independent entity support and explicitly namespaced seeds.

| API | Result / behavior |
| --- | --- |
| `database.initialize()` | Initialize empty or verify the complete `evidence-store/2` schema and manifest; incompatible targets raise `unsupported`. Use a fresh file and resupply sources. |
| `admin.register(CorpusRegistration)` | Register namespaces, writer bindings and explicit `LocalPolicy`; identical original registration is unchanged and returns the **current** policy version, without restoring old grants. Conflicting registration fails. |
| `admin.replace_policy(LocalPolicy, expected_policy_version)` | Atomic policy/state rotation; returns version, affected namespaces and changed-document count. |
| `service.write(WriteRequest)` | `put_document` / `remove_document` -> `WriteOutcome`. `enrich` is rejected as unsupported. |
| `service.write_batch(WriteBatch)` | Ordered `BatchResult`; complete envelope validation precedes independent unit transactions. |
| `current(scope, ExternalDocument)` / `document(scope, document_id)` | Current `DocumentView`, including inactive sources and separate `indexing_reason` / `enrichment_reason` fields. |
| `state(scope, document_id, state_version)` | Immutable historical state/metadata context plus latest-state flag. |
| `history(scope, document_id, *, after_sequence=0, limit=100)` | Ascending `StatePage`, including predecessor, change kind, snapshot and activity. |
| `revisions(scope, document_id, *, after_sequence=0, limit=100)` | Insertion-ordered `RevisionPage` with exact byte length/hash; restores do not duplicate revisions. |
| `content(scope, document_id, revision_id)` | `ContentResult(available, text)`; exact empty text is available, corruption is an error. |
| `anchors(scope, document_id, state_version, *, after_ordinal=0, limit=100)` | `AnchorPage` for that state's immutable set. |
| `revision_anchors(scope, document_id, revision_id, *, after_ordinal=0, limit=100)` | `RevisionAnchorPage` including former members, complete references and origin citations. |
| `evidence(scope, EvidenceRef, *, state_version=None)` | Exact quote/range/hash and metadata. Default context is the passage's first publication state, or the anchor's creation state for anchor-only references; explicit state must contain it. |
| `citation(scope, StoredCitation)` | Round-trip the exact required reference/state/metadata-snapshot relationship. |
| `passages(scope, document_id, state_version, *, after_ordinal=0, limit=100)` | `indexing/1` `PassagePage`: policy, set identity, complete saved evidence/citations in source order; `not_processed` differs from a completed empty set. Limit 1..200. |
| `capabilities()` | `EvidenceCapabilities`, not `FoundationCapabilities`: implemented operations, unsupported features and limits. |

All reads need current trusted `read` authority. Writes need `write_documents`
and an owner/writer/synchronization binding; they do not confer full-text access.
Attribution is not a credential. Namespace policy changes rotate affected document
states independently of corpus-wide access-context freshness.

`LocalPolicy.knowledge_bindings` defaults to `()` and contains strict
`KnowledgeWriterBinding(namespace, principal_id, owner_id, writer_id)` values.
Registration, canonical policy equality, persistence and replacement include these
bindings. Changing one rotates only the affected namespace's token/document states,
as well as corpus policy freshness. A binding does not grant knowledge/seed access
by itself or install a knowledge service.

The pre-release `evidence/2` representation replaces `processing_reason` with
stored indexing and enrichment reasons; both currently report
`processor_not_available`. There is no compatibility alias or migration from
`evidence-store/1`. Reserved schema tables do not add public knowledge, indexing,
query APIs, nor change EvidenceService's unsupported capability list. The separate
processing control API below uses the reserved job tables.

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
[Python example](examples/evidence_intake.py). There is no sync completion, public
passage-production, enrichment, indexing/search or purge API in this service.

Passage reads use the immutable state-to-set association. Generated anchor-only
references require membership through that state's published passage set; supplied
anchor-only references still use the owner's supplied anchor set. A passage
reference additionally requires its exact passage/set/anchor chain. Unknown or
mismatched passage IDs now return `not_found`, not the former `unsupported`.
`member_of_current_anchor_set` continues to describe the **supplied** anchor set;
`is_current_support` also admits genuine current generated/passage membership.
Neither requires vector readiness. Same-transaction support still requires an
exact active dependency state and namespace token, not merely this read-time flag.
The private producer is not a public API; intake capability `passage_policy`
remains `retained_intent_only`.

## Knowledge registry API

`kg.knowledge.KnowledgeAdministration(database, LocalAdminAuthority)` exposes
`register_knowledge_schema(KnowledgeSchema) -> KnowledgeSchemaRegistration`.
The corpus must already exist in `EvidenceDatabase`; an unknown corpus raises
`not_found`. Authority is provisioned by the trusted embedding application, as
for evidence administration, not inferred from an ordinary caller's identity or
namespace grants. This is a local trusted boundary, not hosted authentication or
a per-corpus administrator policy. Bootstrap/schema provisioning is explicitly
outside the scoped execution-report API.

Strict frozen values live in `kg.models.knowledge`:

| Value | Fields / constraints |
| --- | --- |
| `KnowledgeSchema` | `interface_version="knowledge/1"`, `corpus_id`, `schema_version`, 1..1,000 unique `entity_types`, 0..1,000 unique `identifier_schemes`, 0..1,000 uniquely named `predicates`. |
| `PredicateDefinition` | `name`, 1..100 unique declared `subject_types`, one `object_kind` (`entity`, `string`, `integer`, `boolean`, `timestamp`), `object_types`, optional `record_projection`. Entity objects require 1..100 unique declared object types; other kinds require an empty tuple. Names use foundation `Name` syntax. |
| `RecordProjection` | Required `encoding="direct-subject-decision/1"` only, on string predicates only. Ordinary predicates have no projection. |
| `KnowledgeSchemaRegistration` | `interface_version="knowledge/1"`, `corpus_id`, `schema_version`, `status="applied"` or `"unchanged"`. |

Registration revalidates constructed/copied values, uses one canonical write
transaction and never replaces a corpus's installed definition. Reordered
registry/type collections represent the same allowed sets and return unchanged;
changed schema version, types, predicates or descriptor return `state_conflict`.
Invalid inputs raise `invalid_request` before mutation; corrupt stored definitions
raise `internal_error`, not an empty registry or permission to overwrite it.
There is no schema replacement/migration or separate retry ledger for provisioning.

This registers configuration only. It does **not** create entities/assertions,
enforce a submitted assertion's interpretation/support, produce/count decision
records, or expose enrichment, seed replacement or knowledge reads. Existing
evidence capabilities remain unchanged. See the executable
[schema example](examples/knowledge_schema.py).

<a id="canonical-anchor-queries"></a>

## Canonical evidence queries

`kg.query.QueryService(database, identity)` executes validated `QueryRequest`
values against the canonical evidence store. `execute(request)` returns
`kg.models.query.QueryExecution` (`interface_version="query/1"`), containing
the unchanged foundation `QueryResult`, elapsed milliseconds, ordered step
telemetry, a safe stop reason and accounting label. `execute_explained(request,
options=ExplainOptions())` executes once and returns shared `Explained` with
`.outcome` and `.report`. Ordinary summaries are discoverable through
`service.diagnostics.report/recent/for_request`; request IDs correlate distinct
invocations, not durable retries.

The installed capability is **anchor and published passage evidence**, including
generated anchors and authorized immutable history (`canonical-evidence/1`).
The result is one evidence Record identified by anchor ID,
with its full requested reference in SourceSupport. No quote text is added to
foundation results or Q1 reports. Detailed reports include the selected ID,
closure and acknowledged semantic reservation; summary reports omit selected IDs.
`capabilities(scope)` is authorized and reportable. There is no installed
support registry or `inspect_support` method in this slice. Required
resolve/records/count/search/paths return unsupported, never a fake empty result
or legacy adapter fallback. Missing or mismatched passage references return
`not_found`. Passage evidence requires its real state/set/passage/anchor chain,
not vector readiness. Both `codepoint-window/1` and `supplied-anchors/1` kernel
outputs are readable.

An evidence step accepts an `EvidenceRef`, not a `StoredCitation` or explicit
state/set selector. It preserves the entire requested reference, including
`passage_id`; default passage context is its first publication state. Exact
quote/range/hash and origin metadata remain accessible through
`EvidenceService.evidence`, and saved state/metadata contexts through
`EvidenceService.citation`. A later edit, removal, policy change or restoration
does not rewrite that history. Readability alone does not establish current
support: that requires the current active revision and actual set membership.

The whole request is reconstructed/validated, even branches not executed.
Only the selected output's named dependency closure is dispatched. Unrelated
unsupported branches are `not_needed`. One operation and one eligible evidence
`evidence_reference` reservation are charged for a successful evidence read;
there is no additional search-final charge. Missing evidence charges
no public record unit. On any no-data failure, step telemetry is empty and
foundation counters are zero **redacted sentinels**, not measurements.
`work_accounting="redacted"` distinguishes these from authorized
`scoped_semantic_reservations/1`. No private visits/VM/scratch counts are exposed.
Malformed envelopes raise `QueryServiceError` with safe invalid_request failure.

One owner thread serializes execution and diagnostic access, preserving SQLite
observer thread affinity. Per service admission is FIFO, one active call and
eight waiters. A fresh spawned child reads each execution; the owner acknowledges
bounded reservation RPC before consumption and retains accounting across worker
failure. Deadline includes queue, startup, SQL, release and inline reporting.
Close cancels work, withholds staged reports and releases retained observers.
Use the context manager; spawning requires the usual guarded Python entry point.
No performance target or warm-worker behavior is promised.

An original observer precedes the worker snapshot; fresh authorization and
same-connection generation comparison under one short write-excluding fence
precede data/report release. Any intervening canonical commit invalidates,
including unrelated writes, heartbeats and same-content restoration. Initial
denial is forbidden; post-admission policy denial is state_changed. No-data
report redaction is permanent, including nested staged children after parent
eviction. Later diagnostic lookup is a separately bounded authorized observation,
not permission to restore a withheld execution.

## Processing control API

`kg.processing` exports `ProcessingAdministration(database, LocalAdminAuthority)`
and `ProcessingService(database, LocalIdentity)`. Strict frozen request/result
values in `kg.models.processing` carry `processing/1`; invalid or failed calls
raise `EvidenceServiceError` with a safe code/opaque diagnostic ID. Public inputs
are defensively revalidated, including copied/constructed models, with a 16 KiB
serialized control-request limit. This is a local trusted embedding boundary,
not HTTP authentication.

| API | Delivered result and boundary |
| --- | --- |
| `register_plan(PlanRegistration)` | Immutable corpus/plan/version/producer/config definition; identical registration is unchanged, conflicting replacement fails. Kind is currently `index` only. Optional configuration references must already exist; null installs no executable provider. |
| `register_worker(WorkerRegistration)` | Idempotent exact principal/namespace/plan/version/worker/owner/writer binding; requires existing plan/namespace. Trusted provisioning, no fabricated scoped admin grant/report. |
| `schedule(ScheduleRequest)` | Applied/unchanged `ScheduleReceipt(job_id,status)`. Exact document/revision/state/namespace-token dependency; 30-day control receipts and logical dedup independent of worker instance. |
| `claim(WorkerRequest)` | `ClaimResult` with one fenced 60-second lease or `no_work`, inspected count and `scan_truncated`. At most 200 candidate transitions per call; truncation is not proof of no remaining work. |
| `heartbeat(HeartbeatRequest)` | Renewed `Claim` only for matching worker/fence, unexpired lease and live dependency/plan/corpus guard. Heartbeat at most every 20 seconds while working. |
| `job(JobRequest)` / `jobs(JobsRequest)` | Current owned `JobView` / creation-sequence-keyset `JobPage`, limits 1..200; no bodies or unfiltered totals. Status inspection does not advance the durable clock. |
| `capabilities()` | Only delivered operations; `executes_work=False`, `acknowledges_work=False`. |

Every ordinary request carries current `Scope`, real `WorkerSelection` and a
correlation `request_id`. Authority is current `read` plus exact trusted processing
registration, including namespace, principal, owner/writer and plan/version.
Bindings do not confer document/knowledge write grants. Scheduling targets must
come from the canonical store, never content hashes alone. The example shows
trusted target construction; the service independently validates ownership and
the exact immutable state chain. Opaque IDs and a claim are not credentials.

Expired running jobs become `retry_wait` with persisted 1,2,4,...,300-second-capped
backoff, or `failed` at ten attempts. Reclaim occurs during bounded `claim`;
after recording expiry the caller waits until `next_due_at` and claims again.
Fences and lifetime/episode counters persist across process restart. Exact expiry
is inclusive; shared max-observed UTC prevents backward-clock revival. Source
changes supersede old work; plan disable/guard invalidation block execution.
This slice has no explicit retry-episode reset, unblock, cancellation or plan
update API. Registration does not resurrect work or enqueue a background scanner.

All five ordinary methods also have `_explained(request, options)` wrappers.
Summaries are captured by default, detail opt-in. Reports record actual calls,
current status inspection, or explicitly labelled retained receipt facts, never
invented original execution. Fixed `ProcessingSelectionTarget` retains the real
selection even for no-work/empty results; existing `ProcessingTarget` retains
jobs and their historical document dependency. Report bodies/headers/discovery
reauthorize all current grants, exact enabled plan/worker binding and dependencies
under one fresh fence. Disabled-plan status can be inspected as business data,
but its report is unavailable. Denial irreversibly redacts the report group.
Target capacity exhaustion (for example, 200 job targets plus their selection)
can make a sidecar unavailable without truncating the business page.
No source text, durable trace database, new clock or job ledger is introduced.

There is no work executor, success/ack API, provider/knowledge integration,
checkpoint/batch/scanner completion, snapshot/absence removal, purge or readiness
writer here. Indexing remains pending. See
[the example](examples/processing_control.py) and
[control-plane semantics](SPEC.md#durable-processing-control).

## Execution diagnostics

`kg.models.execution` defines strict frozen `execution-report/1` values, shared by
one process-local `kg.diagnostics.DiagnosticService`. The owning service
constructs `service.diagnostics` with its trusted identity, collector and fixed
target authorizer; request data cannot supply an authorization callback.

| API / value | Delivered behavior |
| --- | --- |
| `ExplainOptions(detail="summary", include_quotes=False)` | Detailed capture is opt-in; quotes require `detail="detailed"` and never broaden authorization. |
| `write_explained(request, options)` / `write_batch_explained(batch, options)` | Execute the same ordinary method once, returning `Explained[WriteOutcome]` / `Explained[BatchResult]`. |
| Named read `_explained` wrappers | `current`, `document`, `state`, `content`, `history`, `revisions`, `evidence`, `citation`, `anchors`, `revision_anchors`; same inputs/cursors and typed ordinary result, plus the sidecar. `options` precedes keyword-only pagination/state arguments. |
| `passages_explained(scope, document_id, state_version, options, *, after_ordinal=0, limit=100)` | The same passage read once, plus bounded actual passage-status events and retained exact evidence dependencies; quotes remain opt-in. |
| `diagnostics.report(scope, report_id)` | Authorized retained body, or `ReportAvailability`; an admitted failure's diagnostic ID is also a lookup alias. Never reruns the business operation. |
| `diagnostics.recent(scope, *, limit=20)` | Freshly authorized headers only, newest completion first, stable report-ID tie-break; limit 1..32. |
| `diagnostics.for_request(scope, request_id, *, limit=20)` | Same headers, restricted to correlation within the originating identity/exact scope. Reused request IDs identify separate invocations. |

Ordinary scoped evidence reads/writes attempt summaries without changing their
outcomes. Unscoped static `capabilities()` and trusted bootstrap/schema
provisioning are excluded from this scoped execution-report API. This does not
exclude ordinary scoped knowledge read/write/seed operations or change their
authorization/errors; no admin `Scope`/namespace grant or audit subsystem is
invented. A same-scope batch has one report correlated by `batch_id`;
mixed-scope batches return a `not_collected` sidecar, not a misleading single-scope
report. Their independent unit transactions and result order are unchanged.
Invalid input rejected before capture does not promise a retained diagnostic.

`ExecutionReport` includes generated report/execution IDs, service/operation,
nullable request/diagnostic/parent/step/job/unit correlation, observation kind,
capture level, collected state, safe outcome/reason, authorized configuration
identities and ordered `RecordedEvent(sequence, event)` values. Absent stage
memberships/ranks/scores stay null, not invented zeroes. Captured/displayed counts
count admitted events, not all potentially omitted work; `truncated` marks capture
limits. Serialize without dropping null fields.

`captured_execution` describes this invocation; a replay never recreates an old
trace. Its authorized existing commit fact has `retained_commit_fact` explicitly
on the event. Evidence reads are `current_inspection`, even when they inspect
historical evidence now. Commit observations distinguish not attempted, staged
acknowledgement, confirmed commit, confirmed rollback and unknown. Losing a
commit response is not proof of rollback.

`ReportAvailability(state="not_collected"|"unavailable"|"redacted", execution_id,
diagnostic_id, reason)` has **no** event/count/configuration fields. Unknown,
foreign, expired, evicted and restarted lookups return the same unavailable value.
Fresh authorization checks both the originating identity/exact effective scope and
every retained target for immediate bodies, later bodies and header listings.
Denied/changed disclosure irreversibly clears the group's details; restoring
rights or making a new authorized inspection cannot reveal the old trace.

Limits are five monotonic minutes from completion, at most 32 reports per owning
service instance/identity, 32 summary or 200 detailed events, 256 KiB logical
payload/report and 8 MiB active-plus-retained reservations. The implementation
reserves a full 256 KiB slot before capture, including up to 64 KiB of bounded
authorization/group bookkeeping. Conservative pre-serialization sizing can stop
capture earlier; source excerpts are limited to 8,192 characters each. A disclosure
group admits at most 32 lifetime members and 200 total target descriptors, including
dependencies of evicted parents. Oldest completed reports are evicted first;
active captures are not evicted. Capacity failure changes diagnostic availability,
not business work, model input, ranking, counters or a committed write outcome.
Tracing still consumes real execution time.

The package-owned `knowledge_events`, `indexing_events`, `query_events` and
`processing_events` modules define closed value unions; processing control calls
now emit actual claim/fence/retry/restart/status events. These values alone do not
implement executors or establish full package acceptance. Shared collector/group tests do not
claim real search/provider execution. No private scan/VM meters, raw exceptions,
request bodies or source quotes by default are retained.

The closed operation vocabulary includes K1 `entity`, `entities`, `contributions`
and E4 `register_batch`, `resume_batch`, `batch_status`, `unit_receipt`, `schedule`,
`fail`, `begin_snapshot`, `observe_page`, `finish_snapshot`. Processing decisions
retain exact `queued`, `retry_wait`, `superseded`, `cancelled` observations, in
addition to existing names. Closed reasons include `awaiting_input`,
`dependency_changed`, `retry_scheduled`, `retry_exhausted`, `purge_blocked`,
`plan_disabled`, `authority_unavailable`. Unknown operation/event/status/reason
strings remain invalid. Q1 can represent ambiguity as `stopped` with a
`QueryDecision`, resource limits as `budget_exceeded`, and time as `deadline`;
ordinary query results retain their more specific distinctions. E3 policy details
use `unsupported_policy`, not a new free-form reason.

Private retained targets in `kg.diagnostics._targets` include
`KnowledgeWriterTarget(namespace, owner_id, writer_id)` and
`SeedSetTarget(namespace, owner_id, writer_id, seed_set_id)`. Corpus and principal
are inherited from `AuthorizationBinding`, never invented document/contribution
IDs. The latter represents the exact owned set even when it is empty. Both require
fixed owner authorization of current grants, namespace, exact owner/writer/set
and origin identity/scope; value construction is not proof of authority. K1 owns
those service checks; E1 deliberately rejects these unimplemented target kinds.
All-target checks, group bounds and irreversible redaction apply unchanged.

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

Enrichment change kinds are `entity`, `entity_support`, `alias`, `identifier`, `mention` and
`assertion`. Each has a unique change-set `local_id`. Entity references use
`{"kind": "local", "local_id": "..."}` or
`{"kind": "stored", "entity_id": "..."}`. Local references must name an
entity-creation change in the same set; forward references are valid.

- Entities, entity-support attestations, aliases and identifiers accept either
  `SourceSupport` (an evidence tuple) or `SeedSupport` (required `source_namespace`,
  `seed_set_id` and `seed_key`). The seed namespace must be declared in the request.
  Mentions and assertions require source
  support. Every mention evidence reference must have a non-null passage ID.
- `AddEntitySupport` requires a stored entity reference, name, entity type, local ID
  and support. It represents an independent creation-support attestation, not an
  alias or merge; only an `entity` creation may be a local-reference target.
  Validation does not check the stored entity's existence or name/type equality.
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
The [entity-support fixture](corpora/foundation/entity-support.json) and
[validation example](examples/foundation_values.py) demonstrate the amended values,
not executable enrichment.

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
