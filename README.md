# Local Knowledge Graph

[![CI](https://github.com/markgar/local-knowledge-graph/actions/workflows/ci.yml/badge.svg)](https://github.com/markgar/local-knowledge-graph/actions/workflows/ci.yml)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue.svg)](https://www.python.org/downloads/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A local-first, evidence-backed knowledge engine built on **canonical SQLite and
an optional Ladybug graph projection**. SQLite owns exact supplied text, immutable
revisions, identities, registered knowledge schema, entities, assertions and their
evidence/history. Ladybug provides a rebuildable graph of eligible knowledge for
one exact authorized scope; it is not a second source of authored truth.

Canonical supplied-document search runs keyword and semantic retrieval, fusion
and reranking against the SQLite evidence store. Graph building does not replace
that search pipeline or extract knowledge from prose. The separate Markdown
demonstration has its own database, manifest-driven ingestion and search CLI.

**Pre-alpha:** this is evidence retrieval, not a production question-answering
system. It does not generate answers, infer entities or contradictions, or
reliably decide whether a natural-language question is answerable. Existing
retrieval-quality gates remain unmet.

## Using the KG with an agent

The [use-knowledge-graph skill](.github/skills/use-knowledge-graph/SKILL.md) teaches
an agent the evidence/knowledge model, public interfaces, query and ingestion
recipes, capability checks and stopping rules. Use it to operate an existing
authorized KG instead of rediscovering the interfaces from source code.

For example: `Use /use-knowledge-graph to find the decisions directly recorded for
this project and show their supporting evidence.` The host must provide the store
connection and authorized scope; the skill does not install a tool server.
In Copilot CLI, use `/skills reload` and `/skills info use-knowledge-graph` if the
new skill has not been discovered in the current session.

## How the pieces fit

1. **Supply evidence.** The embedding application registers trusted local identity
   and policy, then submits exact text/metadata/anchors through `EvidenceService`.
   Source connectors and automatic file intake are not part of that service.
2. **Prepare the needed representation.** `IndexService` publishes passages and
   vectors for search. Explicit enrichment writes registered entities/assertions
   with exact anchor/passage support to SQLite; it does not run an extraction agent.
3. **Read or project.** `EvidenceService`/`KnowledgeService` return exact evidence
   and knowledge; `EvidenceSearchService` and `QueryService` execute their supported
   canonical queries. The optional private graph builder projects complete eligible
   entities, relationships and explicit decisions into Ladybug, preserving authored
   identities and proofs rather than selecting a search-result subset.

Native graph queries run in the [developer example](#optional-disposable-graph-example)
and acceptance checks. `kg.graph.LocalGraphSession` manages reusable exact-scope
graphs, guarded private reads and controlled writes; public business traversal/join
facades are not implemented. A canonical write invalidates the captured graph generation;
leftover graph files cannot establish freshness. Start with the
[evidence example](#generic-evidence-service) for the canonical Python API, or the
[Markdown demonstration](#markdown-demonstration) for local file/CLI usage. Their
databases are separate and not interchangeable.

## What is available

| Capability | Current behavior |
| --- | --- |
| Generic evidence | `kg.evidence`: atomic supplied-document writes/removal, ordered batches, exact UTF-8 content, scoped history/anchors/citations, durable retry receipts and trusted local policy. |
| Standalone indexing | `kg.indexing.IndexService`: immutable passages, fenced attempts, actual pinned-provider vector projections, incremental reuse/rebuild, model-free readiness and bounded cleanup. No coordinated job execution. |
| Canonical search | `kg.indexing.EvidenceSearchService`: scoped lexical/dense candidates, fusion/deduplication, reranking and exact supplied-source citations in one guarded snapshot. Both providers and complete matching projections are mandatory. |
| Processing control | `kg.processing`: trusted plan/worker registration, scheduling/deduplication, fenced claims/heartbeats/failure, bounded recovery, and idempotent retry episodes. Controls do not execute or acknowledge work. |
| Owned knowledge | Atomic anchor/passage-backed entities, independent entity support, aliases, identifiers, explicit passage mentions and typed assertions through `EvidenceService.write`; `KnowledgeService` current/history reads and immutable schema registration. Explicit decision assertions produce distinct submitted records. |
| Markdown demonstration | Manifest-selected local Markdown, explicit records, seed entities, structured reads, source context and revision comparison in its separate database. |
| Demonstration search | Full local keyword + semantic retrieval, fusion/deduplication and reranking; matching vector preparation is required. No keyword-only fallback. |
| Foundation values | Strict `foundation/1` request/result validation. Document and bounded enrichment operations execute through `EvidenceService`; canonical anchor/passage-evidence plans execute through `QueryService`. Whole-set seed replacement is not implemented. |
| Canonical queries | `kg.query.QueryService`: full canonical ranked search, historical anchor/passage reads and actual K1 exact entity resolution, explicit decision records/counts, bounded retained support inspection, spawned deadline supervision and fresh release authorization. Paths remain unsupported. |
| Execution diagnostics | Evidence, indexing, processing and query calls retain bounded, authorized in-memory summaries. Named explained wrappers execute once; detailed traces and source quotes require opt-in. |
| Optional local graph | `kg.graph.LocalGraphSession` lazily builds/reuses an exact-scope disposable Ladybug graph, explicitly refreshes it and serializes controlled canonical writes. SQLite stays authoritative; business graph queries remain private/unimplemented. |

There are no live email/Teams connectors, inference/extraction providers, general
query planner, continuation service, or source-level ACL/purge service.
The CLI is a local tool, not an authenticated network service.

For canonical evidence queries, see [the Python example](examples/query_anchor.py)
and [the executable query contract](CONTRACTS.md#canonical-evidence-queries).
Use the service as a context manager and guard executable Python entry points
with `if __name__ == "__main__":` because each execution spawns a fresh worker.

For dependent decision counts, see [the count and support example](examples/query_decisions.py).
Counts cover distinct submitted assertion IDs, not inferred real-world events.
Records display at most 1,000; counts enumerate the eligible selection to EOF or
an explicit public-budget lower bound. The same service can inspect retained
membership in ordinal slices of at most 1,000 for five minutes. Any canonical
write invalidates the set; close/restart loses it. This is not durable continuation.

For a supplied-document ranked query with **controlled providers and no downloads**:

```bash
uv run python examples/query_search.py --database /tmp/query-search.sqlite3
```

Use a fresh path; optionally pass `--text-file document.txt --query "release"`.
The example runs real intake, indexing and supervised full search, then inspects
exact quotes/citations. Its deterministic providers demonstrate composition, not
model quality or natural-language answering. A later literal evidence request is
a separate read, not an atomic search-to-evidence dependency. Ordinary QueryService
search uses the pinned real providers and requires an approved prepared model cache.

## Generic evidence service

Run the self-contained Python example with a **fresh, separate** target:

```bash
uv run python examples/evidence_intake.py --database /tmp/evidence-demo.sqlite3
```

It registers a trusted principal/namespace/writer binding, writes exact text,
discovers canonical anchors, and resolves a saved citation. Repeating the example
replays the original receipt under the same retry key. See
[the example](examples/evidence_intake.py), [service API](CONTRACTS.md#evidence-service-api),
and [storage semantics](SPEC.md#generic-evidence-store).

Evidence operations preserve their ordinary result shapes. Discover summaries with
`service.diagnostics.recent(scope)` or
`service.diagnostics.for_request(scope, request_id)`, then fetch a report by ID.
Use `write_explained(request)` or `citation_explained(scope, citation, options)`
for the ordinary outcome plus an `execution-report/1` sidecar. Reports expire
after five minutes, remain scope/target-authorized, and are not durable audit logs.
Trusted bootstrap/schema provisioning is outside this scoped reporting API;
ordinary scoped read/write/seed operations are not excluded.
See [execution diagnostics](CONTRACTS.md#execution-diagnostics) for limits,
quote opt-in and unavailable/redacted results.

The service does not read source files, parse Markdown, infer knowledge, index
or search. Submitted enrichment does not mark a source fully processed.
Every new state initially reports indexing/enrichment **pending** with
`processor_not_available`. Intake retains passage policy intent without running
processing. The private canonical passage kernel supports `codepoint-window/1`
and `supplied-anchors/1`. The separate `IndexService.process` runs the standalone
index lifecycle; it is not an E4 worker or a CLI command.
`service.passages(scope, document_id, state_version)` reads an immutable published
set, or explicitly reports `not_processed`. Published passage and generated-anchor
citations also resolve through ordinary evidence reads, independently of vectors.

For an existing supplied document, see [the indexing example](examples/indexing_lifecycle.py)
and [standalone indexing contract](CONTRACTS.md#standalone-indexing-api).
Processing initializes the configured local embedding model even when checking an
unchanged projection or processing an empty document. Prepare only approved model
caches; there is no lexical fallback. `status` and `pending` do not load models:
ready means durable completeness, with provider compatibility explicitly unverified.
Indexing does not change enrichment or invalidate knowledge when rebuilding vectors.

For an actual supply -> index -> search example, use a fresh database and approved
local model caches:

```bash
HF_HUB_OFFLINE=1 uv run python examples/canonical_search.py --database /tmp/canonical-search.sqlite3
```

`EvidenceSearchService(database, identity).search(scope, query)` returns exact
quotes, immutable citations, raw reranker scores and a read witness. Any visible
active document without a matching complete projection blocks the whole search.
Empty scopes still initialize both providers and encode the query; there is no
keyword fallback. Calls have a 30-second deadline and bounded work allowances;
source, policy or other canonical commits during a search withhold all results.
See [canonical search contracts](CONTRACTS.md#canonical-full-search).
`QueryService` invokes the same pipeline under its own spawned deadline/budgets.
Neither API implements processing coordination or establishes real-model
quality/workload acceptance.

The canonical store uses the complete `evidence-store/3` schema and the `evidence/2`
service interface. Initialization verifies the actual schema and its recorded
manifest, not just a version marker. Incompatible stores are refused without
repair: use a fresh path and resupply sources, policy/schema and explicit knowledge.
There is no `/2` migration or dual-format reader. Schema presence alone does not
enable service operations. The separate
`ProcessingService` exposes only the control-plane operations below.
There is no snapshot-completion or purge API; omitted batch documents stay active.
The CLI and search pipeline below operate on the Markdown demonstration,
not on `EvidenceDatabase`.

Evidence IDs and revision history survive updates/restores within one store;
creating a fresh store does not reconstruct them. No migration or compatibility
layer is provided, and initialization never deletes an incompatible file.

## Processing control

For the Python processing control plane, run
`uv run python examples/processing_control.py --database /tmp/processing-demo.sqlite3`
with a fresh path. It provisions one document plan/worker, schedules and claims
exact source state, renews the lease and reports an explicit resource-budget
failure with a persisted retry due time. It does **not** process content
or mark indexing ready. See [processing API](CONTRACTS.md#processing-control-api)
for registration authority, lease/retry limits, and the explicitly absent
completion/batch/snapshot operations.

## Knowledge schema provisioning example

```bash
uv run python examples/knowledge_schema.py --database /tmp/knowledge-registry.sqlite3
```

This self-contained example initializes a fresh store and registers a typed corpus
schema with provisioned `LocalAdminAuthority`. A repeat is unchanged; a different
definition/version conflicts. It creates no entities or submitted decisions and
does not enable enrichment/query capabilities. Trusted schema provisioning has no
scoped execution report. See the [registry API](CONTRACTS.md#knowledge-registry-api)
and [storage behavior](SPEC.md#immutable-knowledge-registry).

For real source-backed enrichment, run
`uv run python examples/knowledge_enrichment.py --database /tmp/knowledge-demo.sqlite3`.
It discovers an actual supplied anchor, atomically submits an entity and explicit
decision, and reads the assertion's immutable provenance. Repeating it replays the
same durable IDs. Typed predicates and decision encoding come from the registered
schema, not language inference. Current/history reads and ordinary scoped write
reports and exact owned assertion withdrawal are available; whole-set seed
replacement, atomic correction/supersession and traversal
remain unsupported. `QueryService` composes the real
entity/decision reader for public decision selections and counts.
See [knowledge service contracts](CONTRACTS.md#knowledge-enrichment-and-reads).

To withdraw one exact owned assertion while preserving its evidence/history:

```bash
uv run python examples/withdraw_assertion.py
```

The example creates a temporary store, keeps an independent decision current,
replays the withdrawal receipt, reads the original quote/history, and submits a
separate correction. `--database <new-path>` retains the store without ever
overwriting an existing target. Optional `--graph` uses `LocalGraphSession.write`
and one explicit refresh (requires `--extra graph`); a refresh failure does not
undo a saved withdrawal. No entity retirement or other-owner withdrawal is allowed.

Add `--passages` to `examples/knowledge_enrichment.py` to use the private E3 passage producer and submit
an explicit mention alongside the decision. It needs no models or vectors.
Passage support preserves exact immutable source/state membership; mentions do
not infer relationships or merge same-named entities. Vector-only rebuilds keep
knowledge valid, while source/metadata/passage-policy changes require fresh support.

## Optional disposable graph example

On **macOS 15+ ARM64 with CPython 3.12**, install the optional pinned
`ladybug==0.20.4` runtime and use a fresh synthetic output directory:

```bash
uv run --extra graph python examples/graph_build.py --output /tmp/graph-demo
uv run --extra graph python examples/graph_build.py --case varied-10000 --output /tmp/graph-10k
uv run --extra graph python examples/graph_session.py --output /tmp/graph-session-demo
```

The example supplies explicit canonical facts without inference models, builds,
checkpoints and reopens the graph, executes native queries, compares all authored
IDs and source/seed/endpoint proof associations, and disposes the stage. Installing
the graph extra may require an approved package download; the example itself needs
no model downloads. Output contains canonical SQLite and a measurement receipt,
not a persistently admitted graph. Base-package imports and search need no graph extra.

The session example exercises lazy build, unchanged reuse, canonical proof hydration,
controlled writes, explicit refresh, saved receipts after graph failure, and
untrusted restart. A session binds one exact identity/scope; it never silently
widens scope or treats a reopened file as fresh. `status()` is content-free
last-known state, not a freshness certificate. External commits invalidate the
next read; finish the import and refresh rather than retrying within that request.
Canonical writes remain saved even if graph work fails. Close the session before
replacing/restoring its canonical database file.

Each controller has one FIFO SQLite owner thread and at most eight waiting calls.
Cold reads/refresh share a 300-second admitted deadline; warm reads/writes have
30 seconds, including queue time. Graph reads use cooperative native timeouts of
at most five seconds and separate 8 MiB ceilings for cumulative retained output
and the final returned answer, sharing one scratch pool. Pending cleanup blocks
new builds; retry `refresh()` while open or `close()` after closing. Close may
report `close_pending` while native work/cleanup still owns resources. Business
traversal, decision joins and retained inspection are not public graph APIs.

The complete varied-10,000-decision native build/reopen and exact proof/edge parity
gate passed with the pinned runtime and 256 MiB buffer configuration
([acceptance record](https://github.com/markgar/local-knowledge-graph/issues/132)).
This establishes synthetic build feasibility and fidelity, not end-to-end extraction
from real notes, general performance targets or production quality.

**Experimental native risk:** Ladybug executes in-process with a 256 MiB buffer
pool and two threads. One graph-build operation carries a deadline of at most
300 seconds across its phases; the buffer and cooperative deadline are not hard
total RSS/time limits or process isolation. The separate SQLite TEMP cap remains
128 MiB. Native checkpoint exhaustion and teardown can crash the Python host;
exception handling/cleanup residue cannot contain a segmentation fault. This
limitation is currently accepted; persistent worker isolation is deferred to
[#137](https://github.com/markgar/local-knowledge-graph/issues/137).
Larger buffer capacity does not fix failure containment. Never treat a leftover
graph file as proof of canonical freshness. See
[projection semantics](SPEC.md#private-disposable-graph-staging) for exact scope,
source-observer and cleanup ownership.

## Install

Python 3.12+ and [uv](https://docs.astral.sh/uv/) are required. Routine manually
dispatched CI uses Python 3.12, matching the local default. Enable `full_matrix`
when dispatching CI to additionally check Python 3.13 and 3.14; those versions
are not checked on every change.

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

For the Markdown demonstration, `dense-index` loads the embedding model and builds
vectors. It does **not** prepare the reranker: the first `search`, even one returning
no hits, also initializes
`cross-encoder/ms-marco-MiniLM-L6-v2` at its pinned revision. A successful empty
search therefore requires both providers and a matching current index.

Allow disk space for dependencies, model caches, the canonical database, and
separate vector projections, plus working memory for both models and inference
batches. CPU is supported; CUDA or Apple MPS may be selected when available.
Larger batches, long passages, and the Qwen profile use more resources; reduce
`dense-index --batch-size` if indexing memory is constrained. Resource requirements
depend on corpus size, model profile and runtime; no minimum RAM/disk guarantee is
established.

## Markdown demonstration

The following manifest, `kg` CLI and `kg.retrieval` examples use the separate
Markdown demonstration database (`kg.db.Database`, `src/kg/schema.sql`), not
`EvidenceDatabase` or Ladybug. Its explicit wikilink relationships and subject
expansion are SQLite-backed demonstration behavior, not the canonical graph API.

### Configure a corpus

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

### Ingest, prepare, search

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

JSON is a list of `SearchResult` objects with exact quotes, record/anchor
IDs, source paths, revisions, and heading paths. **Preserve the returned order.**
`rank` is a raw cross-encoder score, higher is better, with record-ID tie-breaking.
It is not confidence and cannot be compared across queries, models, or BM25
scores. Do not sort ascending or apply a BM25 cutoff.

#### Matching profile and contextual settings

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

#### Inspect ingestion and search

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
Ordinary ingestion returns `IngestResult`; explained ingestion returns
`IngestReport` version 1.

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

### Inspect citations, context, and history

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

### Explicit records and current state

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

### Maintenance and errors

After editing sources, run `ingest` and then rebuild each affected dense
projection with its matching profile/contextual settings. Search fails explicitly
while its index is stale; ingestion does not automatically prepare semantic search.
After upgrades, reingest existing corpora and rebuild projections. Ready provider
instances are reused within a Python service lifetime, but freshness is rechecked
on every call; CLI processes load independently.

The demonstration index is rebuildable from source. Keep its database
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
other explanation-specific codes are preserved. `dense-index` reports
`dense_index_failed`. Typer grammar/range errors use usage-error text, even
with JSON requested. Failed ingestion sources exit 1. No missing dependency is
reported as an empty successful result. `kg --verbose ingest ...` emits operational
diagnostics to stderr without report bodies or quotes.

### Agent and Python integration

`kg capabilities --format json` advertises **interface version 2**. The ordinary
search result is a list; explained search returns report version 2. Ingestion
reports use version 1. Prepare a matching dense index and model cache before
searching. `--embedding-profile` and `--contextual` are supported on indexing and
search; `--query-mode` is unsupported and rejected rather than bypassing stages.

The demonstration's Python search entry point is:

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
`RetrievalService` provides structured/evidence reads. Its strict/natural
lexical search, version-1 explanation, and the dense/hybrid/reranked service classes
are low-level composition and evaluation APIs, not alternate product interfaces.
They do not enforce the product facade's stricter readiness behavior.

[`examples/cited_status.py`](examples/cited_status.py) is a model-independent
subprocess client for structured status, error handling, and cited output:

```bash
uv run python examples/cited_status.py corpora/example.yml Atlas
```

The example defaults to `--since 30d`; use its `--since` option to change that
window. The underlying `kg status` command has no default time filter.

## Foundation value validation

`kg.models.foundation` supplies immutable, strict `foundation/1` values for
document-write descriptions, bounded enrichment changes and dependent query plans.
It checks shape, declared scope, exact source slices, references and result
correlation. Models alone do **not** ingest, execute, authorize or persist requests;
the canonical services execute their documented subsets separately, including
document/enrichment writes and supported query plans. There is no CLI command for
submitting these values.

```python
from pathlib import Path
from kg.models.foundation import WriteRequest

request = WriteRequest.model_validate_json(
    Path("corpora/foundation/enrichment.json").read_text(encoding="utf-8")
)
payload = request.model_dump_json()
schema = WriteRequest.model_json_schema()
```

Use [CONTRACTS.md](CONTRACTS.md) for the contract reference,
[corpora/foundation](corpora/foundation/README.md) for examples, and
[benchmarks/foundation](benchmarks/foundation/README.md) for reproducible inputs
and proposed engineering targets. Those targets are not measured performance.

## Development and documentation

For code-affecting changes:

```bash
uv run pytest
uv run ruff check .
uv run mypy
uv build
```

For docs/instruction-only changes, review the diff and relevant links instead;
do not run the full Python suite or dispatch CI. See
[CONTRIBUTING.md](CONTRIBUTING.md) and the current [contracts](SPEC.md).
Routine tests use controlled providers; they do not download/run real models.
Authored fixtures, reviewed gold, citations and component assertions are preserved.

| Document | Purpose |
| --- | --- |
| [SPEC.md](SPEC.md) | Current SQLite/Ladybug architecture, canonical service behavior and separate Markdown demonstration. |
| [CONTRACTS.md](CONTRACTS.md) | Executable evidence, knowledge, indexing, processing, query and diagnostic APIs; separately identified foundation value validation. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Development layout, validation and contribution rules. |

Planned work, implementation designs and delivery progress live in the
[open bounded issues](https://github.com/markgar/local-knowledge-graph/issues?q=is%3Aissue%20is%3Aopen),
grouped by [milestones](https://github.com/markgar/local-knowledge-graph/milestones),
not in repository planning documents. The selected issue's current scope and
linked approved requirements govern implementation.

Evaluation tooling covers [retrieval quality](benchmarks/qasper/README.md),
[agent workflows](benchmarks/agent/README.md),
[work-memory comparisons](benchmarks/work_memory/README.md), and
[product search parity](benchmarks/productization/README.md).
Component benchmark results do not establish product-search quality or readiness.

Report security issues under [SECURITY.md](SECURITY.md).
Released under the [MIT License](LICENSE).
