# Local Knowledge Graph

- `src/kg/` contains the Python package: `config.py` loads corpus manifests, `markdown/` parses source ranges, `ingest/` builds the SQLite index, `retrieval/` reads it, `models/` defines contracts, and `cli.py` exposes commands.
- `src/kg/schema.sql` defines the rebuildable SQLite schema.
- `tests/` contains unit, CLI, ingestion, acceptance, and benchmark tests.
- `corpora/` contains manifests and synthetic fixtures; `examples/` contains client usage; `benchmarks/` contains real-world evaluation tooling and results.
- Start with `README.md` for usage, `SPEC.md` for behavior and architecture, and `CONTRIBUTING.md` for development rules and validation commands.
- `CONTRACTS.md` documents implemented validation-only `foundation/1` values. Future work, shared service requirements and acceptance plans live in the [build roadmap issue](https://github.com/markgar/local-knowledge-graph/issues/28); linked package issues own designs and progress. Keep repo docs about existing behavior and executable assets, not future plans.
- Use the `work-package` skill in `.github/skills/work-package/SKILL.md` when preparing or resuming a package design. Follow `CONTRIBUTING.md`: write specs in session artifact storage outside the tracked repo, obtain independent critique and approval before coding, and publish the full approved spec and review/approval record as an ordinary comment on its package issue. Verify publication before implementation; update the same spec comment for later approved revisions. Do not block on the special issue-artifact feature.
- CI is manual-only and for code-affecting changes. Do not dispatch it or run the full Python suite for documentation/skill-text/instruction-only changes; check their diff and relevant links instead. Follow the classification in `CONTRIBUTING.md`.
- Preserve deterministic behavior, immutable provenance, exact source anchors, corpus isolation, and generic configuration-driven logic.
