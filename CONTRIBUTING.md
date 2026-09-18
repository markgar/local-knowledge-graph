# Contributing

Thanks for helping improve Local Knowledge Graph.

## Development setup

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/) are required.

```bash
git clone https://github.com/markgar/local-knowledge-graph.git
cd local-knowledge-graph
uv sync --extra dev
```

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
- Describe compatibility or migration implications for schema changes.
- Add database changes as a new numbered SQL migration. Never edit a migration
  that has shipped on `main`.
- Add or update a reviewed case in `corpora/acceptance/` for retrieval changes.

For larger changes, open an issue first so the design can be discussed before
implementation.
