# Productization parity validation

This is integration parity, **not new relevance gold or a quality improvement
claim**. Historical datasets, measurements, thresholds, model pins, and lexical
benchmark labels remain unchanged. The product specification remains
pending final combined acceptance and review, including all eight real-model
combinations.

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

The core and benchmark-continuity layers changed no original tests or datasets.
The public CLI migration mappings below distinguish preserved lexical assertions
from intentionally retired public mode selection.

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

### Public CLI layer inventory and assertion mapping

The CLI layer starts from benchmark-continuity commit
`1f9f67d61e369b8ec85affba0f073c4fccbadd21`: **565 tests passed** before edits.
All authored `corpora/` content, manifests, stage definitions, acceptance gold,
core product tests, benchmark adapter tests, and recorded results remain unchanged.

| Original consumer/assertion | Current location and preserved property |
| --- | --- |
| `tests/test_cli.py::test_evidence_command_returns_exact_record_anchor` | Same test; strict `RetrievalService.search()` obtains the record ID instead of public search. The public evidence command still must return exactly `"Evidence passage."`. A new product CLI round-trip separately covers semantic hit IDs. |
| `test_natural_query_mode_is_available_through_cli` | Renamed `test_natural_query_matching_remains_available_at_component_boundary` in the same file. The unchanged `"unrelated evidence"` query must still return exactly `"Evidence passage."` through natural lexical search. Public natural-mode selection is intentionally removed and explicitly tested as an error. |
| `test_dense_commands_are_available_through_cli` | Renamed `test_dense_index_and_product_search_configuration_through_cli`. Both contextual settings, Qwen profile, corpus ID, batch size 16, projection identity and exact `"Semantic evidence."` payload remain asserted. Indexing still uses the dense service; search dispatch uses the facade. |
| `test_dense_encode_error_is_machine_readable` | Same test and exact error/message assertion; the injected failure now crosses `SearchService`, which owns public search. Additional actual-provider failure tests cover initialization and execution. |
| `test_hybrid_query_mode_is_available_through_cli` | Renamed `test_product_search_preserves_hybrid_evidence_payload_through_cli`. Original query, profile, corpus/context settings and exact `"Hybrid evidence."` assertion survive at the public facade dispatch boundary. Component fusion tests remain unchanged. |
| `test_reranked_query_mode_is_available_through_cli` | Renamed `test_product_search_preserves_reranked_evidence_payload_through_cli`. Original query, profile, corpus/context settings and exact `"Reranked evidence."` assertion survive at facade dispatch. Actual pipeline tests separately prove reranker application and score order. |
| `tests/test_search_explain.py::test_cli_quotes_are_separately_opted_in` | Renamed `test_lexical_quotes_are_separately_opted_in`. Direct lexical explanation/rendering retains both format cases, exact private quote inclusion/omission, subject text, FTS expression, score disclaimer and exact-mention assertions. New product CLI quote tests cover both output formats independently. |
| `test_cli_default_search_shape_unchanged` | Renamed `test_lexical_search_shape_unchanged`. Exact private-quote and `SearchResult` field-set assertions remain lexical; new product CLI tests independently pin the full ordinary list/field shape. |
| `test_invalid_flags_fail_before_model_work` | Retains quote/since rejection and all three obsolete semantic-mode invocations. Mode errors now correctly say `invalid_query` with migration guidance rather than `unsupported_explanation_mode`; the obsolete contextual-only rejection is replaced by the new explain-limit dependency. Lexical contextual rejection is preserved explicitly against the private worker. Provider factories are forbidden rather than forbidding construction of lazy services. |
| `test_changes_between_search_and_attribution_are_rejected` | Same mutation and expected `search_state_changed`, now asserted directly on lexical `explain_search()`. Separate product CLI tests require the same code and retry guidance for ordinary/explained edit-and-restore during readiness and scoring. Other lexical explanation assertions are untouched. |
| `tests/test_agent_cli.py::test_agent_cli_publishes_versioned_capabilities` | Only expected interface version changes from 1 to 2; transport and discovered-operation assertions are unchanged. Additional discovery coverage pins report version, trace bounds and migration guidance. |
| `tests/test_source_context.py::test_context_cli_and_capability` | Unchanged source/context/capability assertions. Old strict/natural contextual invocations still fail as `invalid_query`, now because modes are removed. The private-worker contextual rejection test preserves the original lexical incompatibility property. |
| `tests/test_acceptance.py`, `tests/test_atlas_walkthrough.py` | Entire files unchanged: exact lists, negative matches, stage counts, graph scope, edit/restore/history and citations already use the lexical/structured boundary. No semantic output replaces their gold. |
| `examples/cited_status.py`, `tests/test_client_example.py` | Audited and unchanged: structured status only, no search-mode or score assumptions. |
| Historical benchmark consumers | Backend/default selection remains as documented above. No public-facade rerouting or score re-sorting/threshold changes. QASPER instructions already use explicit internal `--strategy`. |

`tests/test_product_cli.py` is separately scoped integration coverage: real
component composition with controlled provider factories, both embedding profiles
and contextual settings, filtering/two-hop scope/isolation, citation offsets and
history, authoritative score order, full union/dedup/fusion/rerank telemetry,
explicit JSON nulls, trace bounds and quote controls, all obsolete mode spellings,
readiness/empty/provider/index errors and recovery, commit consistency, discovery,
and model-free structured operations. It does not redefine lexical gold or claim
real-model relevance. Core and benchmark tests continue to run unchanged.
