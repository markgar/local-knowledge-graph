# Local Knowledge Graph

[![CI](https://github.com/markgar/local-knowledge-graph/actions/workflows/ci.yml/badge.svg)](https://github.com/markgar/local-knowledge-graph/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A deterministic, evidence-backed knowledge engine for configured Markdown
corpora. The project is intentionally local-first: corpus configuration is
data, SQLite is the durable index, and every returned record points to an
immutable source revision and exact quote.

Python 3.12 or newer is required. The supported CI matrix covers Python 3.12,
3.13, and 3.14. The project is pre-alpha: its core storage and retrieval
contracts are usable, but the complete MVP described in [`SPEC.md`](SPEC.md)
is still under development.

The initial scaffold implements the first vertical slice:

- Pydantic contracts for corpus manifests and JSON results.
- Deterministic Markdown anchors with character offsets.
- Versioned SQLite migrations and FTS5 search.
- Idempotent ingestion with immutable document revisions.
- Explicit Markdown task extraction.
- Thin Typer commands over reusable Python services.

It does not infer entities, relationships, decisions, or contradictions.
Decision extraction, structured owner/due-date parsing, conflict detection, and
two-hop traversal remain later MVP slices; their tables and public contracts
are present so those additions do not require a packaging or API redesign.

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
uv run kg actions Atlas --manifest corpora/example.yml --status open --format json
```

The example database is written to `.kg/example.sqlite3`, which is ignored by
Git.

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

See [`SPEC.md`](SPEC.md) for the product requirements and delivery milestones.
Contributions are welcome; see [`CONTRIBUTING.md`](CONTRIBUTING.md). Security
issues should be reported according to [`SECURITY.md`](SECURITY.md).

## License

Released under the [MIT License](LICENSE).
