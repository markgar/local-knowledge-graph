---
type: product-specification
status: active
date: 2026-09-17
tags:
  - knowledge-graph
  - software
  - mvp
---

# Knowledge Graph Software MVP

> **Next product direction:** [`ROADMAP.md`](ROADMAP.md) defines the generic
> knowledge engine target, including natural-language query planning and
> generic text ingestion and agent-assisted graph enrichment. It takes precedence over
> future-direction statements here where they differ. This specification
> retains existing implementation contracts and historical experiment details;
> roadmap capabilities are not implied to be implemented.

## Implementation Status

This document is both the evidence-MVP product contract and the roadmap toward
agent-useful retrieval. Statuses describe the repository as of 2026-09-18:

- **Implemented**: available and covered by automated tests.
- **Partial**: the foundational path exists, but the complete requirement is
  not yet satisfied.
- **Planned**: specified but not implemented.

| Capability | Status | Notes |
| --- | --- | --- |
| Generic corpus manifest and source selection | Implemented | Corpora, paths, aliases, and metadata mappings are configuration data. |
| Immutable revisions and exact source anchors | Implemented | Historical revisions remain retrievable while current-state queries use only the active revision. |
| Idempotent ingestion | Implemented | Unchanged sources do not create new revisions or mutate document state. |
| Ingestion explanations | Implemented | Opt-in per-document outcomes, stored counts, extraction rules, and evidence IDs; source quotes require separate opt-in. |
| Markdown headings, paragraphs, lists, tasks, and wikilinks | Implemented | CommonMark block maps preserve exact source ranges while fenced code is excluded from structural classification. |
| Rebuildable SQLite schema | Implemented | The packaged schema initializes new indexes; pre-alpha schema changes require rebuilding generated databases. |
| FTS5 passage search | Implemented | Queries are corpus-scoped and limited to current active revisions, with strict and natural BM25-ranked modes. |
| Real-world semantic retrieval | In progress | Two isolated local embedding profiles, hybrid fusion, and cross-encoder reranking are implemented and measured; acceptance gates remain unmet. |
| Seed entities, approved aliases, and exact mentions | Implemented | Similar names are never merged automatically. |
| Explicit relationships and graph traversal | Implemented | Anchored wikilink relationships support deterministic one- and two-hop traversal. |
| Explicit open and completed tasks | Implemented | Checkbox status and optional inline owner and due-date fields are extracted. |
| Event-date filtering | Implemented | Configured document dates and revision timestamps support bounded retrieval. |
| `ingest`, `status`, `actions`, `evidence`, and `search` CLI | Implemented | Commands expose text and JSON output over reusable services. |
| Decisions, blockers, and conflicts | Implemented | Explicit structural sections produce cited records; contradiction inference remains a non-goal. |
| Evidence gaps and abstention | Partial | Unknown configured subjects abstain deterministically; E4 evaluated and rejected a top-reranker-score threshold for natural-language answerability. |
| Reviewed acceptance datasets | Implemented | Synthetic corpora verify contracts; QASPER measures real-world evidence retrieval and exposes current limitations. |
| Second-corpus generalization proof | Implemented | A separate research corpus uses the same parser, schema, ingestion, and retrieval services. |
| External client integration | Implemented | A subprocess client handles JSON errors and renders cited status output. |

## Purpose

Build a small, general-purpose local knowledge engine. The software
ingests a configured set of Markdown documents, preserves immutable evidence,
indexes explicit structure and relationships, and exposes cited retrieval
through a reusable Python API and CLI.

For any configured subject or workstream, it should answer:

> What is connected to this subject, what explicit actions and decisions are
> recorded, and which exact source passages support each result?

The primary product surface is an agent or other client. A local Python
package, CLI, and SQLite database provide deterministic ingestion and retrieval
behind that experience.

Topics, workstreams, products, customers, people, aliases, and source paths are
data. None may be hard-coded in parsing or retrieval logic.

## Intended User Experience

A user asks a client a question about a configured subject. The client invokes
the local CLI or Python API, receives structured results with evidence, and
presents a concise answer.

Every substantive statement must include:

- The source note.
- The source revision.
- The heading or structural location.
- The exact supporting passage.
- The date or time context when available.

If evidence is missing, stale, or conflicting, the system must say so rather
than complete the answer from model knowledge.

## Version 1 Scope

### Generalization Boundary

The engine operates on generic:

- Source documents, revisions, and anchors.
- Entities, aliases, mentions, and explicit relationships.
- Actions and decisions.
- Searchable passages and evidence.

The software must accept any corpus manifest and any subject at query time. A
new corpus, topic, or workstream must require no Python or schema changes.

### Sources

Ingest Markdown files selected by a corpus manifest. The manifest declares:

- Corpus ID and display name.
- Vault root.
- Included source paths or path patterns.
- Seed entities and approved aliases.
- Optional metadata-field mappings.

The engine does not contain corpus-specific source defaults and does not crawl
the entire vault unless a manifest explicitly requests it.

### Excluded Sources

Version 1 does not ingest:

- Meeting transcripts.
- Remote email.
- Chat or channel messages.
- Arbitrary cloud documents.
- Live cloud synchronization.
- Task systems other than explicit tasks already present in selected notes.

These sources can be added only after the initial slice demonstrates that they
improve a defined workflow.

## Functional Requirements

### Deterministic Ingestion

The indexer must extract:

- Vault-relative path.
- Frontmatter.
- Document title.
- Heading hierarchy.
- Paragraphs, list items, and task items.
- Obsidian wikilinks.
- Explicit dates and meeting metadata when structurally available.
- Explicit task status, owner, and due date when structurally available.
- Exact source offsets and quoted text.

The indexer must not use an LLM to infer entities, relationships, decisions, or
tasks in Version 1.

#### Explicit record state

Actions and decisions can declare `[key:: version-key]` and
`[supersedes:: earlier-version-key]`. Keys are corpus-scoped and case-sensitive.
The engine persists these declarations against their source anchors in
`record_binding`; it does not mutate the earlier source. Resolution uses only
active current source revisions, independent of ingestion order.

A unique, same-kind, non-cyclic, uncontested reference suppresses the earlier
record from effective actions/decisions/status queries. Chains resolve to their
current endpoint and inherit subject scope from predecessors. Date/status/source
filters apply to the effective record; this is not historical as-of evaluation.
Source evidence search and citation resolution retain superseded assertions.

Missing or duplicate target keys, type mismatches, self-references, cycles,
and competing updates must be explicit diagnostics, never implicit last-write
or latest-date selection. Syntax errors fail the source savepoint. Unresolved
references leave source records indexed; plain ingestion reports additional
`state_warnings`, and status surfaces corpus-level warnings as evidence gaps.
Removing or editing an update removes its former effect.

`record-state` / `RetrievalService.record_state()` and ingestion explanations
expose the actual resolver outcomes, source/target identities, ambiguous
candidates, conflicting sources, effective record IDs, and suppressed IDs.
Trace reads are transaction-consistent, and quotes remain opt-in. Display
limits do not alter state resolution. Inline-code examples and child-list
metadata must not create state declarations on a parent record.

Checkboxes in both bulleted and CommonMark ordered lists produce tasks.
Numbered decisions, blockers, and conflicts omit list markers from summaries
while preserving their exact source quotes.

### Revision and Provenance

Each logical document has a stable identifier. Each distinct content version
creates an immutable revision identified by a content hash.

Every derived record must point to one or more source anchors in a specific
revision. Re-ingesting unchanged content must produce no database changes.
Editing a note must preserve the prior revision and its historical anchors.

### Retrieval

The MVP must support:

- Full-text search over titles, headings, passages, aliases, and explicit tasks.
- Resolution of a requested subject through seed entities, approved aliases,
  wikilinks, and exact mentions.
- One- or two-hop traversal across explicitly linked entities.
- Filtering by source or event date.
- Retrieval of exact supporting passages.
- Identification of explicit open and completed tasks.

Current-state output is a derived view over evidence, not an independent source
of truth.

Subject matching in titles, headings, and record text uses case-insensitive,
literal name boundaries consistent with approved-alias mention extraction.
A substring inside another name is not a match (for example, Atlas does not
match Atlascope). This applies to lexical retrieval, dense candidate filtering,
actions, explicit records, and status. It does not remove existing document-wide
scope inheritance or bounded two-hop graph expansion.

### Structured Output

Commands used by clients must support JSON output. Each result should include:

```text
record_id
record_type
title
summary
status
event_time
source_path
source_revision_id
anchor_id
heading_path
quote
related_entity_ids
```

Fields that are not available from deterministic evidence remain null. They
must not be guessed.

## CLI Contract

The initial executable is `kg`.

### Ingest

```bash
kg ingest --manifest <corpus.yml>
```

Responsibilities:

- Validate configured paths.
- Create new document revisions when content changes.
- Parse deterministic structure.
- Update full-text indexes and explicit relationships.
- Report added, changed, unchanged, missing, and failed sources.

#### Ingestion explanation contract

`kg ingest --explain [--include-quotes] [--explain-limit 50] --format json`
returns an `IngestReport` with `report_version: "1"` in addition to the existing
run counts. Without `--explain`, the `IngestResult` JSON shape is unchanged.
`IngestService.ingest` accepts the corresponding `explain`, `include_quotes`,
and `detail_limit` keyword arguments. This is real ingestion, not a dry run.

The report includes configured entities, unmatched include patterns, and
source-path-ordered document reports. Each document reports its outcome,
machine-readable reasons, identity, current/stored and previous revision IDs,
previous path on moves, revision state (`new`, `reused`, `unchanged`, or
`unavailable`), active state, and whether derived records were rebuilt.
Parser/configuration rebuilds do not claim new content revisions. Failed
sources and newly deactivated sources are explicit; retained evidence for
those inactive sources is not presented as current indexed content.

Successful documents include stored counts, configured entities actually
mentioned and their anchor IDs, indexed source anchors/passages, and explained
structured records. These are current-revision totals, not per-run insertion
counts. The anchor count includes all immutable anchors retained for that
revision; the indexed anchor list follows current passages, which can be fewer
after a parser upgrade. Mention counts count stored mention records, including
overlapping structural anchors, not necessarily distinct source occurrences.

Record rules are `explicit_wikilink`, `checkbox_task`, `decision_heading`,
`blocker_heading`, and `conflict_heading`. Wikilink edges connect a mentioned
configured source entity to a configured wikilink target in the same anchor;
these are not inferred semantic dependencies. Owners remain action metadata,
not inferred assignment edges. Entity mentions use approved names/aliases;
source anchors follow parsed CommonMark blocks, not inferred facts.

The anchor and structured-record lists are independently limited to 50 entries
per document by default (allowed range 1..200), ordered by source offsets with
stable identity tie-breaks. Each list has an explicit truncation flag; counts
and entity-mention summaries remain complete. Entries retain exact offsets,
heading paths, anchor IDs, and passage/record IDs for evidence lookup.
Source quotes appear in CLI JSON only with `--include-quotes`; using that flag
without `--explain` fails before ingestion. Other metadata is not anonymized.

Report reads use the ingestion transaction. Failure to construct a report
aborts the run rather than treating a reporting defect as a bad source.
Reports and quotes are never added to persisted `ingest_run.counts_json` or
operational logs. YAML source-error diagnostics omit source snippets.

#### Query execution telemetry

`search --explain [--include-quotes]` returns a typed lexical search explanation
instead of changing the normal search-result contract. It exposes the executed
FTS expression, filters, corpus fingerprint, actual ranks, matched subject
predicates, document inheritance, and up-to-two-hop graph paths with supporting
edge IDs. Scores are ranking statistics, not confidence. Quotes are opt-in.

The trace supports strict and natural modes. Unsupported semantic modes fail
explicitly before model work. Concurrent database commits invalidate the trace
and require retry, rather than permitting mixed-state attribution.
`supersession_filter_applied: false` makes clear that source search retains
superseded assertions; effective actions and decisions use the state resolver.

### Status

```bash
kg status <subject> --manifest <corpus.yml> --format json
```

Returns evidence-backed sections for:

- Recent material changes.
- Explicit decisions.
- Open actions.
- Completed actions.
- Blockers.
- People, customers, products, and meetings connected to the workstream.
- Evidence gaps and conflicts.

The first implementation may return extracted records rather than polished
natural-language prose. The calling client is responsible for presentation.

### Actions

```bash
kg actions <subject> --manifest <corpus.yml> --status open --format json
```

Returns explicit tasks associated with the workstream, including owner, due
date, status, source, and evidence. Tasks with no explicit owner must remain
unassigned.

### Evidence

```bash
kg evidence <record-id> --manifest <corpus.yml> --format json
```

Returns every source anchor supporting the selected record, including exact
quotes and Obsidian-openable paths.

### Source Range

```bash
kg source-range <anchor-id> --manifest <corpus.yml> --format json
```

Returns the immutable source range identified by an anchor, including its
revision, structural path, offsets, exact quote, and quote hash.

### Source Context

```bash
kg source-context <anchor-id> --manifest <corpus.yml> --format json
```

Returns a `SourceContextResult`: `selected` (the exact source range),
`section_heading` (nullable), `anchors` (source-ordered exact ranges),
`total_anchors`, and `truncated`. Expansion is corpus-scoped and bound to the
selected anchor's immutable revision, not to the current source file.

The nearest containing heading starts the section. Its subsections are
included until the next sibling or ancestor heading, determined by source
position and heading depth rather than heading names. Preamble anchors are
separate from the first headed section; a headingless document is one section.
`--max-anchors` defaults to 50 and accepts 1 through 200. Oversized sections
return a window centered on the selected anchor, adjusted at section edges,
and report truncation explicitly. The selected anchor is always in the window;
the section heading is also returned separately. Only indexed anchors are
returned, not a reconstructed full-text section; code blocks may be absent
and nested list anchors may overlap.

### Revisions

```bash
kg revisions <source-path> --manifest <corpus.yml> --format json
kg compare-revisions <source-path> --manifest <corpus.yml> --format json
```

Lists immutable document revisions and compares exact added, removed,
modified, and unchanged source ranges. The comparison defaults to the current
revision and its immediate predecessor; callers may supply explicit `--from`
and `--to` revision IDs.

An append-only activation log records predecessor transitions independently
of content revisions. Reverting to known content reuses its revision ID,
counts as changed ingestion, and records a new activation. The default
predecessor is that of the latest activation of the selected target revision.
Revision listings still contain unique content revisions in original ingestion
order. Legacy databases have no reliable activation history: ingestion starts
the log at their current state and warns that earlier transitions are unknown.
Comparisons without a recorded predecessor require explicit revision IDs.

### Search

```bash
kg search <query> --manifest <corpus.yml> --subject <subject> --format json
```

Returns ranked source passages using the requested implemented lexical, dense,
hybrid, or reranked query mode. Every mode resolves results to canonical
source anchors.

## Minimal Data Model

### Source Layer

- `source_document`
  - Stable logical identity and vault-relative path.
- `source_revision`
  - Immutable content hash, observed modification time, and ingestion time.
- `source_anchor`
  - Heading path, structural path, offsets, exact quote, and quote hash.
- `revision_activation`
  - Ordered document-state transitions referencing immutable content revisions
    and their immediately preceding revisions.
- `ingest_run`
  - Parser and schema versions, timestamps, status, and counts.

### Knowledge Layer

- `entity`
  - Workstream, person, organization, customer, project, product, topic, or
    meeting.
- `entity_alias`
  - Approved deterministic aliases and wikilink targets.
- `mention`
  - Exact anchored references to an entity.
- `relationship`
  - Explicit relationships derived from links or structured fields.
- `action_item`
  - Explicit task text, status, owner, and due date.
- `decision`
  - Only decisions explicitly identified by source structure in Version 1.

### Retrieval Layer

- `passage`
  - Searchable source text associated with an anchor.
- `passage_fts`
  - Retained revision-level retrieval text, including titles, headings,
    aliases, and passages. Not used directly for BM25 ranking.
- `lexical_projection` and corpus-specific `current_fts_<hash>` tables
  - Current active passage text only, with isolated FTS5 corpus statistics.
    Refreshed atomically with ingestion when the canonical corpus fingerprint
    changes; unchanged ingestions do not rebuild them.

## Identity Rules

Use deterministic identifiers:

```text
document_id = assigned stable UUID
revision_id = hash(document_id + canonical source bytes)
anchor_id = hash(revision_id + structural path + offsets)
record_id = hash(anchor_id + normalized record type + normalized value)
```

Entities may have manually approved aliases supplied by configuration or
recorded review decisions. The MVP must not automatically merge similar names.

Path-based UUIDs are used only when unoccupied. If a path is reused after its
document moved elsewhere, a deterministic unused generation UUID is allocated;
the moved document's identity and revisions must never be overwritten.

## Implementation Shape

Use Python with:

- The standard-library `sqlite3` module.
- SQLite FTS5.
- Typer for the CLI.
- Pydantic for command and output contracts.
- A Markdown parser that preserves source positions.
- One packaged SQL schema file for rebuildable generated databases.

Organize the code as a reusable package:

```text
src/kg/
  cli.py
  config.py
  db.py
  schema.sql
  ingest/
  markdown/
  retrieval/
  models/
tests/
corpora/
  example.yml
```

CLI commands must be thin adapters. Ingestion, retrieval, and database logic
must be callable directly by an agent or a future local review application.

The corpus manifest contains data only. Parser behavior, relationship rules,
and ranking behavior must remain the same across corpora.

## Acceptance Dataset

Each pilot corpus should provide a reviewed set of questions covering:

- Current workstream status.
- Changes during a specified time window.
- Open and completed actions.
- Explicit owners and missing owners.
- Blockers and dependencies.
- Recent meetings.
- Customers and products mentioned.
- People connected through explicit evidence.
- Exact source citations.
- Conflicting passages.
- Questions for which the correct answer is abstention.

Reviewed questions and expected evidence anchors are corpus-specific fixtures,
not production defaults.

## Evidence MVP Status

The evidence substrate is complete, but the agent-useful retrieval milestone
is not:

- [x] A corpus is added entirely through a manifest and source files.
- [x] Re-running ingestion without source changes produces zero data changes.
- [x] Editing one source creates a new revision without destroying prior
  evidence.
- [x] Every returned task, decision, relationship, and status item has an exact
  source anchor.
- [ ] A reviewed real-world question set returns expected evidence with useful
  retrieval recall.
- [ ] Unsupported natural-language questions reliably report insufficient
  evidence or request clarification.
- [x] The database can be deleted and rebuilt from configured sources with
  equivalent logical results.
- [x] An external client can invoke the CLI and present cited JSON output.
- [x] A second synthetic corpus works without Python or schema changes.

## Explicit Non-Goals

The MVP does not include:

- A standalone web application.
- A general-purpose knowledge graph for the full vault.
- Hard-coded topics, workstreams, people, products, or source paths.
- Training proprietary embedding or reranking models.
- Implementing a custom approximate-nearest-neighbor algorithm or vector
  database.
- Treating a vector index, model output, or inferred graph as authoritative
  evidence.
- LLM-based claim or entity extraction.
- Automatic person merges.
- Contradiction or supersession inference.
- GraphRAG community summaries.
- Automatic email drafting or sending.
- Automatic task creation.
- Background synchronization or webhooks.
- A graph database server.

## Delivery Sequence

### Milestone 1: Corpus and contracts

- [x] Define and validate the generic corpus-manifest schema.
- [x] Create an example manifest and synthetic fixture corpus.
- [x] Establish command and JSON contracts.
- [x] Add reviewed synthetic source selections and expected evidence fixtures.

### Milestone 2: Deterministic index

- [x] Create the rebuildable SQLite schema.
- [x] Parse notes into immutable revisions and exact anchors.
- [x] Index headings, passages, wikilinks, exact mentions, and checkbox tasks.
- [x] Prove idempotent ingestion and preservation of historical revisions.
- [x] Parse CommonMark blocks while preserving exact source positions.
- [x] Extract structured owners, due dates, decisions, blockers, conflicts, and
  configured event metadata.

### Milestone 3: Deterministic structured retrieval

- [x] Implement corpus-scoped FTS5 search.
- [x] Implement `status`, `actions`, `search`, and `evidence`.
- [x] Implement current-state filtering without losing historical evidence.
- [x] Implement anchored one-hop wikilink traversal.
- [x] Implement bounded two-hop traversal.
- [x] Implement deterministic decision, blocker, and conflict retrieval.
- [x] Prove a second reviewed corpus requires configuration only.

### Milestone 4: Client integration

- [x] Invoke the CLI from an external client.
- [x] Generate a cited answer from a reviewed synthetic corpus.
- [x] Record unsupported cases as explicit abstention fixtures.

The results of Milestone 4 determine whether the next investment should be
semantic extraction, another source connector, embeddings, or a review UI.

### Milestone 5: Agent-useful retrieval

- [x] Establish a reproducible real-document benchmark with exact gold
  evidence.
- [x] Record sparse lexical retrieval, latency, citation integrity, and
  unanswerable-query baselines.
- [x] Add a versioned dense embedding projection over canonical source
  anchors.
- [x] Combine sparse and dense candidates using deterministic reciprocal-rank
  fusion.
- [x] Rerank the strongest candidates with a local cross-encoder.
- [ ] Calibrate answerability so unsupported questions abstain or request
  clarification.
- [x] Add an agent-oriented evaluation set covering paraphrases, revisions,
  ambiguity, conflicts, multi-document synthesis, and unsupported questions.
- [x] Expose agent-facing evidence search, source-range reads, revision
  comparison, and citation resolution through a stable tool interface.

The canonical SQLite database remains the evidence and provenance store.
Embedding indexes, learned sparse indexes, reranking outputs, and inferred
records are disposable, versioned projections. Every projected result must
resolve back to an immutable `anchor_id` and `revision_id`.

#### Build versus integrate

The project owns:

- Deterministic Markdown parsing and exact source positions.
- Stable documents, immutable revisions, and citation resolution.
- Explicit records and relationships.
- Current-versus-historical evidence semantics.
- Benchmark fixtures, evaluation, and acceptance gates.

The project integrates rather than invents:

- Pretrained embedding and cross-encoder models.
- Vector indexing and nearest-neighbor search.
- Sparse/dense fusion algorithms.
- Model inference runtimes.

The implemented semantic retrieval path uses Sentence Transformers with two
fixed, permissively licensed local retrieval profiles, isolated vector
projections keyed by canonical anchor IDs, and FTS5 for exact lexical search.
The default `gte-modernbert` profile preserves the pinned
`Alibaba-NLP/gte-modernbert-base` behavior. The
`qwen3-embedding-0.6b` profile pins `Qwen/Qwen3-Embedding-0.6B`, applies its
official asymmetric query instruction, and leaves documents uninstructed.
Profile-specific databases prevent vector-space or ranking mixing while
allowing both projections to coexist. It fuses independent rankings with
reciprocal-rank fusion and reranks only a bounded candidate set.
`sqlite-vec` is the lowest-change experimental index; LanceDB is the preferred
embedded alternative if hybrid-search ergonomics or index maturity require a
separate derived store. Neither may replace canonical SQLite provenance.

The E2 fusion contract uses the unchanged E0 natural-BM25 and E1 dense
rankings. Each parent receives identical subject, source, and date filters and
a default candidate pool of 50, expanded when the caller requests more
results. Candidates are deduplicated by canonical passage ID and scored with
weighted reciprocal-rank fusion using `k = 20`, a lexical weight of `1.0`, and
a dense weight of `0.5`; equal scores are ordered by passage ID. These generic
defaults remain constructor parameters rather than corpus-specific rules.
Hybrid search requires a current E1 projection and fails explicitly when that
projection is missing, stale, or incompatible.

Projection identity includes the embedding profile, model and exact revision,
Sentence Transformers, Transformers, PyTorch, and sqlite-vec runtime versions,
actual local inference device and dtype, dimensions, L2 normalization,
source-text version, context behavior, query and document encoding behavior,
and canonical source fingerprint. Generated projections are disposable;
canonical evidence and immutable provenance remain in SQLite. CUDA and Apple
MPS may be used when available, but CPU remains a supported local fallback.

#### Experimental discipline

An opt-in contextual retrieval mode adds source titles and heading paths to
the text sent to the existing embedding and reranking models. The format is
`Title: <title>`, `Heading: <path joined by / >`, and the untouched quote,
separated by blank lines; absent metadata fields are omitted. It does not
change canonical anchors, lexical retrieval, queries, model profiles, or
fusion settings. Use `--contextual` for dense indexing and semantic search.

The `title-heading-passage-v1` source-text version participates in projection
identity and compatibility. Contextual projections have separate
`.contextual.sqlite3` paths; original `passage-text-v1` projections and default
behavior remain unchanged. Dense-index JSON reports the `contextual` mode.
The QASPER evaluator accepts the same flag and labels its output. This is an
implemented experiment, not a measured improvement or a passed acceptance gate.

Retrieval work proceeds one measurable feature at a time:

1. Complete an independent code review and resolve its high-confidence
   findings before measuring a feature's efficacy.
2. Dense retrieval over the existing anchors, with model substitutions
   evaluated as isolated profile experiments.
3. Sparse/dense fusion without changing either underlying retriever.
4. Cross-encoder reranking without changing candidate generation.
5. Answerability calibration without changing retrieval.
6. Agent-tool integration after retrieval and abstention have been measured,
   without treating a successful interface evaluation as evidence that failed
   retrieval or abstention gates have passed.

Each step must preserve the corpus selection, questions, gold evidence, metric
implementation, and prior configuration. Its aggregate result and delta must
be added to [`benchmarks/qasper/RESULTS.md`](benchmarks/qasper/RESULTS.md)
before another retrieval feature begins. Model substitutions, chunking
changes, fusion methods, and reranker substitutions are separate experiments
rather than bundled improvements.

#### Retrieval acceptance gates

On the pinned QASPER fixture:

- Dense plus hybrid retrieval: Recall@5 at least 60%, Recall@10 at least 75%,
  and MRR at least 0.45.
- Cross-encoder reranking: Recall@5 at least 70%, Recall@10 at least 80%, MRR
  at least 0.55, and evidence-set F1@10 at least twice the lexical baseline.
- Citation and anchor integrity must remain 100%.

Before describing the system as useful to an agent, an in-domain evaluation
set must demonstrate:

- Evidence Recall@10 of at least 85%.
- Direct-fact MRR of at least 0.70.
- Claim-level citation correctness of at least 95%.
- False evidence or answers on unsupported questions of at most 5%.
- Correct abstention or clarification on unsupported and ambiguous questions
  of at least 90%.
- Successful completion of at least 80% of representative agent tasks.

These thresholds are project acceptance gates, not claims of universal
retrieval quality.
