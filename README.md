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
demonstration retains its own internal database and manifest-driven APIs.

**Pre-alpha:** this is evidence retrieval, not a production question-answering
system. It does not generate answers, infer entities or contradictions, or
reliably decide whether a natural-language question is answerable. Existing
retrieval-quality gates remain unmet.

## Using the KG with an agent

The installed `kg` command serves **canonical document and knowledge workflows**.
Run `kg --help` and command-specific help; only shipped commands are advertised.
Use `kg skill` for the installed strategy skill, `kg record --from-evidence
evidence:...` for an incomplete current-data scaffold, and `kg record --example` /
`kg record --schema` for grounded input instructions without repository access.

The [use-knowledge-graph skill](.github/skills/use-knowledge-graph/SKILL.md) teaches
an agent evidence/knowledge strategies, deliberate identity reuse, exact support,
query boundaries and stopping rules. Use it to operate an existing
authorized KG instead of rediscovering the interfaces from source code.

For example: `Use /use-knowledge-graph to find the decisions directly recorded for
this project and show their supporting evidence.` Use the configured local profile;
the skill does not install a tool server. Save `kg skill` output as `SKILL.md` in
your agent's `use-knowledge-graph` skill folder to use the wheel's identical copy.
In Copilot CLI, use `/skills reload` and `/skills info use-knowledge-graph` if the
new skill has not been discovered in the current session.

## How the pieces fit

Inspect vocabulary with `kg schema show --json`; new setup is schema-free.
People/project vocabulary is only the explicit `--schema-preset personal/1`
example. `kg schema generate --example` documents selected-excerpt context for
external-agent initial proposals; the core does not interpret text.
`kg schema validate --example` documents
evidence-backed proposals and explicit human-reviewed admin apply. Proposals compare
reuse, extension and deferral. Additions and monotonic endpoint widening preserve
existing IDs, support and authored revisions; schema application never extracts facts.
Record files bind the exact returned revision. See
[schema contracts](CONTRACTS.md#knowledge-registry-api) for authority, privacy, retries
and the explicit fresh-store `/4` compatibility break.

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
graphs, controlled writes, typed cited one-hop `traverse`, and fixed native
`relationship_decisions` queries with exact counts and both proof sides.
A canonical write invalidates the captured generation;
leftover graph files cannot establish freshness. Start with the
[evidence example](#generic-evidence-service) for the canonical Python API, or the
[canonical document CLI](#canonical-document-cli) for local file usage. Both use
the canonical evidence store.

## What is available

| Capability | Current behavior |
| --- | --- |
| Generic evidence | `kg.evidence`: atomic supplied-document writes/removal, ordered batches, exact UTF-8 content, scoped history/anchors/citations, durable retry receipts and trusted local policy. |
| Standalone indexing | `kg.indexing.IndexService`: immutable passages, fenced attempts, actual pinned-provider vector projections, incremental reuse/rebuild, model-free readiness and bounded cleanup. No coordinated job execution. |
| Canonical search | `kg.indexing.EvidenceSearchService`: scoped lexical/dense candidates, fusion/deduplication, reranking and exact supplied-source citations in one guarded snapshot. Both providers and complete matching projections are mandatory. |
| Processing control | `kg.processing`: trusted plan/worker registration, scheduling/deduplication, fenced claims/heartbeats/failure, bounded recovery, and idempotent retry episodes. Controls do not execute or acknowledge work. |
| Owned knowledge | Atomic anchor/passage-backed entities, independent entity support, aliases, identifiers, explicit passage mentions and typed assertions through `KnowledgeService.record`; current/history reads and evidence-backed, explicitly approved additive vocabulary revisions. Explicit decision assertions produce distinct submitted records. |
| Markdown demonstration | Manifest-selected local Markdown, explicit records, seed entities, structured reads, source context and revision comparison in its separate database. |
| Demonstration search | Full local keyword + semantic retrieval, fusion/deduplication and reranking; matching vector preparation is required. No keyword-only fallback. |
| Foundation values | Strict `foundation/1` document/withdrawal request/result validation and strict `record-authoring/1` knowledge requests. Canonical anchor/passage-evidence plans execute through `QueryService`. Whole-set seed replacement is not implemented. |
| Canonical queries | `kg.query.QueryService`: full canonical ranked search, historical anchor/passage reads and actual K1 exact entity resolution, explicit decision records/counts, bounded retained support inspection, spawned deadline supervision and fresh release authorization. Paths remain unsupported. |
| Execution diagnostics | Evidence, indexing, processing and query calls retain bounded, authorized in-memory summaries. Named explained wrappers execute once; detailed traces and source quotes require opt-in. |
| Optional local graph | `kg.graph.LocalGraphSession` lazily builds/reuses an exact-scope disposable Ladybug graph, explicitly refreshes it and serializes controlled canonical writes. Typed `traverse` and `relationship_decisions` return full cited explicit relationship/decision proofs and exact joined counts; SQLite stays authoritative. |

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

The canonical store uses the complete `evidence-store/6` schema and the `evidence/2`
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

For real source-backed authoring, run
`uv run python examples/knowledge_enrichment.py --database /tmp/knowledge-demo.sqlite3`.
It discovers an actual supplied anchor, atomically submits an entity and explicit
decision, and reads the assertion's immutable provenance. Repeating it replays the
same durable IDs. Typed predicates and decision encoding come from the registered
schema, not language inference. Current/history reads and ordinary scoped write
reports and exact owned assertion withdrawal are available; whole-set seed
replacement and atomic correction/supersession remain unsupported.
Typed cited one-hop queries use `LocalGraphSession.traverse`.
`QueryService` composes the real
entity/decision reader for public decision selections and counts.
See [knowledge service contracts](CONTRACTS.md#knowledge-authoring-and-reads).

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

## Cited one-hop relationship queries

With the supported optional graph runtime, run a supplied-note example without
models or automatic extraction:

```bash
uv run --extra graph python examples/graph_relationships.py --output /tmp/cited-relationships
```

Use a fresh output directory. The example explicitly submits Alice/Atlas/ownership
facts supported by the supplied note, then calls `LocalGraphSession.traverse`
with `kg.models.graph.GraphTraversalRequest`. Select exactly one entity ID or
case-sensitive name/eligible alias, a registered entity-valued predicate and
`direction="outgoing"` or `"incoming"`. `max_hops` is strict integer 1 only.
Ambiguous names return distinct candidate IDs rather than selecting a person.

Results are complete, empty, ambiguous or failed; complete results retain every
distinct assertion path and full original evidence/endpoint proof. The exact
standalone result is bounded to 1,000 paths/candidates and 8 MiB: overflow returns
no data, never a prefix. `capabilities()` probes installed runtime support without
building or reading sources; unavailable runtime is explicit, with no fallback.
Later citation hydration is separately authorized historical evidence, not a
new proof of current ownership. Native crash limitations below still apply.
See [query contracts](CONTRACTS.md#typed-cited-relationship-query-api).
Existing `QueryService` paths, joins, paging and natural-language planning remain
unsupported; this is not an arbitrary query-language interface.

### Native relationship-to-decision counts

```bash
uv run --extra graph python examples/graph_relationship_decisions.py --output /tmp/kg-joined-example
```

Use a fresh output directory. `LocalGraphSession.relationship_decisions` takes
`GraphRelationshipDecisionsRequest` with the same exact root, relationship predicate,
direction and strict one-hop boundary, plus `display_limit` (1..1,000). It counts
distinct submitted explicit decision assertion IDs on reached entities in one
native query operation, not unique wording. Each displayed member includes the
complete decision proof and all qualifying parallel relationship IDs; the
response's relationship table contains their complete membership proofs.

`count` is exact even when `display_truncated` is true. The full private selection
must fit the original 8 MiB conservative output and shared scratch/time limits;
limiting display cannot bypass full-proof admission. Failure returns no count or
prefix. There is no public retained graph inspection/paging. Later citation reads
for both relationship and decision supports are separately authorized historical
reads, not renewed ownership. Withdrawal/source changes invalidate old generations;
refresh explicitly and never reuse old results as current authority.
See [joined query contracts](CONTRACTS.md#native-relationship-to-decision-query-api).

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
report `close_pending` while native work/cleanup still owns resources. Typed
one-hop traversal and fixed relationship-to-decision queries are public; arbitrary
joins and retained graph inspection are not.

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

## Canonical document CLI

After installation, `kg` and `python -m kg` work outside the source checkout.
From a prepared checkout, use `uv run kg` (or `uv run --no-sync kg` to avoid
syncing). Discover public verbs with `kg --help`, per-command `--help` and `kg skill`;
the external agent composes them, not a required ingestion pipeline.
Start with guided `kg setup`. It asks for a new store location and explicit local
model approval, and supplies the corpus/policy/writer defaults but no domain schema.
The default profile is `~/.config/local-knowledge-graph/profile.json`; data is
`~/.local/share/local-knowledge-graph/evidence.sqlite3`. `XDG_CONFIG_HOME` and
`XDG_DATA_HOME` override their respective roots.
For a separate experiment, set **both** roots to fresh owned directories before
setup; `--store` alone does not isolate the profile. Use an existing authorized
profile for ordinary work, not a new setup on every invocation.

```sh
kg setup
kg add meeting.md --json
kg read document:RETURNED_ID --json
kg find documents "release plans" --json
kg update document:RETURNED_ID revised.md --expect RETURNED_STATE --json
kg read document:RETURNED_ID --history --json
kg remove document:RETURNED_ID --expect RETURNED_STATE --confirm --json
```

`RETURNED_ID`/`RETURNED_STATE` mean actual values from the preceding response.
Use a returned `evidence:...` target to read an exact citation, or a history
`document:ID@STATE` target to read an earlier state's evidence. Read returns anchor
pages; `--limit` and `--after` continue them without pretending a prefix is the
whole document. JSON entries include the exact
`support: {reference: EvidenceRef, state_version: ...}` plus original evidence,
metadata and citation, for grounded consumers. Empty text has an empty anchor page.

Each `add` creates a new document; filename and content do not imply identity.
Update preserves all saved metadata and uses the explicitly selected document.
Interactive update/removal displays state for confirmation; JSON/noninteractive
mutations require `--expect`, and removal also requires `--confirm`.
Removal deactivates the source, retaining text/history. No facts are extracted.

Add/update save exact UTF-8 without newline normalization, then call the existing
index service. JSON returns the canonical receipt unchanged as `evidence_write`,
the document target/state under `document`, and the exact returned process value
as `search_preparation` (or `null` when preparation was not requested or no process
result exists). If preparation fails after saving, the command returns non-success
while preserving the confirmed evidence receipt. Messages distinguish exact
evidence saved, search/index preparation, authored knowledge (none is submitted by
document commands), and automatic enrichment (not run). A failure with unknown
commit outcome says so. There is no automatic retry or cross-process
exactly-once claim; manual resubmission can duplicate data. A saved document
whose preparation failed remains readable; an explicit update of its observed
state can attempt preparation again.

Text output is readable; `--json` emits one `client/1` object with status, message,
result, error code and exit code. Exit 0 means success/empty, 2 invalid input/
configuration/confirmation or unresolved relationship selection, 3 service read/setup
failure or non-complete query, 4 unsuccessful canonical
write, 5 saved with failed preparation, 6 local/unexpected failure, 7 unknown
write outcome. Receipt and preparation details remain separately represented.
Document search JSON contains hit-free `search_context` plus one ordered,
citation-complete `entries` representation rather than duplicating hit evidence.

For model-free evidence intake, use `kg setup --yes --json`, then
`kg add FILE --evidence-only --json`. Exact reads and evidence-only updates work
before any domain schema is approved. `kg capabilities --json` shows the current
schema state and installed workflow boundaries without checking search readiness.
An external agent can compose read, `schema generate SAMPLE.json`, validate and
explicit human-reviewed apply; see the short task-specific `schema generate
--example` and `schema validate --example` recipes. Generate returns
`awaiting_agent` plus an intentionally incomplete `editable_proposal` containing
only exact known bookkeeping; it does not infer vocabulary or submit anything.

For unattended setup with model preparation use `kg setup --yes --approve-models --json`, optionally
`--store NEW_PATH --model-cache EXISTING_CACHE`. Approval includes the pinned
GTE model's trusted cached code. Without approval, exact reads/removal and
`--evidence-only` add/update work,
while add/update/search fail before loading models. Profile settings remain
explicitly editable local configuration: `models_approved` controls execution,
`model_cache` selects an existing cache. No credentials or Python policy objects
need to be assembled.

`kg setup --attach EXISTING_PROFILE --yes` validates its existing store and scoped
access, then creates this user's profile without initializing the database.
Existing destination profiles/stores are refused rather than overwritten.
Setup has separate registration/file operations: a failure can leave a partial
new store; it reports failure and never deletes or silently resets that store.
Attaching arbitrary SDK stores requires a compatible supplied profile; the CLI
does not discover or grant their policy. This is trusted personal-local use.

### Knowledge workflow

```sh
kg find entities --json
kg find entities Atlas --json
kg read entity:RETURNED_ID --json
kg find relationships Atlas --limit 20 --json
kg record --from-evidence evidence:RETURNED_REFERENCE --json
kg record --example
kg record --schema --json
kg record facts.json --retry-key reviewed-facts-1 --json
kg find decisions Atlas --json
kg find decisions Mira --through owns --json
kg find decisions Atlas --through '^owns' --json
kg read fact:RETURNED_ID --json
kg remove fact:RETURNED_ID --confirm --json
kg read fact:RETURNED_ID --history --json
```

Only explicit setup with `--schema-preset personal/1` installs the example
`person`, `project`, `owns` (person to project), and `decision` (an explicit string
statement about either type) vocabulary. New setup otherwise has no domain schema;
attached stores retain their approved vocabulary. Inspect `kg schema show --json`
for actual terms and `kg find decisions --help` for supported query syntax.
Direct queries return decisions about the selected entity; `--through PREDICATE`
returns decisions about outgoing neighbors, and `--through '^PREDICATE'` about
incoming neighbors. Use an actual registered predicate, not an inferred path.
Entity discovery uses eligible listing and exact case-sensitive name/alias matches,
not semantic search or first-match selection. Empty/incomplete matches do not prove
an entity is new; list/page entities or inspect source evidence first.
Read output supplies identifying support, the exact `entity_id`, and a
`selection_witness` when the entity is typed.

Entity and contribution pages have `has_more` and `next_after`; continue with
`--after`. Relationship inspection filters entity-valued assertions involving
either endpoint. Its limit applies **before filtering**: an empty page with
continuation does not mean no relationships. Names are selected only when one
bounded, fenced service scan establishes uniqueness; ambiguity returns candidates,
and budget failure never becomes a false uniqueness claim. Paging is not a
cross-command snapshot.

Record input is one strict `record-authoring/1` document. Its named `support` map
contains exact copied source or seed captures; each authored identity,
classification, alias, identifier, mention, or assertion names its own support.
Multiple source names are conjunctive evidence. Unknown/unused names, duplicate
keys/evidence, mixed support kinds, conflicting captured states, and the removed
native `changes` shape are rejected. Canonical limits apply after private
expansion. `kg record --from-evidence` authorizes exact current citations and
copies the active schema/support into `result.record_template` with empty
`entities` and `assertions`; it chooses no identity, type, predicate, value,
interpretation, rationale, selection, or approval and performs no write. See
`kg record --example` and `--schema`.

Identity can remain unresolved with exact existence support and no edges. Author
a supported classification claim separately and explicitly select it using the
exact review witness from `kg classifications entity:ID --json`. Existing typed
entities and assertion endpoints use the exact selection witness returned by
`kg read entity:ID --json`. Only the original identity owner/writer selects or
clears. Typed assertions capture selected events; same-type replacements,
A-to-B-to-A and refresh never revive old assertions.
`kg classifications entity:ID --history --json` preserves authorized history;
`kg withdraw-classification fact:ID --retry-key KEY` terminally withdraws a claim
without erasing identity/evidence. Persist the input and retry key before recording.
See [the input recipe](src/kg/client/record-example.md) for compound creation,
selection-only input, bounded subset recovery and unknown-outcome retry.

The service derives dependencies from captured states, never current/latest
replacements. Every endpoint is an explicit local declaration. Successful record
results return the `RecordAuthoringOutcome` directly, including the canonical
authored receipt, copied captures, authored-item mappings, classification outcome,
and bounded private expansion counts. No native plan or flat derived-change
mapping is exposed.
Fact/entity reads include copy-ready `evidence_targets` for exact source inspection.
To reuse a typed entity, declare an `existing` identity with the returned
`entity.entity_id` and `entity.selection_witness`, then reference that declaration's
local ID from an assertion. `target` strings are command arguments, not record
objects. Withdrawal is owned assertion
withdrawal only, requires noninteractive confirmation and preserves history.
Entity/knowledge history remains subject to current authorization.

Decision output includes `decisions` entries with `assertion_id`, `target`, `text`,
captured `support` and exact `evidence_targets`, alongside `count`, `exact`,
`selection_complete` and `display_complete`. Direct decision JSON uses one
nonduplicative `execution` summary plus those hydrated entries; raw parallel
`query`, `inspection`, and `targets` copies are not returned. Relationship-decision
JSON retains its native `graph` proofs unchanged. Human output shows decisions
rather than engine dumps.
Direct decisions use QueryService's unchanged count and bounded retained inspection,
then public contribution reads only for those IDs and final retained reinspection.
These are separate authorized observations, not one atomic snapshot; detected
stale/denied/mismatched results withhold the composite. Reads retain their individual
budgets; large hydration may outlive the five-minute retention and fail explicitly.
`exact: false` remains a lower bound and makes selection/display completeness false,
even if every retained member is shown. `display_truncated` means omitted retained
members, not unknown membership. Displayed rows are not the total.
Retained handles expire at command exit.
Relationship decisions use one fixed native query with complete relationship and
decision proofs and exact counts, even when the display is truncated. Each displayed
decision's `relationship_ids` link to the unchanged proofs in `graph.relationships`.
No additional graph reads or changed hidden-selection budgets are introduced. `--through`
requires optional Ladybug 0.20.4 on macOS 15+ ARM64/CPython 3.12; each invocation
builds a disposable exact-scope graph and closes it. In-process native failures
can terminate the host; its 256 MiB buffer is not an RSS cap or isolation boundary.
No query planner, arbitrary Cypher, cross-process graph reuse or general retained
graph membership inspection is provided. Model/native readiness requires separate
actual-run evidence; controlled-provider CLI tests do not establish it.

### First-use models and resources

Embeddings and reranking run locally through Sentence Transformers/PyTorch;
corpus text is not sent to a remote inference API. The canonical CLI loads pinned
models with `local_files_only=True` and **never downloads models**. Supply a
prepopulated approved cache. Python SDK callers retain their explicit provider
configuration; `IndexService` and `EvidenceSearchService` also accept
`local_files_only=True, model_cache=...`. If a required
model or dependency is blocked/unavailable, report the blocked host/error and
prepare it through an approved route. There is no keyword-only fallback.

`add` prepares embeddings but does **not** prepare the reranker: `find documents`, even returning
no hits, also initializes
`cross-encoder/ms-marco-MiniLM-L6-v2` at its pinned revision. A successful empty
search therefore requires both providers and a matching current index.

Allow disk space for dependencies, model caches, the canonical database, and
separate vector projections, plus working memory for both models and inference
batches. CPU is supported; CUDA or Apple MPS may be selected when available.
Larger batches, long passages, and the Qwen profile use more resources; reduce
the configured index batch size if indexing memory is constrained. Resource requirements
depend on corpus size, model profile and runtime; no minimum RAM/disk guarantee is
established.

## Markdown demonstration

**Historical reference only:** the command examples in this section describe
baseline `b66fa46f7f643cd5cf57571fa34fa6e300aefaa1`, not the installed canonical
CLI above. Old runtime modules/regression tests remain internally available;
their benchmark results do not validate the new CLI. Do not run these historical
`kg` commands against the current entrypoint.

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
subprocess client for historical demo status, error handling, and cited output.
It invokes `python -m kg.legacy_cli` using its current Python interpreter, not
the installed canonical `kg` command:

```bash
uv run python examples/cited_status.py corpora/example.yml Atlas
```

The example defaults to `--since 30d`; use its `--since` option to change that
window. The underlying demo module's `status` command has no default time filter.
Python callers may override `load_status(..., executable=...)` with a single
executable that implements the demo commands; canonical `kg` does not.

## Foundation value validation

`kg.models.foundation` supplies immutable, strict `foundation/1` values for
document and owned-withdrawal writes plus dependent query plans.
It checks shape, declared scope, exact source slices, references and result
correlation. Models alone do **not** ingest, execute, authorize or persist requests;
the canonical services execute their documented subsets separately. Public
knowledge authoring uses `kg.models.authoring.RecordAuthoringRequest` through
`KnowledgeService.record`, while foundation writes retain documents and owned
withdrawals.

```python
from pathlib import Path
from kg.models.authoring import RecordAuthoringDocument

request = RecordAuthoringDocument.model_validate_json(
    Path("record.json").read_text(encoding="utf-8")
)
payload = request.model_dump_json()
schema = WriteRequest.model_json_schema()
```

Use [CONTRACTS.md](CONTRACTS.md) for the contract reference,
[corpora/foundation](corpora/foundation/README.md) for examples, and
[benchmarks/foundation](benchmarks/foundation/README.md) for reproducible inputs
and proposed engineering targets. Those targets are not measured performance.

## Development and documentation

From the repository root, with the declared base/dev environment prepared:

```bash
uv run --no-sync pytest  # Small isolated unit selection, NOT the complete suite.
bash .github/scripts/check_pr.sh --area cli \
  --reason "Changed public command behavior and its service consumers" \
  --report /absolute/path/outside/repository/pr-gate.json
```

The PR command includes the unit selection, a real SQLite/provenance/CLI core,
reviewed behavior-area additions, lint, type checking and distribution build.
The offline build validates declared backend requirements and constructs sdist/wheel
in the explicit prepared interpreter without build isolation; isolated packaging
validation remains a separately recorded release obligation.
Repeat `--area` and add exact `--case` nodes for affected guarantees and consumers;
the example's `cli` area is not a universal selection. Reports require a fresh
external path. The **target** is at most 120 seconds end to end on the reference
prepared host; no timing result is implied by these commands.

`pytest tests` explicitly collects the complete suite, independent of the unit
default. Complete integration/native/capacity acceptance remains a separately
authorized release gate. On the prepared supported native host:

```bash
KG_REQUIRE_NATIVE=1 HF_HUB_OFFLINE=1 uv run --no-sync pytest tests --durations=50 --tb=short
```

Required graph examples and applicable real-model matrices are separate from
pytest. Unrun acceptance is **pending**, not passed. **GitHub Actions is off until
further notice; do not dispatch it.** See CONTRIBUTING for selection review,
environment preparation, timing boundaries and release obligations.

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
