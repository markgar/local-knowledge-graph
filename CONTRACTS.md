# Service and value contract reference

The canonical Python services operate on SQLite `evidence-store/3`. SQLite owns
supplied text/revisions, identities, knowledge schema, entities/assertions, exact
support and history. Optional Ladybug is a rebuildable exact-scope graph
projection, not a second authored store or a replacement for canonical search.
The separate Markdown demonstration/CLI uses its own database and APIs; see
[the architecture](SPEC.md#architecture-and-data-ownership).

This reference covers executable evidence, indexing/search, knowledge, processing
control, query and diagnostic APIs. Each section states its supported operations
and limits; a value's presence in a model does not establish an executable service.

`kg.models.foundation` implements **validation and serialization only**, using
Pydantic models with contract version `foundation/1`. It does not persist data,
enforce ACLs or ownership, execute writes or queries, synchronize sources, process
content, implement retries, or retain snapshots/continuations. Declared grants,
state tokens, receipts and result-set IDs are values, not proof of those services.

`kg.evidence` executes document writes, bounded enrichment and evidence reads;
`kg.query` executes its supported canonical plans using foundation values. Only
the service operations explicitly listed below are executable; broader foundation
query/synchronization descriptions remain validation contracts.

See [the models](src/kg/models/foundation.py) for exact fields and defaults,
[focused tests](tests/test_foundation_contracts.py) for executable checks, and
[SPEC.md](SPEC.md) for implemented product behavior. Future service obligations
and delivery planning are tracked in
[open bounded issues](https://github.com/markgar/local-knowledge-graph/issues?q=is%3Aissue%20is%3Aopen),
not here. Follow each owning issue's current scope and linked approved requirements.

## Evidence service API

`kg.evidence` exports `EvidenceDatabase(Path)`, `EvidenceAdministration(database,
LocalAdminAuthority)`, `EvidenceService(database, LocalIdentity)` and
`EvidenceServiceError`. Administrative/read values live in `kg.models.evidence`;
top-level service values carry `interface_version="evidence/2"`. Existing
`foundation/1` document write/result shapes are unchanged. The pre-release
enrichment values include independent entity support and explicitly namespaced seeds.

| API | Result / behavior |
| --- | --- |
| `database.initialize()` | Initialize empty or verify the complete `evidence-store/3` schema and manifest; incompatible targets raise `unsupported` with recreate/reload guidance. Use a fresh file and resupply sources, policy/schema and explicit knowledge; no migration or automatic reset. |
| `admin.register(CorpusRegistration)` | Register namespaces, writer bindings and explicit `LocalPolicy`; identical original registration is unchanged and returns the **current** policy version, without restoring old grants. Conflicting registration fails. |
| `admin.replace_policy(LocalPolicy, expected_policy_version)` | Atomic policy/state rotation; returns version, affected namespaces and changed-document count. |
| `service.write(WriteRequest)` | `put_document` / `remove_document` / bounded `enrich` / `withdraw_assertion` -> `WriteOutcome`. Enrichment supports the change kinds described below, including explicitly passage-backed mentions. |
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

All reads need current trusted `read` authority. Document writes need `write_documents`
and an owner/writer/synchronization binding; they do not confer full-text access.
Enrichment instead needs `read`, `write_knowledge` and exact knowledge bindings,
plus `seed` for seed-backed contributions; it never requires `write_documents`.
Attribution is not a credential. Namespace policy changes rotate affected document
states independently of corpus-wide access-context freshness.

`LocalPolicy.knowledge_bindings` defaults to `()` and contains strict
`KnowledgeWriterBinding(namespace, principal_id, owner_id, writer_id)` values.
Registration, canonical policy equality, persistence and replacement include these
bindings. Changing one rotates only the affected namespace's token/document states,
as well as corpus policy freshness. A binding does not grant knowledge/seed access
by itself.

The pre-release `evidence/2` representation replaces `processing_reason` with
separate indexing and enrichment reasons; fresh states report
`processor_not_available`. Current default indexing readiness reflects the actual
projection and source/namespace state; historical completion remains historical.
There is no compatibility alias or migration from
older physical formats, including `evidence-store/2`. Reserved schema tables alone do not enable services.
The operations below, not table presence, determine runtime capabilities. The separate
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
passage-production, indexing/search or purge API in this service.

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

## Standalone indexing API

`kg.indexing.IndexService(database, identity)` executes the `indexing/1` standalone
lifecycle on the canonical evidence store. It does not execute processing claims,
acknowledge jobs or interpret/enrich knowledge. Full search is exposed separately
by `EvidenceSearchService`. The existing
typed coordinated participant contract is retained but its entry point fails
`unsupported`: no fake participant constitutes E4 integration.

| Call | Executed behavior |
| --- | --- |
| `process(scope, attribution, document_id, expected_state, configuration=DEFAULT_CONFIGURATION, *, mode="incremental")` | Requires read and exact bound document-writer authority. Admits a monotonic attempt fence, publishes immutable passages, initializes the pinned embedding provider, stages exact quote lexical input and normalized vectors, then atomically publishes a complete projection. `rebuild` recomputes every vector. |
| `status(scope, document_id, configuration=DEFAULT_CONFIGURATION)` | Verifies current source, policy, configuration, immutable set, projection membership, representation/vector hashes, dimensions and finite normalization. Reports pending/ready/failed/inactive plus latest attempt independently; never loads a provider. |
| `pending(scope, configuration=DEFAULT_CONFIGURATION, *, after_document_id=None, limit=100)` | Bounded keyset scan of up to 1..200 active scoped documents. Returns incomplete entries; ready documents can make a page empty. Follow the returned cursor while `has_more`, not the last returned entry. No retained snapshot across pages. |
| `cleanup(scope, attribution, document_id, configuration=DEFAULT_CONFIGURATION, *, limit=1000)` | Reauthorizes read/writer ownership, retires provably stale attempts, and removes at most 1..1000 obsolete derived rows. Preserves canonical evidence, active projection and live staging. Reports `removed` and `has_more`. |

Each call has a named `_explained` wrapper with keyword `options=ExplainOptions()`.
Ordinary calls also retain bounded actual-execution summaries when admitted by the
shared collector, available through `service.diagnostics`. Detailed events report
only observed phases and reuse/publication facts; no additional inference runs.
Source text is absent from these lifecycle reports, including detailed reports.

`IndexConfiguration` defaults to pinned `gte-modernbert`, noncontextual exact
quotes, `generic-lexical/1` and `generic-dense-dot/1`. The alternative profile is
`qwen3-embedding-0.6b`. Contextual configuration requires both `contextual=True`
and `representation="generic-title-quote/1"`; its exact input is title, two LF
characters, then untouched quote. Lexical input is always the exact quote.
The full descriptor determines `configuration_id`; initialized runtime/device/dtype
and encoding identity are separately retained. Unknown or forged configurations
are rejected, not silently downgraded.

`ProcessResult` distinguishes `ready`, `unchanged`, `stale` and `failed`, with exact
state/configuration/attempt IDs, successful set/projection IDs, observed produced/
reused counts, safe failure reason and bounded-cleanup status. Provider failure
can coexist with an older valid complete projection; status reports that projection
ready and the later failed attempt separately. Incremental unchanged still loads
the provider and verifies its actual identity and complete stored projection.
Wrong count/dimension, nonfinite or zero-norm vectors never become ready.

Only the default configuration updates the evidence default indexing summary.
Alternate configurations do not mark that summary ready; enrichment remains
independent. Saved evidence receipts remain original write-time observations.
Rebuilds and cleanup do not rotate source states, passage identities or knowledge
dependencies. Canonical passages committed before provider failure remain readable.

Every invocation shares one private 30-second deadline and the existing 100,000
visit, 10-million SQL instruction and 64-MiB logical scratch allowances. No stage
gets a fresh pool. Provider batches use at most eight sequences (below the
32-passage process ceiling), at most 8,192 conservatively preflighted padded
UTF-8-byte positions including special-token allowance, and 16 MiB provider scratch.
An oversized single representation fails `budget_exceeded`; it is not cropped,
split into different model inputs or silently skipped. Host model memory is extra.
Native provider calls are not preemptible; late output is discarded on return.
Unexpected errors propagate after rollback; failure-status persistence errors
also propagate rather than pretending a failure was durably recorded.

Routine controlled-provider tests establish lifecycle behavior, not real-model
quality, memory or workload acceptance. See [the Python example](examples/indexing_lifecycle.py)
for an actual provider invocation using an existing trusted supplied document.

## Canonical full search

`kg.indexing.EvidenceSearchService(database, identity)` exposes
`search(scope, query, configuration=DEFAULT_CONFIGURATION, *, limit=20)` and
`search_explained(..., options=ExplainOptions())`. Query is a nonblank, valid
Unicode string of at most 4,096 code points with at least one FTS-tokenizable
natural token; limit is an integer 1..100. Both configured local providers must
initialize, including empty scopes. No keyword-only, partial-ranked or stale-index
fallback exists.

`CanonicalSearchResult` (`indexing/1`) contains the effective scope, ordered
`CanonicalSearchHit` values, logical and actual embedding identities, actual
reranker pipeline identity, SQLite/Python/Unicode versions, snapshot read witness
and projection IDs. Each hit contains the full exact `EvidenceView` (including
immutable stored citation and offsets) and finite raw reranker score. Scores are
not confidence. Candidate limits and stream/fusion truncation flags describe
bounded ranking pools, not whole-corpus exhaustion. Standalone `items_consumed`
is acknowledged semantic work, not the number of hits.

Missing, stale or differently initialized current projections raise
`EvidenceServiceError("stale_index")`; unsupported/provider-unavailable execution
raises `unsupported`; malformed provider output fails explicitly. Changed
source/policy/projection or any other canonical commit yields `state_changed`
(authorization failures can be `forbidden`), never ranked data. Invalid or
tokenless queries yield `invalid_request`. Public/private/provider/FILE TEMP or
deadline exhaustion yields `budget_exceeded`, with no private meter disclosure.
Valid existing projections and historical citations are never mutated by search.

Standalone allowances are 10,000 semantic units and one shared private 30-second
deadline, 100,000 visits, 10-million SQL instructions and 64-MiB logical scratch.
The existing verified FILE TEMP cap is 128 MiB. Provider inputs are preflighted
before inference; each reranker call is one sequence, at most 8,192 conservative
padded token positions and 8 MiB scratch, with no input cropping or omission.
Native provider calls are not preemptible in standalone mode; late results are
discarded on return. These are logical bounds, not an RSS/model-cache guarantee.

The private `_search_in_context` entry borrows a live `CanonicalReadContext` and
capture, returns `SearchSelection`, and does not release the result. Its response
has `items_consumed=None`: composed accounting remains the caller's meter. It
reserves only `search_temp`, `search_lexical`, `search_vector`, `search_rerank`,
and `search_final_evidence`; final hydration uses the shared authorized resolver
without an extra `evidence_reference`. Repeated calls inherit all remaining
allowances. The owner must discard the snapshot on failure and perform original-
observer/fresh-authorization release before disclosing successful results.
`_begin_child_capture` stages a report in the parent's existing disclosure group;
the parent owns finishing/release or irreversible redaction. `QueryService` uses
the borrowed search seam in its spawned worker, with Q1-owned bounded capture
transport rather than transferring a live observer across processes.

The shared diagnostic facade retains bounded ordinary summaries and optional
detailed actual candidate memberships/scores/ranks. Explained search executes
once; quote opt-in does not affect business hit quotes. Private/no-data failures
withhold stage, projection, candidate and counter detail across later discovery.
The [supplied-document example](examples/canonical_search.py) runs actual intake,
indexing and search; its controlled-provider test is not real-model acceptance.

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

Registration itself creates no entities/assertions or decisions. The separate
enrichment and read operations below consume this immutable definition.
See the executable
[schema example](examples/knowledge_schema.py).

## Knowledge enrichment and reads

`EvidenceService.write` accepts a bounded `ChangeSet` atomically: `CreateEntity`,
`AddEntitySupport`, `AddAlias`, `AddIdentifier`, `AddMention`, and `AddAssertion`.
References must be actual canonical anchors or E3-produced passages with exact
current document dependencies. E3's common resolver validates the full immutable
source/state/set/passage/anchor chain on the owner's authorized write transaction.
`AddMention` requires passage evidence for every reference; it creates no factual
edge or identity merge. Every input change has one input-ordered mapping. CreateEntity
maps an entity ID; all other changes map contribution IDs. There is no inference,
identity merging, automatic deduplication or document-readiness update.

An immutable string predicate registered with `direct-subject-decision/1` accepts
only explicit string assertions. Its assertion ID is the submitted record ID.
Fresh retry keys produce distinct records even for identical text/support;
same-key retries return the original complete receipt. Ordinary predicates cannot
be relabeled as decisions at read time.

Entity support, aliases and identifiers may instead use namespaced `SeedSupport`.
An exact active slot/definition/attribution repeat is unchanged; changed definitions
conflict. A membership-changing unit advances each affected set's generation once.
Each owner/writer/set permits at most 100 active slots. Omitting a slot never
withdraws it. Whole-set replacement is not installed.

`kg.knowledge.KnowledgeService(database, LocalIdentity)` exposes:

| Method | Result |
| --- | --- |
| `capabilities(scope)` | Authorized `KnowledgeCapabilities`: installed change kinds, support boundary and registered decision encoding. |
| `entity(scope, entity_id, *, mode="current")` | `EntityView`: immutable identity, eligibility, a sequence-selected visible support witness and visible-only more-support flag. |
| `entities(scope, *, name=None, scheme=None, identifier=None, after_sequence=0, limit=100)` | Current exact identity/name/eligible-alias or scheme+identifier selection. At most one selector; no normalization or fuzzy matching. |
| `contribution(scope, contribution_id, *, mode="current")` | `ContributionView`: typed resolved payload, full attribution, schema version, captured evidence and endpoint witnesses. |
| `contributions(scope, entity_id, *, kind=None, mode="current", after_sequence=0, limit=100)` | Attached contributions and incoming/outgoing entity-object assertions, once each in sequence order. |

Modes are `current` and `history`. Pages use limits 1..200, nonnegative keyset
cursors, visible-only `has_more`/`next_after_sequence`, and no hidden totals.
All methods have named `_explained` wrappers; ordinary calls retain scoped summaries.
Trusted schema provisioning remains excluded from scoped reports.

`EvidenceService.write` also accepts `WithdrawAssertion` for an exact caller-owned
assertion ID. Current `read`/`write_knowledge`, exact original owner AND writer
binding in every original assertion namespace, and historical readability of all
support/endpoints are required. Missing/cross-corpus/hidden targets return
`not_found`; visible non-assertions return `invalid_request`; visible wrong
ownership/binding returns `forbidden`. A readable stale assertion remains eligible
for withdrawal. This does not retire entities or retract aliases/support/mentions.

First withdrawal returns `applied` and `AssertionWithdrawalReceipt`; identical
same-key replay returns the original status/event, while a fresh key returns
`unchanged` with that same event. Ordinary receipt expiry/authorization and
independent ordered batch semantics apply; private coordinated withdrawal is
`unsupported`. Corrections are separate enrichment writes, not replacements.

`KnowledgeCapabilities.withdrawal` is `"owned_assertion"`; general `"retraction"`
remains unsupported. `ContributionView.withdrawal` is null unless a first immutable
withdrawal exists, then contains its `withdrawal_id`, `committed_at` and `attribution`.
Authorized history preserves the original payload/provenance/support, with
`is_current=false`; current reads/pages, direct decision counts and graph export
exclude the assertion. Historical evidence and source search remain available.
Graph sessions use the same write path and require refreshed eligible projection
after writes; successful canonical receipts survive refresh failure.

Source support is conjunctive. Any stale supporting state makes the contribution
ineligible. Aliases/identifiers/mentions/assertions cannot activate an unsupported entity.
History still requires current access to every support namespace and a historically
visible creation basis for each endpoint. Missing/inaccessible direct IDs return
`not_found`. New AddEntitySupport may reactivate an historically visible identity
and supply another change's endpoint within the same atomic unit, including forward
references. It never revives an old stale assertion.

`KnowledgeCapabilities.support` is `anchors_passages_and_seed_add`. Supported
variants are explicit rather than inferred from validation-only foundation values:

| Change | Anchor support | Passage support | Seed ADD support |
| --- | --- | --- | --- |
| `entity`, `entity_support`, `alias`, `identifier` | Yes | Yes | Yes |
| `assertion` | Yes | Yes | No |
| `mention` | No | Yes (all references) | No |

Anchor and passage references may be mixed conjunctively except in mentions.
Current/history contribution pages include mentions and accept `kind="mention"`.
Captured evidence retains exact passage IDs, state and metadata provenance; quotes
resolve from the immutable input, not regenerated text. Passage publication is
independent of vector readiness. Vector-only rebuild/reclamation does not stale
knowledge, whereas source, metadata, boundaries or policy A-to-B-to-A transitions
never reactivate an old contribution. Independent current creation support may
keep an entity eligible, but cannot repair another contribution's stale evidence.

The private `kg.knowledge._reader.KnowledgeReader(context, identity)` implements
the existing snapshot selection protocol: `resolve_entity(EntitySelector)`,
`select_decisions(subject_id)`, cursor `read(limit=200)`/`close()`, and exact
`revalidate_member(member)`. Decision pages order by assertion ID (BINARY), retain
complete assertion support and the subject witness selected by contribution
sequence, and distinguish EOF, public budget stop, private/deadline stop and typed
failure. It uses the owner's live context and inherited meter, not another
snapshot. Nested support is privately accounted, never charged as a public
evidence request. This private reader is consumed by `QueryService` for the public
records/count behavior documented below; it is not a separate public query facade.

Each interactive selection retains one 10,000-visit view across pages and nested SQL, within
the existing 100,000-visit root, VM/scratch pool and original deadline. Standalone
read admission/release uses the root; its selection uses the 10,000-visit view.
Standalone calls use a 30-second deadline. Resource failures return no read data
and roll back write units. Receipts use the existing shared key/clock ledger,
expiry-before-digest and full retained-target authorization. A coordinated call
cannot adopt an unlinked ordinary knowledge receipt. Actual E4 lifecycle acceptance
is not claimed by the optional participant seam.

The private bulk-read operation (`kg._execution_budget._graph_build_operation`)
does not change those public defaults. It carries one original finite deadline
(at most 300 seconds remaining), cancellation Event, budget and unchanged
`StepMeter.reserve_public(stage, n)` ABI. Its `graph-build/1` root and automatic
selection children count visits/VM cumulatively without interactive total-work
ceilings; explicit finite child caps still apply. Pages remain at most 200,
scratch remains 64 MiB with existing per-unit limits, and SQLite TEMP remains
128 MiB. Owners must reach actual EOF; a stop cannot authorize a complete prefix.
Terminal cancellation/deadline/resource state and cumulative accounting remain
inspectable through the original operation's private snapshot. Existing Q1
models, public budgets, worker RPC and public diagnostic shapes are unchanged.
This is internal control plumbing, not a public foundation graph-query API or
a hard native-memory/disk/deadline guarantee.

## Optional private graph projection

`kg.graph._build.build_graph` exports complete eligible entities, entity-object
relationships and explicit `direct-subject-decision/1` assertions for one exact
identity/scope. It builds, verifies, checkpoints and reopens a disposable Ladybug
stage using the original canonical source observer. Authored IDs and complete
selected source/seed/endpoint proofs remain tied to authoritative SQLite.

The [developer example](examples/graph_build.py) executes native queries and
parity checks. `kg.graph.LocalGraphSession` supplies reusable local lifecycle,
guarded private reads and controlled writes; it is not a public business
traversal/join facade or a new `QueryService` path operator. Base-package search works without
the optional pinned `ladybug==0.20.4` dependency. Native support is macOS 15+ ARM64
with CPython 3.12. The 256 MiB native buffer/two threads do not provide total RSS
or process isolation; fatal native failures can terminate the host.
See [runtime/example guidance](README.md#optional-disposable-graph-example) and
[exact ownership, freshness and failure semantics](SPEC.md#private-disposable-graph-staging).

The optional `kg.graph.LocalGraphSession` lifecycle uses that bulk operation only
for cold/build-capable reads and refresh. Warm reads and controlled canonical
writes use ordinary budgets; existing foundation/Q1 shapes and limits do not
change. The controller's frozen status/error/generation values are local
lifecycle values, not `foundation/1` result contracts or durable authority.
Graph batch writes return existing `BatchResult` values with per-unit E1 reports;
confirmed receipts are not replaced by a later graph-refresh failure.

<a id="canonical-anchor-queries"></a>

## Canonical evidence queries

`kg.query.QueryService(database, identity, *, search_configuration=DEFAULT_CONFIGURATION)`
executes validated `QueryRequest`
values against the canonical evidence store. `execute(request)` returns
`kg.models.query.QueryExecution` (`interface_version="query/1"`), containing
the unchanged foundation `QueryResult`, elapsed milliseconds, ordered step
telemetry, a safe stop reason and accounting label. `execute_explained(request,
options=ExplainOptions())` executes once and returns shared `Explained` with
`.outcome` and `.report`. Ordinary summaries are discoverable through
`service.diagnostics.report/recent/for_request`; request IDs correlate distinct
invocations, not durable retries.

Installed evidence capability is **anchor and published passage evidence**, including
generated anchors and authorized immutable history (`canonical-evidence/1`).
The result is one evidence Record identified by anchor ID,
with its full requested reference in SourceSupport. No quote text is added to
foundation results or Q1 reports. Detailed reports include the selected ID,
closure and acknowledged semantic reservation; summary reports omit selected IDs.
`capabilities(scope)` is authorized and reportable. Exact K1 entity resolution is
installed; decision records/counts are enabled only for a registered
`direct-subject-decision/1` predicate. Full canonical search is installed; paths
remain unsupported. Missing or mismatched passage references return
`not_found`. Passage evidence requires its real state/set/passage/anchor chain,
not vector readiness. Both `codepoint-window/1` and `supplied-anchors/1` kernel
outputs are readable.

A selected `SearchStep` invokes the actual `EvidenceSearchService` lexical/dense,
fusion/deduplication and reranking pipeline inside the query's spawned read context.
The trusted service configuration must match the prepared index and actual
initialized provider identity; both providers/readiness are required even when
empty. There is no legacy adapter, stage bypass or fallback. Success returns
foundation `RankedResult` with exact references and raw reranker scores, preserving
E3 order; exhaustion is `candidate_pool`, not exhaustive corpus enumeration.
Missing/mismatched projections yield `stale_index` with `index_not_ready`; provider
unavailability yields an unsupported no-data outcome with `provider_unavailable`.

The one-passage case matching both candidate streams reserves exactly five units:
TEMP population, lexical candidate, vector evaluation, rerank and final evidence.
E3 already hydrates the final evidence in the same snapshot: no sixth nested
`evidence_reference` reservation is added. Readiness and fusion add none.
Public exhaustion at any incomplete search stage is a redacted failure, never a
partial ranking. Private allowances and the original query deadline are inherited,
not replaced by the standalone search deadline.

The existing request vocabulary is unchanged. Search can be the selected output;
unrelated steps are pruned. It has no search-to-evidence dependency or count-over-
search operator. Returned references can be passed to a later literal EvidenceStep
or EvidenceService read, but those calls are separate observations. The
[controlled supplied-document example](examples/query_search.py) demonstrates this
distinction and exact quote/citation inspection without downloads or quality claims.

Search diagnostics retain actual worker-captured E3 configuration/stage/candidate
facts in a linked indexing child. The parent `query.nested_execution` event names
its report, retrievable through `service.diagnostics.report`; query discovery lists
parent calls. Detailed quote opt-in applies to that child, not foundation output.
Bounded capture transport is admitted against the original scratch pool before
collection; optional capture capacity failures withhold diagnostics without changing
ranking. If business scratch needs that capacity, the worker discards its capture
and acknowledges reclamation before the supervisor releases the optional reservation
and retries the same scratch request. Both reports remain provisional until the
parent's one fenced release.
Failure or subsequent observer invalidation permanently withholds both, including
after parent eviction. Reports and retained decision support have separate lifetimes.

`resolve` consumes K1's exact ID or case-sensitive name/approved-alias selector.
IDs never fall back to names; aliases do not multiply entities. Multiple eligible
IDs return ambiguity and stop dependent steps. Incomplete resolution cannot
establish uniqueness/absence for records/count, even with a single candidate.
Only direct-subject explicit string decisions are supported: action/status,
blocker/conflict and path semantics are not approximated. Unsupported required
semantics are checked even for an empty subject. A configured empty selection
counts exactly zero.

Each eligible assertion ID is a distinct submitted record instance. Duplicate
text, different writers, and independent fresh submissions are not semantic
deduplication. Counts consume the full K1 selection, not the 1,000-record display.
Standalone records above 1,000 are partial/display-truncated with eligible-set
exhaustion. Public record-budget interruption yields a retained lower-bound
count (`exact=false`, exhaustion `none`); private safety or deadline interruption
discards all previously captured members and returns no data. Semantic accounting
is one unit per eligible resolved entity and decision; count adds none.

`inspect_support(SupportInspectionRequest)` returns `SupportInspection`, both
`query/1`; the explained form executes once with ordinary diagnostic capture.
Requests carry request ID, exact original scope, `result_set_id`,
`records_step_id`, `start_ordinal` (default 0), `limit` (1..1,000; default 1,000),
and a fresh `QueryBudget`. Successful output preserves read-state/set/step
correlation, selection exactness, total retained members, ordered records, next
ordinal/null, exhausted flag and public accounting. One operation and one public
unit per inspected member are charged. Insufficient budget fails the whole slice;
it never returns a partly revalidated page. Failure withholds membership, total,
ordinal, IDs and accounting, apart from the marked zero sentinels.

Membership retains K1's exact assertion dependencies and sequence-selected source
or seed activation witness, not a later witness reconstructed by Q1. Revalidation
uses the real K1 reader. Sets are service/principal/exact-scope/step bound, expire
after five monotonic minutes, and disappear on close/restart. Unknown, expired or
mismatched IDs return not_found after scope authorization. Every canonical commit
invalidates the original observer, including same-content restore and unrelated
writes. Ordinal replay reads retained membership; it is not a new selection.
Foundation continuation remains null.

Retention is bounded to 32 live sets, 8 MiB canonical UTF-8 compact sorted-key JSON
per set/output, and 32 MiB aggregate including staged/transferred copies. Admission
is incremental before serialization/retention; no eviction makes a count succeed.
Quota failure publishes no data or partial support ID. Diagnostics have independent
unchanged target/byte limits: large valid counts may have unavailable reports
(notably 1,001 targets), without changing their business results or support.
See [the executable count/inspection example](examples/query_decisions.py).

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
failure. Deadline includes queue, startup, SQL, terminal-frame delivery, clean
worker exit, release and inline reporting. Success requires a valid completion
frame with acknowledged accounting and exit status zero; process exit or pipe
EOF alone cannot establish success. Terminal delivery and exit waits consume the
original remaining deadline and remain cancellable.
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
| `set_plan_enabled(PlanStateRequest)` | Trusted administrative boolean CAS using `expected_enabled`; toggles only availability, never the immutable plan definition or job identity. No automatic requeue. |
| `schedule(ScheduleRequest)` | Applied/unchanged `ScheduleReceipt(job_id,status)`. Exact document/revision/state/namespace-token dependency; 30-day control receipts and logical dedup independent of worker instance. |
| `claim(WorkerRequest)` | `ClaimResult` with one fenced 60-second lease or `no_work`, inspected count and `scan_truncated`. At most 200 candidate transitions per call; truncation is not proof of no remaining work. |
| `heartbeat(HeartbeatRequest)` | Renewed `Claim` only for matching worker/fence, unexpired lease and live dependency/plan/corpus guard. Heartbeat at most every 20 seconds while working. |
| `fail(FailRequest)` | Current matching claim/fence only. Explicit `transient` + `budget_exceeded` records deterministic retry delay/exhaustion; `permanent` + `invalid_request`, `unsupported` or `internal_error` records terminal failure. Returns `JobView` with safe failure code/opaque diagnostic ID. No arbitrary exception classification or owner acknowledgement. |
| `retry(RetryRequest)` | Only `failed/retry_exhausted`, current `expected_status_version` and fresh guards. Starts a new episode with a 30-day idempotent `RetryReceipt(job_id,status,status_version,retry_episode)`; lifetime attempts remain unchanged. The receipt describes the committed restart, not current job status. |
| `recover(JobsRequest)` | Creation-sequence pagination over nonterminal document jobs, 1..200: `RecoveryResult(examined,changed,unsupported,next_after)`. Expires claims, supersedes stale dependencies and requeues eligible same-plan blocks. Counts/cursor are authorized selection results, not whole-store totals. |
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
backoff, or `failed` at ten attempts per episode. Reclaim occurs during bounded
`claim` or `recover`;
after recording expiry the caller waits until `next_due_at` and claims again.
Fences and lifetime/episode counters persist across process restart. Exact expiry
is inclusive; shared max-observed UTC prevents backward-clock revival. Source
changes supersede old work; plan disable/guard invalidation block execution.
`fail` accepts only the closed classification above, advertised by registered
plan capabilities. The caller is the trusted registered worker reporting a
failure, not an installed executor; corruption/unclassified exceptions are never
automatically retryable. Repeated or late failure messages lose their claim CAS,
including at exact expiry or after worker replacement.

Recovery requeues `plan_disabled` and `authority_unavailable` blocks only with
the same immutable plan enabled, fresh same-principal registration/authority,
current document dependencies and unchanged unblocked guard. Requeue invalidates
the fence and preserves lifetime/episode counters and any not-before time; an
exhausted episode becomes failed rather than running again. Repeated recovery
does not advance job versions/fences or reset delay. Policy changes that rotate
source state supersede the old job rather than repairing it.

`processor_unavailable`, `purge_blocked` and `awaiting_input` blocks are explicitly
counted as unsupported recovery, never silently requeued: this slice has no owner
availability proof, X1 release or resupplied-unit execution. A newly stale target
can still be superseded. Plan disable/enable cannot erase these owner-only blocks.
Terminal success, permanent failure, cancellation and supersession never recover.
Retry only resets the exhausted episode counter; there is no lifetime attempt cap
or automatic new episode. Changed input under a retry key conflicts; inclusive
30-day expiry precedes digest comparison and its tombstone survives clock rollback.
Historical schedule/retry receipt reads require current retained-target authority,
but not a current source or live claim, and never reactivate work.
There is no cancellation, processor installation or plan-definition update API.
Registration does not resurrect work or enqueue a background scanner.

All eight ordinary methods also have `_explained(request, options)` wrappers.
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
- `WithdrawAssertion(operation="withdraw_assertion", contribution_id=...)` requires
  declared `read` and `write_knowledge`; it has no reason/replacement/CAS fields.
  `AssertionWithdrawalReceipt(kind="assertion_withdrawal", contribution_id=...,
  withdrawal_id=...)` names the exact target and first immutable event.
  Successful batch correlation checks its exact contribution ID.
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
