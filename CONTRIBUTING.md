# Contributing

Thanks for helping improve Local Knowledge Graph.

## Architecture and ownership

The canonical engine uses **SQLite for authored evidence and knowledge, with an
optional Ladybug graph projection**. Exact supplied text, immutable revisions,
identities, registered schema, entities/assertions, support and history belong to
`evidence-store/2`. Ladybug holds only rebuildable, exact-scope derived data.
Canonical writes invalidate the captured graph generation; a file or manifest
alone cannot establish freshness. Search remains canonical keyword/vector
retrieval, fusion and reranking, not a Ladybug search replacement.

The private graph builder, reusable `LocalGraphSession` controller and native
examples are implemented; a public business traversal/join facade is not. Keep current behavior
in [SPEC.md](SPEC.md) and [CONTRACTS.md](CONTRACTS.md), and planned scope on issues.
The separately supported Markdown demonstration has its own SQLite schema,
manifest/CLI and retrieval APIs. Do not use it as the canonical service design.

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

For code-affecting changes, run these checks before opening a pull request:

```bash
uv run pytest
uv run ruff check .
uv run mypy
uv build
```

The optional private graph runtime gate is separate from Linux/base-package CI:
on supported macOS 15+ ARM64/CPython 3.12, run
`uv run --extra graph --extra dev pytest tests/test_graph_export.py tests/test_graph_build.py tests/test_graph_native.py`
with `KG_REQUIRE_NATIVE=1` to make an unavailable native runtime fail instead of skip.
Include `tests/test_graph_session*.py` for controller changes, and run
`uv run --extra graph python examples/graph_session.py --output <fresh-path>`
for real lifecycle, proof-hydration, write/refresh and restart acceptance.
Run `uv run --extra graph python examples/graph_build.py --case varied-10000 --output <fresh-path>`
for the complete synthetic native feasibility/parity gate. This experimental
in-process runtime uses pinned Ladybug 0.20.4, a 256 MiB buffer pool and two
threads; that buffer is not a total RSS cap. Native exhaustion/close can crash
the host; see [runtime limits](README.md#optional-disposable-graph-example) and
[#137](https://github.com/markgar/local-knowledge-graph/issues/137).

CI remains **manual-only**, not triggered by pushes or pull requests. Dispatch
the **CI** workflow only for code-affecting changes, using GitHub Actions or
`gh workflow run ci.yml --ref <branch>`, and inspect its Python 3.12
results before merging. Routine CI uses the same version as `.python-version`;
it still runs the full test suite, lint, type checking and distribution build.
For Python-version-sensitive changes or a deliberate multi-version compatibility
check, opt into Python 3.12/3.13/3.14 with
`gh workflow run ci.yml --ref <branch> -F full_matrix=true` and inspect all
selected versions before merging. Code-affecting changes include source, tests,
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
| `src/kg/evidence/`, `src/kg/evidence/schema.sql` | Canonical `evidence-store/2`, exact supplied-source intake/history, trusted local policy and owner transactions. |
| `src/kg/knowledge/` | Immutable schema registry, knowledge reads, enrichment validation and complete eligible graph export. |
| `src/kg/indexing/` | Canonical passage/vector indexing and scoped full search. |
| `src/kg/processing/`, `src/kg/query/` | Processing control plane and supervised canonical query execution. |
| `src/kg/graph/` | Optional Ladybug adapter, complete disposable projection builder and reusable exact-scope session lifecycle. |
| `src/kg/diagnostics/`, `src/kg/_execution_budget.py` | Scoped execution reports and shared accounting/deadlines. |
| `src/kg/models/` | Validated service values, including `foundation/1`; models alone do not execute operations. |
| `src/kg/config.py`, `src/kg/models/manifest.py` | Markdown demonstration manifest loading and source selection. |
| `src/kg/markdown/`, `src/kg/ingest/` | Demonstration Markdown ranges, explicit extraction and demo-store writes. |
| `src/kg/schema.sql`, `src/kg/db.py` | Separate Markdown demonstration SQLite schema and connections, not `evidence-store/2`. |
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
For a CLI change, first run:

```bash
uv run pytest tests/test_cli.py tests/test_product_cli.py tests/test_agent_cli.py tests/test_acceptance.py tests/test_atlas_walkthrough.py tests/test_search_explain.py tests/test_source_context.py tests/test_client_example.py tests/test_agent_benchmark.py
```

Then run the full validation commands above. Version-2 search reports must
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
