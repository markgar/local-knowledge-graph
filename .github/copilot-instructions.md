# Local Knowledge Graph

- `src/kg/` contains the Python package: `config.py` loads corpus manifests, `markdown/` parses source ranges, `ingest/` builds the SQLite index, `retrieval/` reads it, `models/` defines contracts, and `cli.py` exposes commands.
- `src/kg/schema.sql` defines the rebuildable SQLite schema.
- `tests/` contains unit, CLI, ingestion, acceptance, and benchmark tests.
- `corpora/` contains manifests and synthetic fixtures; `examples/` contains client usage; `benchmarks/` contains real-world evaluation tooling and results.
- Start with `README.md` for usage, `SPEC.md` for behavior and architecture, and `CONTRIBUTING.md` for development rules and validation commands.
- `CONTRACTS.md` documents implemented validation-only `foundation/1` values. Future work, shared service requirements and acceptance plans live in the [build roadmap issue](https://github.com/markgar/local-knowledge-graph/issues/28); linked package issues own designs and progress. Keep repo docs about existing behavior and executable assets, not future plans.
- Preserve deterministic behavior, immutable provenance, exact source anchors, corpus isolation, and generic configuration-driven logic.
