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

Checkboxes determine task status. Optional `[owner:: ...]` and `[due:: ...]`
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
| `kg actions` | Return explicit open or completed tasks with owners and due dates. |
| `kg evidence` | Resolve any returned record ID to its exact source anchor. |
| `kg source-range` | Read an immutable source anchor with exact offsets and quote hash. |
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
- `cli.py` only parses command-line options and renders service results.

The project is pre-alpha and indexes are rebuildable from source. Update
`src/kg/schema.sql` directly for schema changes, then rebuild generated
databases. See [`SPEC.md`](SPEC.md) for the product contract and implementation
ledger.
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
