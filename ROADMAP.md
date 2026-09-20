# Generic knowledge engine roadmap

Status: target product direction, not implemented functionality.
Recorded: 2026-09-20.

This roadmap captures the target query capabilities and the full core build map
for generic text ingestion and agent-assisted graph enrichment. It is not an MVP
or first-slice plan. Source-specific connector design remains separate; detailed
contracts and implementation choices are still open.

## Product goal

Build a generic knowledge system that accepts knowledge from extensible ingestion
plugins and agents, preserves supporting evidence, and executes natural-language
queries through complementary retrieval methods.

The product remains Local Knowledge Graph. The graph represents entities and
relationships; the broader system also includes source evidence, structured
records, text indexes, and vector indexes. Multi-method retrieval is an
architectural capability, not a separate product name.

Move from an experiment-led Markdown index to a usable knowledge engine.
Retain useful implementation and regression coverage rather than rewrite
everything. Benchmarks support product development; they do not define the
product or its supported source formats.

This document defines the next product direction. `SPEC.md` retains the existing
implementation contracts and historical experiment details. Where its future
direction conflicts with this roadmap, this roadmap takes precedence; existing
behavior is not changed merely by documenting a target.

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
| Keyword retrieval | Built: FTS5/BM25, strict and natural modes. | Retain and expose through the unified query interface. |
| Semantic retrieval | Built: local embeddings and vector projections. | Integrate into planned query execution and index lifecycle. |
| Fusion and deduplication | Built: keyword/vector reciprocal-rank fusion. | Accommodate additional retrieval paths where appropriate. |
| Reranking | Built: cross-encoder over hybrid candidates. | Apply to relevance-ranked evidence, not exact counts or exhaustive results. |
| Evidence and history | Built: exact quotes, immutable revisions, source ranges, context, and revision comparison. | Generalize source references beyond Markdown files. |
| Graph retrieval | Limited: explicit relationships and bounded two-hop subject expansion. | Add typed, question-driven relationship and path operations. |
| Structured retrieval | Limited: dedicated tasks, decisions, blockers, conflicts, and status operations. | Add supported exact lookups, filters, counts, and aggregations. |
| Automatic retrieval routing | Not built; caller explicitly selects `--query-mode`. | Choose suitable retrieval methods from query intent and constraints. |
| Query plan generation and execution | Not built. | Translate natural language into a validated sequence of supported operations and execute dependencies. |
| Query entity resolution | Known subjects and aliases exist, but no general question-to-entity stage. | Resolve candidate identities and report ambiguity rather than guess. |
| Pagination | Result limits exist; continuation cursors do not. | Add stable continuation with explicit result-set boundaries. |
| Generic ingestion interface | Current ingestion is configured Markdown parsing. | Define source-independent, versioned write contracts. |
| Plugin ecosystem | No generic ingestion plugin contract. | Support independently developed source adapters and agent guidance. |
| Agent-authored knowledge | Current extraction requires explicit Markdown structure. | Support validated entity, relationship, and assertion writes with evidence. |
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
| Document intake | Manifest-selected local Markdown files. | Generic text/metadata ingestion interface and external identities. |
| Evidence | Immutable revision identities and exact parsed source anchors. | Complete supplied-text retention and source-independent evidence addressing. |
| Indexing | Keyword refresh during ingestion; a separate command builds embeddings. | Generic passage processing and coordinated index lifecycle/status. |
| Enrichment handles | IDs exist in storage and ingestion reports. | Stable agent-facing ingestion response and enrichment status contract. |
| Entity discovery | Manifest seed entities and exact alias matching. | Agent-facing candidate lookup and identity reconciliation tools. |
| Graph writes | Importer directly creates mentions and generic wikilink edges. | Validated agent-authored entities, typed relationships, assertions, and evidence links. |
| Interpretation provenance | Existing edges reference source anchors. | Agent attribution, explicit-versus-inferred assertions, and enrichment history. |
| Reprocessing | Source revision handling and explicit task/decision replacements. | General graph idempotency, completion tracking, correction, and stale-evidence handling. |

Remove the manifest's exclusive authority over entity lifecycle: current
ingestion deactivates corpus entities and reactivates configured seeds. That
behavior must not deactivate agent-created entities. Preserve explicit entity
ownership/lifecycle rules while adapting the existing Markdown importer.

### Shared write and lifecycle contracts

Define a versioned contract that can represent:

- Source identity, complete supplied text and supporting evidence, source location,
  revision, event time, and source metadata.
- Stable external IDs and synchronization checkpoints for idempotent updates.
- Entities, identifiers, aliases, typed relationships, and assertions linked
  to supporting source evidence.
- Whether knowledge came from structured source fields or agent interpretation,
  including the responsible plugin/agent and available model provenance.
- Corrections, supersession, source removal, and deletion, with explicit
  current-state and historical behavior.

The core validates writes and maintains integrity across plugins. Identity
matching must support candidate lookup and explicit, auditable, correctable
decisions; similar names must not trigger silent merges.

Conflicting source assertions must be representable without choosing an
unsupported winner. Generalize the existing explicit state-resolution machinery
where useful rather than discarding evidence history.

Keep corpus isolation. Define permission handling before connecting sources
with differing access rules. Define the distinction between deactivating a
source while retaining history and actually purging retained content.

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

## Build dependencies, not a first-slice plan

Generic intake and evidence identities underpin both indexing and graph writes.
The knowledge model and identity contracts underpin agent tools and enrichment
lifecycle. Query planning executes against these stable read contracts.
Processing, access, retention, and observability apply across all of them.

The natural-language query planner is not a prerequisite for graph enrichment
tools. Concrete email, meeting-notes, and Teams adapters are not prerequisites
for a source-independent ingestion core. Preserve the existing evidence and
retrieval foundations while replacing Markdown-specific coupling.

The first implementation workstream is specified in
[`PRODUCTIZATION_SPEC.md`](PRODUCTIZATION_SPEC.md): preserve fixtures and
ingestion, retire E-stage product organization, and make normal search use the
complete existing reranked hybrid pipeline. Later implementation sequencing and
release boundaries remain to be planned. The full scope above is unchanged.

## Productization: one KG, not a sequence of experiments

Productization is required work across the core build map, not a cosmetic rename
or a final documentation task. Users and plugin authors should understand and
operate the KG without knowing what E0, E1, or any later experiment meant.
The immediate implementation contract is
[`PRODUCTIZATION_SPEC.md`](PRODUCTIZATION_SPEC.md). It deliberately changes the
public search default while preserving component behavior and fixture coverage;
it does not implement the future query planner or generic ingestion core.

### Product organization and supported workflows

- Organize production interfaces, code, documentation, and tests around
  capabilities: ingestion, evidence, indexing, identity, graph enrichment, and
  query execution. Experiment IDs must not define public contracts or required
  user workflows.
- Document a coherent path to install, configure, ingest, inspect processing
  status, enrich knowledge, query, and maintain the system. Examples should
  demonstrate supported behavior rather than a sequence of research stages.
- Provide supported defaults and configuration with clear compatibility and
  lifecycle rules. Ordinary callers should not need to choose an experiment or
  manually assemble retrieval stages. Retain meaningful advanced controls and
  diagnostics, not competing experimental product paths.
- Review existing commands, flags, fixtures, and implementation scaffolding.
  Retain or generalize useful capabilities; retire obsolete experiment-only
  machinery with explicit migration or deprecation where consumers are affected.
  Do not assume an implementation is disposable merely because it began as an
  experiment.

### Documentation and historical results

- Rewrite the README around the KG's purpose, supported capabilities, setup,
  workflows, and limitations as those capabilities are delivered.
- Evolve `SPEC.md` into the current product contract organized by subsystem.
  Preserve past experiment designs and measured results in clearly historical
  benchmark documentation, separate from current behavior and the roadmap.
- Remove E-stage sequencing as a prerequisite for product development. In
  particular, the older requirement to finish and record each experiment before
  starting the next feature is not the governing development workflow.
- Describe capabilities as supported, planned, deprecated, or explicitly
  experimental where that distinction is real. Do not relabel unfinished work
  as production-ready or erase known quality limitations.

### Product testing and release criteria

- Convert useful E-stage checks into capability-based regression coverage:
  storage/provenance integrity, indexing, retrieval, graph writes, identity,
  lifecycle, query plans, and end-to-end contracts.
- Preserve benchmark datasets, reproducible configurations, and historical
  scores. Benchmarks measure product behavior; they are not the product's
  architecture or a series of releases users must navigate.
- Gate releases on documented supported workflows, correctness, evidence
  integrity, retrieval quality, reliability, latency, and resource cost.
  Establish replacement criteria explicitly rather than silently lowering or
  deleting an unmet experimental threshold.
- Separate routine product tests from expensive model evaluations, while
  retaining both with documented execution and release responsibilities.

Productization is complete when normal setup and use require no knowledge of
the E-series, current documentation describes the delivered KG rather than its
research history, and tests and release criteria map to supported capabilities.
Historical experiment identifiers may remain in archived results for traceability.

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
- Public workflows and current product documentation stand on their own without
  E-stage terminology or sequencing. Useful experimental checks survive as
  product regression/evaluation coverage, with migration paths for changed
  public interfaces.
- A representative, permission-approved usage set measures evidence quality,
  structured-query correctness, latency, and resource cost. Set concrete budgets
  before claiming the release is ready; existing benchmark results alone are not
  proof of real-world usefulness.

## Decisions still open

- First real connector, source system, and representative user questions.
- Plugin packaging and how ingestion agents are hosted or invoked.
- Initial knowledge schema, extension rules, and identity reconciliation policy.
- Rule-based versus model-assisted planning, and the initial operator set.
- Local-only versus configurable hosted model providers and data-egress policy.
- Retention, purge, and access-control requirements for actual connected sources.
- Cursor/snapshot implementation and measurable quality, latency, and cost gates.

These are implementation decisions to resolve, not capabilities already built.
The current SQLite foundation can be reused; this roadmap does not require a
new database product, a hosted service, or an autonomous query agent.
