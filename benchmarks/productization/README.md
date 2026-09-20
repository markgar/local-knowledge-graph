# Productization parity validation

This is integration parity, **not new relevance gold or a quality improvement
claim**. Historical datasets, measurements, thresholds, model pins, and lexical
benchmark labels remain unchanged. The product specification remains
"specified, not implemented" until combined acceptance, including all eight real
model combinations.

The [2026-09-20 real-model run](RESULTS.md) passed all eight combinations.
The complete [machine-readable report](results-2026-09-20.json) preserves the
actual outputs, configuration, and provenance.

## Run the real matrix

From the repository root with the normal approved dependency/model caches:

```bash
uv sync --extra dev
uv run python benchmarks/productization/validate.py \
  --output .kg/productization/real-model-run
```

Use a new output directory for each run; existing runs are never overwritten.
The runner copies selected source files byte-for-byte, writes isolated manifests
and canonical/vector databases, and leaves authored fixtures and historical
benchmark databases untouched. It uses CPU, two PyTorch threads, indexing batches
of eight, and sequential model lifetimes to limit shared-machine resource use.
It preserves the providers' pinned model/revision and encoding/scoring behavior.
Models may use their normal approved cache/download paths. A network-policy
failure is a blocker, not a reason to change feeds, model pins, or trust settings.

The fixed matrix is `corpora/atlas.yml` and `corpora/atlas-state.yml`, each with
`gte-modernbert` and `qwen3-embedding-0.6b`, each with contextual false/true.
Every combination uses the existing pinned cross-encoder and runs
`archive signing certificate` unscoped, scoped to `Atlas`, and with a source
filter excluding every passage. `SearchService.search` and `explain_search`
are compared with `RerankedRetrievalService` using the same actual provider
instances. Ordered IDs and citation fields must match exactly. Scores use
relative tolerance `1e-5`, absolute tolerance `1e-6`; controlled tests use exact
scores. Explanations are serialized without `exclude_none=True` to retain
explicit null stage membership/contributions.

Before parity, **fresh product services** execute both empty-result methods.
The runner requires successful requests for both ready providers and zero
reranker scoring calls. Building the projection initializes the real embedding
provider; the first empty product search initializes the real reranker. Later
requests share these initialized instances. This checks product readiness
independently of the component's empty-result shortcut. Failure injection in
fast tests separately covers unavailable embeddings and reranker dependencies.

`report.json` is checkpointed after each combination and records command, code
revision plus source hashes, fixture fingerprints and per-file hashes, model
revisions, dependency versions, hardware/device, projection identity, effective
configuration, ordered results/citations, explanation traces, and exact errors.
All eight combinations are listed from the outset. A blocked or failed
combination leaves overall status `incomplete` and the command exits nonzero.
Controlled results cannot count as real-model acceptance. Retain the report here
under a distinct run filename when publishing; local databases stay in `.kg/`.

## Consumer inventory and assertion mapping

No existing tests or datasets were renamed, moved, deleted, or weakened.

| Consumer | Preserved backend / assertions |
| --- | --- |
| `benchmarks/agent/evaluate.py` | Three search workflows explicitly select natural lexical; seven reviewed task outcomes, citation round-trips, and revision comparisons remain in `tests/test_agent_benchmark.py`. |
| `benchmarks/work_memory/evaluate.py` | Omitted mode explicitly means strict; explicit strict/natural, graph restrictions, quote/explain errors, frozen dates, and telemetry remain covered in `tests/test_work_memory_benchmark.py`. |
| `benchmarks/work_memory/{discovery,expanded,interleaved}.py` | Generators and transitive evaluator consumers are unchanged; `tests/test_{discovery,expanded,interleaved}_work_memory.py` retain source/gold, deterministic generation, tool, and scoring assertions. |
| `benchmarks/qasper/evaluate.py` | Already selects strict/natural/dense/hybrid/reranked low-level services explicitly. No implementation change; profile/contextual handling, warmup, labels, and raw-score consumption remain intact. |
| `benchmarks/qasper/calibrate.py` | Consumes frozen evaluator scores, not search or the public facade; unchanged negative calibration result and assertions. |
| `tests/test_benchmark_continuity.py` | Adds actual strict-vs-natural negative/positive matches, model-free worker execution, worker errors/quote opt-in, subprocess routing, and independent backend-selection checks for every QASPER strategy. |
| `tests/test_productization_validation.py` | Adds all eight controlled parity combinations, exact-score gates, independent empty readiness, mismatches, dependency blockers, durable reporting, overwrite refusal, and nonzero incomplete exit. |

The inherited core baseline is commit
`aa2c1bb5fe1fa46439622f2efd696a5dd828eaed`: **535 tests passed** before edits.
The original transition baseline is `ecc0da83ce284e60aa6cb0946976a80d37f4fdad`.
The initial corpus tree was `a382b28467818b5bebc6f92d88aa8fe812a67d42`;
the historical benchmark tree before adapter edits was
`bcf85e25cd94d60be1f6f122f51bb1da48fa8d1b`.
