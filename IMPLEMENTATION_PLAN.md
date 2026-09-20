# Full roadmap implementation plan

Status: F0/V0 foundation implemented (validation/fixtures/protocol only);
all downstream packages remain planned.
Updated: 2026-09-20.

## Purpose and scope

Deliver the complete target in [ROADMAP.md](ROADMAP.md), using the existing
product rather than rewriting it. The roadmap defines requirements;
[SPEC.md](SPEC.md) defines implemented behavior; this plan defines work packages,
dependencies, coordination, and acceptance. The foundation's validation-only
values are implemented; planned service behavior is not a supported API.

The scope includes all twelve core systems in the roadmap and a downstream
adapter track for Markdown, email, meeting notes, and Teams. Concrete connector
providers and designs remain separate decisions. Implementation waves describe
dependency order, not a reduced MVP or permission to omit later capabilities.

The fastest reliable approach is a shared contract foundation followed by three
parallel implementation lanes, with integration, evaluation, and review
throughout. Avoid both a single giant sequential stack and independent sessions
inventing incompatible contracts.

## Starting point and constraints

The baseline includes the full search pipeline, exact evidence/history, structured
reads, explicit record-state resolution, the private ingestion intake/writer
split, and `foundation/1` validation models, fixtures and evaluation inputs.
The foundation delivers contract validation, not storage/execution guarantees
or production quality.

Keep Python, SQLite, the local CLI/JSON boundary, existing retrieval algorithms,
and approved model/cache behavior unless a later decision justifies a change.
This roadmap does not require a new database, hosted service, frontend, or
autonomous query agent.

Important constraints for the first work packages:

- `source_revision` currently retains content hashes and anchors, not complete
  historical supplied text. Retain complete content for the new intake contract,
  but do not fabricate missing text for legacy revisions or discard their evidence.
- The manifest importer currently controls seed-entity activation. Agent-created
  knowledge needs explicit ownership/lifecycle rules before multiple writers
  coexist; a Markdown ingest must not deactivate another writer's entities.
  It also deletes corpus-wide aliases and deactivates unselected documents,
  and re-extraction/config rebuilds replace mentions and relationships.
  Ownership must cover document synchronization scopes and individual knowledge
  contributions, not just entities.
- `ingest/_prepared.py` and `_writer.py` are private implementation boundaries,
  not a validated public write API. Do not expose trusted internal payloads
  directly to agents or external plugins.
- Preserve existing identities, exact citations, fixture content, reviewed gold,
  historical results, corpus isolation, and public compatibility unless an
  intentional change has explicit migration and regression coverage.
- Existing unmet relevance and answerability gates remain visible. Completing
  interfaces or passing synthetic cases does not establish production readiness.

## Completed foundation and next handoff

F0/V0 is implemented. The [foundation specification](FOUNDATION_SPEC.md) is
the shared contract reference, not a design phase to repeat. Its delivered
artifacts are:

| Deliverable | Reference | Remaining obligation |
| --- | --- | --- |
| Strict versioned values and validation | [`kg.models.foundation`](src/kg/models/foundation.py) and [contract tests](tests/test_foundation_contracts.py) | E1/K1/Q1 implement persistence and execution behind these boundaries. |
| Ownership, evidence, atomic changes, support, read-state and compatibility semantics | [Shared contracts](FOUNDATION_SPEC.md#shared-contracts) | Owning packages enforce them against real stored data, including mixed-writer safety. |
| A01-A17 scenarios and contract examples | [Acceptance inputs](corpora/foundation/README.md) | Integration outcomes remain pending under their package owners; shape checks are not substitutes. |
| Deterministic workload and initial numeric targets | [Evaluation protocol](benchmarks/foundation/README.md) | V1 measures real services and records misses; targets are not measured results. |

**Next: E1, K1 and Q1.** Scope each as a reviewable implementation change using
the settled contracts. The coordinator owns cross-lane contract/schema decisions;
E1 owns migrations, K1 contribution storage, and Q1 coherent dependent execution.
Integrate real producers and consumers before accepting a handoff.

Contracts may evolve through coordinated, versioned changes. Do not reopen the
foundation wholesale or invent source permissions, retention requirements, or
model-egress policy to unblock a lane.

## Parallel implementation lanes

| Lane | Responsibilities | Initial handoff |
| --- | --- | --- |
| Evidence and processing | Generic intake, canonical content/history, Markdown adapter, passage policies, index lifecycle, durable processing and diagnostics. | Validated source-neutral writes and stable evidence references. |
| Knowledge and agents | General knowledge model, graph writes/tools, provenance, identity reconciliation, knowledge changes, and enrichment lifecycle. | Scoped entity/assertion writes against supported evidence. |
| Query execution | Typed executor, structured/graph operations, exact aggregates, natural-language planning, continuation, and query telemetry. | Deterministic execution using existing search/evidence/structured operations. |

Begin with up to three concurrent implementation sessions, one per ready lane.
Validation and independent review can overlap implementation where their inputs
are stable. This is an initial coordination policy, not a requirement to keep
three sessions busy when dependencies or shared files make that unsafe.

Access/retention, plugin integration, and evaluation are cross-cutting work.
Assign them explicit owners and acceptance cases; do not leave them as
unowned tasks for a final hardening phase.

## Work packages and dependencies

There are 23 work packages: **2 complete, 21 remaining**. `Next` means the shared
foundation prerequisite is available, not that implementation has started.
`Planned` packages follow their prerequisites; preparation may overlap.
A package is not necessarily one large PR or a permanently running agent.
Split it into small, complete, reviewable changes with executable acceptance criteria.

The prerequisites below identify the contracts or capabilities needed to
complete integration. Design, fixtures, and tests against agreed contracts may
start earlier. Do not use placeholders or mocks as evidence that the integrated
capability is finished.

| ID | Status | Work package and completion boundary | Prerequisites |
| --- | --- | --- | --- |
| F0 | Complete | Shared versioned contracts, compatibility decisions and executable validation tests; no storage/execution implementation. | Baseline |
| V0 | Complete | A01-A17 scenarios, synthetic contract fixtures, reproducible workload and initial numeric targets; integrated outcomes and service measurements remain pending. | F0 |
| E1 | Next | Generic validated text/metadata/batch intake; external identities; complete supplied-content retention; source-neutral references; current/historical reads and legacy migration. | F0 |
| E2 | Planned | Markdown uses the shared intake boundary while retaining its existing extraction, identity and exact-citation behavior. | E1 |
| E3 | Planned | Versioned passage processing and validated supplied boundaries; coordinated lexical/vector indexing, incremental/rebuild behavior and explicit freshness, independent of graph completion. | E1 |
| E4 | Planned | Reliable local processing for ingestion/index/enrichment: durable states, retries, checkpoints, interruption recovery, concurrency/stale-work handling, migrations and diagnostics. | E1 |
| K1 | Next | General entities/identifiers/aliases, typed relationships/assertions, evidence links, interpretation provenance, ownership and scoped validated writes. | F0 |
| K2 | Planned | Versioned agent tools for evidence inspection, candidate/entity lookup, neighborhood reads, entity creation, mentions and supported assertion writes. | E1, K1 |
| K3 | Planned | Cross-source identity candidate discovery, explicit linking, auditable merge/unmerge or reversible equivalents, and ambiguity without silent identity decisions. | K1 |
| K4 | Planned | Corrections, retractions, supersession, competing assertions and source edit/removal effects; separate historical knowledge from currently supported knowledge. | E1, K1 |
| K5 | Planned | Enrichment lifecycle: pending revisions, agent attribution, idempotent retries, partial completion, stale-submission checks and re-enrichment without duplicate knowledge. | K2, E4 |
| Q1 | Next | Deterministic typed query executor with validated dependent operations, existing search/structured/evidence adapters, explicit outcomes, limits and execution telemetry. | F0 |
| Q2 | Planned | Typed graph traversal/paths and exact structured lookup/filter/count/aggregate operations, entity ambiguity handling, and inspection of supporting records. | Q1, K1 |
| Q3 | Planned | Natural-language planning into supported operations, including dependent graph/structured plans, clarification and unsupported outcomes, with bounded time/model/traversal budgets. | Q1, Q2 |
| Q4 | Planned | Stable continuation across supported result types: snapshot identity, cursor expiry/data-change behavior, no duplicate/skipped results and honest exhaustion semantics. | Q1 |
| X1 | Planned | Complete access/retention enforcement across reads, writes, graph/query operators and indexes; deactivation versus actual purge of canonical and derived content, including affected snapshots. | F0, E1, K1, E3, E4 |
| I1 | Planned | Independently usable plugin contract: packaging, versions/capabilities, intake/tools, source-specific agent instructions and conformance without core edits. | E1, K2 |
| I2 | Planned | Actual ingestion-agent integration connecting evidence-first intake, inspection, interpreted writes and lifecycle; keep interpretation outside the core. | I1, K5 |
| S1 | Planned | Email adapter for messages, threads, participants, source links and synchronization through the shared contracts; select concrete provider separately. | I1 |
| S2 | Planned | Meeting-notes/transcript adapter for text, participants, event times, locations and synchronization; select actual formats/providers separately. | I1 |
| S3 | Planned | Teams-message adapter with stable source references, incremental synchronization and permission handling through the same contracts. | I1 |
| V1 | Planned | Representative, permission-approved evaluation of evidence, retrieval, graph/structured correctness, agents/planning, latency, cost and resource usage against initial targets. Runs incrementally as services become available. | V0 |
| R1 | Planned | Staged core and live-workflow acceptance, compatibility/migration/recovery validation, packaging/docs, independent combined-diff review and explicit release-gate assessment. Full-scope completion still requires all packages. | Core milestone: F0/V0, E/K/Q, X1, I1/I2 and applicable V1; final milestone: all preceding packages |

For E1/K1, the shared evidence reference contract permits parallel development;
integrated graph writes must still validate actual stored evidence. Q2 does not
need to wait for the complete identity merge/unmerge implementation. K5 can
start before all conflict/correction behavior is finished, but final integration
must exercise their interaction. Q4 can start with the deterministic executor;
it must ultimately cover the supported graph and structured results as well.

The connector prerequisites permit fixture-based development once I1 is stable.
Permission-approved live connector acceptance additionally requires X1 and the
provider, credential, synchronization and retention decisions. I2 is required
for the complete agent-assisted workflow. Fixture-only adapters do not satisfy
the final live-workflow acceptance gate.

V1 runs incrementally as capabilities appear; its final assessment must use
the integrated implementation. R1 is not complete merely because every
individual branch passed its own tests.

### Coverage of the roadmap's twelve core systems

| Roadmap system | Primary packages |
| --- | --- |
| 1. Generic document ingestion | F0, E1, E2 |
| 2. Canonical evidence storage | F0, E1 |
| 3. Text processing and indexing | E3, E4 |
| 4. General-purpose knowledge model | K1, K4 |
| 5. Agent-facing graph tools | K2, I2 |
| 6. Identity and reconciliation | K3 |
| 7. Enrichment lifecycle | K5, E4, I2 |
| 8. Knowledge change and conflicts | K4, K3, K5 |
| 9. Unified query planning and execution | Q1, Q2, Q3, Q4 |
| 10. Scope, access and retention | F0, X1, all operation boundaries |
| 11. Reliable processing and operations | E4, E3, K5, R1 |
| 12. Stable contracts and evaluation | F0, V0, I1, V1, R1 |

## Remaining execution order

1. **First parallel wave:** E1, K1 and Q1. Each lane builds against agreed
   contracts; integrate their evidence and scope boundaries as soon as available.
2. **Dependent capabilities:** start ready packages rather than waiting for a
   whole lane to finish. E2/E3/E4, K2/K3/K4, Q2/Q4 and verification can overlap
   subject to file ownership and stable prerequisites.
3. **Agent and query integration:** K5, I1/I2 and Q3 as their dependencies land.
   Continue X1, recovery testing and V1 throughout, not only at the end.
4. **Adapters and full workflows:** S1/S2/S3 can proceed independently after
   plugin contracts stabilize. Live acceptance waits for the explicit policy
   gates. Finish R1 against the combined code and real evaluation evidence.

Build the deterministic executor before depending on a natural-language model.
The planner chooses only validated operations; it must not execute arbitrary
generated code or SQL, become an autonomous query agent, or generate the final
narrative answer. Exact counts cover the complete matching indexed scope, never
a top-k search sample.

Keep existing product search available as an execution capability; do not
silently reinterpret its full-pipeline contract while adding the broader query
interface. Version intentional public changes.

## Sessions, PRs and integration

- Keep one coordinator for the dependency map, cross-lane contract decisions,
  integration state, review findings and genuine user decisions.
- Give each implementation session one PR-sized outcome with complete context,
  owned files, prerequisites, acceptance cases, and an explicit stop condition.
  Do not launch 23 sessions at once.
- Prefer small PRs into stable `main`. Use shallow stacks only for real
  dependencies; a higher layer starts from a committed/pushed prerequisite.
  Avoid one long stack that blocks unrelated work.
- Coordinate edits to shared schema, IDs, contracts and capability discovery.
  Other lanes must not independently redefine them. Test real producer/consumer
  composition before declaring their handoff complete.
- Review finished work while other lanes continue. Fix findings in the owning
  branch, rerun affected checks, and review the fixes before merging.
- Integrate frequently. Run fresh combined-diff review at major milestones,
  covering new files, changed assertions, compatibility, and cross-lane behavior.
- Serialize heavyweight model runs on the current machine; keep ordinary
  development and controlled-provider tests parallel where independent.
- Update current documentation with delivered behavior. Keep future decisions
  and work status in this plan/roadmap or associated work items, not as claims
  that unfinished features are implemented.

The private intake/writer boundary is available. Extract evidence/history responsibilities
from `RetrievalService` when generic evidence intake needs that boundary, and
extract dense projection storage/build responsibilities with index lifecycle.
Do not add another broad restructuring phase, speculative empty packages, a
generic repository/DI framework, or changes based only on file-size thresholds.

## Acceptance and review gates

Each package needs behavior, tests, documentation, relevant compatibility
coverage, and independent review. Preserve existing gold and component coverage;
add separately scoped expectations for new behavior.

Run the smallest relevant checks during development, then the repository's
full checks at integration points under [CONTRIBUTING.md](CONTRIBUTING.md):
`uv run pytest`, `uv run ruff check .`, `uv run mypy`, and `uv build`.
Use controlled providers for routine tests and real-model checks where the
changed behavior requires them. A dependency/network block is incomplete
validation, not a passing skip; never bypass organizational controls.

The central integrated scenario is:

1. Ingest supplied text and make it keyword/semantic searchable before enrichment.
2. Have an ingestion agent inspect evidence and add supported knowledge.
3. Query ranked evidence, dependent relationships, and an exact count.
4. Edit or remove the source, retry work, and submit a stale interpretation.
5. Verify current state, retained history, scope, freshness and explicit failures.
6. Exercise continuation and actual purge, checking affected derived stores and
   snapshots rather than only the canonical document row.

Include ambiguity, competing assertions, ownership coexistence, interruption,
concurrent writers, partial completion and cross-source permissions.
Ownership coexistence must demonstrate that Markdown synchronization cannot
deactivate another connector's documents or erase agent-authored aliases, mentions
or relationships, including during forced parser/config rebuilds. E1/K1 must
owner-scope those mutations or explicitly reject mixed-writer storage **before**
enabling new writes; E2 must complete coexistence.
Partial/failed source enumeration must not be treated as a complete snapshot.
Exercise an independently packaged adapter without adding source-specific core
branches. Real-agent/provider and permission-approved usage evaluation must
remain distinct from deterministic mocks.

Use the [delivered V0 workload and initial targets](benchmarks/foundation/README.md).
Measure incrementally as capabilities appear and review budget changes explicitly.
Do not silently lower historical unmet thresholds or infer improved relevance
from additional interfaces. Record code/configuration/model/data identities and
failures so evaluations are reproducible.

R1 has separate acceptance milestones, not reduced scope. Core acceptance covers
the integrated E/K/Q capabilities, X1, plugin/agent integration and applicable
evaluation using an independently packaged synthetic adapter. It does not wait
for all live connector providers to be available. Live-workflow acceptance records
permission-approved S1/S2/S3 results separately. Blocked live workflows remain
incomplete, and full-scope R1 completion still requires every package. Neither
milestone by itself converts unmet production-quality gates into passes.

## Remaining decisions

The foundation settles initial identities, code-point offsets, contribution
ownership, bounded atomic change sets, conjunctive support, coherent query-state
semantics, compatibility rules and numeric limits/targets. Keep SQLite, Python,
local CLI/JSON transport and current retrieval models. Do not repeat those
decisions unless implementation evidence requires a reviewed contract change.

The decisions below remain open. Resolve them with the owning package; escalate
choices that change public behavior, source access or data egress.

| Decision | Recommended starting position | Required before |
| --- | --- | --- |
| Storage enforcement and migration | Choose transaction, receipt/state storage, predicate registry and coherent-read mechanisms that enforce the settled contracts; preserve existing evidence and owner-scope mutations. | E1/K1/Q1 acceptance |
| Planner strategy | Build deterministic execution first; choose rules/model assistance behind its validated operator boundary. | Q3 |
| Ingestion-agent host | Publish explicit tools/instructions; keep interpretation outside the core. Choose the actual hosting/invocation integration. | I2 |
| Model providers and data egress | Preserve local-first, approved cache/download behavior. Hosted inference requires an explicit decision and authorization. | Any new provider integration |
| Real sources and representative questions | Choose actual email/meeting/Teams providers and a permission-approved workflow; use synthetic conformance fixtures meanwhile. | Live S1/S2/S3 acceptance |
| Permissions and retention | Implement the settled scope/support boundaries; agree concrete source ACL, retention and purge rules before mixing live sources. | X1 and live connector acceptance |
| Cursor behavior | Agree snapshot identity, expiry and data-change outcomes; distinguish bounded candidate exhaustion from corpus exhaustion. | Q4 |
| Evaluation and target revisions | Measure implemented services against V0 targets; select representative approved real workloads and review revisions without erasing historical misses. | Incremental V1 and R1 |

## Foundation delivery and next package boundary

Read this plan, [FOUNDATION_SPEC.md](FOUNDATION_SPEC.md),
[ROADMAP.md](ROADMAP.md), [SPEC.md](SPEC.md), and
[CONTRIBUTING.md](CONTRIBUTING.md), then confirm the latest `main` and baseline
validation. F0 delivers `kg.models.foundation` validation/serialization only.
V0 delivers `corpora/foundation` scenarios and `benchmarks/foundation` reproducible
inputs/numeric targets, not integrated behavior or measured service performance.
All other package IDs remain planned.

Review the settled foundation contract and package-specific acceptance recipes.
The coordinator owns shared contract/schema decisions; E1 owns migrations,
K1 owns contribution storage and Q1 owns coherent dependent execution; V1 owns
measurement. Launch ready lanes only through separately authorized work.
Do not restart completed foundation work, add a broad restructuring phase,
start all packages simultaneously, or
silently choose unresolved live-source/model policies.
