# Product search parity validation

This matrix checks the Markdown demonstration's `kg.retrieval` product search
pipeline against its reranked component using real embedding and cross-encoder
models. It is **integration
parity, not a relevance benchmark or quality-improvement claim**. Product
`SearchService.search` and `explain_search` execute lexical/dense retrieval,
fusion, and reranking; this runner checks their results and citation fields.
It does not exercise canonical `kg.indexing`/`kg.query` service composition or
Ladybug graph building. See [current architecture](../../SPEC.md#architecture-and-data-ownership)
for those separate execution boundaries.

## Run the real-model matrix

From the repository root with the
[development environment](../../CONTRIBUTING.md) and approved model caches:

```bash
uv run python benchmarks/productization/validate.py \
  --output .kg/productization/real-model-run-1
```

`--output` is required and must name a new directory. There is no CLI selector for
individual combinations or controlled providers, and no resume/overwrite mode.
The runner copies selected sources byte-for-byte, writes isolated manifests and
canonical/vector databases, and leaves authored fixtures and benchmark databases
untouched. It uses CPU, two PyTorch threads, indexing batches of eight, and
sequential model lifetimes. Model/revision pins and encoding/scoring behavior
come from the existing providers.

Models may use normal approved cache/download paths. A network-policy failure is
a blocker, not a reason to change feeds, model pins, or trust settings. Report the
blocked host and use cached/local or otherwise approved resources; do not bypass
organizational controls.

## Matrix and assertions

The eight combinations are:

- [`corpora/atlas.yml`](../../corpora/atlas.yml) and
  [`corpora/atlas-state.yml`](../../corpora/atlas-state.yml).
- `gte-modernbert` and `qwen3-embedding-0.6b`.
- Contextual representation disabled and enabled.

Every combination uses the pinned cross-encoder and the query
`archive signing certificate`: unscoped, scoped to `Atlas`, and with a source
filter excluding every passage. Product ordinary/explained search is compared
with `RerankedRetrievalService` using the same provider instances. Nonempty cases
must return candidates. Ordered IDs and citation fields match exactly; scores
use relative tolerance `1e-5` and absolute tolerance `1e-6`. Explanation hits
omit ordinary-result summary/status fields. Reports retain explicit JSON nulls
for absent stage membership/contributions, and returned anchors are resolved to
source ranges.

Before parity, fresh product services execute both empty-result methods.
Each must request both ready providers and make zero reranker scoring calls.
Indexing initializes the embedding provider; the first empty product search
initializes the reranker. Later requests share those instances. This checks
product readiness independently of a component's empty-result shortcut.

## Reports and reproducibility

`report.json` lists all combinations from the outset and is checkpointed before
and after each combination. It records:

- Command, code revision, source hashes, dependencies, and hardware/device.
- Fixture fingerprints and per-file hashes, model revisions, projection identity,
  and effective configuration.
- Ordered results/citations, explanation traces, readiness checks, and errors.

A failed or blocked combination leaves overall status `incomplete`; the command
exits 1 unless every combination passes. Failures retain error type, message,
traceback, and stage. Keep each new report under a distinct run label and leave
local databases in the untracked workspace. Do not overwrite frozen reports,
authored gold, or source snapshots to obtain a passing comparison.

The [real-model results](RESULTS.md),
[machine-readable report](results-2026-09-20.json), and
[integrated validation record](final-integrated-2026-09-20/README.md) preserve
recorded runs; they do not validate subsequent code changes.

## Related checks and benchmark boundaries

Routine regression tests use controlled provider factories and do not download
models. Matrix-specific coverage can be run with:

```bash
uv run pytest tests/test_productization_validation.py tests/test_benchmark_continuity.py
```

Controlled matrix tests cover exact parity scores, empty readiness, mismatches,
dependency blockers, durable reports, overwrite refusal, and nonzero incomplete
exit. Product CLI integration coverage is in `tests/test_product_cli.py`.
Controlled-provider success is not real-model acceptance or measured relevance.

The other benchmarks deliberately keep their own retrieval contracts:

| Benchmark | Retrieval boundary |
| --- | --- |
| [Agent CLI](../agent/README.md) | Natural lexical worker plus structured/evidence CLI operations; scripted contract checks, not agent-generated answers. |
| [Work-memory](../work_memory/README.md) | Strict lexical search by default, optional natural mode, shared document access and arm restrictions; no semantic models. |
| [QASPER](../qasper/README.md) | Explicit strict/natural/dense/hybrid/reranked component strategies; calibration consumes raw reranker scores. |

Preserve their labels, reviewed expectations, source/gold hashes, frozen dates,
and score meanings. Do not route lexical baselines through product search or
interpret this parity matrix as passing their unmet retrieval/answerability gates.
