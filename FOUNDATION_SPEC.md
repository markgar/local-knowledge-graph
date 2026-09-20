# Foundation specification: F0 and V0

Status: draft for review; proposed contracts, not implemented APIs.
Recorded: 2026-09-20.
Parent: [implementation plan](IMPLEMENTATION_PLAN.md).

## Objective and boundary

Make the evidence, knowledge, and query lanes safe to implement independently.
F0 establishes the smallest shared contract surface and executable contract
checks. V0 defines the acceptance scenarios and evaluation protocol that later
packages must satisfy. Neither package delivers the complete ingestion engine,
graph store, processing scheduler, query planner, or connectors.

[SPEC.md](SPEC.md) remains the authority for implemented behavior. This document
does not change existing commands, identities, JSON reports, source fixtures, or
reviewed gold. The full [roadmap](ROADMAP.md) remains in scope.

The ownership, contract-gating, separate acceptance milestones, and early-budget
changes agreed during plan review are reflected here. Concrete contract choices
below are recommendations for review unless marked as a selected starting
direction. Neither status authorizes changes to existing public behavior or
access to live sources.

## Deliverables and exit gate

| Deliverable | Required evidence before the first parallel wave |
| --- | --- |
| Contract decisions | Agreed initial identity, offset, ownership, write, knowledge, scope, and query semantics; no unresolved decision that forces E1, K1, or Q1 to invent a shared boundary. |
| Executable contracts | Versioned request/result models, validation rules, serialization examples, and positive/negative tests following the existing Pydantic patterns. No empty service framework. |
| Compatibility design | Mapping from existing IDs and public results; legacy-content handling; schema migration ownership and preservation cases. |
| Acceptance inventory | V0 cases below with deterministic inputs, expected outcomes, owning packages, and an explicit distinction between contract checks and future integration checks. |
| Evaluation protocol | Agreed initial workloads and numeric operating/quality budgets, recorded before implementation fans out; measurement method and retained historical gates. |
| Handoff | One owner for shared contracts/schema, bounded E1/K1/Q1 work items, and a record of deferred decisions with owners and deadlines. |

Writing this document satisfies none of the executable gates. F0 can deliver
model validation and serialization tests without implementing persistence.
V0 must not introduce permanently skipped tests or passing mocks that claim to
prove future storage, concurrency, permission, or recovery behavior.

## Proposed shared contracts

Names in this section describe semantic fields, not a finalized Python API.
The F0 implementation should use a cohesive contract module under `src/kg/models/`
and the repository's existing test conventions. Do not expose `_prepared.py` or
`_writer.py` as public input contracts.

### 1. Scope, identity, and ownership

Every operation carries a corpus scope and an effective access context.
Caller-supplied writer/plugin labels provide attribution, not authorization.
The trusted local invocation boundary establishes which source namespaces and
operations that caller may use; possession of an object ID grants no access.
An access context is not a claim that a local CLI protects against an operating
system user who can directly read the database files.

| Concept | Proposed semantics |
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
Define the deterministic active-entity rule before K1 begins.

Document synchronization is similarly confined to an explicitly declared source
namespace and synchronization scope. A complete snapshot may deactivate absent
documents only after successful completion. A partial page, failed enumeration,
or interrupted run is not proof of absence. Incremental synchronization uses
explicit changes/removals; absence from a batch never implies deletion.
Concurrent synchronization runs need a generation/precondition check before an
older run can deactivate documents discovered by a newer run.

The current Markdown importer deactivates unselected documents across a corpus
and deletes corpus-wide aliases. E1/K1 coexistence and E2 adaptation must remove
that cross-writer authority, not merely add owner labels. If legacy documents
cannot be assigned an unambiguous owner, require explicit migration mapping
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

Source title, location, event time, and metadata have explicit historical versus
current semantics. Updating them without changing text must not mutate prior
citations or bypass state-version checks. F0 must specify their snapshot storage
and versioning independently of the content revision; do not overload a content
hash with permissions or processing status.

New revisions retain complete supplied text. Legacy revisions without full text
return an explicit content-unavailable state while retaining their valid anchors,
quotes, and history. Do not concatenate anchors into invented full content.
Backfill is permitted only from verified matching source content.

Passage processing identifies its policy/version separately from the immutable
source revision. Reprocessing may replace the active passage projection, but
must not repoint old evidence IDs to different text. E3 owns the processing
implementation; F0 establishes evidence identity and reference validity.

### 3. Writes, retries, and transaction boundaries

| Field or rule | Proposed semantics |
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
sanitize retry responses; a retry must not resurrect purged content. F0 records
retry retention and post-expiry semantics before claiming durable retry behavior.

Failures expose typed codes and bounded diagnostics, not source text or secrets
in operational logs. Invalid evidence or an unsupported operation must not become
a successful empty result. Size and batch limits must be explicit in the contract
and capability description; their numeric values are an F0 decision.

#### Selected starting direction: bounded enrichment change sets

Following the rubber-duck review, start with one atomic change set that can
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
document ingestion, indexing, or cross-corpus writes inside the set. Numeric
limits on operations, evidence references, and payload size remain F0 decisions.
How multiple citations jointly or independently support an assertion remains a
separate foundation decision; atomicity does not resolve that question.

This starting direction avoids making agents orchestrate partial entity/assertion
writes. Revisit it if executable examples show excessive complexity. Before a
public release it can be revised with coordinated contract changes; after clients
depend on it, preserve compatibility or introduce an explicit contract version.

### 4. Knowledge contributions

The initial schema must distinguish entities, external identifiers, aliases,
passage mentions, and typed assertions/relationships. Co-mention does not create
a factual relationship. Similar names or candidate scores do not merge entities.

An assertion records its subject, predicate/type, typed object/value, evidence
references, explicit-source versus inferred interpretation, contribution owner,
and attribution. F0 must choose the initial supported value types and predicate
validation rules; arbitrary unvalidated JSON is not the extensibility mechanism.

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

## V0 acceptance inventory

All cases below are planned. F0 checks shapes and validation where possible;
owning packages must later demonstrate actual integrated behavior.

| ID | Setup and required outcome | Integration owners |
| --- | --- | --- |
| A01 | Submit identical external IDs in different corpora/namespaces; identities remain isolated. Retry within one namespace produces no duplicate document. | E1 |
| A02 | Supply CRLF, combining characters, and non-BMP text; full content and exact ranges round-trip. Wrong quotes/out-of-range references reject without writes. | E1, E3 |
| A03 | Migrate legacy evidence with missing full content; IDs/quotes/history survive and content reads explicitly report unavailable. Verified backfill preserves identity. | E1, E2 |
| A04 | Co-locate Markdown, synthetic email, seed and agent contributions. Reingest Markdown, remove a seed and omit an email document; unrelated documents/entities/aliases survive. | E1, E2, K1 |
| A05 | Interrupt or fail snapshot enumeration and interleave old/new synchronization runs; only a successful authorized current snapshot can deactivate its own absent documents. | E1, E2, E4 |
| A06 | Retry after commit but before response; no duplicate writes. Reuse a key with changed payload, and race two expected-version updates; conflicts have no partial mutation. | E1, K1, E4 |
| A07 | Mix valid and invalid independent batch items; every item has one ordered outcome, failed units leave no rows, successful units remain durable. | E1, K1 |
| A08 | Edit, deactivate and restore the same content, then submit old enrichment; state-version checks reject it even when the content revision matches again. | E1, K4, K5 |
| A09 | Submit same-named entities, mentions, and competing explicit/inferred assertions; no silent identity merge or factual edge, and attribution/evidence remains inspectable. | K1, K2, K3, K4 |
| A10 | Exercise denied evidence/history, alias lookup, graph paths, counts, diagnostics and resumed results; none disclose unauthorized contributions or totals. | X1, K2, Q1, Q2, Q4 |
| A11 | Use more matching tasks than the search limit; dependent resolve/filter/count returns the full eligible count and inspectable support, not top-k cardinality. Ambiguity prevents guessing. | Q1, Q2, Q3 |
| A12 | Commit text before enrichment; indexing becomes ready independently. Interrupt jobs and change source state; retries converge and stale jobs cannot mark current work complete. | E3, E4, K5 |
| A13 | Page through tied results; no duplicates/skips within the supported result set. Exercise expiry, data changes, access revocation, and bounded-pool exhaustion explicitly. | Q4, X1 |
| A14 | Purge content while jobs/cursors/retry records exist, then retry/restart; managed stores cannot return or resurrect purged content and failures remain recoverable. | X1, E4, K5, Q4 |
| A15 | Execute existing Markdown, structured-read, citation/history and product-search workflows; preserve their contracts and authored component gold. | E2, Q1, R1 |
| A16 | Metadata-only and access-only updates retain historical citation context, change relevant state tokens, and cannot leave a stale projection falsely ready. | E1, E3, X1 |
| A17 | Create entities and an assertion in one change set using request-local handles and evidence from two documents. Verify returned IDs and identical retry results. Change either document or submit an invalid local reference before commit; the entire set leaves no new entities, aliases, or assertions. | E1, K1, K2 |

## Evaluation setup and budgets

V0 proposes a reproducible synthetic workload before real-source selection:
two corpora, at least two writer namespaces in one corpus, 1,000 documents
covering small notes and multi-passage text, and at least 200 matching tasks for
a query with a limit of 20. Include Unicode/newline fixtures, repeated content,
ambiguous identities, denied sources, updates/restores, and failures. Record
exact fixture sizes, generator seed/version, and operation mix.

Correctness gates are exact: no cross-scope disclosure in authored cases,
no duplicate committed writes on retries, exact quotes/counts, and no lost or
repointed historical citations outside explicit purge. Existing relevance and
answerability thresholds remain unchanged and separately reported.

Before the first wave, agree numeric targets for intake throughput, structured
and graph p95 latency, cold/warm search latency, peak memory, disk growth,
recovery time, and planner/agent time, token and cost limits. Each budget needs
hardware/runtime/model identity, workload, measurement method, owner, and the
package where it becomes enforceable. Do not invent measured baselines.

Numeric performance targets remain an explicit open F0/V0 gate in this draft,
not a decision deferred until release. Capability-specific real-model and
approved real-source workloads supplement the synthetic workload as they become
available. Record misses incrementally and review any budget revision; do not
lower a threshold retrospectively to label a failing run accepted.

## Acceptance milestones and implementation handoff

**Foundation accepted:** the F0/V0 exit evidence above exists and blocking
decisions below are resolved. Independent E1/K1/Q1 work can then start. Contract
acceptance is not proof of implemented storage or execution behavior.

**Core accepted:** E/K/Q, X1, I1/I2, and applicable V1/R1 gates pass on integrated
code, including an independently packaged synthetic adapter and actual
agent integration. This milestone does not require all three live connectors
and does not establish production readiness or change known quality failures.

**Live workflows accepted:** S1/S2/S3 each have approved provider/access/retention
decisions and live synchronization/enrichment/query evidence. R1's final full-scope
assessment remains open until these and all remaining gates are assessed.

F0 should be one bounded contract-and-test change, split only if review size
requires it. V0 supplies fixtures/scenarios and the initial budget record, not
implementations of later systems. Do not launch implementation lanes merely
because this draft has been written.

| Decision still requiring agreement | Deadline |
| --- | --- |
| Exact field names/types, version negotiation and initial limits; initial query operation/result vocabulary | F0 exit |
| Namespace registration, legacy owner mapping, active-entity contribution rule, alias/identifier ownership | F0 exit |
| State-version and metadata-history representation; retry-key lifetime and expiry/purge behavior | F0 exit |
| Initial assertion value/predicate schema and compatibility mapping for existing explicit records | F0 exit |
| Joint versus independent evidence support, including current/visible eligibility after one supporting source changes or becomes inaccessible | F0 exit |
| Consistency boundary across dependent steps of one query execution, independently of later cursor implementation | F0 exit |
| Initial synthetic workload and numeric evaluation budgets; shared contract/schema and evaluation owners | F0/V0 exit, before parallel implementation |
| Concrete planner and ingestion-agent host | Q3 and I2 respectively |
| Cursor storage, expiry and data-change policy within the agreed scope/result contract | Q4 |
| Actual providers, source permissions, retention duration, backup/purge policy, or hosted model egress | Before the relevant live integration; never inferred from fixtures |

Resolve first-wave decisions by review of this draft and the executable
contracts. Keep later decisions deferred with explicit owners; they must not
silently become defaults in an implementation session.
