# Local Knowledge Graph

[![CI](https://github.com/markgar/local-knowledge-graph/actions/workflows/ci.yml/badge.svg)](https://github.com/markgar/local-knowledge-graph/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A deterministic, evidence-backed knowledge engine for configured Markdown
corpora. The project is intentionally local-first: corpus configuration is
data, SQLite is the durable index, and every returned record points to an
immutable source revision and exact quote.

Python 3.12 or newer is required. The supported CI matrix covers Python 3.12,
3.13, and 3.14. The Version 1 MVP described in [`SPEC.md`](SPEC.md) is implemented and covered
by synthetic acceptance corpora. The project remains pre-alpha while its
interfaces receive broader real-world testing.

The engine provides:

- Pydantic contracts for corpus manifests and JSON results.
- CommonMark block parsing with exact source ranges.
- Versioned SQLite migrations and FTS5 search.
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

## Quick start

```bash
uv run kg ingest --manifest corpora/example.yml
uv run kg search "release" --manifest corpora/example.yml --format json
uv run kg search "What supports the release?" --manifest corpora/example.yml --query-mode natural
uv run kg actions Atlas --manifest corpora/example.yml --status open --format json
uv run kg status Atlas --manifest corpora/example.yml --since 30d --format json
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
| `kg status` | Return cited material, decisions, actions, blockers, relationships, conflicts, and evidence gaps. |
| `kg actions` | Return explicit open or completed tasks with owners and due dates. |
| `kg evidence` | Resolve any returned record ID to its exact source anchor. |
| `kg search` | Search current passages with FTS5. |

`search` and `actions` support `--source` and `--since`; `status` supports
`--since`. Search defaults to strict all-term matching; `--query-mode natural`
uses safely quoted any-term matching with BM25 ranking for natural-language
questions. Durations use forms such as `12h`, `30d`, or `4w`.

With `--format json`, output follows the Pydantic contracts in `kg.models`.
Manifest, argument, query, and lookup failures are emitted to stderr as:

```json
{"error":"invalid_manifest","message":"Could not read manifest ..."}
```

Use `--verbose` for migration and ingestion diagnostics.

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
- `db.py` owns connections, transactions, and migration application.
- `cli.py` only parses command-line options and renders service results.

Schema migrations are append-only SQL files. Released migrations must never be
edited; compatibility changes require a new numbered migration. See
[`SPEC.md`](SPEC.md) for the product contract and implementation ledger.
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

## License

Released under the [MIT License](LICENSE).
