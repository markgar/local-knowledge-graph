# Local Knowledge Graph: current contracts

Status: implemented capabilities in a **pre-alpha** package. Productization
acceptance is recorded in the
[final integrated validation and review](benchmarks/productization/final-integrated-2026-09-20/README.md).
Interface changes are not a
claim of production readiness, improved relevance, or calibrated answerability.

This document describes current behavior. [`ROADMAP.md`](ROADMAP.md) describes
future query planning, generic ingestion, and agent-authored enrichment; those
capabilities are not implemented. The original implementation ledger and research
sequence are preserved in [benchmark history](benchmarks/history/evidence-mvp.md).
Historical stages are not prerequisites for developing product features.

## Boundaries and invariants

The package ingests manifest-selected local Markdown, stores canonical evidence
in SQLite, and exposes cited search and structured reads through Python and a
thin CLI. Configuration is data: subjects, aliases, paths, and corpora must not
require parser or retrieval code changes. The caller generates narrative answers;
retrieval does not generate an answer or infer truth from model knowledge.

- Documents have stable identities, including unambiguous moves. Reused paths
  receive an unused deterministic identity rather than overwriting a moved document.
- Revisions are immutable content versions. Anchors preserve exact offsets,
  structural and heading paths, quotes, and quote hashes within a revision.
- Every derived record resolves to source evidence. Historical citations retain
  their original revision and title, even after edits, removals, or restores.
- Current retrieval uses active current revisions of the requested corpus.
  Other corpora, removed documents, and historical revisions cannot alter its
  lexical ranking statistics. Model/vector projections never replace provenance.
- No inferred entities, relationships, owners, decisions, blockers, contradictions,
  or cross-source completion are generated. Similar names are not merged.

## Configuration and ingestion

`kg ingest --manifest <corpus.yml>` validates source selection, parses Markdown,
and updates the canonical database and corpus-local lexical projection.
`load_manifest()` and `IngestService.ingest()` expose the same configuration and
ingestion boundaries to Python.

Manifests specify corpus ID/display name, vault root, database, include patterns,
seed entities/approved aliases, and optional metadata mappings such as
`event_time: date`. Paths are relative to the manifest. Symlinks are rejected by
default; sources over `max_source_bytes` fail explicitly. Source discovery,
parsing rules, and ranking remain generic across corpora.

CommonMark headings, paragraphs, lists, checkboxes, and wikilinks produce exact
source ranges. Numbered and bulleted checkboxes supply task status. Inline
`[owner:: ...]` and `[due:: ...]` fields supply optional task metadata. Items and
paragraphs under exact Decision(s), Blocker(s), and Conflict(s) headings become
explicit records. Fenced code is not classified as structured knowledge.
Approved aliases create exact mentions; wikilinks create cited relationships
between configured entities in an anchor, not inferred dependencies.

Unchanged ingestion reuses revisions without changing document state. Edits
preserve prior evidence. Restores reuse an immutable content revision but record
a new activation and its immediate predecessor. Source removal and failures
deactivate current evidence without destroying retained history. Parser/config
changes can rebuild extracted records without claiming a new content revision.

Internally, `ingest/_intake.py` translates local Markdown into the private values
in `ingest/_prepared.py`; `ingest/_writer.py` persists those values without reading
files or depending on Markdown parser objects. `IngestService` remains the public
orchestrator and owns transactions, per-source savepoints, seed synchronization,
schema adaptation, deactivation, and lexical refresh. The writer uses that existing
transaction; unchanged revisions still skip record-state validation and rebuilding.
These are internal boundaries, not a generic ingestion API or a new source format.

### Ingestion diagnostics

`ingest --explain [--include-quotes] [--explain-limit 50] --format json` returns
`IngestReport` with `report_version: "1"`; ordinary `IngestResult` is unchanged.
This performs ingestion, not a dry run. Python uses `explain`, `include_quotes`,
and `detail_limit`.

The report gives configured entities, unmatched patterns, source-ordered
document outcomes/reasons, identity, current/previous revisions and paths,
revision reuse, active state, and `records_rebuilt`. Successful documents include
stored counts, mentioned entities, source anchors/passages, structured records,
extraction rules, and evidence IDs. Counts are stored revision totals, not rows
newly created by this run; historical anchors may outnumber current passages.

Anchor and record lists are independently bounded to 50 per document (1..200),
ordered by source offsets and identity ties, with explicit truncation and complete
counts. Quotes require opt-in; metadata is not anonymized. Reports use the
ingestion transaction; report construction failure aborts ingestion. Report bodies
and quotes are not stored in `ingest_run` or operational logs. Malformed YAML
errors omit source snippets. Failed sources produce a nonzero ingestion exit.

## Index preparation and model readiness

The workflow is **ingest -> matching dense-index -> search**. Ingestion does not
build vectors or load semantic models. `kg dense-index` / the low-level
`DenseRetrievalService.build_index()` build a disposable sqlite-vec projection
beside the canonical database, keyed to exact passage/anchor/revision identities.

`--embedding-profile` selects `gte-modernbert` (default) or
`qwen3-embedding-0.6b` on both indexing and search. Model names, pinned revisions,
dimensions, normalization, prompts, and encoding behavior are defined in
[`dense.py`](src/kg/retrieval/dense.py). GTE retains its compatibility-default
projection path. Qwen uses its official asymmetric query instruction, 1,024
normalized dimensions, and uninstructed documents. The pinned reranker is
`cross-encoder/ms-marco-MiniLM-L6-v2`, revision
`233902d25c440f23af6f7d6e94d2946bac0bee0a`.

`--contextual` is opt-in on both commands. It supplies `Title: <title>`,
`Heading: <path joined by / >`, and untouched passage text separated by blank
lines, omitting absent metadata. It changes neither queries nor lexical
retrieval nor canonical quotes. `title-heading-passage-v1` contextual projections
are separate from `passage-text-v1` projections and can coexist.

Projection compatibility includes profile, pinned model, runtime versions,
actual device/dtype, dimensions, normalization, context and encoding behavior,
source-text version, and corpus fingerprint. CUDA, MPS, or CPU may be selected
by the local runtime; device/runtime changes may require rebuilding. Ingestion
that changes the fingerprint invalidates the selected projection until rebuilt.

Product search validates arguments before readiness and enforces readiness on
**every call**, including empty corpora and filters matching no passages.
Missing/stale/incompatible projections, unavailable embeddings, and unavailable
rerankers fail explicitly. No keyword-only, vector-only, unreranked, or empty-list
fallback represents success. Building vectors prepares embeddings, not the
reranker; the first product search initializes the latter separately.

Providers load lazily and are cached within a service lifetime, not globally
across CLI processes. The index-readiness verdict is not cached; a service must
fail after an invalidating edit and recover after rebuilding without reloading
ready providers. Only approved package/model downloads and caches may be used;
network-policy failures are blockers, never bypassed.

## Product search

```python
from pathlib import Path

from kg.db import Database
from kg.retrieval import SearchService

search = SearchService(Database(Path("index.sqlite3")), "corpus-id")
results = search.search("release evidence", subject="Atlas", limit=20)
report = search.explain_search(
    "release evidence", subject="Atlas", limit=20,
    include_quotes=False, trace_limit=50,
)
```

Both methods accept keyword-only `subject`, `limit`, `since` (a datetime;
prefer timezone-aware values), and `source_path`. Construction also accepts
`embedding_profile` and `contextual`.
There are no public stage-bypass options.

`kg search <query> --manifest <corpus.yml>` uses this facade: natural any-term
BM25 keyword candidates and semantic candidates receive identical constraints,
then canonical-ID deduplication and weighted reciprocal-rank fusion feed the
cross-encoder. The existing defaults remain lexical weight 1.0, dense weight
0.5, fusion constant 20, and candidate pools of 50 expanded for larger requested
limits. CLI result limit defaults to 20 and accepts 1..100.

Subject scoping uses configured entities/aliases, literal name boundaries,
document inheritance, and bounded undirected two-hop explicit relationships.
Atlas does not match Atlascope. No general query-to-entity resolution or new
graph candidate generator is implied. `--source` and `--since` retain existing
source/date semantics; date filtering uses configured event times with revision
timestamps as fallback. CLI durations use positive `h`, `d`, or `w` units.

Ordinary JSON is still `list[SearchResult]`: record identity/type, title, summary,
status, event time, source path/revision, anchor, heading path, exact quote,
related entities, and `rank`. Returned list order is authoritative: raw
cross-encoder score **descending**, then record ID for ties. Scores are not
confidence, not comparable across queries/models, and not historical BM25 scores.
Consumers must not re-sort ascending or apply a former BM25 threshold.

### Execution explanations

`search --explain [--include-quotes] [--explain-limit 50]` calls
`SearchService.explain_search()` and returns `ProductSearchExplanation`,
`report_version: "2"`. The trace limit is 1..200 and is valid only with
`--explain`, as is quote opt-in. Text output renders the same diagnostic object;
use `--format json` for the machine-readable contract.

The report captures the actual execution, not a second search: corpus identity
and fingerprint; effective filters and natural lexical expression; projection,
profile, model revisions/pipeline versions and contextual configuration;
candidate limits, fusion weights/constant; score meanings/tie-breakers; complete
stage counts; bounded graph scope/supporting edges; and all final hits.
`active_current_revisions_only` is true, `supersession_filter_applied` is false.

Each candidate has `record_id`, nullable lexical/dense/reranker positions and raw
scores, fusion position/score and individual contributions, shortlist membership,
and nullable final position. Missing stages and contributions serialize as
explicit JSON **null**, not zero or omitted keys. Use `model_dump()` /
`model_dump_json()` **without `exclude_none=True`**. The serializer independently
omits hit quote keys unless requested; candidate entries never carry text.

Candidates are displayed in final-hit order, then remaining reranker order,
then remaining fusion order, deduplicated and capped. `total_candidates`,
`displayed_candidates`, `trace_limit`, and `truncated` expose display bounds.
Changing display limits/quotes does not change scoring, counts, or the separate
hits list. Paths, names, headings, query/filter text and metadata remain visible;
omitting quotes is not anonymization.

Ordinary and explained search reject intervening database commits, including
edit-and-restore with an unchanged final fingerprint. Neither retries silently
nor returns partial mixed-state results. Retry after writes finish. Unrelated
corpus writes in the same database may also conservatively invalidate a call.

## Structured state and evidence reads

These operations use `RetrievalService`, not semantic search, and require no
vector index or model initialization. Their signatures and payloads are unchanged.

| CLI operation | Contract |
| --- | --- |
| `actions <subject>` | Explicit tasks; optional open/completed status, source and since filters. Missing owner remains null. |
| `status <subject>` | Cited recent material, effective actions/decisions, blockers, relationships, conflicts and evidence gaps. No default date window. |
| `record-state` | Current explicit task/decision replacement audit; bounded display and opt-in quotes. |
| `evidence <record-id>` | Exact supporting anchor and citation. |
| `source-range <anchor-id>` | Immutable quote, offsets/hash, location, revision and current-state indicator. |
| `source-context <anchor-id>` | Selected range, nullable section heading, source-ordered anchors, total and truncation. |
| `revisions <source-path>` | Unique immutable content revisions in original ingestion order. |
| `compare-revisions <source-path>` | Added/removed/modified/unchanged ranges; optional `--from` and `--to`. |

Python additionally exposes direct decisions, blockers, conflicts, and relationship
reads. Structured output leaves unavailable fields null rather than guessing.

### Explicit record replacement

Tasks and decisions may declare a corpus-scoped, case-sensitive
`[key:: version-key]` and `[supersedes:: earlier-version-key]`. Keys use 1..128
ASCII letters, digits, dots, underscores, colons, or hyphens, starting with a
letter/digit. A replacement is a complete same-kind record, not a field patch.

Unique, non-cyclic, uncontested references suppress predecessors from effective
actions/decisions/status and inherit subject scope. Resolution uses active
current sources independent of ingestion order. Filters apply to effective
records, not historical as-of state. Search and status recent material retain
superseded evidence. Prose and latest timestamps do not choose winners.

Missing/duplicate keys, cross-kind links, self-links, cycles and competing
updates produce diagnostics, not arbitrary suppression. Syntax errors fail a
source savepoint. Unresolved references leave evidence indexed, add ingestion
`state_warnings`, and appear in status evidence gaps. Removing/editing an update
removes its former effect. Record-state and ingestion diagnostics use the actual
resolver and report support, candidates, effective and suppressed IDs.

### Context and history

Context starts at the nearest containing heading and includes subsections until
the next sibling/ancestor heading. Repeated names do not merge sections;
preambles and headingless documents are handled separately. The selected anchor
is always included in a centered window (default 50, `--max-anchors` 1..200).
The heading is returned separately, even outside that window. Each range retains
its own offsets, hash and revision. These are parsed blocks, not reconstructed
full sections: code can be absent and nested list anchors can overlap.

Historical context reads stored anchors, never current file bytes. Revision
comparison defaults to the target's latest activation predecessor, so restores
compare against the state before restoration, not original creation. Upgraded
databases without earlier activation history require explicit revision IDs;
unknown transition order is not invented.

## CLI versions, errors, and compatibility

`kg capabilities --format json` advertises **interface_version `"2"`** and search
configuration, score, readiness, explanation, and migration guidance. Search
report version advances to `"2"`; unrelated payload/report versions do not change.

`--query-mode` is removed. Every former value, **including `reranked`**, fails
with migration guidance (`invalid_query` for JSON). Omit the flag and prepare
the matching projection. It is not an ignored alias or a hidden lexical route.

Domain errors are emitted on stderr as `{"error": "...", "message": "..."}` with
exit code 2 and no success payload. Dense readiness maps to
`dense_index_unavailable`; reranker failures to `reranker_unavailable`; ordinary
`SearchStateChangedError` and changed-state explanations to `search_state_changed`.
Other `SearchExplanationError.code` values survive. Invalid search semantics or
configuration use `invalid_query`; manifest errors use `invalid_manifest`.
Typer's option grammar/range errors retain its usage-error format, even when
JSON was requested. `dense-index` failures retain `dense_index_failed`.
Other operation-specific error mappings are unchanged.

Existing `RetrievalService.search()` (strict/natural lexical), lexical
`SearchExplanation`, `DenseRetrievalService`, `HybridRetrievalService`, and
`RerankedRetrievalService` remain low-level Python composition/evaluation APIs.
They are not alternate public product modes and do not inherit stricter product
readiness or concurrency semantics. Legacy lexical explanations remain version 1.

## Storage, maintenance, and validation

### Foundation value contracts (validation only)

`kg.models.foundation` implements strict `foundation/1` request/result values for
supplied content, evidence, document writes, atomic enrichment descriptions,
batch correlation, synchronization/metadata snapshots and dependent query plans.
`model_validate_json()` checks types, bounds, exact code-point source slices,
request-local references, supporting-document dependency coverage and declared
scope. `model_dump_json()` round-trips these immutable values without normalizing
source text. `BatchResult.validate_for()` and `QueryResult.validate_for()` check
request/result correlation. `FoundationCapabilities` explicitly says
`validation_only`; it does not change `kg capabilities` or product interface 2.

These models are **not** callable ingestion/query services or enforcement of
database integrity, authorization, atomicity, idempotency or read isolation.
An access context is trusted-boundary input, not proof of permission. No schema,
existing command/result, source fixture or authored gold changes accompany them.
The shared semantics, compatibility/migration duties and pending integration gates
are in [FOUNDATION_SPEC.md](FOUNDATION_SPEC.md). Representative contract fixtures
and the synthetic workload/budget protocol complete F0/V0, not E1/K1/Q1.

### Existing canonical storage

[`src/kg/schema.sql`](src/kg/schema.sql) owns rebuildable canonical SQLite tables
for source documents/revisions/anchors/activations, entities/aliases/mentions,
relationships, structured records/bindings, passages, lexical projections, and
ingest summaries. Dense projections are independently disposable.

After upgrades, reingest each corpus and rebuild matching dense projections.
Pre-alpha schema changes may require a fresh database. Preserve the old database
when historical evidence is needed: rebuilding from current sources cannot
recreate past content or activation history.

Reviewed [acceptance corpora](corpora/acceptance/) and incremental Atlas scenarios
pin exact evidence, graph scope, abstention, isolation, ingestion, edits/restores,
and history. Lexical expected lists remain component assertions, not semantic
gold. Separate product tests cover the entire pipeline and CLI with controlled
providers; routine tests do not download models.

[QASPER results](benchmarks/qasper/RESULTS.md) preserve measured relevance,
latency, cost, and the rejected answerability threshold. Existing
[retrieval acceptance gates](benchmarks/history/evidence-mvp.md#retrieval-acceptance-gates)
remain unmet and unchanged. Productization parity does not satisfy those gates.
The [real-model productization matrix](benchmarks/productization/README.md)
and its durable results are integration evidence, not new relevance gold.
See [CONTRIBUTING.md](CONTRIBUTING.md) for validation commands.
