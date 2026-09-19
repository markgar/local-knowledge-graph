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

### Revisions

```bash
kg revisions <source-path> --manifest <corpus.yml> --format json
kg compare-revisions <source-path> --manifest <corpus.yml> --format json
```

Lists immutable document revisions and compares exact added, removed,
modified, and unchanged source ranges. The comparison defaults to the current
revision and its immediate predecessor; callers may supply explicit `--from`
and `--to` revision IDs.

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
  - FTS5 index over titles, headings, aliases, and passage text.

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

Retrieval work proceeds one measurable feature at a time:

1. Complete an independent code review and resolve its high-confidence
   findings before measuring a feature's efficacy.
2. Dense retrieval over the existing anchors, with model substitutions
   evaluated as isolated profile experiments.
3. Sparse/dense fusion without changing either underlying retriever.
4. Cross-encoder reranking without changing candidate generation.
5. Answerability calibration without changing retrieval.
6. Agent-tool integration after retrieval and abstention pass their gates.

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
