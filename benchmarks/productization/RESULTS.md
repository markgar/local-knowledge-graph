# Productization parity: 2026-09-20

**All eight required real-model combinations passed.** This establishes parity
with the existing reranked component, not improved retrieval quality. At the time
of this earlier run, combined acceptance remained outstanding and the public
CLI/documentation layer was separate. Subsequent
[final integrated validation and review](final-integrated-2026-09-20/README.md)
accepted the transition at `12aeadf4c7733cd7abdf238f1198896992dd49e3`.
The original measurements and report below are unchanged.

## Reproduction and identity

```bash
uv sync --extra dev
uv run python benchmarks/productization/validate.py \
  --output .kg/productization/real-model-2026-09-20
```

- Code: `4753daa23725c7f5747aca8e9431fa7c5c0b1202`, based on core
  `aa2c1bb5fe1fa46439622f2efd696a5dd828eaed` / PR #16.
- UTC execution: `2026-09-20T17:30:57.854742+00:00` through
  `2026-09-20T17:33:59.757646+00:00`; exit code 0.
- Hardware: macOS 27.0, arm64 Mac16,11, 12 logical CPUs, 24 GiB RAM.
  Both providers forced to CPU; two PyTorch threads; sequential combinations.
- Runtime: Python 3.12.13, Sentence Transformers 5.7.0, Transformers 5.17.0,
  PyTorch 2.14.0, sqlite-vec 0.1.9. Full installed versions are in the report.
- GTE: `Alibaba-NLP/gte-modernbert-base`,
  revision `752e76f479f37e13f5e956c0a277bd7ccea80714`.
- Qwen: `Qwen/Qwen3-Embedding-0.6B`,
  revision `97b0c614be4d77ee51c0cef4e5f07c00f9eb65b3`.
- Reranker: `cross-encoder/ms-marco-MiniLM-L6-v2`,
  revision `233902d25c440f23af6f7d6e94d2946bac0bee0a`.
- Corpus tree: `a382b28467818b5bebc6f92d88aa8fe812a67d42`, unchanged from the
  original transition baseline. Per-source SHA-256, manifest hashes, corpus
  fingerprints, projection IDs, effective configuration, device/dtype encoding
  identity, and code-file hashes are recorded for every applicable case.

The full [report](results-2026-09-20.json) has SHA-256
`4421b66a1f6d5fb4e56eb5d58276aef8d7e16fd0e09a96185e8550f94a6f5434`.
It is copied byte-for-byte from the runner. Its worktree-status snapshot lists
only the then-untracked execution log, not source edits. All recorded code-file
hashes were checked against the committed implementation after execution.
Generated SQLite indexes and the execution log remain local under `.kg/`.

## Outcomes

Each row includes three parity cases for `archive signing certificate`:
unscoped, subject `Atlas`, and a source filter excluding all passages.
Every row returned 20, 20, and 0 hits respectively in component, ordinary
product, and explained product search.

| Corpus | Embedding | Contextual | Parity | Empty readiness, ordinary/explained | Seconds |
| --- | --- | --- | --- | --- | ---: |
| atlas | gte-modernbert | false | Pass | Pass / Pass | 27.55 |
| atlas | gte-modernbert | true | Pass | Pass / Pass | 8.43 |
| atlas | qwen3-embedding-0.6b | false | Pass | Pass / Pass | 25.13 |
| atlas | qwen3-embedding-0.6b | true | Pass | Pass / Pass | 33.59 |
| atlas-state | gte-modernbert | false | Pass | Pass / Pass | 7.14 |
| atlas-state | gte-modernbert | true | Pass | Pass / Pass | 9.66 |
| atlas-state | qwen3-embedding-0.6b | false | Pass | Pass / Pass | 29.25 |
| atlas-state | qwen3-embedding-0.6b | true | Pass | Pass / Pass | 40.23 |

Ordered record IDs and citations matched exactly across all 24 parity cases.
The maximum observed absolute score difference was **0**, for both ordinary and
explained product search versus the component. The unchanged real-model gate is
relative tolerance `1e-5`, absolute tolerance `1e-6`; controlled tests use exact
equality. Full version-2 explanations retain explicit null stage membership and
fusion contributions.

The 16 separate empty-readiness checks used fresh product services before parity
and requested both initialized providers, with zero reranker scoring calls.
The first empty product call initialized the real pinned reranker; embedding
initialization had already occurred during index preparation. All subsequent
calls shared the same provider instances within their combination.

**Blockers: none.** Normal approved cache/download behavior succeeded.
The model loader emitted an unauthenticated Hugging Face rate-limit warning;
it did not prevent validation. No feed, trust setting, model pin, source,
ranking parameter, tolerance, or acceptance threshold was changed.

## Regression checks and preservation

- Inherited baseline: `uv run pytest` -> 535 passed.
- Final implementation: `uv run pytest` -> 565 passed.
- `uv run ruff check .` -> passed.
- `uv run mypy` -> passed, 27 source files.
- `uv build` -> passed, wheel and source distribution.
- Original 435 tests and the core layer's 100 added tests remain unchanged;
  this layer adds 30 tests. No assertion moves or replacements.
- The corpus tree and historical source/gold/results files remain unchanged.
  Historical benchmark changes are limited to the two subprocess consumers and
  current instructions; QASPER evaluation/calibration implementations are intact.

See the [consumer inventory and assertion mapping at the measured revision](https://github.com/markgar/local-knowledge-graph/blob/4753daa23725c7f5747aca8e9431fa7c5c0b1202/benchmarks/productization/README.md#consumer-inventory-and-assertion-mapping)
for the complete continuity boundary.
