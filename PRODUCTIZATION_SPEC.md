# KG productization specification

Status: specified, not implemented.
Priority: first implementation workstream in `ROADMAP.md`.
Date: 2026-09-20.

## Outcome

Present and operate the existing Local Knowledge Graph as one product, not a
sequence of experiments. Preserve the investment in fixtures, evidence
correctness, ingestion, and retrieval implementations.

Normal search uses the complete existing retrieval pipeline without asking the
caller to select an individual capability:

**Keyword + semantic retrieval -> fusion and deduplication -> reranking ->
evidence-backed results.**

This is organizational cleanup plus an intentional change to the public search
contract. It is not a retrieval algorithm redesign, a rewrite of ingestion, or
completion of the broader knowledge-engine roadmap.

## Non-negotiable preservation requirements

- Preserve authored fixture content, manifests, scenario stages, gold evidence,
  expected source anchors, and reviewed expected behavior.
- Preserve existing ingestion semantics: source selection, Markdown parsing,
  configured entities/aliases, explicit record extraction, revisions, source
  activation, idempotency, failures, and explicit record-state resolution.
- Preserve exact citations, historical evidence, corpus isolation, source/date
  filtering, and existing graph-assisted subject scoping.
- Preserve the keyword, dense, fusion, and reranking implementations and their
  component-level regression coverage.
- Preserve benchmark datasets, historical results, and the configuration needed
  to reproduce those results.
- Do not delete, skip, weaken, or regenerate expected evidence merely to make
  the changed search interface pass tests.

Fixture moves or renames are not required to make the product coherent. Prefer
leaving valuable data in place. If a move is necessary, retain content and update
all consumers. Distinguish a scenario's incremental ingestion stages from the
E-series research stages: incremental scenarios remain useful product tests.

## Scope

### One public search behavior

`kg search <text> --manifest <path>` runs the existing reranked hybrid service.
Introduce `kg.retrieval.SearchService` as the supported product-facing Python
search facade, and have the CLI use it. Its public contract is:

```python
SearchService(
    database: Database,
    corpus_id: str,
    *,
    embedding_profile: EmbeddingProfile = DEFAULT_EMBEDDING_PROFILE,
    contextual: bool = False,
)

search(
    query: str,
    *,
    subject: str | None = None,
    limit: int = 20,
    since: datetime | None = None,
    source_path: str | None = None,
) -> list[SearchResult]

explain_search(
    query: str,
    *,
    subject: str | None = None,
    limit: int = 20,
    since: datetime | None = None,
    source_path: str | None = None,
    include_quotes: bool = False,
    trace_limit: int = 50,
) -> ProductSearchExplanation
```

Both methods belong to `SearchService`. Export the version-2
`ProductSearchExplanation` contract from `kg.retrieval` alongside the facade;
retain the existing lexical `SearchExplanation` contract for low-level callers.
`kg search --explain` calls the facade's `explain_search()` method. Expose
`--explain-limit` as the CLI equivalent of `trace_limit`, with default 50 and
allowed range 1-200. Reject an explicitly supplied `--explain-limit` without
`--explain`, just as `--include-quotes` requires `--explain`. Invalid Python trace
limits raise `ValueError` before provider initialization.

The facade composes the existing reranked service; it does not change
`RetrievalService.search()` into a hybrid entry point. That lexical method is
already used inside the hybrid implementation and must not recurse into the
product facade. Keep existing service imports and component signatures working
for compatibility, internal composition, and evaluation, but document them as
low-level APIs rather than alternative product search interfaces.

Reuse the existing error taxonomy: `SearchQueryError` for invalid query/filter
semantics, `ValueError` for invalid configuration/limits, `DenseIndexError` for
dense-index/embedding readiness, and `RerankerError` for reranker readiness or
execution failures. Preserve corresponding CLI error codes. Explanation failures
retain explicit `SearchExplanationError` codes. Preserve test injection seams
without exposing retrieval-stage bypasses on the product API.

Ordinary product search also rejects intervening database commits, including
edit-and-restore that returns the corpus fingerprint to its original value.
Observe the full execution, from before readiness checks through final result
assembly; fingerprint comparison alone is insufficient. Use
`SearchStateChangedError`, a `SearchQueryError` subclass with code
`search_state_changed`, for ordinary search, and preserve
`SearchExplanationError` with that same code for explained search. Both CLI paths
emit `search_state_changed` with an instruction to retry; neither silently
retries or returns partial results. Conservatively rejecting unrelated-corpus
commits in the same database is acceptable, matching existing explanation
behavior. Existing component fingerprint checks remain intact.

Constructing services or using structured/evidence operations must not initialize
models. Readiness is enforced when executing product search, after argument
validation. Existing low-level component behavior is preserved.

The existing pipeline supplies:

1. Natural-mode BM25 keyword candidates and semantic candidates.
2. Existing reciprocal-rank fusion and canonical-ID deduplication.
3. Existing cross-encoder reranking.
4. Existing evidence-backed search results with deterministic tie-breaking.

Retain the current fusion parameters, candidate-pool rules, default embedding
profile, pinned models, and passage representation. Do not bundle model tuning,
new chunking, changed ranking weights, or contextual-mode promotion into this
workstream. Existing supported embedding-profile and contextual configuration
must remain coherent across indexing and querying.

Carry subject, source, date, corpus, and result-limit constraints through the
full pipeline. Existing graph-assisted subject scoping remains available when
the caller supplies a subject. This does not introduce automatic entity
resolution or a new graph candidate generator.

No public search mode may bypass semantic retrieval, fusion, or reranking.
Remove `--query-mode` from the supported product search interface rather than
silently accepting an old mode and changing its meaning. Old invocations must
fail clearly with migration guidance; do not silently ignore the argument.
Document that even the old `--query-mode reranked` invocation now omits the flag.

Search remains evidence retrieval, not answer generation. Preserve the ordinary
JSON list and `SearchResult` fields. Returned list order is authoritative:
`rank` is the raw cross-encoder score, higher is better, with the existing
record-ID tie-breaker. It is not confidence or comparable with historical BM25
scores, other models, or other queries. Audit consumers that sort or threshold
this field. Ranking and ordering for the old default search intentionally change;
that is not permission to alter citation contents.

Advance the advertised agent `interface_version` from `"1"` to `"2"` to mark
the changed search default, score semantics, and removed argument. Publish the
new explanation as `report_version: "2"`; do not present a semantic/fused trace
as the old lexical report. Unchanged operation payloads do not need arbitrary
schema changes. Document both version changes in migration notes.

### Readiness and indexing

Keep `kg ingest` behavior unchanged. Keep the explicit dense-index operation
available and explain it as normal index preparation, not a research stage.
The documented workflow must prepare the indexes and models needed for search.

Retain compatibility and freshness checks for the selected projection. Missing,
stale, or incompatible vector indexes and unavailable embedding/reranking models
must produce explicit errors and actionable setup/rebuild guidance.

Never return keyword-only, vector-only, or unreranked results as if the full
search succeeded. An empty result set is not a substitute for an unavailable
dependency. Valid product searches must verify the selected projection and the
embedding and reranking providers even for an empty corpus or filters excluding
all passages. Only then may they return `[]` without scoring any candidates.
Use real provider initialization/readiness, not a cache-file existence check.
Cache ready providers within the service lifetime to avoid needless reloading.

Cache loaded provider instances, not an index-readiness verdict. Every ordinary
or explained call must recheck the selected projection's freshness and
compatibility against current corpus state, even when the same facade has
already completed a search. A successful call followed by ingestion must fail
if the projection is stale; rebuilding the matching projection must allow the
same facade instance to recover without reconstructing it or reloading models.
An initialization failure must not be cached as success.

This is deliberately stricter than the old reranked service, which skips reranker
initialization for an empty candidate list. Apply the stricter requirement at
the product facade; successful ranking parity does not require preserving this
old failure behavior. Ordinary and explained product search have the same
readiness requirements.

The supported workflow is `ingest -> dense-index -> search`. After a source
change invalidates the vector projection, search fails explicitly until the
matching index is rebuilt. `dense-index` prepares the embedding projection, not
the reranker; document reranker initialization on first product search separately.
Index preparation must use the same embedding profile and contextual setting as
search. Do not imply that unchanged ingestion automatically prepares everything.

Document first-use model preparation, local resource requirements, and approved
download/cache behavior. Network-policy failures must be reported, not bypassed.
Automatic indexing orchestration belongs to the later ingestion lifecycle work.

### Other operations and diagnostics

Preserve actions, status, record-state, evidence, source-range, source-context,
revisions, and revision comparison. These are useful structured/read operations,
not competing search modes; do not force them through semantic search.

Preserve diagnostic capabilities. Today `search --explain` is lexical-only;
it must not remain a hidden lexical-only route under the new product search.
Make product search explanations describe the actual full pipeline, including
the effective configuration, filters, candidate/ranking stages, and applicable
subject-scope evidence. Explanations must reflect the execution they describe,
not an independently rerun search against possibly changed data.

The version-2 trace must include:

- Corpus fingerprint, selected projection, model/profile identities, and
  contextual setting.
- Effective query/filters, lexical expression, candidate limits, and stage counts.
- A bounded candidate trace identifying lexical/dense ranks and scores, fusion
  contributions/scores, reranker scores, and final order where each applies.
  Missing stage membership is explicit, not a fabricated zero score.
- Score meanings, tie-breakers, and existing subject-attribution paths with
  supporting evidence IDs.
- Displayed versus total trace counts and explicit truncation. Limiting the
  displayed trace must not change the retrieval calculation.

The version-2 JSON report has the following stable top-level shape:

| Field | Contract |
| --- | --- |
| `report_version` | Literal `"2"`. |
| `corpus_id`, `corpus_fingerprint` | Corpus identity and fingerprint for the observed execution. |
| `query`, `filters`, `lexical_expression` | Original query, effective subject/source/date/result-limit constraints, and executed natural-mode FTS expression. Preserve existing date-filter semantics. |
| `configuration` | Selected projection ID, embedding profile and model revision, reranker model revision, provider pipeline versions, contextual setting, effective candidate limits, fusion weights, and fusion constant. |
| `score_semantics`, `tie_breakers` | Stage-specific score directions/meanings and actual deterministic tie-breakers. |
| `stage_counts` | Counts for lexical candidates, dense candidates, deduplicated union, fused shortlist, reranked candidates, and returned hits, before display truncation. |
| `subject_scope` | Existing bounded graph scope and supporting evidence paths. |
| `active_current_revisions_only`, `supersession_filter_applied` | Preserve the existing values `true` and `false`; source search does not hide superseded assertions. |
| `quotes_included` | Whether quote disclosure was explicitly requested. |
| `hits` | All final results up to the requested result limit, in returned order, using the existing explained-hit citation and subject-attribution fields. `rank` is the reranker score. Quotes are omitted unless opted in. |
| `candidates` | Bounded per-candidate stage trace, keyed within each entry by canonical `record_id`. |
| `trace_limit`, `total_candidates`, `displayed_candidates`, `truncated` | Requested display bound, deduplicated union size, displayed entry count, and whether entries were omitted. |

Each candidate entry contains `record_id`, nullable `lexical` and `dense` stage
objects (each with one-based `position` and raw `score`), `fusion` (one-based
position over the full union, fused score, separate lexical/dense contributions,
and `selected_for_reranking`), nullable `reranker` (one-based position and raw
score), and nullable `final_position`. Absent stages and absent fusion
contributions are explicit JSON `null`, never zero or silently omitted;
`final_position` is null for candidates outside the returned result limit.
Candidate entries contain no passage text; quote opt-in applies to `hits`.
Fusion contributions record the actual weighted reciprocal-rank terms.

Choose displayed candidates deterministically: returned hits in final order,
then remaining reranked candidates in reranker order, then remaining union
candidates in fusion order; deduplicate by canonical ID and take `trace_limit`.
The separate `hits` list is never shortened by the trace limit. Position
numbering precedes display truncation. Changing the trace limit or quote opt-in
must not change candidate generation, scoring, counts, or final results.

This is execution telemetry, not a generated natural-language rationale.
Capture the data during the actual execution. Explained and ordinary search
must yield the same ordered IDs and scores under identical data, configuration,
and providers; quotes appear in explanations only on explicit opt-in.

Keep detailed lexical diagnostics internally for tests and evaluation. Preserve
quote opt-in, bounded output, explicit truncation, and consistency protection.
In particular, retain detection of intervening commits, including edit-and-restore
that leaves the final corpus fingerprint unchanged. Fingerprint equality alone
is not sufficient for explanation consistency. Do not delete explanation tests
merely because their current CLI wiring changes.

### Product organization and documentation

- Rewrite the README around current installation, configuration, ingestion,
  index preparation, search, evidence inspection, and maintenance workflows.
- Organize `SPEC.md` around current capabilities and contracts, not E0-E5.
  Keep unimplemented architecture clearly in the roadmap.
- Preserve historical experiment narratives and measured results in benchmark
  history, with working links and reproducible configuration references.
- Remove requirements to advance through E-stages or finish one experimental
  measurement before developing another product feature.
- Update CLI help, agent capabilities, examples, contributing guidance, and
  current benchmark instructions to describe the supported product interface.
- Retire obsolete experimental scaffolding only after checking its consumers.
  Keep meaningful configuration and low-level evaluation tools.

Historical labels may remain in archived results and baseline adapters.
Completion is not defined by eliminating every occurrence of an E-number.
Do not claim production readiness or improved retrieval quality from renaming
and changing the default alone; keep known limitations visible.

## Tests and fixture protection

Before implementation, inventory fixtures, gold expectations, benchmark
configurations, affected entry points, and their test consumers. Record the
baseline test outcomes and any existing failures. Keep an explicit mapping for
tests or datasets that are renamed or moved.

### Component coverage

Retain independent tests for lexical, dense, hybrid, and reranked behavior.
These exercise internal capabilities, not user-selectable product modes.
Keep existing component expectations where algorithms have not changed.

If any CLI, acceptance, scenario, or integration test currently pins strict or
natural lexical results, preserve that assertion at the lexical component
boundary. This includes exact quote lists and negative matches in
`tests/test_acceptance.py` and `tests/test_atlas_walkthrough.py`. Their expected
lists are not automatically semantic-search gold.

Add separate product-search assertions using the same source fixtures rather
than replacing lexical expected lists. Preserve the authored source/gold files;
add distinctly scoped product expectations where needed. Review the test mapping
to ensure moved assertions still run and check the same property.

### Product integration coverage

Add or update tests proving:

- Unqualified CLI and product-level Python search use the complete pipeline.
- Both candidate sources contribute, duplicate evidence is merged, and the
  reranker's ordering is actually applied.
- Subject/source/date filters, corpus isolation, limits, and graph-assisted
  subject scoping remain correct end to end.
- Evidence IDs, quotes, source offsets, historical citations, context reads,
  and revision comparisons remain valid.
- Missing/stale/incompatible indexes and unavailable models fail explicitly
  without degraded success responses, including empty corpora and filters that
  exclude all passages. Cover embedding and reranker failures independently for
  ordinary and explained search.
- Source edit -> stale-index failure -> matching index rebuild -> successful
  search works without changing ingestion semantics, including on the same
  facade instance with cached providers for ordinary and explained calls.
- Public search enforces readiness while legacy lexical and structured/evidence
  operations remain usable without semantic models.
- Removed mode arguments provide explicit migration errors.
- Version-2 explanations describe the full execution without exposing quotes by
  default; final rankings match ordinary search and intervening commits,
  including edit-and-restore, are detected.
- Ordinary search also rejects intervening commits with `search_state_changed`,
  including edit-and-restore and commits during readiness checks.
- Python and CLI explanations use the version-2 contract; trace bounds, explicit
  null membership, deterministic truncation, and quote opt-in do not affect
  returned hits or retrieval calculations.
- Capability discovery advertises interface version 2 and migrated consumers
  respect the documented score semantics.
- Existing ingestion scenarios, including edits, restores, removals, failures,
  and state replacements, retain their expected behavior.

Use existing injection patterns and controlled providers for fast tests; do not
make every fixture test download or run large models. Also exercise the real
configured models on representative existing fixtures to confirm actual
integration. If dependencies or network policy block that validation, report it
as incomplete rather than substituting mocked evidence for real-model success.

The required real-model matrix uses `corpora/atlas.yml` and
`corpora/atlas-state.yml`, each with both `gte-modernbert` and
`qwen3-embedding-0.6b`, each with contextual mode off and on (eight combinations).
Use the existing pinned cross-encoder in every combination. Build isolated
indexes from unchanged fixture sources; do not overwrite fixture manifests or
historical benchmark databases.

For every combination, run `"archive signing certificate"` both unscoped and
with subject `"Atlas"`, plus a source filter matching no passage. Compare
ordinary and explained product search against the existing explicit
`RerankedRetrievalService` path under identical configuration and provider
instances. Require identical ordered IDs and citations; controlled-provider
tests require exact scores, while real-model scores may differ only within
relative tolerance `1e-5` and absolute tolerance `1e-6`. The empty case must
verify the product readiness contract separately, not inherit the component's
reranker shortcut. These are integration/parity checks, not new relevance gold
or evidence of improved retrieval quality.

Record commands, code revision, fixture fingerprints, model revisions,
dependency versions, hardware/device, configuration, comparison outcomes, and
blocked combinations under a distinct `benchmarks/productization/` validation
label, without modifying historical measurements. All eight combinations must
pass before this workstream is accepted as complete. A blocked check leaves
acceptance outstanding; do not mark the specification implemented or claim
completion based on mocks. Implementation may proceed while checks are blocked,
but any decision to merge with outstanding validation requires an explicit
maintainer exception identifying the missing checks.

### Benchmark continuity

Historical per-retriever comparisons must still be reproducible through
evaluation/internal interfaces, without reintroducing public search modes.
Every historical adapter must select its original strategy explicitly,
including adapters that previously omitted `--query-mode` and inherited the
lexical default. Update subprocess-based adapters as well as direct callers.

In particular, audit the work-memory evaluator's unqualified search forwarding.
Preserve each arm's retrieval strategy, tool restrictions, frozen dates, model
configuration where applicable, and original labels. Legacy lexical evaluation
must not acquire a dependency on semantic models or vector indexes.

Test backend selection, not only equality between evaluation arms: two arms
silently switching to reranked search could still return equal results.
Record new full-pipeline product evaluations under distinct labels without
overwriting or reinterpreting historical scores.

Do not overwrite old measurements or lower unmet thresholds to declare this
transition successful. Compare the new normal search to the existing reranked
pipeline under identical configuration; this work is not expected to improve
its retrieval algorithm.

## Acceptance criteria

These criteria accept the productization transition, not attainment of all
future product-quality targets. Existing unmet retrieval-quality gates remain
visible; do not lower them or claim improvement from this interface migration.
Unrelated model/quality tuning is not a hidden prerequisite for this workstream.

1. Existing fixture content and reviewed gold evidence remain intact, with no
   lost assertion coverage hidden by test renaming or removal.
2. Ingestion and non-search read commands preserve their existing contracts.
3. Normal search uses the full existing reranked hybrid pipeline with no public
   switch for a weaker, single-capability search.
4. Under identical data, configuration, and providers, normal search matches
   the former explicit reranked path's successful rankings, filters, and
   citations. The explicit stricter empty-result readiness contract is tested
   separately rather than treated as a parity regression, as is the stricter
   ordinary-search intervening-commit protection.
5. Index/model readiness failures are explicit and never silently downgrade
   retrieval; setup and recovery instructions are documented.
6. Search explanations correspond to the full execution, and existing diagnostic
   information and quote controls are preserved where applicable.
7. Historical component evaluation remains reproducible, while current docs,
   help, and examples require no knowledge of the E-series.
8. The existing test suite, updated integration coverage, lint, type checks,
   and package build pass; pre-existing failures and blocked real-model checks
   are separately reported rather than masked. The required real-model matrix
   passes before final acceptance; a merge exception does not count as passing
   validation or completing the workstream.
9. The migration notes identify the changed search default, removed mode
   argument, public Python facade and legacy component boundary, score semantics,
   edit/reindex lifecycle, model/index prerequisites, and interface/report
   version changes.

## Explicitly out of scope

- Generic text ingestion or new source connectors.
- Agent-authored graph writes and enrichment lifecycle.
- New entity extraction or identity reconciliation behavior.
- Natural-language query routing/planning, general graph execution, aggregates,
  and continuation cursors.
- New retrieval models, ranking strategies, or revised fixture gold labels.
- Autonomous query agents or narrative answer generation.

These remain in `ROADMAP.md`. This work delivers a coherent product surface for
what exists today and establishes the baseline for those larger changes.
