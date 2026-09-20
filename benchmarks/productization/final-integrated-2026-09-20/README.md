# Final integrated productization acceptance: 2026-09-20

**The productization transition is implemented and accepted.** Independent local
validation and a fresh whole-diff review cover original baseline
`ecc0da83ce284e60aa6cb0946976a80d37f4fdad` through tested code tip
`12aeadf4c7733cd7abdf238f1198896992dd49e3`. The reviewer reported no significant
issues or actionable findings. The subsequent acceptance commit records only
documentation and generated evidence, not runtime or test changes.

This is integration/contract acceptance, not evidence of improved relevance,
production readiness, or release quality. Pre-alpha limitations and unmet
retrieval/answerability gates remain unchanged. No merges were performed.
No GitHub CI checks currently appear on the stack PRs; the successful checks
reported here are **local validation**, not GitHub CI results.

## Observed outcomes

| Check at tested code tip | Outcome |
| --- | --- |
| `uv run pytest -v` | 649 passed in 77.56 seconds |
| `uv run ruff check .` | Passed |
| `uv run mypy` | Passed; 27 source files |
| `uv build` | Passed |
| Real-model matrix | All 8 combinations passed again |
| Actual public CLI ingest/index/search/explain smoke | 20 identical ordered IDs and scores; 44 traced candidates |
| Coordinator serialization inspection | Report version 2; no hit quote keys by default; explicit null stage memberships retained |
| Whole baseline-to-tip review | No actionable findings |

The matrix covers Atlas and Atlas-state, GTE and Qwen embeddings, and contextual
off/on, using the pinned cross-encoder. The full report retains configuration,
model/runtime/hardware identities, source hashes, exact results, parity cases,
and independent empty-readiness observations. It reports real-model execution
from `2026-09-20T17:58:26.597607+00:00` to
`2026-09-20T18:01:27.685940+00:00`; the validation summary was recorded at
`2026-09-20T18:05:31.990344+00:00`. Approved cached models were used.

The CLI smoke used `corpora/atlas.yml`, query `archive signing certificate`, and
subject `Atlas`. Its ordinary and explained outputs below preserve actual
observations, not regenerated examples. The coordinator verified version, quote
omission and null membership independently; artifact preservation also checked
exact ordered IDs/scores, 20 hits, and 44 candidates.

The authored corpus tree remains `a382b28467818b5bebc6f92d88aa8fe812a67d42`.
Fixture/gold/historical-results preservation and the
[assertion migration mapping](../README.md#public-cli-layer-inventory-and-assertion-mapping)
remain intact. The [earlier run](../RESULTS.md) and its
[original JSON report](../results-2026-09-20.json) were not overwritten.

## Immutable generated artifacts

These four files were copied byte-for-byte from the coordinator's final validation
artifacts. SHA-256 values were verified before and after copying.

| Artifact | SHA-256 |
| --- | --- |
| [Validation summary](integrated-validation.json) | `e7b1c111fc2c193c7c4c85d04e565806043025c271677b0afc97b6dfbbb80fc9` |
| [Full real-model matrix](integrated-model-results.json) | `56c8f37df945db78fe6dd8dd17ebdced39f5057a3a9a7accb35210307d831637` |
| [Ordinary CLI search](cli_search_ordinary.json) | `3350997faa41ad4b3dc5cf4f8014b7e22bcbb32603800dfe4504dd6332ffa185` |
| [Explained CLI search](cli_search_explain.json) | `0fc0a4fe72e7647731142afed36715aba5a3126dc10fae2ace0e379061ccb133` |

Reproduce the matrix with the [existing runner instructions](../README.md#run-the-real-matrix)
and a new output directory. Keep this run distinct from future measurements.
