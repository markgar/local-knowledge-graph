# Build roadmap

This is the sole delivery plan. [README.md](README.md) explains usage,
[SPEC.md](SPEC.md) describes implemented behavior, and [CONTRACTS.md](CONTRACTS.md)
defines shared technical rules. Linked issues own execution detail and progress;
this document owns package boundaries, dependencies and overall sequence.

## Destination and current position

Build a local-first knowledge engine that accepts supplied text from independent
plugins, preserves exact evidence, stores agent-authored knowledge, and executes
bounded natural-language queries across search, structured records and graph paths.
Keep Python, SQLite, local CLI/JSON transport and the existing retrieval pipeline.
No new hosted service, frontend or autonomous query agent is required.

Today the product ingests configured Markdown and provides reranked hybrid search,
structured state, exact citations and revision/context reads. F0/V0 is complete:
`foundation/1` validates values, with contract fixtures and evaluation inputs.
Generic writes, query execution, source ACL/purge enforcement and live connectors
are not implemented. Retrieval-quality and answerability gates remain unmet.

The target workflow is: ingest text, index it without waiting for enrichment,
let an ingestion agent inspect and submit supported knowledge, then query evidence,
relationships or exact aggregates. Plugins own source access/format/synchronization;
agents own interpretation; the core validates, stores and enforces scope; the
query service plans/executes; the calling agent composes the final answer.
Source assertions and inferred interpretations stay distinguishable.

## First build round

Only these three packages have implementation issues. None has started.

| Package | Tracking issue | First deliverable |
| --- | --- | --- |
| E1 - Evidence intake | [Generic evidence intake and canonical storage](https://github.com/markgar/local-knowledge-graph/issues/24) | Real source-neutral writes, retained content/revisions, metadata/state and evidence reads. |
| K1 - Knowledge writes | [Knowledge contributions and atomic writes](https://github.com/markgar/local-knowledge-graph/issues/25) | Owned entities/aliases/assertions and evidence-backed atomic change sets. |
| Q1 - Query execution | [Deterministic dependent query execution](https://github.com/markgar/local-knowledge-graph/issues/26) | A bounded executor over real supported search/structured/evidence adapters. |

E1 and K1 may develop against agreed interfaces in parallel, but **K1 integration
requires actual E1 evidence**. E1 owns canonical migration integration; K1
coordinates contribution-schema changes with it. Q1 can start with current
services; it must explicitly reject operations or restrictions those adapters
cannot faithfully support. Typed ownership/path semantics belong to Q2, not
guessed interpretations of free-text owners or wikilinks.

Before enabling new writers, E1/K1 must owner-scope legacy destructive mutations
or reject mixed-writer use before mutation in both new and legacy entry points.
The gate covers document deactivation, alias deletion, and mention/relationship
rebuilds. E2 delivers complete Markdown coexistence. Contract validation alone
does not satisfy this gate.

## Work packages and dependencies

**2 complete, 21 remaining.** `Next` means ready to scope, not already running.
`Planned` work stays here until its prerequisites justify an implementation issue.
Each package may span several linked, reviewable PRs; it is not a monolithic PR.
Prerequisites below govern integrated acceptance; preparation can begin earlier.

| ID | Status | Completion boundary | Prerequisites |
| --- | --- | --- | --- |
| F0 | Complete | Versioned validation/serialization, shared semantics and compatibility rules; no service enforcement. | Baseline |
| V0 | Complete | A01-A17 recipes, contract fixtures, deterministic workload and numeric targets; not integrated outcomes or measured service performance. | F0 |
| E1 | Next | Generic text/metadata/batch intake, external identities, full-content retention, exact references, current/history reads and legacy migration. | F0 |
| E2 | Planned | Markdown adaptation to shared intake with owner-scoped coexistence and preserved extraction, identities and citations. | E1 |
| E3 | Planned | Versioned passage policies and supplied boundaries, coordinated lexical/vector processing, incremental/rebuild behavior and explicit freshness independent of enrichment. | E1 |
| E4 | Planned | Durable processing states, retries, checkpoints, interruption recovery, stale/concurrent-work handling, migrations and diagnostics. | E1 |
| K1 | Next | General entities/identifiers/aliases, typed assertions/relationships, evidence links, interpretation provenance and scoped owned writes. | F0; E1 evidence for integration |
| K2 | Planned | Versioned agent tools for evidence inspection, candidate/entity lookup, neighborhoods, creation, mentions and assertions. | E1, K1 |
| K3 | Planned | Cross-source identity candidates, explicit linking, auditable reversible merge/unmerge and ambiguity without silent decisions. | K1 |
| K4 | Planned | Corrections, retractions, supersession, competing assertions and source edit/removal effects; separate history from current support. | E1, K1 |
| K5 | Planned | Pending-revision enrichment, attribution, idempotent retries, partial completion, stale-submission checks and duplicate-free re-enrichment. | K2, E4 |
| Q1 | Next | Deterministic dependent execution, existing service adapters, typed outcomes, enforced budgets and telemetry. | F0 |
| Q2 | Planned | Typed graph traversal/paths and dependent graph-to-record operations; exact structured lookup/filter/count/aggregates, ambiguity and inspectable support. | Q1, K1 |
| Q3 | Planned | Natural-language planning into supported dependent operations, clarification/unsupported outcomes and bounded time/model/traversal budgets. | Q1, Q2 |
| Q4 | Planned | Stable continuation for supported result types, snapshot identity, expiry/change rules, no duplicate/skipped results and honest exhaustion. | Q1 |
| X1 | Planned | Access/retention enforcement across reads/writes/operators/indexes; deactivation versus actual purge, including affected derived data and snapshots. | F0, E1, K1, E3, E4 |
| I1 | Planned | Independently usable plugin packaging, versions/capabilities, intake/tools, source-specific agent instructions and conformance without core edits. | E1, K2 |
| I2 | Planned | Actual ingestion-agent integration: evidence-first intake, inspection, interpreted writes and lifecycle; interpretation stays outside the core. | I1, K5 |
| S1 | Planned | Email adapter: messages, threads, participants, source links and synchronization; provider chosen separately. | I1; X1/policy approval for live acceptance |
| S2 | Planned | Meeting notes/transcripts: text, participants, event times/locations and synchronization; formats/providers chosen separately. | I1; X1/policy approval for live acceptance |
| S3 | Planned | Teams adapter: stable message references, incremental synchronization and permission handling. | I1; X1/policy approval for live acceptance |
| V1 | Planned | Approved representative evaluation of evidence/retrieval, graph/structured correctness, identity/lifecycle, agents/planning, latency, cost and resources. | V0; integrated capabilities as available |
| R1 | Planned | Combined integration, compatibility/migration/recovery, packaging/docs, independent review and explicit release-gate assessment. | Core: E/K/Q, X1, I1/I2 and applicable V1; full scope: all packages |

The sequence is first-round services, ready dependent capabilities, agent/planner
integration, then complete core and live workflows. Do not wait for an entire
lane to finish before starting ready work. Access/retention, evaluation and
integration have named package owners throughout, not a final unowned hardening
phase. Do not restart completed foundation work or add a broad refactor phase.

## Cross-cutting acceptance

Use the [A01-A17 recipes and owners](corpora/foundation/README.md) as the single
acceptance inventory, and the [workload and targets](benchmarks/foundation/README.md)
as the evaluation protocol. Issues select applicable cases; they do not duplicate
the contract reference or declare shared scenarios passed before integration.

Every implemented surface must preserve exact evidence, immutable provenance,
corpus isolation, current public behavior and reviewed gold. Do not fabricate
missing historical text, silently merge identities, choose truth by write order,
or invent permissions/egress authorization. Public compatibility changes require
an explicit version/migration decision and regression coverage.

Query execution returns ranked evidence, records, paths or aggregates with
scope, revision context and inspectable support. It must report ambiguity,
unsupported requests, partial work, stale indexes and failures honestly.
Counts cover the full eligible indexed set, not top-k. Reranking applies to
ranked evidence, not exact counts. Graph extraction must not gate passage search.
No unrestricted generated SQL/code or autonomous query loop is allowed.
Continuation preserves its documented result set, distinguishes bounded-candidate
from corpus exhaustion, and never overrides revocation or purge.

The integrated workflow must cover ingestion before enrichment, real agent writes,
ranked search, dependent relationships, exact counts, edits/removals, stale retries,
interruption, concurrent writers, competing assertions, continuation and purge
across canonical/derived stores. An independently packaged adapter must work
without source-specific core branches; mocks cannot establish that result.

**Foundation acceptance is complete. Core and live acceptance remain pending.**
Core acceptance requires integrated E/K/Q, X1, I1/I2 and applicable V1, including
actual agent integration and an independently packaged synthetic adapter. It does
not wait for all live providers. Live acceptance separately requires approved
S1/S2/S3 workflows. Full-scope R1 needs both; neither automatically establishes
production quality or turns unmet relevance/answerability gates into passes.

## Execution rules and remaining decisions

One coordinator owns cross-lane contracts/schema and integration. Start at most
three ready implementation sessions, each with a PR-sized outcome, owned files,
dependencies, applicable acceptance cases and a stop condition. Use shallow
stacks only for real dependencies; integrate frequently and serialize heavyweight
model runs on the shared machine.

Each issue requires actual behavior, tests and current documentation. Run focused
checks during development and [full validation plus final-head CI](CONTRIBUTING.md)
before merge. Require independent complete-diff review, fix actionable findings,
and re-review fixes. Review integrated changes at milestones; individual branch
success is not sufficient. Record network/permission blocks as incomplete work,
not passing skips, and never bypass organizational controls.

| Open decision | Owner / deadline |
| --- | --- |
| Concrete transaction, receipt/state storage, migration and coherent-read mechanisms | E1/K1/Q1, before their service acceptance |
| Later predicate/value/operator extensions and identity reconciliation mechanisms | K1/K3/Q2, with coordinated contract changes |
| Plugin packaging and actual ingestion-agent host/invocation | I1/I2, before integration |
| Rules versus bounded model-assisted planning | Q3, before integration |
| Cursor storage, expiry and data-change behavior | Q4, before exposing continuation |
| Actual source providers, ACLs, retention, backup/purge policy, or hosted inference/egress | X1/S1/S2/S3 and relevant provider owner, before live use |
| Approved real workloads, measurements and justified target revisions | V1, incrementally and before R1 |

Track execution in the three linked issues. Create later issues only when ready;
all remaining scope stays in this table. Split oversized packages into linked
tasks rather than another planning document. Update this roadmap when a package
actually lands, and keep implementation details in the issues and shared rules
in the contract reference.
