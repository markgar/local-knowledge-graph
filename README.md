# Local Knowledge Graph

[![CI](https://github.com/markgar/local-knowledge-graph/actions/workflows/ci.yml/badge.svg)](https://github.com/markgar/local-knowledge-graph/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A local-first, evidence-backed knowledge engine for configured Markdown corpora.
SQLite stores immutable source revisions and exact citations. Search combines
keyword and semantic retrieval, fuses and deduplicates candidates, then reranks
them with a local cross-encoder. Structured status, tasks, and evidence reads
remain available without semantic models.

**Pre-alpha:** this is evidence retrieval, not a production question-answering
system. It does not generate answers, infer entities or contradictions, or
reliably decide whether a natural-language question is answerable. Existing
retrieval-quality gates remain unmet. The public interface migration does not
improve the underlying ranking algorithm or establish production readiness.
The productization transition is [accepted after final combined validation and
review](benchmarks/productization/final-integrated-2026-09-20/README.md).

## Install

Python 3.12+ and [uv](https://docs.astral.sh/uv/) are required; CI covers Python
3.12, 3.13, and 3.14.

```bash
git clone https://github.com/markgar/local-knowledge-graph.git
cd local-knowledge-graph
uv sync --extra dev
```

Use `uv run` to run commands in the project-local `.venv`. The checked-in
[`uv.toml`](uv.toml) uses the package feed approved for the primary Microsoft
development environment and contains no credentials. If it is unavailable,
use an index approved for your environment; do not bypass organizational
network controls or change feeds merely to evade a block.

### First-use models and resources

Embeddings and reranking run locally through Sentence Transformers/PyTorch;
corpus text is not sent to a remote inference API. First use may download pinned
model files through Hugging Face's normal cache path. Use only approved download
sources or a prepopulated, approved local cache. `HF_HOME` can select a cache;
`HF_HUB_OFFLINE=1` requires cached files instead of network access. If a required
model or dependency is blocked/unavailable, report the blocked host/error and
prepare it through an approved route. There is no keyword-only fallback.

`dense-index` loads the embedding model and builds vectors. It does **not** prepare
the reranker: the first `search`, even one returning no hits, also initializes
`cross-encoder/ms-marco-MiniLM-L6-v2` at its pinned revision. A successful empty
search therefore requires both providers and a matching current index.

Allow disk space for dependencies, model caches, the canonical database, and
separate vector projections, plus working memory for both models and inference
batches. CPU is supported; CUDA or Apple MPS may be selected when available.
Larger batches, long passages, and the Qwen profile use more resources; reduce
`dense-index --batch-size` if indexing memory is constrained. Historical
[QASPER measurements](benchmarks/qasper/RESULTS.md) recorded embedding caches
of 287.7 MiB for GTE and 1,151.6 MiB for Qwen, excluding the reranker and runtime.
Those are measurements on one setup, not minimum RAM/disk guarantees.

## Configure a corpus

[`corpora/example.yml`](corpora/example.yml) is ready to use. To define your own:

```yaml
corpus_id: example
display_name: Example corpus
vault_root: notes
database: ../.kg/example.sqlite3
include:
  - "**/*.md"
allow_symlinks: false
max_source_bytes: 5000000
seed_entities:
  - entity_id: example-project
    name: Example Project
    entity_type: project
    aliases: [Example]
metadata_fields:
  event_time: date
```

Paths are relative to the manifest. Corpus configuration is data, not Python.
Only selected sources are ingested. Symlinks are rejected by default and oversized
sources fail explicitly. Generated databases under `.kg/` are ignored by Git.

## Ingest, prepare, search

```bash
uv run kg ingest --manifest corpora/example.yml
uv run kg dense-index --manifest corpora/example.yml
uv run kg search "What supports the release?" --manifest corpora/example.yml --format json
uv run kg actions Atlas --manifest corpora/example.yml --status open --format json
uv run kg status Atlas --manifest corpora/example.yml --format json
```

Search always runs **keyword + semantic -> fusion/deduplication -> reranking**.
It returns evidence, not an answer. `--subject` restricts by names, approved
aliases, document inheritance, and explicit graph relationships up to two hops.
Similar names do not merge. `--source` restricts source paths, `--since` accepts
durations such as `12h`, `30d`, or `4w`, and `--limit` accepts 1..100 (default 20).
Date filters use configured event time, then observed modification/ingestion time
when absent. Search sees active current revisions, including superseded source
assertions; use structured status/actions to inspect effective records.

JSON remains a list of `SearchResult` objects with exact quotes, record/anchor
IDs, source paths, revisions, and heading paths. **Preserve the returned order.**
`rank` is a raw cross-encoder score, higher is better, with record-ID tie-breaking.
It is not confidence and cannot be compared across queries, models, or historical
BM25 scores. Do not sort ascending or apply an old BM25 cutoff.

### Matching profile and contextual settings

Default `gte-modernbert` uses the pinned `Alibaba-NLP/gte-modernbert-base`.
The other fixed profile, `qwen3-embedding-0.6b`, uses pinned
`Qwen/Qwen3-Embedding-0.6B`, normalized 1,024-dimensional vectors, its official
query instruction, and uninstructed documents. Both are Apache-2.0 models.
Exact model/configuration pins are in [`dense.py`](src/kg/retrieval/dense.py)
and [`rerank.py`](src/kg/retrieval/rerank.py).

Always use the **same profile and contextual setting** for preparation and search:

```bash
uv run kg dense-index --manifest corpora/example.yml --embedding-profile qwen3-embedding-0.6b --contextual
uv run kg search "What is holding up approval?" --manifest corpora/example.yml --embedding-profile qwen3-embedding-0.6b --contextual --format json
```

`--contextual` includes source title and heading path in embeddings and reranking,
without changing lexical retrieval, query text, or exact quotes. It is an opt-in
representation, not an LLM-generated summary. Profile-specific and contextual
projections coexist. Their compatibility includes model revision, runtime
versions, actual inference device/dtype, encoding behavior, and corpus fingerprint;
changing hardware/runtime or source representation can require rebuilding.

### Inspect ingestion and search

```bash
uv run kg ingest --manifest corpora/example.yml --explain --format json
uv run kg search "release evidence" --manifest corpora/example.yml --explain --format json
uv run kg search "release evidence" --manifest corpora/example.yml --explain --explain-limit 200 --include-quotes --format json
```

Ingestion explanations **perform ingestion**, not a dry run. They show each
document's outcome/reasons, revisions, stored counts, entities, exact anchors,
explicit records, and state diagnostics. Counts describe stored evidence, not
newly created rows. `--explain-limit` bounds anchor/record lists independently
per document (default 50, 1..200); truncation is explicit. Quotes require opt-in.
Ordinary ingestion output and ingestion report version 1 are unchanged.

Search explanations are `ProductSearchExplanation` **report version 2**.
They capture the actual full execution: effective filters/configuration,
projection and model identities, natural lexical expression, complete stage
counts, lexical/dense positions and scores, actual fusion contributions,
reranker scores, final positions, and graph-scope supporting evidence.
Absent stage memberships/contributions are explicit JSON `null`, not zero.

The candidate display defaults to 50 entries; `--explain-limit 1..200` requires
`--explain`. It does not change calculations or shorten the separate final hits
list. `displayed_candidates`, `total_candidates`, and `truncated` expose the bound.
Returned hits appear first, followed by remaining reranked and then fused
candidates. `--include-quotes` also requires `--explain`; hit quote keys are
omitted otherwise. Paths, headings, names, query text and metadata remain visible:
reports are **not anonymized**. Ordinary search and evidence reads include quotes.

Both search paths reject intervening database commits, including an edit and
restore that leaves the final fingerprint unchanged. `search_state_changed`
means retry once writes finish, not partial success; unrelated corpus writes in
the same database can also invalidate a call. Telemetry is not an explanation of
model reasoning or evidence of answerability.

## Inspect citations, context, and history

Use IDs and source paths from returned results with the same manifest:

```bash
uv run kg evidence <record-id> --manifest corpora/example.yml --format json
uv run kg source-range <anchor-id> --manifest corpora/example.yml --format json
uv run kg source-context <anchor-id> --manifest corpora/example.yml --max-anchors 50 --format json
uv run kg revisions <source-path> --manifest corpora/example.yml --format json
uv run kg compare-revisions <source-path> --manifest corpora/example.yml --format json
```

These commands do not load semantic models or require vectors. Context expands
within the selected anchor's stored revision and containing section, including
subsections until the next sibling/ancestor heading. It returns the selected range,
section heading, source-ordered anchors and explicit truncation. The centered
window always retains the selected anchor (`--max-anchors` 1..200). These are
parsed evidence blocks, not reconstructed full sections; code blocks may be
absent and nested list anchors can overlap.

Historical citations remain valid after source edits, removals, and restores.
Revision comparison defaults to the immediate predecessor of the target's latest
activation; explicit `--from`/`--to` select revisions. Legacy indexes without
activation history require explicit IDs rather than guessed transition order.

## Explicit records and current state

```markdown
## Actions

- [ ] Review the release. [owner:: Avery] [due:: 2026-10-01] [key:: review-v1]

## Decisions

- Use SQLite for the local index.

## Blockers

- Approval is still pending.
```

Bulleted or numbered checkboxes supply task status. Exact Decision(s), Blocker(s),
and Conflict(s) headings classify explicit records. Wikilinks and approved aliases
create cited mentions/relationships. Missing owners and dates remain missing;
prose promises do not become tasks and a completion email does not close another
document's checkbox.

For an explicit cross-document update, give the replacement its own key:

```markdown
- [x] Review completed. [owner:: Avery] [key:: review-v2] [supersedes:: review-v1]
```

Keys are corpus-scoped and case-sensitive. Replacements are complete same-kind
task/decision records, not field patches. Effective actions/decisions/status
suppress uniquely resolved predecessors while preserving citations and inherited
subject scope. Missing/duplicate keys, cycles, cross-kind or competing updates
produce diagnostics, not a latest-date winner. Removing/editing the replacement
removes its former effect. This is not historical as-of evaluation.

```bash
uv run kg ingest --manifest corpora/atlas-state.yml --explain --format json
uv run kg record-state --manifest corpora/atlas-state.yml --format json
uv run kg status Atlas --manifest corpora/atlas-state.yml --format json
```

`record-state` audits the resolver without ingestion. Ingestion explanations also
show the state snapshot. Quotes require `--include-quotes`; display limits never
limit resolution. Plain ingestion reports `state_warnings` for unresolved links,
while indexed sources remain available. Status includes these evidence gaps.

The ten-document [Atlas scenario](corpora/atlas.yml) and its
[incremental stages](corpora/fixtures/atlas-stages.yml) preserve prose-only
negative cases, exact citations, two-hop scope, isolation, edits and restores.
The separate twelve-document [Atlas state scenario](corpora/atlas-state.yml)
adds explicit completion and rescheduling. To search either, run matching
`dense-index` after ingestion. These fixtures are correctness tests, not proof
of general agent reasoning quality.

## Maintenance and errors

After editing sources, run `ingest` and then rebuild each affected dense
projection with its matching profile/contextual settings. Search fails explicitly
while its index is stale; ingestion does not automatically prepare semantic search.
After upgrades, reingest existing corpora and rebuild projections. Ready provider
instances are reused within a Python service lifetime, but freshness is rechecked
on every call; CLI processes load independently.

The pre-alpha canonical schema is rebuildable from source. Keep an old database
separately when historical revisions are needed: a fresh index from current files
cannot recreate past contents or activation history. Parser upgrades do not rewrite
historical source anchors; legacy malformed anchors require a fresh database if
corrected current evidence is needed.

Domain failures with `--format json` emit stderr objects and exit 2, for example:

```json
{"error":"dense_index_unavailable","message":"... run 'kg dense-index' with matching settings ..."}
```

Search errors include `invalid_manifest`, `invalid_query`,
`dense_index_unavailable`, `reranker_unavailable`, and `search_state_changed`;
other explanation-specific codes are preserved. `dense-index` retains
`dense_index_failed`. Typer grammar/range errors retain usage-error text, even
with JSON requested. Failed ingestion sources exit 1. No missing dependency is
reported as an empty successful result. `kg --verbose ingest ...` emits operational
diagnostics to stderr without report bodies or quotes.

## Agent/Python integration and migration

`kg capabilities --format json` advertises **interface version 2**. The ordinary
search list shape and non-search operations are preserved; the default pipeline,
score meaning, and explanation contract changed.

**Remove `--query-mode` from all product CLI calls, even `--query-mode reranked`.**
Every old invocation fails with explicit migration guidance rather than selecting
a stage or silently ignoring the flag. Prepare a matching dense index and model
cache before using unqualified search. `--embedding-profile` and `--contextual`
remain supported on both indexing and search.

The supported Python entry point is:

```python
from pathlib import Path
from kg.config import load_manifest
from kg.db import Database
from kg.retrieval import SearchService

corpus = load_manifest(Path("corpora/example.yml"))
service = SearchService(Database(corpus.database), corpus.corpus_id)
hits = service.search("release evidence", limit=5)
report = service.explain_search("release evidence", limit=5, trace_limit=50)
payload = report.model_dump(mode="json")  # Keep explicit null stage memberships.
```

This assumes ingestion and matching indexing have already completed.
`RetrievalService` still provides structured/evidence reads. Its strict/natural
lexical search, legacy version-1 explanation, and the dense/hybrid/reranked service
classes remain low-level compatibility and evaluation APIs, not alternate product
interfaces. They do not acquire the product facade's stricter readiness behavior.

[`examples/cited_status.py`](examples/cited_status.py) is a model-independent
subprocess client for structured status, error handling, and cited output:

```bash
uv run python examples/cited_status.py corpora/example.yml Atlas
```

## Development, evidence, and future work

```bash
uv run pytest
uv run ruff check .
uv run mypy
uv build
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and the current [contracts](SPEC.md).
Routine tests use controlled providers; they do not download/run real models.
Authored fixtures, reviewed gold, citations and component assertions are preserved.

[QASPER results](benchmarks/qasper/RESULTS.md) retain historical relevance,
latency, resource costs, and the failed answerability calibration. The original
[experiment ledger and acceptance gates](benchmarks/history/evidence-mvp.md)
remain available; no unmet gate was lowered. The
[agent baseline](benchmarks/agent/README.md) and
[work-memory comparison](benchmarks/work_memory/README.md) explicitly preserve
historical lexical strategies through internal evaluation adapters.
[Productization parity validation](benchmarks/productization/README.md) is
separately labeled integration evidence, not improved relevance or new gold.

[ROADMAP.md](ROADMAP.md) covers unimplemented generic text ingestion, source
plugins, agent-authored enrichment, identity reconciliation, query planning and
continuation. [SPEC.md](SPEC.md) defines the current product contracts.

Report security issues under [SECURITY.md](SECURITY.md).
Released under the [MIT License](LICENSE).
