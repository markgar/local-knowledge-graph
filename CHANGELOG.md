# Changelog

All notable changes to this project will be documented here.

The project follows [Semantic Versioning](https://semver.org/) after its first
stable release. Pre-1.0 releases may contain breaking changes.

## Unreleased

### Canonical engine and graph projection

- Canonical `evidence-store/3` SQLite storage owns exact supplied text, immutable
  revisions, trusted local policy, identities, registered knowledge schema,
  entities/assertions, support and history. Python evidence, knowledge, indexing,
  processing-control, query and diagnostic services expose the operations in
  [CONTRACTS.md](CONTRACTS.md). The Markdown demonstration/CLI uses a separate store.
- Exact owned assertion withdrawal uses the ordinary authorized write/receipt path,
  preserves immutable evidence/history, and excludes withdrawn contributions from
  current decisions/counts and graph export. Same-key replay and fresh-key unchanged
  receipts retain the first event. Physical format `/3` requires a fresh store/reload
  for incompatible older formats; no migration, dual reader or automatic reset.
- Canonical indexing publishes immutable passages and vector projections;
  full search performs scoped keyword/dense retrieval, fusion and reranking.
  Query composition supports exact evidence, search, entity resolution, explicit
  decision records/counts and bounded retained support inspection. Knowledge is
  explicitly supplied, not automatically extracted from prose.
- The optional private Ladybug builder exports complete eligible relationships
  and explicit decisions for an exact scope, verifies authored IDs and proof/edge
  associations, and checkpoints/reopens a disposable stage. The developer example
  executes native queries; the complete varied-10,000-decision parity gate passed.
  There is no public business traversal/join facade.
- `kg.graph.LocalGraphSession` adds lazy exact-scope build/reuse, explicit refresh,
  serialized controlled writes, guarded private reads and confirmation-aware
  cleanup on one FIFO owner thread. Fresh source/authorization fences withhold
  stale results; confirmed canonical receipts survive later graph failures.
  Cold and warm operations retain their original finite budgets, native reads
  have cooperative timeout caps, and immutable output is bounded. Close/restart
  cannot grant trust to leftover graph files.
- The graph runtime is pinned to Ladybug 0.20.4 on macOS 15+ ARM64/CPython 3.12,
  with a 256 MiB native buffer pool and two threads. It is in-process and can
  terminate the host on fatal native failures; buffer/deadline controls are not
  hard isolation. Base imports/search do not require the optional extra.

### Changed

- Markdown demonstration `kg search` now uses `kg.retrieval.SearchService`: natural keyword plus
  semantic retrieval, canonical-ID fusion/deduplication, and cross-encoder
  reranking. Agent `interface_version` is now `"2"`.
- Removed `--query-mode` from product search, including its former `reranked`
  value. Old invocations fail with migration guidance. Omit the flag, prepare a
  matching `dense-index`, and retain matching profile/contextual settings.
- Ordinary search preserves `list[SearchResult]`, but `rank` is now the raw
  reranker score (higher is better). Preserve returned order; old BM25
  thresholds and cross-query score comparisons are invalid.
- Product `search --explain` now reports the actual full pipeline as
  `ProductSearchExplanation` version 2, with model/projection identities, stage
  counts/scores, explicit null membership, graph evidence, and bounded candidate
  telemetry. `--explain-limit` defaults to 50 (1..200) and, like quote opt-in,
  requires `--explain`.
- Product search requires current matching indexes and ready embedding/reranking
  providers even for empty results. Ingestion remains separate from vector
  preparation. Source changes require matching reindexing; failures never fall
  back to lexical search. Both search paths reject intervening database commits
  with `search_state_changed` and retry guidance.
- Structured/evidence CLI contracts and legacy low-level Python services remain
  available without semantic model initialization. Historical lexical benchmark
  adapters explicitly preserve their original strategies, labels, and evidence.
- Current documentation is organized by capabilities and workflows; the original
  experiment ledger is archived under `benchmarks/history/`. Existing unmet
  quality gates and measured results are unchanged. Final combined productization
  validation and review accepted the transition at code revision `12aeadf4c773`;
  the separately preserved integrated evidence does not establish release quality.

### Fixed

- Numbered checkboxes omitted from actions and numbered decisions retaining
  their list markers in summaries.
- Subject filters matching unrelated longer names such as Atlascope for Atlas;
  metadata filtering now shares ingestion's literal alias-boundary matching.
- Document identity collisions when reusing the original path of a moved source.
- Parent task owners and due dates overwritten by nested tasks or code examples.
- Fenced and indented code inside list items creating semantic relationships.
- Source offsets corrupted by Unicode separators and non-LF newlines.
- Cross-corpus and historical-document contamination of BM25 ranking statistics.
- Historical record citations displaying the current document title.
- Reverted content reported as unchanged and compared against the wrong state.
  An activation log now tracks transitions separately from immutable revisions.

### Added

- A frozen ten-question work-memory comparison for actual KG-versus-Markdown
  agent answers, with withheld gold, exact-citation scoring, identical source
  snapshots, and request/result journals for measured tool effort.
- Machine-readable strict/natural search telemetry through the low-level lexical API,
  with executed lexical expressions, filters, ranks, subject-scope predicates,
  graph evidence paths, opt-in quotes, and concurrent-change detection.
- Generic, explicitly keyed task/decision supersession across documents, with
  preserved citations, subject inheritance, and safe unresolved-reference
  diagnostics. Ingestion telemetry and `record-state` expose the resolver's
  actual decisions; a separate Atlas fixture exercises the feature.
- A ten-document Atlas corpus with staged ingestion expectations, two-hop
  evidence retrieval, similar-name isolation, prose-only negative cases,
  exact-citation checks, and task edit/revert coverage.
- Opt-in `ingest --explain` text/JSON reports with document outcomes, stored
  record counts, extraction rules, source anchors, and evidence IDs. Quotes
  require `--include-quotes`; detailed lists have explicit limits.
- Opt-in title/heading-aware embeddings and reranking with isolated contextual
  projections, unchanged exact evidence, and QASPER comparison support.
- A bounded `source-context` CLI and API operation for reading surrounding
  section anchors from the selected immutable revision.

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
