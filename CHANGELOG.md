# Changelog

All notable changes to this project will be documented here.

The project follows [Semantic Versioning](https://semver.org/) after its first
stable release. Pre-1.0 releases may contain breaking changes.

## Unreleased

### Added

- Initial Python package, SQLite schema, Markdown ingestion, FTS5 retrieval,
  evidence lookup, task queries, and CLI.
- Example corpus, automated tests, and Python 3.12-3.14 CI.
- CommonMark source anchoring, structured task fields, explicit decisions,
  blockers, conflicts, and cited two-hop relationships.
- A second synthetic corpus, reviewed acceptance cases, rebuild equivalence,
  stable move detection, and an external cited-status client.
- Machine-readable JSON errors and verbose operational diagnostics.
- A reproducible QASPER benchmark fixture with real scientific papers, gold
  evidence evaluation, and a documented lexical-retrieval baseline.
- A natural-language lexical search mode using safe FTS5 term expansion and
  BM25 ranking.
