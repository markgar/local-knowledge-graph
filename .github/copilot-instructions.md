# Local Knowledge Graph

- `src/kg/` contains the Python package: `config.py` loads corpus manifests, `markdown/` parses source ranges, `ingest/` builds the SQLite index, `retrieval/` reads it, `models/` defines contracts, and `cli.py` exposes commands.
- `src/kg/schema.sql` defines the rebuildable SQLite schema.
- `tests/` contains unit, CLI, ingestion, acceptance, and benchmark tests.
- `corpora/` contains manifests and synthetic fixtures; `examples/` contains client usage; `benchmarks/` contains real-world evaluation tooling and results.
- Start with `README.md` for usage, `SPEC.md` for behavior and architecture, and `CONTRIBUTING.md` for development rules and validation commands.
- `CONTRACTS.md` is the shared technical reference for validation-only `foundation/1` values and pending service obligations. `ROADMAP.md` is the sole delivery plan; linked GitHub issues own execution detail. Do not describe planned targets as implemented behavior or create overlapping planning docs.
- Preserve deterministic behavior, immutable provenance, exact source anchors, corpus isolation, and generic configuration-driven logic.
