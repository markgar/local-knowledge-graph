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
- An evidence-backed roadmap for dense retrieval, hybrid fusion, reranking,
  calibrated abstention, and agent-readiness acceptance gates.
- A versioned sqlite-vec projection and pinned local embedding model for dense
  retrieval over canonical source anchors.
- Deterministic reciprocal-rank fusion and pinned local cross-encoder
  reranking over unchanged canonical evidence candidates.
- Two isolated local embedding profiles for the existing pinned GTE
  ModernBERT model and pinned Qwen3-Embedding-0.6B, including profile-specific
  projections, asymmetric Qwen query encoding, runtime compatibility checks,
  CLI and QASPER selection, and a direct measured comparison.
- Paper-grouped E4 answerability calibration over unchanged E3 reranker
  scores, including out-of-fold selective metrics and a documented negative
  result.
- A versioned agent-facing JSON CLI with immutable source-range reads,
  revision listing and comparison, plus a reviewed seven-workflow E5
  evaluation.
