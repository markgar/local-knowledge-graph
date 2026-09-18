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

This document is both the Version 1 product contract and a record of current
implementation progress. Statuses describe the repository as of 2026-09-17:

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
| SQLite schema and atomic migrations | Implemented | Versioned SQL migrations are packaged with the Python distribution. |
| FTS5 passage search | Implemented | Queries are corpus-scoped and limited to current active revisions, with strict and natural BM25-ranked modes. |
| Seed entities, approved aliases, and exact mentions | Implemented | Similar names are never merged automatically. |
| Explicit relationships and graph traversal | Implemented | Anchored wikilink relationships support deterministic one- and two-hop traversal. |
| Explicit open and completed tasks | Implemented | Checkbox status and optional inline owner and due-date fields are extracted. |
| Event-date filtering | Implemented | Configured document dates and revision timestamps support bounded retrieval. |
| `ingest`, `status`, `actions`, `evidence`, and `search` CLI | Implemented | Commands expose text and JSON output over reusable services. |
| Decisions, blockers, and conflicts | Implemented | Explicit structural sections produce cited records; contradiction inference remains a non-goal. |
| Evidence gaps and abstention | Implemented | Unsupported subjects return explicit evidence gaps without model completion. |
| Reviewed acceptance datasets | Implemented | Two synthetic corpora define expected actions, decisions, blockers, relationships, citations, and abstention. |
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
kg status <subject> --since 30d --format json
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
kg actions <subject> --status open --format json
```

Returns explicit tasks associated with the workstream, including owner, due
date, status, source, and evidence. Tasks with no explicit owner must remain
unassigned.

### Evidence

```bash
kg evidence <record-id> --format json
```

Returns every source anchor supporting the selected record, including exact
quotes and Obsidian-openable paths.

### Search

```bash
kg search <query> --subject <subject> --format json
```

Returns ranked source passages using FTS5. Vector search is not part of the
MVP.

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
- `schema_migration`
  - Applied schema versions.

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
- Plain versioned SQL migration files.

Organize the code as a reusable package:

```text
src/kg/
  cli.py
  config.py
  db.py
  migrations/
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

## Definition of Done

The MVP is complete when:

1. A corpus is added entirely through a manifest and source files.
2. Re-running ingestion without source changes produces zero data changes.
3. Editing one source creates a new revision without destroying prior evidence.
4. Every returned task, decision, relationship, and status item has an exact
   source anchor.
5. A reviewed pilot question set returns the expected evidence with useful
   retrieval recall.
6. Unsupported questions clearly report insufficient evidence.
7. The database can be deleted and rebuilt from the configured sources with
   equivalent logical results.
8. An external client can invoke the CLI and present a cited answer from its
   JSON output.
9. A second synthetic corpus works without Python or schema changes.

## Explicit Non-Goals

The MVP does not include:

- A standalone web application.
- A general-purpose knowledge graph for the full vault.
- Hard-coded topics, workstreams, people, products, or source paths.
- Embeddings or `sqlite-vec`.
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

- [x] Create atomic migrations and the SQLite schema.
- [x] Parse notes into immutable revisions and exact anchors.
- [x] Index headings, passages, wikilinks, exact mentions, and checkbox tasks.
- [x] Prove idempotent ingestion and preservation of historical revisions.
- [x] Parse CommonMark blocks while preserving exact source positions.
- [x] Extract structured owners, due dates, decisions, blockers, conflicts, and
  configured event metadata.

### Milestone 3: Useful retrieval

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
