# Full roadmap implementation plan

Status: F0/V0 foundation implemented (validation/fixtures/protocol only);
all downstream packages remain planned.
Recorded: 2026-09-20.
Starting baseline: `c4162bac3bc5c5d40006e47ad66678fd44230f73`.

## Purpose and scope

Deliver the complete target in [ROADMAP.md](ROADMAP.md), using the existing
product rather than rewriting it. The roadmap defines requirements;
[SPEC.md](SPEC.md) defines implemented behavior; this plan defines work packages,
dependencies, coordination, and acceptance. Proposed contracts in this document
are not existing supported APIs.

The scope includes all twelve core systems in the roadmap and a downstream
adapter track for Markdown, email, meeting notes, and Teams. Concrete connector
providers and designs remain separate decisions. Implementation waves describe
dependency order, not a reduced MVP or permission to omit later capabilities.

The fastest reliable approach is a shared contract foundation followed by three
parallel implementation lanes, with integration, evaluation, and review
throughout. Avoid both a single giant sequential stack and independent sessions
inventing incompatible contracts.

## Starting point and constraints

Productization and the preparatory structure work are complete. The baseline
includes the full search pipeline, exact evidence/history, structured reads,
explicit record-state resolution, and the private ingestion intake/writer split.
The last implementation validation passed 655 tests, lint, type checks, and
package build. These are baseline observations, not validation of future work.

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

## Foundation: agree the contracts that cross lanes

Start with a small, executable contract-and-acceptance foundation, not an
exhaustive design exercise or a collection of empty framework packages.
Agree enough for independently implemented producers and consumers to interoperate:

| Boundary | Decisions to establish |
| --- | --- |
| Evidence | Source/external/document/revision identities; complete supplied content; source locations; passage/anchor identities; offset and encoding semantics; legacy-history compatibility. |
| Writes | Validation; batch outcomes; idempotency; expected-revision preconditions; caller/transaction ownership; explicit failures and partial outcomes. |
| Knowledge | Entities, identifiers, aliases, assertions, relationships and evidence links; explicit versus inferred knowledge; plugin/agent attribution; seed versus agent ownership. |
| Scope and lifecycle | Access context on every operation; processing states and freshness; deactivation versus purge; stale submissions and concurrent changes. |
| Queries | Typed operators and results; dependent execution; bounded budgets; ambiguity/unsupported outcomes; snapshot and continuation semantics. |
| Compatibility | Versioned external contracts, capability discovery, schema/data migration, and adapters for existing public behavior. |

Scope, provenance, and revision preconditions must be present from the start,
not retrofitted after individual features are built. Define one owner for
cross-cutting contract and schema decisions. Contracts can evolve through
coordinated, versioned changes; they are not frozen forever.

The [F0/V0 foundation specification](FOUNDATION_SPEC.md) records initial shared
semantics, strict versioned models/tests, acceptance inventory and evaluation
targets. Contract validation is implemented, not generic storage or execution.
E1/K1/Q1 can use these boundaries; no downstream lane is started by this change.

Create representative acceptance scenarios alongside these contracts.
No model or real connector is needed to establish deterministic storage and
execution invariants. Do not invent source permissions, retention requirements,
or model-egress policy merely to unblock an implementation session.

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

There are 23 work packages. A package is a planning unit, not necessarily one
large PR or a permanently running agent. Split it into small, complete,
reviewable changes with executable acceptance criteria.

The prerequisites below identify the contracts or capabilities needed to
complete integration. Design, fixtures, and tests against agreed contracts may
start earlier. Do not use placeholders or mocks as evidence that the integrated
capability is finished.

| ID | Work package and completion boundary | Prerequisites |
| --- | --- | --- |
| F0 | Shared versioned contracts and compatibility decisions described above, with executable contract tests and recorded unresolved decisions. | Baseline |
| V0 | Cross-capability acceptance scenarios: evidence identity, retries, scope, ownership, ambiguity, exact operations, recovery, continuation and purge; agreed initial workloads and measurable budgets before parallel implementation. | F0 |
| E1 | Generic validated text/metadata/batch intake; external identities; complete supplied-content retention; source-neutral references; current/historical reads and legacy migration. | F0 |
| E2 | Markdown uses the shared intake boundary while retaining its existing extraction, identity and exact-citation behavior. | E1 |
| E3 | Versioned passage processing and validated supplied boundaries; coordinated lexical/vector indexing, incremental/rebuild behavior and explicit freshness, independent of graph completion. | E1 |
| E4 | Reliable local processing for ingestion/index/enrichment: durable states, retries, checkpoints, interruption recovery, concurrency/stale-work handling, migrations and diagnostics. | E1 |
| K1 | General entities/identifiers/aliases, typed relationships/assertions, evidence links, interpretation provenance, ownership and scoped validated writes. | F0 |
| K2 | Versioned agent tools for evidence inspection, candidate/entity lookup, neighborhood reads, entity creation, mentions and supported assertion writes. | E1, K1 |
| K3 | Cross-source identity candidate discovery, explicit linking, auditable merge/unmerge or reversible equivalents, and ambiguity without silent identity decisions. | K1 |
| K4 | Corrections, retractions, supersession, competing assertions and source edit/removal effects; separate historical knowledge from currently supported knowledge. | E1, K1 |
| K5 | Enrichment lifecycle: pending revisions, agent attribution, idempotent retries, partial completion, stale-submission checks and re-enrichment without duplicate knowledge. | K2, E4 |
| Q1 | Deterministic typed query executor with validated dependent operations, existing search/structured/evidence adapters, explicit outcomes, limits and execution telemetry. | F0 |
| Q2 | Typed graph traversal/paths and exact structured lookup/filter/count/aggregate operations, entity ambiguity handling, and inspection of supporting records. | Q1, K1 |
| Q3 | Natural-language planning into supported operations, including dependent graph/structured plans, clarification and unsupported outcomes, with bounded time/model/traversal budgets. | Q1, Q2 |
| Q4 | Stable continuation across supported result types: snapshot identity, cursor expiry/data-change behavior, no duplicate/skipped results and honest exhaustion semantics. | Q1 |
| X1 | Complete access/retention enforcement across reads, writes, graph/query operators and indexes; deactivation versus actual purge of canonical and derived content, including affected snapshots. | F0, E1, K1, E3, E4 |
| I1 | Independently usable plugin contract: packaging, versions/capabilities, intake/tools, source-specific agent instructions and conformance without core edits. | E1, K2 |
| I2 | Actual ingestion-agent integration connecting evidence-first intake, inspection, interpreted writes and lifecycle; keep interpretation outside the core. | I1, K5 |
| S1 | Email adapter for messages, threads, participants, source links and synchronization through the shared contracts; select concrete provider separately. | I1 |
| S2 | Meeting-notes/transcript adapter for text, participants, event times, locations and synchronization; select actual formats/providers separately. | I1 |
| S3 | Teams-message adapter with stable source references, incremental synchronization and permission handling through the same contracts. | I1 |
| V1 | Representative, permission-approved evaluation of evidence, retrieval, graph/structured correctness, agents/planning, latency, cost and resource usage against agreed budgets. | V0 |
| R1 | Staged core and live-workflow acceptance, compatibility/migration/recovery validation, packaging/docs, independent combined-diff review and explicit release-gate assessment. Full-scope completion still requires all packages. | Core milestone: F0/V0, E/K/Q, X1, I1/I2 and applicable V1; final milestone: all preceding packages |

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

## Execution order

1. **Foundation:** F0 with acceptance-scenario preparation for V0. Resolve the
   cross-lane contracts and preserve the baseline before implementation fans out.
   Agree initial V0 workloads and numeric quality/operating budgets at this gate.
2. **First parallel wave:** E1, K1 and Q1. Each lane builds against agreed
   contracts; integrate their evidence and scope boundaries as soon as available.
3. **Dependent capabilities:** start ready packages rather than waiting for a
   whole lane to finish. E2/E3/E4, K2/K3/K4, Q2/Q4 and verification can overlap
   subject to file ownership and stable prerequisites.
4. **Agent and query integration:** K5, I1/I2 and Q3 as their dependencies land.
   Continue X1, recovery testing and V1 throughout, not only at the end.
5. **Adapters and full workflows:** S1/S2/S3 can proceed independently after
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

The readiness refactor is already done. Extract evidence/history responsibilities
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

Set initial representative workloads and concrete quality, latency, cost and
resource budgets during F0/V0, before the first parallel implementation wave.
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

## Decisions and defaults

These decisions remain visible work, not assumptions hidden in child prompts.
Resolve only what blocks the next package, while preserving the rest of the
scope. Escalate choices that change public behavior, source access or data egress.

| Decision | Recommended starting position | Required before |
| --- | --- | --- |
| Storage and deployment | Keep SQLite, Python, current retrieval models and local CLI/JSON transport; introduce no hosted service by default. | F0 |
| Source identity, offsets, ownership and knowledge schema | Agree explicit, versioned semantics including source synchronization and alias/contribution ownership, with compatibility for existing evidence; extensibility does not mean unvalidated arbitrary writes. | F0 exit, before E1/K1/Q1 |
| Planner strategy | Build deterministic execution first; choose rules/model assistance behind its validated operator boundary. | Q3 |
| Ingestion-agent host | Publish explicit tools/instructions; keep interpretation outside the core. Choose the actual hosting/invocation integration. | I2 |
| Model providers and data egress | Preserve local-first, approved cache/download behavior. Hosted inference requires an explicit decision and authorization. | Any new provider integration |
| Real sources and representative questions | Choose actual email/meeting/Teams providers and a permission-approved workflow; use synthetic conformance fixtures meanwhile. | Live S1/S2/S3 acceptance |
| Permissions and retention | Define scope in F0; agree concrete source ACL and retention/purge rules before mixing live sources. | X1 and live connector acceptance |
| Cursor behavior | Agree snapshot identity, expiry and data-change outcomes; distinguish bounded candidate exhaustion from corpus exhaustion. | Q4 |
| Quality and operating budgets | Agree initial representative workloads and numeric acceptance thresholds without erasing known limitations; measure incrementally and explicitly review changes. | F0/V0 exit, before parallel implementation |

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
Do not reopen completed
productization or structural cleanup, start all packages simultaneously, or
silently choose unresolved live-source/model policies.
