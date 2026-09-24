# Contributing

Thanks for helping improve Local Knowledge Graph.

## Architecture and ownership

The canonical engine uses **SQLite for authored evidence and knowledge, with an
optional Ladybug graph projection**. Exact supplied text, immutable revisions,
identities, registered schema, entities/assertions, support and history belong to
`evidence-store/4`. Ladybug holds only rebuildable, exact-scope derived data.
Canonical writes invalidate the captured graph generation; a file or manifest
alone cannot establish freshness. Search remains canonical keyword/vector
retrieval, fusion and reranking, not a Ladybug search replacement.

The private graph builder, reusable `LocalGraphSession` controller and native
examples and typed cited one-hop query API are implemented; graph joins are not. Keep current behavior
in [SPEC.md](SPEC.md) and [CONTRACTS.md](CONTRACTS.md), and planned scope on issues.
The separately supported Markdown demonstration has its own SQLite schema,
manifest and retrieval APIs. Its historical CLI regression tests use
`kg.legacy_cli`; the installed `kg.cli` uses canonical services.

## Development setup

Python 3.12 or newer and [uv](https://docs.astral.sh/uv/) are required.

```bash
git clone https://github.com/markgar/local-knowledge-graph.git
cd local-knowledge-graph
uv sync --extra dev
```

Dependency resolution uses the repository's [`uv.toml`](uv.toml). It points to
the package feed required by the primary Microsoft development environment and
contains no credentials. If that feed is unavailable in your environment,
use a package index approved for your environment. Never change feeds or bypass
organizational controls to evade a blocked download. Report the blocked host and
use cached/local or otherwise approved dependencies and models.

### Fast feedback and the local PR gate

From the repository root, use the prepared base/dev environment:

```bash
uv run --no-sync pytest
uv run --no-sync pytest tests/unit/test_manifest.py
bash .github/scripts/check_pr.sh --area cli --reason "Reviewed command and service impact" \
  --report /absolute/path/outside/repository/pr-gate.json
```

Plain pytest intentionally selects a **small isolated unit boundary**, initially
six moved modules (77 existing function definitions), not all unit coverage or
the complete suite. Additional gate-tool value tests live there. The feedback
targets are roughly 1-3 seconds focused and <=5 seconds for this default on a
prepared host; these are targets, not measurements.

The PR command runs those units, a mandatory real SQLite/authorization/provenance/
receipt/CLI core, the selected behavior checks, `ruff check .`, kg-scoped `mypy`,
and an offline distribution build. The reviewed selectors and pinned primary
layers live in `tests/support/gates.py`. Supported areas are evidence, schema,
knowledge, indexing, query, processing, graph, cli, demo, evaluation and validation.
Repeat `--area` and add exact `--case tests/file.py::test_name` selectors for changed
behavior and downstream consumers. No flag subtracts mandatory cases.

Authors and reviewers must inspect the report's changed paths and dependency
argument, not just select an area matching a filename. Storage/schema/authorization
and exact-proof changes need their affected query, index, graph and public-command
consumers; locking/deadline changes need actual affected process cases. Unknown
ownership, missing/stale selectors, unsupported dependency/build changes,
unexpected skips and empty mandatory selections fail closed. Pytest-only
pyproject organization edits are distinct from dependency/build/runtime changes.

Reports use a fresh external path and distinguish passed, failed, incomplete and
not-run work. The public shell launcher's prepared-Python supervisor measures
the complete outer uv invocation
(startup, collection, fixtures, tests, lint/type/build and cleanup); inner timings
are diagnostic only. It enforces <=59 seconds, reserving one second for reporting,
against the **<=60-second public-command target**. Missing timing/report persistence
cannot pass. Reporting overhead must also fit in qualification; no overhead bound
is assumed on every machine. Direct use of the inner Python runner is not a
completed timed gate.

The supervisor keeps uv, its runner and checks in one owned process group,
relays cancellation, and waits for group shutdown (escalating resistant children).
Startup/cancellation/cleanup failures cannot yield a passing report. Full explicit
collections execute guarded units first, before integration tests may load real
optional runtimes; preloaded real runtimes still fail the unit boundary.

Record actual first-fresh-tool-cache and warm timings on the reference host,
Apple M4 Pro ARM64 / 24 GiB RAM / CPython 3.12, with actual tool versions. Do not
claim the target achieved without evidence. Preparation is separate: install only
declared dependencies through approved sources, preserve constraints, and report
blocked hosts instead of bypassing controls. Normal gates are offline and no-sync;
missing packages/build requirements block them rather than trigger downloads. Do not
clear shared caches, share mutable test stores, shorten capacities, silently omit
checks or kill tests at the time limit. A correctness, preparation or time conflict
must be surfaced before further repeated qualification runs.

Inside the timer, the gate checks every current `build-system.requires` constraint
against the prepared interpreter's installed distributions, reporting exact versions.
Missing/incompatible requirements fail before checks start. It then constructs
both sdist and wheel with `uv build --offline --no-build-isolation --python
<prepared-interpreter> --out-dir <fresh-owned-temp>`. This proves artifact
construction in that environment, **not build isolation or absence of undeclared
build dependencies**. Unsupported build backends, backend paths, requirement extras
or URLs require reviewed policy rather than partial verification; dependency/backend
changes remain outside ordinary validation-only scope.

Portable scoped premerge checks can defer full integration/native/capacity
acceptance until release when reviewed portable evidence is adequate. This is
**not** a waiver of essential premerge safety for high-risk runtime changes.
If that coverage cannot fit one minute, obtain an explicit scope/policy decision;
never hide required premerge checks outside the clock.

### Classification and full release acceptance

Each collected parameter case has exactly one primary marker: `unit`, `service`,
`functional`, `process`, `native` or `acceptance`. `requires_native` is an orthogonal
requirement for real Ladybug cases. Unit-directory cases are auto-marked; mixed
files/parameters require explicit classification. Filename, SQLite usage and
similar-looking cases are not timing or redundancy evidence. Collection is guarded
against real model/native imports; unit and PR execution keep that in-process
guard. It is not a subprocess sandbox. Existing controlled model stubs stay valid.

Changing tests must preserve case/parameter coverage, original capacities, authored
gold, exact evidence and real race/rollback behavior. Test-organization changes
compare normalized before/after node inventories, not just case counts. Use
`pytest tests --collect-only --test-inventory <fresh-external-path>` to resolve all
area selectors without executing tests. No optional model/native initialization
is permitted during collection.

The explicitly authorized release command is:

```bash
KG_REQUIRE_NATIVE=1 HF_HUB_OFFLINE=1 uv run --no-sync pytest tests --durations=50 --tb=short
```

The explicit `tests` argument bypasses the small default and applies no layer
exclusion. Run lint, type checking and build for that revision too. A base-host
run with optional native skips is not complete supported-native acceptance.
Additional required examples/matrices below remain separately recorded. A passing
selected PR run never means these unrun gates passed.

Separately authorized release packaging must also run isolated `uv build --out-dir
<fresh-path>` using normal configured sources, and record its actual revision and
outcome. The PR report lists this isolated-packaging obligation as not_run;
prepared-environment construction or an older preparation build does not satisfy it.

The optional private graph runtime gate is separate from Linux/base-package CI:
on supported macOS 15+ ARM64/CPython 3.12, run
`uv run --extra graph --extra dev pytest tests/test_graph_export.py tests/test_graph_build.py tests/test_graph_native.py`
with `KG_REQUIRE_NATIVE=1` to make an unavailable native runtime fail instead of skip.
Include `tests/test_graph_decisions*.py` for relationship-to-decision API changes.
Its unchanged varied 1,001-decision fixture checks full hidden proof parity and
measures conservative/serialized output, original scratch overlap and release;
do not shrink it, omit proof fields or raise limits to pass capacity acceptance.
Include `tests/test_graph_session*.py` for controller changes, and run
`uv run --extra graph python examples/graph_session.py --output <fresh-path>`
for real lifecycle, proof-hydration, write/refresh and restart acceptance.
Run `uv run --extra graph python examples/graph_build.py --case varied-10000 --output <fresh-path>`
for the complete synthetic native feasibility/parity gate. This experimental
in-process runtime uses pinned Ladybug 0.20.4, a 256 MiB buffer pool and two
threads; that buffer is not a total RSS cap. Native exhaustion/close can crash
the host; see [runtime limits](README.md#optional-disposable-graph-example) and
[#137](https://github.com/markgar/local-knowledge-graph/issues/137).

GitHub Actions is **OFF until further notice**. Do not dispatch or enable it.
The retained manual-only workflow has no push or pull-request triggers. Its test
command explicitly uses `pytest tests` so future authorized invocation cannot
silently become unit-only after the default changes. It retains Python 3.12 and
its optional 3.12/3.13/3.14 compatibility matrix, lint, type checking and build;
optional native skips on Linux do not establish native acceptance.
Code-affecting changes include source, tests,
source fixtures (including Markdown input documents), executable scripts, dependencies,
build/package configuration and CI configuration.

For documentation-, skill-text-, instruction- or issue-template-only changes,
review the diff and relevant links/examples instead. Do **not** dispatch CI or
run the full Python suite merely to merge those changes.

The workflow also guards the Python checks: a lightweight change check runs
before dependency installation. It compares feature branches against their
merge base with the default branch; on the default branch it checks the latest
commit against its first parent. Documentation-only or empty changes skip the
checks, even if manually dispatched with `full_matrix=true`. Unknown paths
conservatively require CI; failure to determine the changed files fails the check rather than claiming a
documentation-only change. Classification lives in
[ci_scope.py](.github/scripts/ci_scope.py).

## Repository layout

| Path | Responsibility |
| --- | --- |
| `src/kg/evidence/`, `src/kg/evidence/schema.sql` | Canonical `evidence-store/4`, exact supplied-source intake/history, trusted local policy and owner transactions. |
| `src/kg/knowledge/` | Immutable schema revisions and approved additive evolution, knowledge reads, enrichment validation and complete eligible graph export. |
| `src/kg/indexing/` | Canonical passage/vector indexing and scoped full search. |
| `src/kg/processing/`, `src/kg/query/` | Processing control plane and supervised canonical query execution. |
| `src/kg/graph/` | Optional Ladybug adapter, complete disposable projection builder and reusable exact-scope session lifecycle. |
| `src/kg/diagnostics/`, `src/kg/_execution_budget.py` | Scoped execution reports and shared accounting/deadlines. |
| `src/kg/models/` | Validated service values, including `foundation/1`; models alone do not execute operations. |
| `src/kg/config.py`, `src/kg/models/manifest.py` | Markdown demonstration manifest loading and source selection. |
| `src/kg/markdown/`, `src/kg/ingest/` | Demonstration Markdown ranges, explicit extraction and demo-store writes. |
| `src/kg/schema.sql`, `src/kg/db.py` | Separate Markdown demonstration SQLite schema and connections, not `evidence-store/4`. |
| `src/kg/retrieval/`, `src/kg/cli.py` | Demonstration search, structured/evidence reads and local text/JSON CLI. |
| `tests/`, `corpora/` | Automated coverage, manifests, source fixtures and acceptance inputs. |
| `benchmarks/`, `examples/` | Scoped evaluation tools/results and executable canonical-service, graph-build and demo clients. |

## Starting work: issue, file spec, critic, implementation

Issues organize future work; file specs support design and criticism; repository
docs describe delivered behavior. Working specs belong outside the tracked repo,
not alongside `SPEC.md`.

For guided execution, use the repository's
[`work-package` skill](.github/skills/work-package/SKILL.md):

> Use /work-package for issue `<issue-number>`. Design and critic review only; do not implement.

The skill follows the steps below and stops for approval before implementation.
One work session (the current session or one delegated app subsession) can own
design, revisions and authorized implementation end to end. Independent critique
uses a fresh-context subagent, not a second app session or worktree.
In Copilot CLI, use `/skills reload` if it was added during the current session,
then `/skills info work-package` to confirm discovery.

1. **Choose a ready package.** Inspect the
   [open bounded issues](https://github.com/markgar/local-knowledge-graph/issues?q=is%3Aissue%20is%3Aopen)
   and their [milestones](https://github.com/markgar/local-knowledge-graph/milestones).
   Read the selected issue's current scope, prerequisites and acceptance criteria,
   following its links to applicable approved requirements, not superseded tracker
   instructions.
   Start a design session from current main; choosing an issue is not approval
   to begin implementation.
2. **Write a real Markdown spec file.** Use the session's artifact storage outside
   the repository, for example `files/<package>-spec.md` within the session directory.
   Record its absolute path so the author and critic review the same file.
   Read current code before proposing changes. Include the package issue link,
   baseline commit, scope/non-goals, APIs, storage/schema and migration changes,
   transaction/state/failure handling, compatibility, acceptance tests, unresolved
   decisions, and PR-sized implementation slices with stopping points. Link shared
   requirements linked by the owning issue rather than copying the whole backlog.
3. **Have an independent critic review the file.** Use a general-purpose subagent
   with fresh context and a read-only assignment in the current worktree, not a
   separate app session or worktree. Give it the file's absolute path, the package
   issue, shared requirements and code baseline. Ask for concrete gaps, unsafe
   assumptions, missing edge cases and testability concerns, with spec-section/code
   references. The critic reviews; it does not edit or implement.
4. **Revise and approve.** Address findings in that same file, record dispositions,
   and re-review material changes. Resolve blocking decisions and obtain explicit
   approval of the design and first implementation slice before coding.
5. **Publish the full approved spec on the package issue.** Post an ordinary issue
   comment containing the full spec, revision/baseline, critic findings/dispositions
   and user approval record. This is required after approval; do not ask separately
   whether to publish it or block on the special issue-artifact feature. A summary
   or local file path alone is not a durable handoff. Read the comment back to
   verify publication before implementation or handoff, and save its URL/ID in the
   local working file. For later approved revisions, update that same spec comment,
   preserving revision/approval history and any concurrent edits. Never replace an
   approved record with an unapproved draft. Preserve this record before archiving
   the work session.
6. **Implement an approved slice.** Continue in the same work session with the issue,
   approved spec comment and stopping point; no new session is required. If main has
   changed, check the design against it before coding. Run applicable acceptance tests and the validation
   commands above, obtain independent complete-diff review, resolve findings and
   re-review fixes before merge. Link each PR to the package issue; update repo
   docs for behavior actually delivered.
7. **Record progress and close on evidence.** After each merge, record completed
   slices and remaining work on the package issue. Close only when its acceptance
   criteria are demonstrated, then update any linked current dependency/coverage
   records. One merged slice does not necessarily complete a package.

Use this starting prompt, substituting the package and issue:

> Inspect current main and issue `<issue-number>`. Write a `<package>-spec.md` implementation design
> in this session's artifact storage, outside the tracked repo, and report its
> absolute path. Follow the spec contents in CONTRIBUTING.md. Do not implement.

Then give a fresh-context general-purpose subagent this read-only prompt:

> Review the spec at `<absolute path>` against its package issue, shared
> requirements and referenced code baseline. Identify concrete correctness,
> compatibility, migration, failure-handling and acceptance-test gaps. Cite spec
> sections and code where relevant. Do not edit or implement; report findings for
> the author to address before approval.

## Pull requests

- Keep changes focused and include tests for behavior changes.
- Preserve deterministic behavior and exact source provenance.
- Do not add corpus-specific parsing or retrieval rules.
- Update public documentation when contracts or commands change.
- Keep the [KG user skill](.github/skills/use-knowledge-graph/SKILL.md) current in
  the same change that delivers or alters a public capability. Update its recipes,
  capability boundaries and failure guidance together; do not advertise planned
  APIs as implemented.
- Canonical schema changes belong to `src/kg/evidence/schema.sql` and its exact
  admission/manifest checks. `src/kg/schema.sql` belongs only to the Markdown
  demonstration. Preserve exact authored evidence/history within supported stores.
  Experimental format changes may require an explicit fresh store and source
  reload; migration, dual-read and backward compatibility are not required.
  Never silently alter/delete incompatible stores. Graph/vector projections are
  disposable, not a substitute for canonical evidence.
- Add or update a reviewed case in `corpora/acceptance/` for retrieval changes.
  Do not rewrite authored gold to match a new ranking default: lexical expected
  lists remain low-level component assertions. Add separately scoped product
  search cases for the full pipeline.
- Keep repository documentation focused on implemented behavior and executable
  assets. [Open bounded issues](https://github.com/markgar/local-knowledge-graph/issues?q=is%3Aissue%20is%3Aopen)
  own planned scope, dependencies, applicable shared requirements, acceptance,
  designs and progress; milestones group goals.
  Do not add repository planning documents or duplicate the issue inventory.
  Follow the [file-spec and critic workflow](#starting-work-issue-file-spec-critic-implementation)
  before building; close a package only with actual acceptance evidence.
  Update docs with each delivered behavior change.
  Update `CONTRACTS.md`, models, examples and tests together when changing
  implemented value contracts. Do not present shape validation as storage,
  authorization, atomicity or query execution.

## Search and evaluation boundaries

For canonical supplied documents, use `kg.indexing.IndexService` to prepare the
matching projection and `EvidenceSearchService` for full search, or
`kg.query.QueryService` for supervised query composition. Use `EvidenceService`
and `KnowledgeService` for their documented exact reads. Graph builds consume
complete eligible canonical knowledge, never search candidates or top-k results.

For the Markdown demonstration, use `kg.retrieval.SearchService` and unqualified
`kg search`. Prepare a matching `dense-index` with the same embedding
profile/contextual configuration; no stage may be bypassed or silently degraded.
Use `RetrievalService` for demonstration model-independent structured/evidence
reads. Both search workflows require their configured embedding and reranking
providers; neither silently falls back to keyword-only search.

Routine tests inject controlled providers at the existing embedding/reranker
factory seams. Do not download or run real models for ordinary unit/CLI tests.
Shared controlled product providers and script-loading helpers live in
`tests/support/`; import them there rather than from collected test modules.
Keep component-specific fakes and authored fixture/gold semantics distinct.
The following is an explicit broader **legacy-demo** CLI selection for relevant
integration/release acceptance, not the universal canonical CLI PR gate:

```bash
uv run pytest tests/test_cli.py tests/test_product_cli.py tests/test_agent_cli.py tests/test_acceptance.py tests/test_atlas_walkthrough.py tests/test_search_explain.py tests/test_source_context.py tests/test_client_example.py tests/test_agent_benchmark.py
```

For routine PRs use the reviewed local PR gate above; keep broader acceptance
pending until explicitly run. Version-2 search reports must
serialize without `exclude_none=True`: missing stage memberships/contributions
are explicit nulls, while the report serializer handles quote opt-in.

Evaluation tools explicitly select their components. Agent/work-memory lexical
runs use the private `benchmarks/_lexical_search.py` worker; QASPER selects its
component strategy directly. Do not route fixed lexical benchmarks through the
product facade, change their defaults, or overwrite measured results.
Preserve source snapshots, dates, question/tool restrictions, citations and gold.
Record new runs under distinct labels; unmet quality gates must not be relabeled
as passed.

The [product search matrix](benchmarks/productization/README.md) documents
real-model parity checks, approved cache usage, and durable result requirements.
Controlled-provider success cannot substitute for required real-model acceptance.
Foundation [contract inputs](corpora/foundation/README.md) and
[workload targets](benchmarks/foundation/README.md) are deterministic validation
and evaluation preparation, not integration results. Pending integration recipes
and their owners live in the
[evaluation issues](https://github.com/markgar/local-knowledge-graph/milestone/3).

For larger changes, open an issue first so the design can be discussed before
implementation.
