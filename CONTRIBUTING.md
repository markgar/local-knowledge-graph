# Contributing

Thanks for helping improve Local Knowledge Graph.

## Development setup

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/) are required.

```bash
git clone https://github.com/markgar/local-knowledge-graph.git
cd local-knowledge-graph
uv sync --extra dev
```

Dependency resolution uses the repository's [`uv.toml`](uv.toml). It points to
the package feed required by the primary Microsoft development environment and
contains no credentials. If that feed is unavailable in your environment,
replace `index-url` with your approved PEP 503 index or remove the file to use
uv's default public index.

Before opening a pull request, run:

```bash
uv run pytest
uv run ruff check .
uv run mypy
uv build
```

## Pull requests

- Keep changes focused and include tests for behavior changes.
- Preserve deterministic behavior and exact source provenance.
- Do not add corpus-specific parsing or retrieval rules.
- Update public documentation when contracts or commands change.
- Update `src/kg/schema.sql` directly for database changes; indexes are
  rebuildable from source during pre-alpha development.
- Add or update a reviewed case in `corpora/acceptance/` for retrieval changes.

For larger changes, open an issue first so the design can be discussed before
implementation.
