# Local Knowledge Graph

[![CI](https://github.com/markgar/local-knowledge-graph/actions/workflows/ci.yml/badge.svg)](https://github.com/markgar/local-knowledge-graph/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A deterministic, evidence-backed knowledge engine for configured Markdown
corpora. The project is intentionally local-first: corpus configuration is
data, SQLite is the durable index, and every returned record points to an
immutable source revision and exact quote.

Python 3.12 or newer is required. The supported CI matrix covers Python 3.12,
3.13, and 3.14. The deterministic evidence and provenance MVP described in
[`SPEC.md`](SPEC.md) is implemented. Dense retrieval, hybrid fusion, and local
cross-encoder reranking are measured; answerability remains an active
development area, so the project is pre-alpha.

The engine provides:

- Pydantic contracts for corpus manifests and JSON results.
- CommonMark block parsing with exact source ranges.
- A rebuildable SQLite schema and FTS5 search.
- Idempotent ingestion with immutable document revisions.
- Stable document identity across unambiguous file moves.
- Explicit tasks, owners, due dates, decisions, blockers, and conflicts.
- Approved entity aliases, exact mentions, and bounded two-hop relationships.
- Current-state retrieval without losing historical evidence.
- Thin Typer commands over reusable Python services.

Lexical ranking statistics are isolated to the active revisions of each
corpus. Other corpora, removed sources, and historical revisions cannot
change that corpus's BM25 scores. Historical citations retain revision-bound
titles. Restoring older content reuses its immutable revision but records a
new activation, so `compare-revisions` compares against the state immediately
before the restore rather than the revision's original creation time.

It does not use an LLM or infer entities, relationships, decisions, blockers,
or contradictions. Those records must be explicit in source structure.

## Install from source

```bash
git clone https://github.com/markgar/local-knowledge-graph.git
cd local-knowledge-graph
uv sync --extra dev
```

This creates a project-local `.venv`. Run commands through `uv run` or activate
the environment directly.

The checked-in [`uv.toml`](uv.toml) routes Python dependencies through
Microsoft's package feed because direct package downloads from public PyPI are
blocked in the primary development environment. This configuration contains
no credentials. Contributors outside that environment should replace
`index-url` with their approved PEP 503 package index or remove `uv.toml` to
use uv's default public index.

## Quick start

```bash
uv run kg ingest --manifest corpora/example.yml
uv run kg dense-index --manifest corpora/example.yml
uv run kg dense-index --manifest corpora/example.yml --embedding-profile qwen3-embedding-0.6b
uv run kg search "release" --manifest corpora/example.yml --format json
uv run kg search "What supports the release?" --manifest corpora/example.yml --query-mode natural
uv run kg search "What supports the release?" --manifest corpora/example.yml --query-mode dense
uv run kg search "What supports the release?" --manifest corpora/example.yml --query-mode hybrid --embedding-profile qwen3-embedding-0.6b
uv run kg search "What supports the release?" --manifest corpora/example.yml --query-mode reranked --embedding-profile qwen3-embedding-0.6b
uv run kg actions Atlas --manifest corpora/example.yml --status open --format json
uv run kg status Atlas --manifest corpora/example.yml --format json
```

The example database is written to `.kg/example.sqlite3`, which is ignored by
Git.

### Inspect an ingestion

```bash
uv run kg ingest --manifest corpora/example.yml --explain
uv run kg ingest --manifest corpora/example.yml --explain --include-quotes --format json
```

`--explain` reports each document's outcome and why it happened: new content,
an unchanged revision, a move, restored content, reactivation, a parser/config
rebuild, failure, or removal from the selected sources. This **runs ingestion**;
it is not a dry run. To walk through sources one at a time, add each file to a
separate test corpus before running the command again.

The report separates configured entities from entities actually mentioned,
lists stored counts and source anchors, and explains explicit relationships,
actions, decisions, blockers, and conflicts. Every listed passage or record has
an evidence ID. Counts describe the stored revision, **not rows created during
this run**; `records_rebuilt` distinguishes extraction from reuse. Unchanged
documents still show their existing evidence.

Quotes are omitted unless `--include-quotes` is supplied with `--explain`.
Reports still contain source paths, headings, entity names, owners, and dates;
they are not anonymized. Up to 50 indexed anchors and 50 structured records are
shown per document; `--explain-limit 200` raises each limit. Truncation is
explicit and counts remain complete. Use `kg evidence <record-id>` or
`kg source-range <anchor-id>` with the same manifest to inspect exact evidence.

Text output is for inspection; `--format json` returns the versioned
`IngestReport` contract. Ordinary ingestion output remains unchanged. Reports
are generated from the ingestion transaction and are not persisted in
`ingest_run`; that table retains only the small run summary. Operational logs
stay on stderr (`kg --verbose ingest ...`), without report bodies or quotes.
Malformed YAML errors report locations without echoing source snippets.

### Atlas incremental scenario

[`corpora/atlas.yml`](corpora/atlas.yml) indexes ten fictional work documents:
planning notes, email exports, a security review, an audit-system blocker,
technical notes, a changed launch decision, a calendar discrepancy, a completion
email, an agenda, and a similarly named unrelated project.

```bash
uv run pytest tests/test_atlas_walkthrough.py -v
uv run kg ingest --manifest corpora/atlas.yml --explain
uv run kg status Atlas --manifest corpora/atlas.yml --format json
uv run kg search "archive signing certificate" --subject Atlas --manifest corpora/atlas.yml
```

The staged tests add one document at a time to an isolated database, following
the declared expectations in
[`atlas-stages.yml`](corpora/fixtures/atlas-stages.yml). They check extraction,
exact citations, two-hop retrieval, unchanged records, project isolation,
and a subsequent task edit and revert. The normal ingest command above loads
all ten documents into `.kg/atlas.sqlite3`; the fixture sources remain unchanged.

Atlas connects to Security Review, which connects to Audit Trail. This lets
retrieval find the audit certificate blocker without adding "Atlas" to that
document. Atlascope remains a separate project: subject filtering uses the same
case-insensitive name boundaries as entity mentions, not substring matching.
Numbered checklists work alongside bulleted checklists.

The negative cases are intentional: prose promises do not create tasks,
completion emails do not close checkboxes in other documents, and a new launch
date does not automatically supersede other sources. Both dated accounts stay
retrievable; conflicts require an explicit structural record. This is a
synthetic correctness scenario, not a benchmark of agent reasoning or natural
language extraction quality.

### Explicit cross-document updates

Use a corpus-scoped, case-sensitive key to identify an explicit task or
decision, then name that key on a replacement record in another document:

```markdown
- [ ] Confirm the review date. [owner:: Avery] [key:: review-v1]
```

```markdown
- [x] Confirmed the review date. [owner:: Avery] [key:: review-v2] [supersedes:: review-v1]
```

The same fields work on records under `## Decisions`. A replacement can reopen
a task or revise a decision; it must have the same record kind as its target.
Keys identify individual record versions, not a shared mutable task ID.
Give each replacement its own key if another update will refer to it.
Replacements are full records, not field patches: owner and due-date metadata
must be stated again if they should appear on the replacement.
Keys allow 1-128 ASCII letters, digits, dots, underscores, colons, and hyphens,
starting with a letter or digit. A record can supersede one target.

`actions`, `decisions`, and the structured sections of `status` return effective
records. Replacements inherit their predecessors' subject association even
without repeating a project name. Original sources, exact citations, and
revision histories remain untouched. Search and `status.recent_material`
continue to expose source evidence, including superseded assertions; neither
is a list of current facts.

No timestamp or prose-based winner is inferred. Missing or duplicate keys,
cross-kind links, self-references, cycles, and competing replacements produce
diagnostics rather than hiding an arbitrary record. Forward references resolve
after the target is ingested. Only active current source revisions participate;
editing or removing an update removes its prior effect. This is not an
as-of-time query and does not resolve blockers or conflicts automatically.

```bash
uv run kg ingest --manifest corpora/atlas-state.yml --explain --format json
uv run kg record-state --manifest corpora/atlas-state.yml --format json
uv run kg status Atlas --manifest corpora/atlas-state.yml --format json
```

The separate twelve-document fixture exercises explicit completion and
rescheduling while the ten-document baseline retains prose-only negative cases.

The telemetry is produced by the same resolver used for retrieval:
`ingest --explain` includes record keys and a post-ingestion state snapshot;
`record-state` audits the current snapshot without ingesting. It reports source
and target evidence IDs, resolution outcomes, ambiguous candidates, competing
sources, effective replacement IDs, and exactly which records are suppressed.
Limits truncate displayed links explicitly, never the resolution calculation.
Quotes require `--include-quotes`. Diagnostics still contain paths and keys.
Plain ingestion adds `state_warnings` only when references are unresolved;
sources remain indexed and exit status still reflects source-ingestion failures.
`status.evidence_gaps` also surfaces corpus-wide state warnings.

### Query telemetry

```bash
uv run kg search "archive signing certificate" --subject Atlas --manifest corpora/atlas-state.yml --explain --format json
```

`search --explain` exposes the actual strict/natural lexical expression,
filters, corpus fingerprint, BM25 ranks, and the predicates that admitted each
hit. Subject traces distinguish direct mentions, title/heading matches,
document-level inheritance, and bounded graph expansion, with supporting edge
IDs and hop counts. This is diagnostic telemetry, not a confidence score or an
agent-generated explanation.

Ordinary search output is unchanged. Explain output omits source quotes unless
`--include-quotes` is supplied; paths, names, headings, and query text remain
visible. Explain currently supports strict/natural lexical modes, not semantic
modes. It rejects concurrent database commits rather than combining results
and explanations from inconsistent states. The trace explicitly records that
supersession does not filter source search; use structured status/actions and
`record-state` to inspect effective record state.

## Corpus manifests

A corpus is configured entirely through YAML:

```yaml
corpus_id: example
display_name: Example corpus
vault_root: notes
database: ../.kg/example.sqlite3
include:
  - "**/*.md"
allow_symlinks: false
max_source_bytes: 5000000
seed_entities:
  - entity_id: example-project
    name: Example Project
    entity_type: project
    aliases:
      - Example
metadata_fields:
  event_time: date
```

Paths are resolved relative to the manifest. Symlinks are rejected by default,
and sources larger than `max_source_bytes` fail ingestion explicitly.

## Explicit Markdown conventions

The indexer recognizes only evidence that is present in the Markdown:

```markdown
## Actions

- [ ] Review the release. [owner:: Avery] [due:: 2026-10-01]

## Decisions

- Use SQLite for the local index.

## Blockers

- Approval is still pending.

## Conflicts

- The two cited sources report different dates.
```

Checkboxes in bulleted or numbered lists determine task status, including
`1. [ ]` and `1) [x]`. Optional `[owner:: ...]` and `[due:: ...]`
fields provide task metadata. Items and paragraphs under exact `Decision`,
`Decisions`, `Blocker`, `Blockers`, `Conflict`, or `Conflicts` headings become
explicit records. Wikilinks and approved aliases create exact mentions and
cited relationships; similar names are never merged automatically.

## CLI

| Command | Purpose |
| --- | --- |
| `kg ingest` | Validate a manifest and update immutable revisions and current indexes. |
| `kg capabilities` | Describe the versioned JSON CLI operations available to agents. |
| `kg dense-index` | Build a profile-specific, versioned sqlite-vec projection with pinned local embeddings. |
| `kg status` | Return cited material, decisions, actions, blockers, relationships, conflicts, and evidence gaps. |
| `kg record-state` | Audit applied and unresolved explicit record replacements and their evidence. |
| `kg actions` | Return explicit open or completed tasks with owners and due dates. |
| `kg evidence` | Resolve any returned record ID to its exact source anchor. |
| `kg source-range` | Read an immutable source anchor with exact offsets and quote hash. |
| `kg source-context` | Expand a hit into its containing section's anchored evidence in the same revision. |
| `kg revisions` | List the immutable revision history for a source document. |
| `kg compare-revisions` | Compare added, removed, modified, and unchanged source ranges. |
| `kg search` | Search current passages with FTS5. |

`search` and `actions` support `--source` and `--since`; `status` supports an
optional `--since` and otherwise considers all dated evidence. Search defaults
to strict all-term matching; `--query-mode natural`
uses safely quoted any-term matching with BM25 ranking, `--query-mode dense`
uses the local versioned embedding projection, and `--query-mode hybrid`
combines their unchanged rankings with deterministic reciprocal-rank fusion.
`--query-mode reranked` scores the unchanged top 50 hybrid candidates with a
pinned local cross-encoder before returning the requested result count.
Durations use forms such as `12h`, `30d`, or `4w`.

Dense indexing uses the pinned Apache-2.0
`Alibaba-NLP/gte-modernbert-base` revision through Sentence Transformers. The
GTE profile remains the default and preserves its existing `.dense.sqlite3`
projection path and encoding behavior. The second fixed profile is the
Apache-2.0 `Qwen/Qwen3-Embedding-0.6B` revision
`97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`. It uses 1,024-dimensional,
L2-normalized embeddings, the model's 32K context behavior, the official
`query` prompt
`Instruct: Given a web search query, retrieve relevant passages that answer the query`
on queries, and no instruction on documents.

Select either profile with `--embedding-profile gte-modernbert` or
`--embedding-profile qwen3-embedding-0.6b` on `dense-index` and dense, hybrid,
or reranked `search`. Both profile projections remain on disk simultaneously;
switching does not rebuild an existing compatible projection. Sentence
Transformers uses the best available local PyTorch backend, such as CUDA, MPS,
or CPU. The actual device and tensor dtype are part of projection
compatibility, along with the profile, model revision, runtime versions,
dimensions, normalization, context and encoding behavior, source-text version,
and canonical corpus fingerprint.

Models are downloaded on first use. Embeddings are stored in disposable
profile-specific sqlite-vec databases beside the canonical corpus database;
every vector remains keyed to its exact passage, anchor, and source revision.
No corpus text is sent to an API.

### Context-aware retrieval (opt-in)

Use `--contextual` on both `dense-index` and dense, hybrid, or reranked
`search` to include the document title and full heading path alongside each
passage when embedding and reranking. The query, lexical ranking, model
profiles, and canonical quotes remain unchanged. Context is deterministic
source metadata, not an LLM-generated summary.

```bash
uv run kg dense-index --manifest corpora/example.yml --contextual
uv run kg search "What is holding up approval?" --manifest corpora/example.yml --query-mode reranked --contextual --format json
uv run kg source-context <anchor-id> --manifest corpora/example.yml --format json
```

Contextual projections use separate `.contextual.sqlite3` files and a distinct
source-text version, so they coexist with the original profile indexes.
Commands without `--contextual` retain the passage-only semantic representation.
Passing the flag to strict or natural search is an explicit argument error.
The new mode is experimental; the recorded QASPER results do not measure it.

`source-context` returns the selected exact range, its nearest containing
heading, and source-ordered anchors in that section, including subsections.
The next sibling or ancestor heading ends the section; repeated heading names
do not merge sections. Before the first heading, only the document preamble is
returned. Headingless documents form one section.

Output is bounded to 50 anchors by default (`--max-anchors 1..200`), centered
around the selected hit when necessary. `total_anchors` and `truncated` expose
omitted context; the selected hit is always included and the section heading
is returned separately even when outside the window. Each anchor retains its
own quote, offsets, hash, and revision. These are parsed evidence blocks, not
a reconstructed full section: unindexed blocks such as fenced code are absent,
and nested list anchors may overlap. Historical hits expand within their
original stored revision, without reading the current source file.

With `--format json`, output follows the Pydantic contracts in `kg.models`.
Manifest, argument, query, and lookup failures are emitted to stderr as:

```json
{"error":"invalid_manifest","message":"Could not read manifest ..."}
```

Use `--verbose` for schema and ingestion diagnostics.

## External client

[`examples/cited_status.py`](examples/cited_status.py) invokes the CLI as a
subprocess, handles structured errors, and renders a short report with source,
heading, and revision citations:

```bash
uv run python examples/cited_status.py corpora/example.yml Atlas
```

## Development

```bash
uv run pytest
uv run ruff check .
uv run mypy
uv build
```

## Package boundaries

- `config.py` owns manifest validation and source discovery.
- `markdown/` converts source bytes into positioned structural records.
- `ingest/` coordinates revisions, anchors, passages, and explicit tasks.
- `retrieval/` performs database reads and maps rows to public contracts.
- `db.py` owns connections, transactions, and schema initialization.
- `lexical.py` maintains corpus-local current-state FTS5 projections and their
  canonical fingerprints.
- `cli.py` only parses command-line options and renders service results.

The project is pre-alpha and indexes are rebuildable from source. Update
`src/kg/schema.sql` directly for schema changes, then rebuild generated
databases. See [`SPEC.md`](SPEC.md) for the product contract and implementation
ledger.

### Updating existing indexes

Run `kg ingest --manifest <corpus.yml>` for each existing corpus after upgrading.
Parser version 5 additionally recognizes numbered checklists and normalizes
numbered explicit-record summaries. Version 4 repaired task-field ownership,
code-block exclusion, and CommonMark newline mapping for current sources.
Ingestion also creates the
corpus-local lexical projection; legacy indexes without it return an explicit
search error until ingestion completes. Rebuild each dense projection you use
with `kg dense-index` (including `--contextual` when applicable) after
reingestion invalidates its fingerprint.

Parser version 6 and ingestion schema version 4 add explicit record bindings.
Reingest existing corpora to populate them. Unannotated documents keep their
existing structured-record behavior.

The activation log starts with the currently observed state of an upgraded
document. Earlier transitions cannot be recovered from content hashes alone.
Comparisons without a recorded predecessor require explicit `--from` and
`--to`; the system does not guess a historical transition order. New edits
and reverts record their predecessors automatically.

Existing historical source anchors are not rewritten by the parser upgrade.
If an older index contains malformed anchors from the previous parser, build
a fresh database from available source files. Keep the old database separately
if its historical revisions are needed: rebuilding from current files cannot
recreate past document contents or past activation history.

Contributions are welcome; see [`CONTRIBUTING.md`](CONTRIBUTING.md). Security
issues should be reported according to [`SECURITY.md`](SECURITY.md).

## Acceptance corpora

`corpora/acceptance/` contains reviewed questions and exact expected evidence
for two unrelated synthetic corpora. Tests also verify abstention, immutable
history, rebuild equivalence, corpus isolation, source moves, failed-source
deactivation, and manifest-driven reindexing.

## Real-world benchmark

[`benchmarks/qasper/`](benchmarks/qasper/) provides a reproducible evaluation
against 50 real scientific papers and 179 human-authored QASPER questions with
gold supporting paragraphs and unanswerable cases. The repository includes the
downloader, deterministic paper IDs, Markdown converter, evaluator, and
aggregate baseline results without redistributing the source papers.

[`benchmarks/qasper/RESULTS.md`](benchmarks/qasper/RESULTS.md) preserves the
baseline and the completed experiment history one feature at a time, including
each metric delta, configuration, latency, and index cost.

The benchmark currently shows that the evidence substrate is reliable but the
retrieval layer is not yet agent-ready:

| Metric | Current natural-search baseline |
| --- | ---: |
| Evidence Recall@1 | 12.2% |
| Evidence Recall@5 | 40.2% |
| Evidence Recall@10 | 54.6% |
| Mean reciprocal rank | 0.302 |
| Unanswerable false-evidence rate | 100.0% |
| Anchor integrity | 100.0% |

The first dense-only experiment retained 100% anchor integrity but
underperformed natural BM25, reaching 29.1% Recall@5, 42.7% Recall@10, and
0.264 MRR. It is preserved as a measured negative result and is combined with
natural BM25 by the E2 hybrid-fusion implementation.

E2 improves the same fixture to 41.6% Recall@5, 60.9% Recall@10, and 0.334 MRR
while retaining 100% anchor integrity. It remains below the agent-useful
acceptance gates and has not yet been validated on an email- and
meeting-notes-style evaluation corpus.

E3 reranks the unchanged top 50 E2 candidates with a pinned local
cross-encoder, reaching 58.5% Recall@5, 72.2% Recall@10, and 0.446 MRR while
retaining deterministic rankings and 100% anchor integrity. This is a material
improvement but remains below the reranked acceptance gates.

A controlled GTE-versus-Qwen substitution kept all retrieval architecture and
benchmark inputs unchanged. Qwen improved dense Recall@10 from 42.7% to 64.2%
and hybrid Recall@5 from 41.6% to 46.6%, but the unchanged reranker produced
essentially the same final quality: 58.5% Recall@5 for both profiles, with
Qwen at 71.6% rather than 72.2% Recall@10. Qwen also required about four times
the model cache, 3.6 times the indexing time, and a 32.8% larger vector index.
GTE therefore remains the compatibility default. QASPER is one scientific
paper regression dataset, not the sole basis for selecting a general-purpose
embedding model.

The project should therefore be understood as an experimental evidence index,
not a production question-answering system.

## Agent CLI

E5 uses the local CLI and machine-readable JSON as the agent integration
boundary. Run `kg capabilities --format json` to discover the versioned
operations. Search results can be followed through `kg evidence` and
`kg source-range`; `kg revisions` and `kg compare-revisions` expose immutable
history without requiring an agent SDK or network service.

The reviewed workflow benchmark in [`benchmarks/agent/`](benchmarks/agent/)
covers paraphrased retrieval, multi-document status, explicit conflicts,
ambiguous cross-source results, unsupported subjects, citation round-trips,
and revision comparison. MCP can be added later as a transport adapter over
the same service contracts.

The separate [`work-memory comparison`](benchmarks/work_memory/README.md)
evaluates actual agent answers against a plain-Markdown baseline. It freezes
twelve source documents and ten questions before running two fresh agents:
one using read-only KG commands, one using Markdown search/batch reads.
Predeclared gold checks current facts, abstention, and exact supporting
citations. Tool journals measure actual calls, returned bytes, and tool time;
natural-language claims receive a separate review. This small synthetic pilot
does not establish general superiority over Markdown or replace QASPER.

## Roadmap

The next phase keeps SQLite as the canonical evidence store while adding
replaceable retrieval projections:

1. Improve answerability beyond the rejected E4 top-score threshold.
2. Expand agent evaluation beyond the initial stable JSON CLI workflows.

The project will integrate established embedding models, vector indexes, and
rerankers rather than inventing them. Its responsibility remains immutable
provenance, exact citations, explicit records, deterministic rebuilding, and
measured retrieval quality. Detailed acceptance gates are recorded in
[`SPEC.md`](SPEC.md#milestone-5-agent-useful-retrieval).

The first candidates are
[Sentence Transformers](https://www.sbert.net/) with a permissively licensed
retrieval model, [sqlite-vec](https://github.com/asg017/sqlite-vec) for a
minimum-change local experiment, or
[LanceDB](https://github.com/lancedb/lancedb) if the derived retrieval index
needs stronger hybrid-search support. These components remain replaceable;
canonical evidence stays in SQLite.

## License

Released under the [MIT License](LICENSE).
