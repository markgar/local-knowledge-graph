# Generic knowledge engine roadmap

Status: Markdown retrieval and F0/V0 validation foundation implemented;
generic services and source integrations remain planned.
Updated: 2026-09-20.

This roadmap captures the target query capabilities and the full core build map
for generic text ingestion and agent-assisted graph enrichment. It is not an MVP
or first-slice plan. Initial shared contracts are implemented as validation-only
values; service implementations and source-specific connector designs remain open.

[`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md) organizes delivery of this
full scope into work packages, dependencies, parallel lanes, and acceptance gates.
It records package status, implementation order and acceptance requirements.
The shared contracts and acceptance cases are in
[`FOUNDATION_SPEC.md`](FOUNDATION_SPEC.md). Validation models and synthetic inputs
are implemented; generic storage, authorization and execution are not.

## Current position

| Area | Status |
| --- | --- |
| Markdown ingestion, exact evidence/history, structured reads and reranked hybrid search | Implemented; [SPEC.md](SPEC.md) defines supported behavior and limitations. |
| Internal intake/writer separation | Implemented private boundary, not a generic write API. |
| F0: shared contracts | Complete for `foundation/1` validation/serialization and initial compatibility semantics. |
| V0: acceptance inputs | Complete for A01-A17 recipes, contract fixtures, deterministic workload and initial numeric targets; integrated outcomes are pending. |
| E1 / K1 / Q1 | Next: generic evidence storage/intake, contribution storage/writes and deterministic query execution. None has started. |
| Remaining systems and connectors | Planned under their dependencies; full core and live-workflow acceptance remain pending. |

The implementation plan tracks **2 complete and 21 remaining packages**.
This counts the new build plan, not the capabilities already supplied by the
Markdown product. Contract validation does not imply working storage, retries,
authorization, query execution, or measured quality/performance.

## Product goal

Build a generic knowledge system that accepts knowledge from extensible ingestion
plugins and agents, preserves supporting evidence, and executes natural-language
queries through complementary retrieval methods.

The product remains Local Knowledge Graph. The graph represents entities and
relationships; the broader system also includes source evidence, structured
records, text indexes, and vector indexes. Multi-method retrieval is an
architectural capability, not a separate product name.

Extend the current Markdown engine without rewriting its evidence and retrieval
foundations. Benchmarks support product development; they do not define the
product or its supported source formats.

This document defines targets and remaining gaps. [SPEC.md](SPEC.md) defines
implemented behavior; the roadmap does not change that behavior merely by
documenting a target.

## Responsibility boundaries

| Component | Responsibility |
| --- | --- |
| Ingestion plugin | Access a source, understand its format, expose source-specific tools and guidance, and support synchronization. |
| Ingestion agent | Interpret source content, extract entities and relationships, reconcile identities, and submit evidence-backed knowledge through the core interface. |
| Core knowledge system | Validate and store knowledge, identities, source evidence, revisions, and history; maintain queryable indexes and enforce scope boundaries. |
| Query service | Interpret a natural-language request into bounded supported operations, execute retrieval or structured queries, and return evidence-backed results. |
| Calling agent | Interpret results, compose an answer, and optionally request more results, more context, or a reformulated query. |

An ingestion agent is always part of the intended ingestion workflow and owns
interpretation. The core stores and indexes supplied text, then validates and
persists the graph changes supplied by the agent. Text indexing must not wait for
graph enrichment.

Semantic entity extraction is not a responsibility of the core.
Plugins may extract structured fields mechanically, such as sender addresses or
meeting dates. Agents may infer additional entities and relationships. The core
must distinguish source assertions from inferred assertions and retain their
evidence and extraction provenance.

The query service has no autonomous agent loop and does not generate the final
narrative answer. A bounded model-assisted query planner is permitted; planning
is not the same responsibility as autonomous investigation or answer generation.

## Current implementation versus target

"Built" means code exists, not that real-world quality or production readiness
has been established. Existing retrieval acceptance gates remain unmet.

| Capability | Current state | Required work |
| --- | --- | --- |
| Keyword retrieval | Built: internal FTS5/BM25 components; natural candidates feed product search. | Retain within future query execution. |
| Semantic retrieval | Built: local embeddings and vector projections. | Integrate into planned query execution and index lifecycle. |
| Fusion and deduplication | Built: keyword/vector reciprocal-rank fusion. | Accommodate additional retrieval paths where appropriate. |
| Reranking | Built: cross-encoder over hybrid candidates. | Apply to relevance-ranked evidence, not exact counts or exhaustive results. |
| Evidence and history | Built: exact quotes, immutable revisions, source ranges, context, and revision comparison. | Generalize source references beyond Markdown files. |
| Graph retrieval | Limited: explicit relationships and bounded two-hop subject expansion. | Add typed, question-driven relationship and path operations. |
| Structured retrieval | Limited: dedicated tasks, decisions, blockers, conflicts, and status operations. | Add supported exact lookups, filters, counts, and aggregations. |
| Automatic retrieval routing | Not built; product search always runs reranked hybrid retrieval with no mode selector. | Choose suitable operations from query intent and constraints, including structured/graph execution. |
| Query plan generation and execution | Typed plan/result validation exists; planning and execution do not. | Implement coherent dependent execution (Q1), richer graph/structured operators (Q2) and natural-language planning (Q3). |
| Query entity resolution | Known subjects and aliases exist, but no general question-to-entity stage. | Resolve candidate identities and report ambiguity rather than guess. |
| Pagination | Result limits exist; continuation cursors do not. | Add stable continuation with explicit result-set boundaries. |
| Generic ingestion interface | Markdown ingestion runs today; `foundation/1` validates source-independent write descriptions without persisting them. | Implement generic intake/storage and adapt Markdown (E1/E2). |
| Plugin ecosystem | Shared values exist, but plugin packaging, tools and invocation do not. | Support independently developed source adapters and agent guidance (I1/I2). |
| Agent-authored knowledge | Bounded enrichment change-set validation exists; current stored extraction still requires explicit Markdown structure. | Implement entity/contribution storage, validated writes and agent tools (K1/K2). |
| Cross-source identity reconciliation | Seed entities and approved aliases exist. | Add candidate lookup and auditable identity link/merge/correction operations. |

## Query requirements

Provide one logical query interface. Its transport can reuse the existing CLI
and Python services; a network server is not a prerequisite.

Accept natural-language text plus optional exact constraints, such as corpus,
entity, source, date range, result limit, and continuation cursor. Preserve
scope and any applicable access restrictions across every operation.

Support these execution paths:

- Exact structured lookup, filtering, counting, and aggregation.
- Keyword retrieval for exact terms and identifiers.
- Semantic retrieval for meaning and paraphrases.
- Graph traversal for relationships, dependencies, and paths.
- Combinations and dependent sequences of those operations.
- Candidate fusion, deduplication, and reranking when returning ranked evidence.

The graph is not the sole entry point. Source passages must remain discoverable
when ingestion did not extract the right entity or relationship.

### Query plans

The planner selects from supported, validated operations, with explicit limits
on traversal, candidate counts, execution time, and model usage. It must not
execute unrestricted model-generated code or database statements.

| Request | Example execution plan |
| --- | --- |
| Find notes about launch readiness. | Keyword and vector retrieval, fuse, deduplicate, rerank. |
| What projects does Sam own? | Resolve Sam, traverse ownership relationships, return supported projects. |
| Find decisions for projects Sam owns. | Resolve Sam, find owned projects, retrieve their decision records. |
| How many open tasks does Sam own? | Resolve Sam, filter effective task records, count the complete matching set within scope. |

If identity is ambiguous or the request is unsupported, return an explicit
outcome the caller can act on. Do not silently choose a person or substitute a
top-k search for an exact operation.

### Results and continuation

Return typed results: ranked evidence, records, relationship paths, or aggregate
values. Include supporting source references and revision/time context.
Aggregates must expose their scope and a way to inspect their supporting records;
a count is about indexed knowledge, not a guarantee of completeness in the world.

Expose enough execution information to explain which operations ran, the
constraints used, truncation, and failures. Relevance scores are not truth or
answerability probabilities.

Pagination continues a stable result set. Distinguish exhaustion of a bounded
candidate pool from exhaustion of all potentially relevant evidence. Specify
cursor expiry and behavior when underlying data changes. Expanding the search
budget or reformulating the question is distinct from asking for the next page.

Preserve direct evidence and context-read operations. Return empty, ambiguous,
unsupported, partial, stale-index, and failed outcomes explicitly as appropriate;
none should masquerade as a complete successful answer.

## Ingestion and core requirements

The core accepts supplied text and metadata without needing to understand email,
meeting notes, Teams messages, or other source-specific formats. Acquisition,
format conversion, and source-specific guidance belong outside this core scope.

### Evidence first, graph enrichment afterward

1. The core accepts the document, preserves its supplied text and revision, and
   creates passages under a configured policy or validated supplied boundaries.
2. The core indexes the passages for keyword and semantic retrieval and returns
   stable document, revision, and passage IDs with processing status.
3. The ingestion agent examines the stored evidence and looks up existing
   entities, identifiers, related passages, and graph neighborhoods.
4. The agent submits new entities, passage-to-entity links, and supported typed
   relationships or assertions between entities.
5. The core validates scope, references, and write integrity, persists the
   enrichment with provenance, and records its completion or partial status.
6. Source changes, corrected identities, or revised interpretations can trigger
   further enrichment without duplicating prior writes or erasing history.

Passage mentions and entity-to-entity assertions are different operations.
A passage mentioning Sam and Atlas does not, by itself, establish that Sam owns
Atlas. Similarity can find candidates; it is not evidence of a factual edge.
Inferred assertions must remain distinguishable from explicit source statements.

Storage and indexing are mechanical, configured processing rather than agent
interpretation. Chunking and embedding configurations must be versioned.
Embedding inference is not a promise of bit-for-bit reproducibility across
hardware or runtime versions.

### Full core build map

The following are the major systems to build or generalize, not an implementation
sequence. The query work recorded above remains part of this same roadmap.

| System | Required capability |
| --- | --- |
| 1. Generic document ingestion | Source-independent text and metadata submission, stable external IDs, revisions, batch writes, validation, and explicit outcomes. |
| 2. Canonical evidence storage | Complete supplied content, immutable revisions, stable passage references, source locations, timestamps, provenance, and current/historical reads. |
| 3. Text processing and indexing | Versioned passage policies, keyword indexes, embedding generation, vector indexes, coordinated updates, freshness, and rebuilds independent of graph completion. |
| 4. General-purpose knowledge model | Extensible entities, identifiers, aliases, typed relationships, assertions, and evidence links rather than a universal schema defined by Markdown tasks and headings. |
| 5. Agent-facing graph tools | Document inspection, entity lookup, neighborhood reads, entity creation, passage linking, and validated relationship/assertion writes. Interpretation stays with the agent. |
| 6. Identity and reconciliation | Cross-document candidate matching, explicit identity linking, auditable merge/unmerge or equivalent corrections, and ambiguity handling without silent merges. |
| 7. Enrichment lifecycle | Pending and processed revision tracking, agent attribution, retries, partial completion, idempotency, and re-enrichment without duplicate knowledge. |
| 8. Knowledge change and conflicts | Corrections, retractions, supersession, competing assertions, source updates/removals, and separation of historical from currently supported knowledge. |
| 9. Unified query planning and execution | Validated natural-language plans across structured, keyword, semantic, and graph operations; dependent execution, exact counts, fusion/reranking, typed results, context, and stable pagination. |
| 10. Scope, access, and retention | Corpus boundaries, applicable permissions across every operation, retention, deactivation, and actual deletion of content and affected derived knowledge/indexes. |
| 11. Reliable processing and operations | Coordination of ingestion/indexing/enrichment jobs, observable progress and failures, interruption recovery, concurrent-write handling, stale-submission detection, migrations, and diagnostics. |
| 12. Stable contracts and evaluation | Versioned APIs/tools usable by future plugins without core edits; contract and real-world evaluation for evidence, retrieval, graph writes, identity, lifecycle, performance, and cost. |

### Existing ingestion foundations and gaps

| Area | Current implementation | Gap to target |
| --- | --- | --- |
| Document intake | Manifest-selected local Markdown files; generic write values validate only. | Implement text/metadata intake, external identity mapping and persistence. |
| Evidence | Immutable revision identities and exact parsed source anchors. | Complete supplied-text retention and source-independent evidence addressing. |
| Indexing | Keyword refresh during ingestion; a separate command builds embeddings. | Generic passage processing and coordinated index lifecycle/status. |
| Enrichment handles | IDs exist in storage/ingestion reports; foundation receipt/progress values validate only. | Return real durable identities and processing status from generic services. |
| Entity discovery | Manifest seed entities and exact alias matching. | Agent-facing candidate lookup and identity reconciliation tools. |
| Graph writes | Importer creates mentions/wikilink edges; generic change sets are descriptions, not writes. | Persist agent-authored entities, typed relationships, assertions and evidence links. |
| Interpretation provenance | Stored edges reference anchors; foundation values describe attribution/support. | Persist agent attribution, explicit-versus-inferred assertions and enrichment history. |
| Reprocessing | Source revision handling and explicit task/decision replacements. | General graph idempotency, completion tracking, correction, and stale-evidence handling. |

Current ingestion has corpus-wide authority over seed activation, alias deletion
and unselected-document deactivation; re-extraction replaces mentions and
relationships. Before enabling another writer, E1/K1 must owner-scope these
mutations or explicitly reject mixed-writer storage. E2 must implement actual
coexistence under the foundation's document and contribution ownership rules.

### Shared write and lifecycle contracts

The foundation defines initial identities, offsets, ownership, bounded change
sets, conjunctive support, retry policy and coherent-query-state semantics.
The remaining service work must persist and enforce:

- Source identity, complete supplied text and supporting evidence, source location,
  revision, event time, and source metadata.
- Stable external IDs and synchronization checkpoints for idempotent updates.
- Entities, identifiers, aliases, typed relationships, and assertions linked
  to supporting source evidence.
- Whether knowledge came from structured source fields or agent interpretation,
  including the responsible plugin/agent and available model provenance.
- Corrections, supersession, source removal, and deletion, with explicit
  current-state and historical behavior.

Service implementations must validate stored references and maintain integrity
across plugins. Identity matching must support candidate lookup and explicit, auditable, correctable
decisions; similar names must not trigger silent merges.

Conflicting source assertions must be representable without choosing an
unsupported winner. Generalize the existing explicit state-resolution machinery
where useful rather than discarding evidence history.

Keep corpus isolation. Implement and approve permission handling before connecting
sources with differing access rules. Enforce the specified distinction between
deactivating a source while retaining history and actually purging retained content.

Text and vector indexes remain derived from canonical knowledge and evidence.
Expose index freshness and rebuild behavior so ingestion updates cannot silently
produce inconsistent query results.

## Future source plugins (separate scope)

New source plugins must not require changes to core parsing or query logic.
The core supplies the compatibility contracts; concrete source adapters and
their agent guidance are separate work, not prerequisites for defining the core.

- Adapt Markdown ingestion to the common interface.
- Support an email plugin for messages, threads, participants, and source links.
- Support a meeting-notes plugin for notes or transcripts, participants,
  timestamps where available, and source references.
- Support future Teams-message and other source adapters through the same core
  contracts, with source-specific behavior outside the engine.
- Leave room for future sources without assuming every input is Markdown.

Plugins own source-specific access and synchronization, not separate graph,
search, or answer engines. Define how agent instructions and executable adapter
tools are packaged before calling the plugin interface stable.

## Remaining delivery sequence

Generic intake and evidence identities underpin both indexing and graph writes.
The knowledge model and identity contracts underpin agent tools and enrichment
lifecycle. Query planning executes against these stable read contracts.
Processing, access, retention, and observability apply across all of them.

The natural-language query planner is not a prerequisite for graph enrichment
tools. Concrete email, meeting-notes, and Teams adapters are not prerequisites
for a source-independent ingestion core. Preserve the existing evidence and
retrieval foundations while replacing Markdown-specific coupling.

1. **Next: E1/K1/Q1.** Implement evidence intake/storage, knowledge contributions,
   and deterministic execution using the delivered foundation contracts.
2. **Dependent capabilities.** Add Markdown adaptation, passage/index lifecycle,
   processing recovery, graph tools, identity/correction behavior, richer query
   operators and continuation as their prerequisites become available.
3. **Agent and planner integration.** Connect enrichment, independently packaged
   plugins and bounded natural-language planning. Access/retention and evaluation
   run alongside the lanes, not only at the end.
4. **Core and live acceptance.** Accept integrated core capabilities separately
   from permission-approved email, meeting and Teams workflows. Full roadmap
   acceptance requires both; fixture-only adapters do not establish live readiness.

The [implementation plan](IMPLEMENTATION_PLAN.md) owns the detailed package graph.
Product interfaces stay capability-based; package IDs are coordination labels,
not CLI modes or user prerequisites. Preserve exact evidence, reviewed gold and
benchmark results. Maintain current behavior in `SPEC.md`, require explicit
compatibility handling for public changes, and keep routine controlled-provider
tests separate from real-model evaluations.

## Core acceptance requirements

- Supplied text is preserved and keyword/semantic searchable before graph
  enrichment completes, with explicit processing and freshness status.
- Agent tools can inspect evidence, look up identities, and submit validated
  evidence-backed entities, mentions, relationships, and assertions.
- Retries do not duplicate graph knowledge. Partial enrichment, stale agent
  submissions, interrupted processing, and concurrent changes are handled
  explicitly.
- Inference provenance, identity corrections, and affected assertions remain
  auditable across re-enrichment and source revisions.
- An external caller submits natural language without selecting a retrieval
  mode; the service executes a supported plan or reports why it cannot.
- Search, direct lookup, a dependent graph query, and an exact count have
  reviewed end-to-end cases, including ambiguous and unsupported requests.
- Exact counts cover the matching indexed scope rather than a top-k sample.
- Every returned assertion can be traced to evidence; context and historical
  citations remain resolvable after ordinary source updates.
- Pagination has no duplicates or skipped results within its documented
  snapshot semantics; changed or expired snapshots are handled explicitly.
- Repeated ingestion is idempotent. Edits, removals, identity corrections,
  conflicting assertions, and index freshness have explicit coverage.
- Versioned contracts can be exercised by independent source adapters without
  source-specific changes to core parsing or query logic. Actual connector
  implementation is tracked separately.
- Public workflows and documentation describe supported capabilities directly,
  with regression/evaluation coverage and migration paths for changed interfaces.
- A representative, permission-approved usage set measures evidence quality,
  structured-query correctness, latency, and resource cost against the
  [initial targets](benchmarks/foundation/README.md). Targets and synthetic
  contract checks alone are not proof of real-world usefulness.

## Decisions still open

- First real connector, source system, and representative user questions.
- Plugin packaging and how ingestion agents are hosted or invoked.
- Later knowledge-schema extensions and identity reconciliation implementation;
  initial types/support/ownership rules are settled in the foundation.
- Rule-based versus model-assisted planning; the initial typed operator vocabulary
  is settled in the foundation, not yet executable.
- Local-only versus configurable hosted model providers and data-egress policy.
- Retention, purge, and access-control requirements for actual connected sources.
- Cursor/snapshot implementation and measured quality, latency, and cost results;
  initial numeric engineering targets are recorded under `benchmarks/foundation/`.

These are implementation decisions to resolve, not capabilities already built.
The current SQLite foundation can be reused; this roadmap does not require a
new database product, a hosted service, or an autonomous query agent.
