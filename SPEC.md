# Local Knowledge Graph: current contracts

Status: implemented capabilities in a **pre-alpha** package. Retrieval-quality
and answerability requirements are not met; this is not a production-ready
question-answering system.

This document describes current behavior. [CONTRACTS.md](CONTRACTS.md) describes
foundation values and the generic evidence service API. Future work and service requirements live
in the [build roadmap issue](https://github.com/markgar/local-knowledge-graph/issues/28);
they are not implemented capabilities.

## Generic evidence store

`kg.evidence.EvidenceDatabase`, `EvidenceAdministration` and `EvidenceService`
implement the new Python-only canonical evidence engine. Its packaged
`kg/evidence/schema.sql` uses SQLite application ID `0x4b474531`, user version 2
and `evidence-store/2`. Initialization atomically creates an empty target or
verifies that exact format. Old/unknown nonempty files (including version 1) are rejected without
changing headers, journal mode, schema or rows. There is no migration/reset API:
use a fresh path and explicitly resupply content. Old canonical and independent
dense-projection writers also reject this format under their schema/write lock.

The format contains the complete 65-table, 29-explicit-index canonical schema,
including reserved knowledge, passage/index and processing/synchronization storage.
There are no canonical views or triggers, query-result tables, report tables or
FTS virtual tables. Their absence is part of admission, not permission to create
partial variants under version 2. Schema presence never enables a service.

`canonical-sqlite-manifest/1` records every explicit object's definition hash and
the whole-schema signature in the same transaction as DDL and headers. The
signature covers ordered SQLite schema SQL, columns, foreign keys, and explicit
and implicit index structure; implicit autoindex names are not identity.
Descriptors use canonical compact ASCII JSON and SHA-256 with the manifest
version plus a NUL separator as the domain. Admission compares actual structure,
the exact format row and every manifest row against the packaged schema.
Unexpected/missing/altered objects and copied or incomplete manifests fail closed.
Foreign keys are enabled and verified on every connection. Competing initializers
recheck under the write lock; failed initialization rolls back schema, manifest and
headers together. WAL is enabled only after successful admission.
Structural PRAGMA reads are batched within each admission snapshot, sharing index
descriptors only inside that check. Actual metadata, headers and manifest rows are
read afresh on every admission, including the owner's locked recheck; no schema
cookie or prior connection admission substitutes for exact verification. All
batched SQL, timeout helpers and fetched rows retain ordinary private accounting.
During structural batches, an instruction-level progress callback spends the
prepaid quanta across SQLite's nested PRAGMA programs, so substatement remainders
cannot escape accounting. Unused prepayments are not refunded; other statements
retain the ordinary progress-handler granularity.

External identity is exact `(corpus, namespace, external_id)`, not location or
content. Each document has an immutable owner and synchronization scope; several
owners may be registered in one namespace. Local identity/admin authority must
be provisioned by the embedding application, not taken from untrusted requests.
The local policy checks principal, fresh corpus policy version, declared grants
and namespaces, operation and writer binding. This is not hosted authentication,
provider ACL synchronization or protection from someone controlling Python/files.

Exact strict UTF-8 bytes are stored as BLOBs, including empty content, CRLF, BOM,
NUL, combining marks and non-BMP characters. Revisions are document-specific,
domain-separated digests; content hashes are plain SHA-256. Existing bytes are
compared before reuse. Supplied anchors use half-open Unicode code-point ranges
and exact quote equality; the service does not generate anchors. Immutable
anchor sets are independent of content revisions, so removed members remain
discoverable in the historical revision inventory.

Each content, metadata, anchor-set, passage-policy, activity or effective namespace
policy change creates a fresh state token and metadata snapshot. Per-document
sequence orders states; no-op writes retain state. Remove retains content/history;
restore reuses an identical revision but never its old activation state.
Metadata equality sorts attribute keys and preserves scalar types/source strings.
Attribution-only changes add committed provenance/receipts without state churn.
Every new state also inserts one content-free `state_intent` and increments the
mutation epoch for its exact `(corpus, namespace, owner, synchronization_scope)`
inside that same transaction. This includes policy rotations of inactive sources.
Writers sharing that owner/scope share an epoch; other scopes are isolated.
No-op writes, receipt replays and unchanged policy replacements do neither.
New scope rows start at generation zero; this bookkeeping does not complete a
snapshot or schedule a job. New corpus registration creates an unblocked
processing guard at epoch zero.

Each write owns one `BEGIN IMMEDIATE` transaction with CAS checks and an atomic
receipt/provenance ledger. Only committed applied/unchanged units reserve a retry
key, scoped by corpus/writer/operation. Identical authorized retry returns the
original status/receipt under the new request ID, before testing stale state.
Changed committed-key input conflicts. At 30 days (inclusive), replay removes its
response payload and permanently expires the key; it never executes again.
A durable max-observed UTC clock watermark prevents observed expiry reversal.
This is lazy response expiration, not source retention/purge or trusted elapsed
time against a hostile OS clock. Retained receipt targets are reauthorized.
After an uncertain commit, retry the same key, not a new one.

Batches revalidate the entire envelope before item zero, then commit valid units
independently in input order, continuing after unit failures. There is no batch
rollback, generation/snapshot synchronization or absence-based removal.
All fresh states report pending indexing/enrichment, with separate stored
`indexing_reason` and `enrichment_reason` values of `processor_not_available`.
EvidenceService has no indexing/search/readiness setter or public passage production.
The separate IndexService owns standalone processing and default index readiness.
Explicit bounded enrichment uses the same owner transaction and shared key ledger.

The private `kg.indexing._passages` kernel prepares exact authorized source outside
the publication lock, then reauthorizes the current active state/writer/policy in an
owner-held transaction. It publishes a whole immutable passage set or compares an
existing set exactly; partial membership is never committed. Code-point windows
are contiguous, disjoint 1,024-character slices without trimming/normalization.
Supplied-anchor policy preserves overlaps, sorting by start/end/local ID; nonempty
text without anchors explicitly fails `unsupported` with `boundaries_required`.
Unknown tokens remain valid intake intent but fail processing; empty text produces
a completed empty set. Publication does not change indexing/enrichment readiness.

Domain-separated deterministic IDs bind revision, policy definition and ordered
exact boundaries. Generated anchors occupy a separate origin domain and revision
inventory, never the owner's supplied set. A state cannot swap published sets.
Metadata/policy/boundary changes require ordinary new E1 states; metadata-only
publication can reuse the same canonical set without rewriting its first-publication
history. Private `prepare`/`publish`/`produce` are kernel seams, not full processing
or claim/attempt/search implementations.

Current/history/content/citation reads use one authorized SQLite snapshot and a
fresh policy-version check before release; a concurrent policy change returns
`state_changed`, never the captured data. Policy replacement advances corpus
freshness and creates new states for all documents (including inactive ones) in
exactly affected namespaces. Unaffected namespace tokens/states remain stable.
Historical citations resolve their explicit state/snapshot, not today's title.
Pages are bounded keyset reads, not retained multi-call query snapshots.

The private `TransactionEvidence.validate_current()` validates active current
dependencies and full reference chains on the transaction owner's connection:
exact revision/state, namespace policy token, current anchor membership and stored
byte/quote integrity. Validation rejects use after the context exits; returned data
is not a reusable credential.
This is the K1 integration boundary. Passage
references require real published state/set/passage/anchor membership. Historical
evidence remains readable, but new support requires the exact current active state.
Generated anchor-only citations use that state's passage membership; ordinary
supplied-anchor support is unchanged. Vectors and disposable projections never
participate in these dependencies.

`EvidenceService.passages` and its explained wrapper expose bounded immutable
source-order pages and distinguish not_processed from a published empty set.
Evidence/citation/revision-inventory and diagnostic retained-target checks use the
same exact membership resolver. Snapshot hydration preflights source, quote and
metadata lengths before decoding, keeps scratch ownership until snapshot exit,
and reserves its operation's public event once: direct evidence_reference or
search_final_evidence, not both. Canonical search uses that same resolver.
Scoped inherited budgets/limited views are never reset.

### Standalone canonical indexing

`kg.indexing.IndexService` executes document-sized incremental/rebuild units.
One logical configuration binds fixed embedding model pins, dimensions, exact
representation, lexical and dense-scoring versions. Each initialized provider
supplies its separate runtime/encoding identity. Durable status is explicitly
`provider_compatibility="unverified"`; status/pending do not initialize models.
The separate `EvidenceSearchService` consumes those projections; coordinated E4
completion remains unsupported.

Admission advances the document/configuration slot's persistent fence and retires
an unfinished predecessor. Obsolete payload drains in transactions deleting at
most 1,000 derived rows before replacement staging starts. Each durable phase
rechecks exact writer/scope, current active source state and namespace token, plus
the latest nonterminal attempt. Models run outside write locks. Superseded workers
cannot regrow staging or overwrite a newer result, including through failure writes.

The passage kernel publishes one immutable state/set association before vector
work. Projection members contain quote-only lexical input, exact representation
hashes and finite L2-normalized little-endian float32 vectors. Publication verifies
the complete manifest and atomically installs all rows and the active pointer,
completes the attempt and updates default indexing readiness. No partial stage is
search-ready. Empty membership is complete only after actual passage publication,
provider initialization/identity validation and projection publication.

Incremental reuse is document-local and requires identical passage identity,
representation bytes/hash and full provider identity with vector integrity checks.
Metadata-only noncontextual changes can reuse vectors but require a new state-bound
projection. Rebuild recomputes vectors without rewriting canonical evidence or
knowledge dependencies. Repeated incremental processing verifies a matching complete
projection before returning unchanged. Crashed staging is discarded by a new fenced
attempt; inference never resumes from a fake checkpoint.

Cleanup is separately authorized, preserves active projections/live staging and
canonical history, and retains at most 100 payload-free terminal summaries after
draining. The fence counter survives pruning. Interrupted postpublication cleanup
can leave `cleanup_pending`; explicit bounded cleanup converges without treating
physical disk/WAL reclamation as complete. Historical citations continue to resolve
after projection removal. All process/read/cleanup paths inherit their invocation's
single private budget and deadline; exhaustion is explicit, not partial readiness.

### Scoped canonical full search

`kg.indexing.EvidenceSearchService` executes one full search on a canonical read
snapshot. It uses the existing observer-before-snapshot admission and original
observer release fence. Search creates no canonical rows or durable leases.
Current, active, authorized documents must all have complete projections matching
both logical configuration and initialized embedding identity. A later failed
attempt does not invalidate a still-valid complete projection. Empty membership
still requires both pinned providers and a valid encoded query.

Private FILE TEMP FTS5 contains only visible current projection quotes, with
`unicode61 remove_diacritics 0` and BM25 weight 1.0. Titles never enter lexical
statistics. Natural query tokens use Python Unicode `\w+`, casefold and ordered
deduplication; tokenizer-empty tokens are discarded by a same-tokenizer probe.
Quoted token phrases are OR-combined, never interpreted as caller-provided FTS
syntax. No surviving token is `invalid_request`.

Dense retrieval scans every scoped normalized float32 vector using `math.fsum`
dot products and a bounded top-k heap. Both streams retain `max(50, limit)` with
passage-ID ties; weighted reciprocal-rank fusion uses lexical 1.0, dense 0.5 and
constant 20. Deduplication is by passage ID, not quote equality. At most that same
candidate limit goes to pinned MiniLM, using configured exact quote or title/two-LF/
quote representation. Reranker calls use one sequence, bounded UTF-8 input-position
preflight, and unchanged native encoding. Scores remain raw, not confidence.

The original private row/VM/scratch/provider/deadline allowances cover readiness,
TEMP, vector decoding, ranking and the shared final evidence resolver. Only five
semantic stages reserve public units: eligible TEMP insertion, lexical admission,
every eligible dense evaluation, deduplicated rerank input and final evidence.
The controlled one-passage case consumes five, not six. Incomplete execution has
no ranked-result fallback. TEMP is connection-local and drops after success;
failed snapshots must be discarded by their owner, closing TEMP and scratch.

The private borrowed-context entry returns a `SearchSelection` containing the
existing `RankedSelection`/session-bound `ProjectionHandle` and exact hydrated
response. It neither creates a pool nor releases/closes the caller's snapshot.
The caller must retain the original observer and perform final release. Child
captures join the existing disclosure group and cannot publish independently.
`QueryService` consumes this seam in its spawned worker; its supervisor retains
the original observer and owns final disclosure.

Ordinary summaries and detailed candidate events describe the actual single
execution. Detailed events preserve stage absence, scores, ranks, RRF contributions
and return-limit decisions. Quote capture is opt-in. No-data failures irreversibly
redact candidates/configurations; report capacity failure cannot alter ranking.
Controlled-provider tests demonstrate mechanics and exact provenance only, not
real-model quality, representative workload performance or complete E3 acceptance.

### Immutable knowledge registry

`kg.knowledge.KnowledgeAdministration` provisions one immutable typed registry per
existing corpus. Its `knowledge/1` values define allowed entity types, identifier
schemes and subject/object-typed predicates. The optional string-only
`direct-subject-decision/1` descriptor is part of immutable configuration identity;
registration is not record production or a knowledge-write service.

The K1-owned facade uses the existing provisioned `LocalAdminAuthority` and
canonical `writing` owner. The private kernel requires an active owner context
whose identity matches that authority and addresses only the supplied corpus.
Regular `Scope`/`LocalIdentity` values and policy grants are not administrative
credentials. Trusted bootstrap/provisioning deliberately has no scoped diagnostic
capture; no administrator namespace grant or audit subsystem is introduced.

Only the existing `knowledge_schema` table is written. Definitions use compact
canonical JSON with sorted object keys and sorted registry/type collections;
corpus, schema/interface versions and optional descriptor/nulls are included.
The stored hash is SHA-256 over those UTF-8 bytes. Repeats validate the persisted
definition/hash/version and compare full canonical content, not only a digest.
Equivalent reordered definitions return unchanged without modifying stored bytes;
any changed definition/version conflicts. Invalid stored state is an explicit
internal error, never silently repaired.

The owner serializes concurrent registrations with `BEGIN IMMEDIATE`; rollback
removes an uncommitted registration and reopening retains a committed one.
Different corpora remain independent. Provisioning adds no contribution, retry
receipt, seed, state-intent, report or processing-readiness record and performs no
DDL or migration. See [CONTRACTS.md](CONTRACTS.md#knowledge-registry-api) for exact
value limits and the delivered API.

### Atomic owned knowledge

Anchor/passage-backed enrichment uses the existing canonical schema and the evidence
dispatcher's single write owner. It revalidates exact namespace/owner/writer
bindings, the immutable registry, all direct support, stored endpoints and the
whole planned post-state before committing mappings, provenance and a complete
receipt together. Independent creation-support contributions activate identity;
aliases, identifiers, mentions and assertions do not. Forward AddEntitySupport can supply
a stored endpoint within the unit, but only for an historically visible,
nonretired identity with exactly matching name/type.

Passage support uses E3's exact immutable membership resolver on that same
authorized transaction, with the validated passage/set IDs persisted alongside
source state, anchor and metadata snapshot. Explicit mentions use only passages
and do not create assertions, deduplicate identities or link same-named entities.
They have the same conjunctive support, endpoint eligibility, scoped history and
durable retry rules as other contributions. Passage publication suffices before
vectors are ready. Vector-only rebuild preserves knowledge; source, metadata,
boundary and policy changes followed by restoration cannot resurrect stale support.

The assertion row is the submitted decision record when its immutable predicate
descriptor is `direct-subject-decision/1`. Explicit string submissions are distinct
by assertion ID, not statement text. Subject association is the stored subject,
not a query-time interpretation. Current eligibility requires the full original
source support and a visible current creation witness, selected by contribution
sequence. Historical reads retain stale payloads/citation identities without
claiming current support and still require complete present-day read access.

Seed additions maintain owned slots and append membership events in that same
transaction. Exact active repeats preserve IDs/generation; changes conflict.
Omission does not withdraw anything. The supported kernel does not expose
whole-set replacement, correction,
retraction, traversal or inference.

Operation-specific retained manifests authorize ordinary writes, retries and
scoped reports without document-write grants or fabricated target IDs. Settled
linked replay preserves expiry-before-digest and performs no new acknowledgement;
unlinked ordinary knowledge receipts cannot be adopted by coordination.
Controlled participant tests are not actual E4 acceptance.

Read services use the actual authorized snapshot and a fresh release fence.
Interactive snapshot selection retains one 10,000-visit child allowance across all pages and
nested work; admission/release retains the original 100,000 root/deadline.
Shared VM and scratch allowances remain cumulative. Every eligible entity/decision
admitted to a composed selection reserves its public event once; internal
support checks do not masquerade as public evidence requests. The private adapter
produces real selection records but does not itself implement Q1 count or retained
query support. See [knowledge contracts](CONTRACTS.md#knowledge-enrichment-and-reads).

## Durable processing control

`kg.processing` uses the existing complete canonical schema, not a second queue
database. Trusted administration registers immutable plans and exact worker
bindings without granting source access. Ordinary calls require the current
principal's read policy plus the exact registered selection. Document targets
include exact immutable state, revision and namespace policy token; owner,
namespace and corpus are checked against canonical evidence. Logical work keys
also include principal/owner/writer, plan version and definition hash. Restoring
identical bytes still creates distinct work because the activation state changes.

Scheduling owns one shared `writing` transaction for job/dependency and
processing control receipt. It reuses the canonical UTC watermark, 30-day expiry
and permanent key tombstones. Settled schedule/retry replay authorizes the retained
selection/document before clock/expiry and incoming digest; it does not require
the old source to be current or advance job progress. Expired/conflicting retries
commit required clock/expiry maintenance before raising a typed error.

Claims serialize with `BEGIN IMMEDIATE`, order due candidates by due time,
creation sequence and opaque ID, inspect at most 200 candidates, and CAS the
status/fence. Heartbeats require exact live worker/fence and current dependency,
enabled plan and corpus guard epoch. A dead worker's inclusive lease expiry is
recorded as persisted retry delay or ten-attempt exhaustion; old fences cannot
renew replacement work. The service never executes content or promises resumable
inference. It exposes no completion/acknowledgement; claims and job history cannot
establish evidence readiness. Stale source work is superseded, disabled plans
and corpus guard invalidation block it.

Explicit worker failure is a current-claim CAS, not a business owner result.
Only the registered closed transient resource-budget classification schedules
retry; permanent validation/unsupported/internal failures remain terminal.
`retry` requires exhausted failure plus exact status version and all fresh guards.
The atomic restart resets only episode attempts, increments episode/status/fence,
and commits its control receipt alongside the job. Replay returns that historical
receipt even after later job progress; conflicting/expired replay cannot restart it.

Bounded recovery scans nonterminal jobs by creation sequence. It supersedes stale
dependencies, records expired running leases and requeues eligible disabled-plan/
authority blocks without resetting counters or shortening a persisted not-before
time. Current live claims and future retries remain unchanged; repeated recovery
does not change job versions. Owner-only processor/purge/awaiting-input blocks
are explicitly unsupported and remain blocked (unless their target becomes stale).
No availability inference, purge release, guard-epoch adoption or E3 readiness
mutation occurs. Terminal rows are excluded. Administrative plan toggling uses
boolean CAS and leaves the immutable definition/logical job identity intact.

Standalone calls share a five-second private budget, not per-helper deadlines.
Private transaction entry can inherit an existing budget unchanged. Status uses
the shared observer-before-snapshot and fresh release fence without clock/DML.
Reports use the same bounded process-local collector and fixed target fence,
including exact selection authority when there is no job. Diagnostic construction
is isolated from business execution; commit reports use the owner context only
after exit. Registration is trusted provisioning outside scoped reports.

Scheduling, heartbeat, failure, retry and recovery commits invalidate a concurrent Q1 execution's original
observer even though they do not change source content or readiness. Q1 withholds
that execution's data/accounting/report as `state_changed`; a fresh query can
observe the new generation. E4 status/report inspection performs no canonical
commit and does not invalidate that generation. Passage-aware E1 report checks
and E4 selection checks remain owner-specific: losing an E4 worker binding does
not revoke otherwise-authorized historical passage reads, and neither authorizer
accepts the other owner's target kinds.

No physical schema/format change, second receipt ledger, state-intent scanner,
batch/checkpoint/snapshot completion, knowledge mutation, provider publication or
readiness write is included. The reserved participant interfaces below remain
integration seams, not actual E4 acknowledgements. Public API and limits are in
[CONTRACTS.md](CONTRACTS.md#processing-control-api).

### Private shared execution primitives

`_coordination` defines typed write/index participants and exact unit/document/key
values. `_dispatch` runs optional trusted document participants on E1's own
transaction, never an outer transaction. Classification precedes replay; settled
success reauthorizes retained targets before durable clock/expiry and digest checks.
It does not run new-work guards or acknowledge again. First observation of an
ordinary document receipt requires its accepted state still be current before
acknowledgement. New writes atomically include content, state intent, epoch,
provenance and receipt. The participant's final guard/settlement is immediately
followed by owner commit. Participants cannot commit, roll back or retain a live
connection past owner exit. Commit exceptions remain `unknown`, not assumed rollback.

`_lifecycle.deactivate_absent` creates an inactive immutable E1 state and exact
run-linked removal provenance in a caller's live canonical owner context, without
inventing retry keys. It is not a public snapshot-completion API. The schema and
installed capabilities are unchanged; controlled participants are not an E4 service.

`_read_context` provides private observer-before-snapshot admission, one authorized
snapshot and a fresh `BEGIN IMMEDIATE` release fence. The observer compares
`data_version` only on its original persistent connection, including commits in
the observer/snapshot gap. Every canonical commit invalidates that generation.
Release reauthorizes the exact identity/scope while writers are excluded and
unlocks by rollback, without canonical DML. Retained references explicitly own the
observer lifetime; a later inspection can use a new call's budget, never reset an
existing one. Read adapters cannot mutate canonical data or end the snapshot;
connection-local TEMP is configured as `temp_store=FILE` before participant
controls, with readback and a verified 128 MiB page cap. Admission requires a
reported `TEMP_STORE=0`, `1` or `2` compile option; forced-memory (`3`) or
unverifiable builds fail with `unsupported`. FILE mode permits SQLite to spill
TEMP to files; SQLite/OS caches and the temporary-directory filesystem still
apply. This is not a guarantee of physical disk writes or a process RSS bound.

`kg._execution_budget` separates semantic reservations from inherited private
visits, SQL VM, scratch/provider and absolute-deadline allowances. Interactive local step
views share one private pool: 100,000 visits, 10,000,000 prepaid VM instructions
(quanta at most 1,000), and 64 MiB aggregate scratch. Individual text/context and
reranker reservations are at most 8 MiB; vector batches at most 16 MiB, with
provider batches at most eight sequences/8,192 padded token positions. Reservations
precede consumption; scratch ownership cannot be copied and release is idempotent.
Private counters are absent from public accounting. The public search schedule
has five one-passage events: TEMP, lexical, vector, rerank, final evidence.
Readiness, fusion and counting an existing selection add none. These are shared
contracts, not an installed generic search. The evidence query executor below
consumes the same budget operations through supervisor-owned reservation RPC.

`PrivateBudget.limited(max_visits=10_000)` creates a cumulative local view of the
same pool/deadline. Visits reserve against every ancestor cap atomically; VM,
scratch and provider reservations use the original pool. Sibling views never
reset global consumption. `CanonicalReadContext.using_budget(view)` applies the
view to both its step meter and all connection fetches on the existing snapshot,
including helper SQL and EOF lookahead. Retain and reuse that view across pages
of one selection; enter the scope on every fetch. Nested scopes can only inherit,
not replace or widen, the current view. Exit restores the previous accounting
boundary, not the allowance. These are trusted synchronous owner contexts, not
concurrently shareable connections or a multiprocess transport.

The private `_graph_build_operation(deadline=..., cancel=...)` creates a separate
`graph-build/1` operation for internal bulk reads. It retains the exact original
absolute deadline (at most 300 seconds remaining), cancellation Event, budget and
StepMeter; existing public calls cannot select it. Root and profile-selected
knowledge-reader children count visits without a cumulative ceiling, and SQL
continues prepaying 1,000-instruction quanta without the interactive 10M ceiling.
Explicit `limited(max_visits=N)` still enforces N through every ancestor. No
page, child, connection or phase resets work totals. The 200-item page maximum
is a batch bound, not total coverage or evidence of eligible EOF.

Bulk reads retain the same 64 MiB logical scratch, per-unit/provider limits and
128 MiB SQLite TEMP cap. Deadline/cancellation checks occur at reservations,
fetches, SQL progress and short root-lock waits; SQLite busy waits are at most
100 ms per statement rather than the unchanged interactive 5,000 ms. Stops latch
on the bulk root. `CancelledStop` is a private-resource subtype; owners use the
original operation's immutable accounting snapshot to distinguish its latched
reason, including after the Event is cleared. Diagnostics retain cumulative
visits/prepaid VM/semantic units, live/peak scratch and original expiry after
terminal failure; cleanup can still release resources. These are cooperative
controls, not hard deadlines, filesystem quotas or process/native RSS isolation.

`CanonicalReadContext._reserve_scratch` returns a registered reservation;
`_release_reservation` releases and unregisters it idempotently, even after a
stop or context exit. This permits bounded owner-managed temporary lifetimes.
Interactive cursor/Store retention is unchanged. Bulk selection discards each
rejected activation's scratch immediately while preserving the complete selected
witness and charging any retained copy. These controls do not provide a public bulk API.
No canonical format, eligibility, scope, exact witness or Q1 RPC contract changes.

The separate private `evidence._graph_observer` boundary retains one physical
SQLite source observer and its original `data_version` baseline across operations.
`open_graph_source()` issues one immutable `GraphSourceBinding` for the exact
validated identity/scope. Only that issued binding object can open an operation;
copied bindings, narrower scopes and replacement connections cannot authorize
the original source. The resolved database path is pinned. Hot database-file
replacement/restore while an owner is open is unsupported.

Every `operation(binding, identity, scope, deadline, budget)` pins the supplied
current pool and absolute deadline. It reauthorizes/checks currentness on entry,
offers one optional canonical snapshot, and ends release through a single short
fresh `BEGIN IMMEDIATE` authorization/change fence. Each operation has a fresh
read-session ID: retaining source identity does not revive older snapshot handles.
All polls/admission/fence work are privately accounted; there are no semantic
record charges for freshness checks. Sequential phases of one request must use
that same request pool/deadline, not reset them. Independent later requests may
use new budgets. Between operations the source has no bound budget, progress
callback, polling or open read transaction. Existing Q1 observers, retained
inspection, limits and spawned workers remain on their unchanged paths.

Graph-source ownership is explicit and same-thread. Active operations borrow
the source; closing an active source, reentrant binding and cross-thread use
are rejected. Source changes, policy denial or loss of the physical connection
invalidate trust permanently. Deadline/resource stops abort the operation, not
the unchanged source baseline. Any result or readiness prepared inside a release
fence remains tentative until its context exits successfully; consumers must
undo publication on failure. Native work or long serialization must not run
under the fence. This boundary alone provides no graph builder, public graph
queries, controller/refresh lifecycle or durable freshness token.

Graph acquisition takes cleanup custody immediately after SQLite connect, before
shared budget/row-factory/PRAGMA setup. A failed setup followed by failed close
raises private `GraphSourceCleanupError` with the original typed failure and a
cleanup-only source owner. A logical closed flag is not physical-close evidence:
graph disposal confirms success only after base SQLite close returns, retaining
the original resource for explicit owner-thread cleanup retry otherwise. Such a
source can never resume reads or reopen itself. Ordinary database opens share the
same initialization implementation without adopting this graph ownership path.

### Private disposable graph staging

`knowledge._graph_export` enumerates one bulk canonical snapshot to explicit EOF
in authored-ID order, all eligible entities (including isolated ones), registered
entity-object relationships and explicit `direct-subject-decision/1` assertions.
It reuses canonical eligibility and selected-witness logic, not search candidates
or top-k results. Export pages own at most 200 items/8 MiB serialized data; a
byte-boundary pending item is emitted/semantically charged exactly once. Full
selected source conjunctions, passages, seed membership and parallel authored
assertions retain their identities. Coverage includes exact schema definition.

`graph._build.build_graph(..., staging_parent=..., operation=...,
expected_coverage=None)` requires the original bulk operation and exact identity/
scope. It creates a unique private stage, streams page transactions, verifies
native node/edge counts and inserted property/endpoint/ordinal equality, checkpoints,
closes and reopens read-only, then fences with the original physical observer.
The complete manifest is not a durable freshness token. `StagedGraph.transfer`
accepts only that original current operation, moves ownership once and leaves the
verified OPEN native reader and original parked observer for the future controller.
Publication and public traversal/joins/retained inspection are not implemented.

The optional runtime is Ladybug 0.20.4 on macOS 15+ ARM64/CPython 3.12.
Both writer and reader use a 256 MiB native buffer pool/two threads. The 300-second
request deadline, logical scratch allowance, bounded pages and 1 GiB staging disk
check are operational controls, not hard native RSS/time/process limits. Native
calls receive remaining-time query timeouts and before/after cancellation checks.
Borrowed native row pages expire at the next read/close; retained copies require
caller accounting. Ordinary and bulk current budgets are supported without a new pool.

Catchable failures withhold stages, preserve original failure/accounting, unwind
snapshots and attempt explicit cleanup. Failed cleanup transfers one cleanup-only
residue; disposal marks confirmed resources released and never blindly retries
an indeterminate native close. An explicitly committed transaction whose
post-commit checkpoint fails is not rolled back or replayed; the stage is rejected.
**Fatal native failures can terminate the host**, including checkpoint exhaustion
followed by native database-close SIGSEGV. Python cleanup cannot contain this.
This experimental risk is accepted, not fixed by increased capacity; persistent
worker isolation is deferred to [#137](https://github.com/markgar/local-knowledge-graph/issues/137).
SQLite format and Q1 paths remain unchanged; never reopen/admit leftover stages.

`writing(database, identity, *, deadline=None, budget=None)` accepts that same
inherited/local view; an explicit deadline must match it exactly. An owner can
also narrow an already-budgeted write with `CanonicalWriteContext.using_budget`.
Admission/schema checks and nested support fetches are accounted. Deadline-only
writes create one ordinary pool; calls without either argument retain E1's
unbudgeted default. Resource/deadline exceptions in the body roll back normally.
Final acknowledgement/settlement still proceeds immediately to owner commit,
without a new deadline or live-claim recheck.

K1 witness/selection DTOs preserve the producer's contribution-sequence witness
verbatim, including distinct seed and source bases. E3 projection handles contain
scoped immutable identities and are usable only in their live read session.
Neither DTOs nor handles install runnable knowledge/indexing adapters.

K1 retained-member revalidation uses a reader-local cache tied to one live
canonical context: immutable schema, at most 200 source proofs, 200 activation
bases and 200 exact full witness proofs. Each member still reads its actual
assertion and complete support, under the original root budget and its local
10,000-visit view. Exact supplied sequence/basis/dependency bundles are compared;
a cached alternative activation never replaces the retained witness.
Cache reservations are separate from transient per-member scratch and are
released on reader close, with context teardown reclaiming outstanding handles.
Inspection closes the reader on every exit. No cache transfers authority across
contexts, scopes, calls or workers, and the shared 100,000-visit/10-million-VM
ceilings are unchanged.

### Canonical evidence query execution

`kg.query` implements canonical full ranked search, anchor/passage evidence and actual K1-dependent
resolution/decision records/counts documented in
[CONTRACTS.md](CONTRACTS.md#canonical-evidence-queries). It consumes the actual
E3 passage resolver and K1 snapshot readers, not an alternate producer or legacy
adapter. Paths and durable continuation remain unsupported. Selected
closure is computed from validated named dependencies; unsupported required
operations fail before dispatch, while unrelated branches are pruned.

An owner thread holds the original SQLite observer and the authoritative public/
private ledgers. A spawned process receives only immutable request/session/
deadline values, never live connections, collectors or local pools. Its bounded
64KiB synchronous control frames reserve visits, VM quanta, scratch and retained
local views in that original owner pool. No refund of acknowledged semantic
charges occurs on death. Scratch handles are reclaimed after verified child
cleanup. The child uses the shared canonical read context and evidence resolver:
Q1 does not duplicate evidence eligibility or passage rules. No unbounded result
frames cross the control pipe; source text is excluded except bounded explicitly
opted-in diagnostic quote events.

Selected search borrows the actual E3 service in that worker context with the
original deadline, step meter and supervisor-owned private pool. Projection
handles never leave the worker/session. E3's already-hydrated final references
become a foundation RankedResult without another evidence charge; the single
matching-passage public schedule remains five. No request-language dependency is
added, and pruned search steps do not load providers. Capabilities advertise the
operation, not current index/provider readiness.

Worker-local actual E3 capture is transported as size-declared bounded chunks
into a supervisor-owned indexing child in the existing parent disclosure group.
Q1 reserves its transport/copy allowance before enabling capture; allocation
failure discards only optional diagnostics. Business scratch can reclaim this
allowance after the worker discards capture and acknowledges that discard; the
same rejected reservation is retried without refunding public work or resetting
the deadline/private pool. The child has the original observer,
exact document/evidence targets and parent step linkage. It cannot publish from
the worker or before parent release. The supervisor's existing short fence
authorizes both data and prepared reports; later child lookup rechecks the
retained complete group even after parent eviction. No report is a support set.

Dependent K1 steps share one spawned canonical snapshot and one supervisor-owned
pool/deadline. Synchronous, size-declared JSON chunks carry exact producer witness
bundles into a private staged registry. Only a successfully validated result and
original-observer release fence can atomically publish a count's support identity.
Private/deadline failure discards any earlier prefix; a public-budget prefix is
explicitly inexact. The count adds no semantic reservations and never counts the
displayed page. Complete standalone records display at most 1,000.

The owner-thread registry retains immutable canonical member payloads, not live
SQLite snapshots, for five minutes with 32-set/8-MiB-per-set/32-MiB-aggregate
ceilings including in-flight copies. Inspection sends bounded slices to a new
supervised worker for exact K1 revalidation under its fresh request budget, then
uses the original observer at release. Writers are not blocked during membership
validation. Scope/step mismatch, expiry, close/restart and generation invalidation
cannot trigger a rerun under an old identity. Report target saturation discards
diagnostic capture only; it does not shorten or invalidate count membership.

Published generated anchors and passage references use the same operation-specific
hydration as supplied anchors: exact historical membership and byte validation,
one `evidence_reference` charge, and retained scratch in the original pool.
The supervisor retains the complete reference for data and diagnostic release.
No passage rows, citation contexts or search results are synthesized by Q1.

All target preflight occurs outside the short release fence. Fresh scope
authorization plus original observer comparison excludes intervening canonical
changes; prepared report publication reuses that owning fence rather than
deadlocking by acquiring a second writer connection. Business execution is
outside the diagnostic allocation guard. Later diagnostic listing/body reads
share one lookup deadline/private pool across all headers and target checks.
Report retention failure does not make valid evidence execution fail.

### Shared execution diagnostics

`kg.diagnostics` owns one bounded collector, authorized discovery facade and
parent/child disclosure group. `kg.models.execution` imports closed package event
values, not package services. E1 captures actual ordinary read/write phases and
the canonical owner's commit observation; its named explained methods call the
same operation once. Stored outcomes, schema/signature, receipt ordering and
semantic/private execution budgets are unchanged.

Capture admission reserves a full report slot before event validation/copying.
Logical JSON sizing short-circuits without allocating a serialization; quote and
batch/target limits apply before capture copies. Report bodies are prepared outside
authorization fences. The collector never evicts active work and never interrupts
business stages to fit a trace. Retention failure after a confirmed commit produces
diagnostic unavailability, not a failed write.

Trusted owner integrations use `Collector.begin_capture`, `Capture.retain`,
`append`, `quote`, `configure` and `finish`. `finish` returns a non-disclosing
`PreparedReport` handle, not release authority. Retain **all** dependencies that
make a summary sensitive, even if a detailed event will not fit. Exact evidence,
writer, knowledge witness, owned seed-set/knowledge-writer binding, indexing
configuration and processing target values
form a bounded union. E1's authorizer implements only its own target kinds and
rejects unimplemented kinds. Owner integrations must supply fixed typed target
checks, never arbitrary request predicates.
Owned seed targets do not require a fabricated contribution for an empty set.
Trusted bootstrap/schema provisioning is excluded from this scoped execution
report API; ordinary scoped knowledge read/write/seed operations are not excluded.
There is no administrative audit subsystem or invented admin namespace grant.
Construct diagnostic-only event/target values inside `with capture.guard():`;
this catches allocation failure without covering or suppressing any business
execution. Collector admission, sizing and retention independently isolate
allocation failure. Both boundaries log a fixed safe availability reason.

Nested captures share one `DisclosureGroup`. A child cannot publish itself;
until the owning parent releases the group under its fresh authorization fence,
public child lookup is unavailable. Retained original-observer references bind
later disclosure to the same canonical generation. Group terminal redaction,
scope and all dependency bindings survive parent eviction until the final child
expires. No-data failure/close clears event/configuration payloads irreversibly;
supervisor close finalizes active reservations without publishing them.

Public disclosure reauthorizes all dependencies under a fresh read-only
`BEGIN IMMEDIATE` fence, released by rollback. Listing also checks the complete
returned header page under one final fence. No report lookup creates another report,
advances the receipt clock, writes a canonical table, commits or runs a business
operation. Reports disappear with service/process lifetime and are not durable
audit records or retained query support. See [CONTRACTS.md](CONTRACTS.md#execution-diagnostics)
for the public API, exact fixed limits and explicit unavailable/redacted shapes.

The following sections describe the **separate Markdown demonstration**, unless
explicitly referring to `kg.evidence`. Its fixture/gold assets are preserved; its
databases and APIs are not a migration/compatibility contract for the new engine.

## Markdown demonstration boundaries and invariants

The package ingests manifest-selected local Markdown, stores canonical evidence
in SQLite, and exposes cited search and structured reads through Python and a
thin CLI. Configuration is data: subjects, aliases, paths, and corpora must not
require parser or retrieval code changes. The caller generates narrative answers;
retrieval does not generate an answer or infer truth from model knowledge.

- Documents have stable identities, including unambiguous moves. Reused paths
  receive an unused deterministic identity rather than overwriting a moved document.
- Revisions are immutable content versions. Anchors preserve exact offsets,
  structural and heading paths, quotes, and quote hashes within a revision.
- Every derived record resolves to source evidence. Historical citations retain
  their original revision and title, even after edits, removals, or restores.
- Current retrieval uses active current revisions of the requested corpus.
  Other corpora, removed documents, and historical revisions cannot alter its
  lexical ranking statistics. Model/vector projections never replace provenance.
- No inferred entities, relationships, owners, decisions, blockers, contradictions,
  or cross-source completion are generated. Similar names are not merged.

## Configuration and ingestion

`kg ingest --manifest <corpus.yml>` validates source selection, parses Markdown,
and updates the canonical database and corpus-local lexical projection.
`load_manifest()` and `IngestService.ingest()` expose the same configuration and
ingestion boundaries to Python.

Manifests specify corpus ID/display name, vault root, database, include patterns,
seed entities/approved aliases, and optional metadata mappings such as
`event_time: date`. Paths are relative to the manifest. Symlinks are rejected by
default; sources over `max_source_bytes` fail explicitly. Source discovery,
parsing rules, and ranking remain generic across corpora.

CommonMark headings, paragraphs, lists, checkboxes, and wikilinks produce exact
source ranges. Numbered and bulleted checkboxes supply task status. Inline
`[owner:: ...]` and `[due:: ...]` fields supply optional task metadata. Items and
paragraphs under exact Decision(s), Blocker(s), and Conflict(s) headings become
explicit records. Fenced code is not classified as structured knowledge.
Approved aliases create exact mentions; wikilinks create cited relationships
between configured entities in an anchor, not inferred dependencies.

Unchanged ingestion reuses revisions without changing document state. Edits
preserve prior evidence. Restores reuse an immutable content revision but record
a new activation and its immediate predecessor. Source removal and failures
deactivate current evidence without destroying retained history. Parser/config
changes can rebuild extracted records without claiming a new content revision.

Internally, `ingest/_intake.py` translates local Markdown into the private values
in `ingest/_prepared.py`; `ingest/_writer.py` persists those values without reading
files or depending on Markdown parser objects. `IngestService` remains the public
orchestrator and owns transactions, per-source savepoints, seed synchronization,
schema adaptation, deactivation, and lexical refresh. The writer uses that existing
transaction; unchanged revisions still skip record-state validation and rebuilding.
These are internal boundaries, not a generic ingestion API or a new source format.

### Ingestion diagnostics

`ingest --explain [--include-quotes] [--explain-limit 50] --format json` returns
`IngestReport` with `report_version: "1"`; ordinary ingestion returns `IngestResult`.
This performs ingestion, not a dry run. Python uses `explain`, `include_quotes`,
and `detail_limit`.

The report gives configured entities, unmatched patterns, source-ordered
document outcomes/reasons, identity, current/previous revisions and paths,
revision reuse, active state, and `records_rebuilt`. Successful documents include
stored counts, mentioned entities, source anchors/passages, structured records,
extraction rules, and evidence IDs. Counts are stored revision totals, not rows
newly created by this run; historical anchors may outnumber current passages.

Anchor and record lists are independently bounded to 50 per document (1..200),
ordered by source offsets and identity ties, with explicit truncation and complete
counts. Quotes require opt-in; metadata is not anonymized. Reports use the
ingestion transaction; report construction failure aborts ingestion. Report bodies
and quotes are not stored in `ingest_run` or operational logs. Malformed YAML
errors omit source snippets. Failed sources produce a nonzero ingestion exit.

## Index preparation and model readiness

The workflow is **ingest -> matching dense-index -> search**. Ingestion does not
build vectors or load semantic models. `kg dense-index` / the low-level
`DenseRetrievalService.build_index()` build a disposable sqlite-vec projection
beside the canonical database, keyed to exact passage/anchor/revision identities.

`--embedding-profile` selects `gte-modernbert` (default) or
`qwen3-embedding-0.6b` on both indexing and search. Model names, pinned revisions,
dimensions, normalization, prompts, and encoding behavior are defined in
[`dense.py`](src/kg/retrieval/dense.py). GTE retains its compatibility-default
projection path. Qwen uses its official asymmetric query instruction, 1,024
normalized dimensions, and uninstructed documents. The pinned reranker is
`cross-encoder/ms-marco-MiniLM-L6-v2`, revision
`233902d25c440f23af6f7d6e94d2946bac0bee0a`.

`--contextual` is opt-in on both commands. It supplies `Title: <title>`,
`Heading: <path joined by / >`, and untouched passage text separated by blank
lines, omitting absent metadata. It changes neither queries nor lexical
retrieval nor canonical quotes. `title-heading-passage-v1` contextual projections
are separate from `passage-text-v1` projections and can coexist.

Projection compatibility includes profile, pinned model, runtime versions,
actual device/dtype, dimensions, normalization, context and encoding behavior,
source-text version, and corpus fingerprint. CUDA, MPS, or CPU may be selected
by the local runtime; device/runtime changes may require rebuilding. Ingestion
that changes the fingerprint invalidates the selected projection until rebuilt.

Product search validates arguments before readiness and enforces readiness on
**every call**, including empty corpora and filters matching no passages.
Missing/stale/incompatible projections, unavailable embeddings, and unavailable
rerankers fail explicitly. No keyword-only, vector-only, unreranked, or empty-list
fallback represents success. Building vectors prepares embeddings, not the
reranker; the first product search initializes the latter separately.

Providers load lazily and are cached within a service lifetime, not globally
across CLI processes. The index-readiness verdict is not cached; a service must
fail after an invalidating edit and recover after rebuilding without reloading
ready providers. Only approved package/model downloads and caches may be used;
network-policy failures are blockers, never bypassed.

## Product search

```python
from pathlib import Path

from kg.db import Database
from kg.retrieval import SearchService

search = SearchService(Database(Path("index.sqlite3")), "corpus-id")
results = search.search("release evidence", subject="Atlas", limit=20)
report = search.explain_search(
    "release evidence", subject="Atlas", limit=20,
    include_quotes=False, trace_limit=50,
)
```

Both methods accept keyword-only `subject`, `limit`, `since` (a datetime;
prefer timezone-aware values), and `source_path`. Construction also accepts
`embedding_profile` and `contextual`.
There are no public stage-bypass options.

`kg search <query> --manifest <corpus.yml>` uses this facade: natural any-term
BM25 keyword candidates and semantic candidates receive identical constraints,
then canonical-ID deduplication and weighted reciprocal-rank fusion feed the
cross-encoder. The existing defaults remain lexical weight 1.0, dense weight
0.5, fusion constant 20, and candidate pools of 50 expanded for larger requested
limits. CLI result limit defaults to 20 and accepts 1..100.

Subject scoping uses configured entities/aliases, literal name boundaries,
document inheritance, and bounded undirected two-hop explicit relationships.
Atlas does not match Atlascope. No general query-to-entity resolution or new
graph candidate generator is implied. `--source` and `--since` retain existing
source/date semantics; date filtering uses configured event times with revision
timestamps as fallback. CLI durations use positive `h`, `d`, or `w` units.

Ordinary JSON is still `list[SearchResult]`: record identity/type, title, summary,
status, event time, source path/revision, anchor, heading path, exact quote,
related entities, and `rank`. Returned list order is authoritative: raw
cross-encoder score **descending**, then record ID for ties. Scores are not
confidence, not comparable across queries/models, and not BM25 scores.
Consumers must not re-sort ascending or apply a former BM25 threshold.

### Execution explanations

`search --explain [--include-quotes] [--explain-limit 50]` calls
`SearchService.explain_search()` and returns `ProductSearchExplanation`,
`report_version: "2"`. The trace limit is 1..200 and is valid only with
`--explain`, as is quote opt-in. Text output renders the same diagnostic object;
use `--format json` for the machine-readable contract.

The report captures the actual execution, not a second search: corpus identity
and fingerprint; effective filters and natural lexical expression; projection,
profile, model revisions/pipeline versions and contextual configuration;
candidate limits, fusion weights/constant; score meanings/tie-breakers; complete
stage counts; bounded graph scope/supporting edges; and all final hits.
`active_current_revisions_only` is true, `supersession_filter_applied` is false.

Each candidate has `record_id`, nullable lexical/dense/reranker positions and raw
scores, fusion position/score and individual contributions, shortlist membership,
and nullable final position. Missing stages and contributions serialize as
explicit JSON **null**, not zero or omitted keys. Use `model_dump()` /
`model_dump_json()` **without `exclude_none=True`**. The serializer independently
omits hit quote keys unless requested; candidate entries never carry text.

Candidates are displayed in final-hit order, then remaining reranker order,
then remaining fusion order, deduplicated and capped. `total_candidates`,
`displayed_candidates`, `trace_limit`, and `truncated` expose display bounds.
Changing display limits/quotes does not change scoring, counts, or the separate
hits list. Paths, names, headings, query/filter text and metadata remain visible;
omitting quotes is not anonymization.

Ordinary and explained search reject intervening database commits, including
edit-and-restore with an unchanged final fingerprint. Neither retries silently
nor returns partial mixed-state results. Retry after writes finish. Unrelated
corpus writes in the same database may also conservatively invalidate a call.

## Structured state and evidence reads

These operations use `RetrievalService`, not semantic search, and require no
vector index or model initialization.

| CLI operation | Contract |
| --- | --- |
| `actions <subject>` | Explicit tasks; optional open/completed status, source and since filters. Missing owner remains null. |
| `status <subject>` | Cited recent material, effective actions/decisions, blockers, relationships, conflicts and evidence gaps. No default date window. |
| `record-state` | Current explicit task/decision replacement audit; bounded display and opt-in quotes. |
| `evidence <record-id>` | Exact supporting anchor and citation. |
| `source-range <anchor-id>` | Immutable quote, offsets/hash, location, revision and current-state indicator. |
| `source-context <anchor-id>` | Selected range, nullable section heading, source-ordered anchors, total and truncation. |
| `revisions <source-path>` | Unique immutable content revisions in original ingestion order. |
| `compare-revisions <source-path>` | Added/removed/modified/unchanged ranges; optional `--from` and `--to`. |

Python additionally exposes direct decisions, blockers, conflicts, and relationship
reads. Structured output leaves unavailable fields null rather than guessing.

### Explicit record replacement

Tasks and decisions may declare a corpus-scoped, case-sensitive
`[key:: version-key]` and `[supersedes:: earlier-version-key]`. Keys use 1..128
ASCII letters, digits, dots, underscores, colons, or hyphens, starting with a
letter/digit. A replacement is a complete same-kind record, not a field patch.

Unique, non-cyclic, uncontested references suppress predecessors from effective
actions/decisions/status and inherit subject scope. Resolution uses active
current sources independent of ingestion order. Filters apply to effective
records, not historical as-of state. Search and status recent material retain
superseded evidence. Prose and latest timestamps do not choose winners.

Missing/duplicate keys, cross-kind links, self-links, cycles and competing
updates produce diagnostics, not arbitrary suppression. Syntax errors fail a
source savepoint. Unresolved references leave evidence indexed, add ingestion
`state_warnings`, and appear in status evidence gaps. Removing/editing an update
removes its former effect. Record-state and ingestion diagnostics use the actual
resolver and report support, candidates, effective and suppressed IDs.

### Context and history

Context starts at the nearest containing heading and includes subsections until
the next sibling/ancestor heading. Repeated names do not merge sections;
preambles and headingless documents are handled separately. The selected anchor
is always included in a centered window (default 50, `--max-anchors` 1..200).
The heading is returned separately, even outside that window. Each range retains
its own offsets, hash and revision. These are parsed blocks, not reconstructed
full sections: code can be absent and nested list anchors can overlap.

Historical context reads stored anchors, never current file bytes. Revision
comparison defaults to the target's latest activation predecessor, so restores
compare against the state before restoration, not original creation. Upgraded
databases without earlier activation history require explicit revision IDs;
unknown transition order is not invented.

## CLI versions, errors, and compatibility

`kg capabilities --format json` advertises **interface_version `"2"`** and search
configuration, score, readiness, explanation, and unsupported-option guidance.
Search explanation report version is `"2"`; ingestion report version is `"1"`.

`--query-mode` is unsupported. Every value, **including `reranked`**, fails
with guidance (`invalid_query` for JSON). Omit the flag and prepare the matching
projection. It is not an ignored alias or a hidden lexical route.

Domain errors are emitted on stderr as `{"error": "...", "message": "..."}` with
exit code 2 and no success payload. Dense readiness maps to
`dense_index_unavailable`; reranker failures to `reranker_unavailable`; ordinary
`SearchStateChangedError` and changed-state explanations to `search_state_changed`.
Other `SearchExplanationError.code` values survive. Invalid search semantics or
configuration use `invalid_query`; manifest errors use `invalid_manifest`.
Typer's option grammar/range errors retain its usage-error format, even when
JSON was requested. `dense-index` failures retain `dense_index_failed`.
Evidence/context/history lookups use operation-specific not-found errors;
invalid revision selections use `invalid_revision_comparison`. Invalid task status
uses `invalid_status`; malformed time windows on structured commands use
`invalid_since`. Failed ingestion sources produce a report and exit code 1.

`RetrievalService.search()` (strict/natural lexical), lexical
`SearchExplanation`, `DenseRetrievalService`, `HybridRetrievalService`, and
`RerankedRetrievalService` remain low-level Python composition/evaluation APIs.
They are not alternate public product modes and do not inherit stricter product
readiness or concurrency semantics. Lexical explanations use version 1.

## Storage, maintenance, and validation

### Foundation value contracts (validation only)

`kg.models.foundation` implements strict `foundation/1` request/result values for
supplied content, evidence, document writes, atomic enrichment descriptions,
batch correlation, synchronization/metadata snapshots and dependent query plans.
`model_validate_json()` checks types, bounds, exact code-point source slices,
request-local references, supporting-document dependency coverage and declared
scope. `model_dump_json()` round-trips these immutable values without normalizing
source text. `BatchResult.validate_for()` and `QueryResult.validate_for()` check
request/result correlation. `FoundationCapabilities` explicitly says
`validation_only`; it does not change `kg capabilities` or product interface 2.

These models are **not** callable ingestion/query services or enforcement of
database integrity, authorization, atomicity, idempotency or read isolation.
An access context is trusted-boundary input, not proof of permission. The canonical
Markdown schema/CLI do not consume these values. The separate `kg.evidence`
service consumes document envelopes and enforces the guarantees described above.
The implemented value rules are in [CONTRACTS.md](CONTRACTS.md).
Representative contract fixtures and the synthetic workload/budget protocol
exercise validation and supply evaluation inputs, not service integration results.

### Existing canonical storage

[`src/kg/schema.sql`](src/kg/schema.sql) owns rebuildable canonical SQLite tables
for source documents/revisions/anchors/activations, entities/aliases/mentions,
relationships, structured records/bindings, passages, lexical projections, and
ingest summaries. Dense projections are independently disposable.

After upgrades, reingest each corpus and rebuild matching dense projections.
Pre-alpha schema changes may require a fresh database. Preserve the old database
when historical evidence is needed: rebuilding from current sources cannot
recreate past content or activation history.

Reviewed [acceptance corpora](corpora/acceptance/) and incremental Atlas scenarios
pin exact evidence, graph scope, abstention, isolation, ingestion, edits/restores,
and history. Lexical expected lists remain component assertions, not semantic
gold. Separate product tests cover the entire pipeline and CLI with controlled
providers; routine tests do not download models.

The [QASPER evaluation](benchmarks/qasper/README.md) measures component retrieval
and answerability; current relevance and answerability gates remain unmet.
The [real-model product search matrix](benchmarks/productization/README.md)
checks integration parity, not relevance or production readiness.
See [CONTRIBUTING.md](CONTRIBUTING.md) for validation commands.
