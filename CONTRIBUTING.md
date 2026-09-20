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
use a package index approved for your environment. Never change feeds or bypass
organizational controls to evade a blocked download. Report the blocked host and
use cached/local or otherwise approved dependencies and models.

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
  Do not rewrite authored gold to match a new ranking default: lexical expected
  lists remain low-level component assertions. Add separately scoped product
  search cases for the full pipeline.

## Search and evaluation boundaries

Use `kg.retrieval.SearchService` and unqualified `kg search` for product search.
Prepare a matching `dense-index` with the same embedding profile/contextual
configuration; no stage may be bypassed or silently degraded. Use
`RetrievalService` for model-independent structured/evidence reads.

Routine tests inject controlled providers at the existing embedding/reranker
factory seams. Do not download or run real models for ordinary unit/CLI tests.
For a CLI change, first run:

```bash
uv run pytest tests/test_cli.py tests/test_product_cli.py tests/test_agent_cli.py tests/test_acceptance.py tests/test_atlas_walkthrough.py tests/test_search_explain.py tests/test_source_context.py tests/test_client_example.py tests/test_agent_benchmark.py
```

Then run the full validation commands above. Version-2 search reports must
serialize without `exclude_none=True`: missing stage memberships/contributions
are explicit nulls, while the report serializer handles quote opt-in.

Historical evaluation explicitly selects low-level components. Agent/work-memory
lexical runs use the private `benchmarks/_lexical_search.py` worker; QASPER selects
its component strategy directly. Do not route frozen lexical benchmarks through
the product facade, change their defaults, or overwrite historical results.
Preserve source snapshots, dates, question/tool restrictions, citations and gold.
Record new experiments under distinct labels; there is no E-stage prerequisite
for product development, and unmet quality gates must not be relabeled as passed.

The separate [productization matrix](benchmarks/productization/README.md) documents
real-model parity checks, approved cache usage, and durable result requirements.
Controlled-provider success cannot substitute for required real-model acceptance.
The [final integrated record](benchmarks/productization/final-integrated-2026-09-20/README.md)
documents acceptance after combined validation and review; later changes still
require applicable regression and real-model checks.

For larger changes, open an issue first so the design can be discussed before
implementation.
